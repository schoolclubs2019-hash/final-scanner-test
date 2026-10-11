#!/usr/bin/env python3
"""Fresh non-Kent canary, then durable domain-affine workers on one Azure VM."""
import argparse
import fcntl
import json
import multiprocessing
import os
import signal
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from storefront import storefront_family

from authority_batch import (load_queue, prepare, scan_one, publish_one, worker, publication_lock,
                             with_audit_policy, export_visit_audit, verify_saved_audit, host_key)
from rendered_evidence import BrowserRenderer
from scanner_pipeline import pipeline_fingerprint, authority_rescore_department
from scanner_store import Store, digest
from scan_policy import PAUSE_REASONS, normalize_policy, revisit_state, with_policy
from catalogue_audit import normalize_audit_policy
from htt_publish import production_preflight


MAX_MANIFEST_WORKERS = 16


def validate_legacy_queue(records):
    if len(records) != 646 or any(is_kent(r) or not str(r['brand'])[0].upper() in 'ABCD' for r in records):
        raise ValueError('Production launch requires the 646 eligible non-Kent A-D records')


def checkpoint_json(path, value):
    """Atomically replace a small control/checkpoint file."""
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def manifest_request(records, workers, config, render):
    if not isinstance(records, list) or not records:
        raise ValueError('Manifest requires a non-empty explicit queue')
    if type(workers) is not int or not 1 <= workers <= MAX_MANIFEST_WORKERS:
        raise ValueError(f'Manifest workers must be between 1 and {MAX_MANIFEST_WORKERS}')
    policy = normalize_policy(config.get('execution_policy'))
    if policy is not None and workers != policy['workers']:
        raise ValueError('Manifest worker count must match the saved execution policy')
    identities, seen = [], {'brand_id': set(), 'htt_id': set()}
    for record in records:
        for key in seen:
            value = record.get(key)
            if (type(value) not in (str, int) or not str(value).strip()
                    or str(value) != str(value).strip() or str(value) in seen[key]):
                raise ValueError('Manifest requires unique, explicit stable ' + key + ' values')
            seen[key].add(str(value))
        if not isinstance(record.get('brand'), str) or not record['brand'].strip():
            raise ValueError('Manifest requires a non-empty brand name')
        identities.append({key: str(record[key]) for key in ('brand_id', 'htt_id')})
    return {'schema': 1, 'scope': 'explicit-authority-manifest', 'workers': workers,
            'queue_digest': digest(records), 'identities': identities,
            'held_brand_ids': [str(r['brand_id']) for r in records if authority_rescore_department(r) is None],
            'scanner_fingerprint': pipeline_fingerprint(), 'config_hash': digest(config),
            'render': bool(render)}


def shard_manifest(state, workers, expected_ids, fingerprint):
    """Verify persisted queues read-only, including any already-created run identity."""
    state = Path(state)
    expected_files = {f'W{i}{suffix}' for i in range(1, workers + 1) for suffix in ('.db', '.json')}
    observed_files = {p.name for pattern in ('W*.db', 'W*.json') for p in state.glob(pattern)}
    if observed_files != expected_files:
        raise ValueError('Incomplete or unexpected durable shards; refusing reassignment')
    assignments, observed_ids, domain_owners = {}, {}, {}
    for number in range(1, workers + 1):
        name = f'W{number}'
        rows = json.loads((state / (name + '.json')).read_text())
        if not isinstance(rows, list):
            raise ValueError('Invalid durable shard manifest')
        saved = sorted(rows, key=lambda r: str(r['brand_id']))
        db = sqlite3.connect((state / (name + '.db')).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            queue = db.execute('SELECT brand_id,brand,data,run_id FROM queue').fetchall()
            actual = sorted((json.loads(row[2]) for row in queue), key=lambda r: str(r['brand_id']))
            if actual != saved:
                raise ValueError('Durable queue changed; refusing reassignment')
            for brand_id, brand_name, data, run_id in queue:
                record = json.loads(data)
                if str(record['brand_id']) != brand_id or record['brand'] != brand_name:
                    raise ValueError('Durable queue identity changed')
                if brand_id in observed_ids:
                    raise ValueError('Stable brand_id assigned to more than one worker')
                observed_ids[brand_id] = str(record['htt_id'])
                host = host_key(record)
                if domain_owners.setdefault(host, name) != name:
                    raise ValueError('Storefront assigned to more than one worker')
                if run_id:
                    row = db.execute('SELECT brand_id,data FROM runs WHERE id=?', (run_id,)).fetchone()
                    run = json.loads(row[1]) if row else {}
                    if (not row or row[0] != brand_id or run.get('scan_run_id') != run_id
                            or run.get('scanner_fingerprint') != fingerprint
                            or any(str(run.get(k)) != str(record.get(k)) for k in
                                   ('brand_id', 'htt_id', 'brand', 'domain', 'cpt', 'department'))):
                        raise ValueError('Durable run identity or scanner fingerprint changed')
        finally:
            db.close()
        assignments[name] = {'brand_ids': [str(r['brand_id']) for r in saved], 'queue_digest': digest(rows)}
    if observed_ids != {r['brand_id']: r['htt_id'] for r in expected_ids}:
        raise ValueError('Durable assignment does not match the explicit stable identities')
    return assignments


def prepare_manifest(queue, state, records, workers, config, render, stop_requested=None):
    """Create once, then validate only: no reset, re-enqueue, or reshard on restart."""
    state = Path(state)
    request = manifest_request(records, workers, config, render)
    manifest_path, intent_path = state / 'launch-manifest.json', state / 'launch-intent.json'
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text())
        if saved.get('request') != request:
            raise ValueError('Manifest scope, queue, workers, rendering, configuration or scanner changed')
        actual = shard_manifest(state, workers, request['identities'], request['scanner_fingerprint'])
        if saved.get('assignments') != actual:
            raise ValueError('Immutable worker assignment changed')
        return saved
    if intent_path.exists() and json.loads(intent_path.read_text()) != request:
        raise ValueError('Manifest preparation intent changed')
    if any(state.glob('W*.db')) or any(state.glob('W*.json')) or (state / 'queue-digest').exists():
        raise ValueError('Unattested or incomplete durable shards; use a new state, never reset existing work')
    if not intent_path.exists():
        checkpoint_json(intent_path, request)
    snapshot = state / 'launch-queue.json'
    if snapshot.exists() and json.loads(snapshot.read_text()) != records:
        raise ValueError('Immutable input queue snapshot changed')
    if not snapshot.exists():
        checkpoint_json(snapshot, records)
    policy = normalize_policy(config.get('execution_policy'))
    audit_policy = normalize_audit_policy(config['sitemap_audit_policy']) if 'sitemap_audit_policy' in config else None
    if policy is None and audit_policy is None:
        prepare(snapshot, state, workers, resolve=True, stop_requested=stop_requested)
    else:
        prepare(snapshot, state, workers, resolve=policy is None, stop_requested=stop_requested,
                execution_policy=policy, sitemap_audit_policy=audit_policy)
    if policy is not None:
        (state / 'queue-digest').write_text(request['queue_digest'])
    saved = {'request': request, 'assignments': shard_manifest(
        state, workers, request['identities'], request['scanner_fingerprint'])}
    checkpoint_json(manifest_path, saved)
    return saved


def parse_stop_at(value):
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp.astimezone(timezone.utc).timestamp()
    except (AttributeError, OverflowError, ValueError):
        raise argparse.ArgumentTypeError('Use an explicit UTC offset, for example 2026-10-08T00:00:00Z') from None


def stop_deadline(state, requested=None):
    path = Path(state) / 'launch-control.json'
    if requested is not None:
        checkpoint_json(path, {'stop_at': requested})
    if not path.exists():
        raise ValueError('A new manifest launch requires --stop-at with a finite UTC deadline')
    deadline = json.loads(path.read_text()).get('stop_at')
    if type(deadline) not in (float, int) or not 0 < deadline < 253402300800:
        raise ValueError('Invalid persisted launch deadline')
    return deadline


class StopRequested:
    def __init__(self, state, deadline, event, signal_pending=None):
        self.path, self.deadline, self.event = Path(state) / 'STOP', deadline, event
        self.signal_pending = signal_pending

    def __call__(self):
        # Event.set() takes a multiprocessing lock. Never call it from the
        # signal handler, which could interrupt that same lock in Event.wait().
        if self.signal_pending is not None and self.signal_pending[0]:
            self.event.set()
        return self.event.is_set() or self.path.exists() or time.time() >= self.deadline


@contextmanager
def drain_signals():
    previous = {}
    pending = [False]
    def request_drain(*_):
        pending[0] = True
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_drain)
        yield pending
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def manifest_launch_lock(state):
    with (Path(state) / 'launch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another launcher owns this manifest state') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_manifest_worker(state, name, config_path, render, deadline, event, phase='first', revisit_round=None):
    with drain_signals() as pending:
        run_worker(state, name, config_path, render, True, phase, revisit_round,
                   stop_requested=StopRequested(state, deadline, event, pending))


def launch_manifest(args, records, config):
    with manifest_launch_lock(args.state):
        # Preserve the exact canary and parity gates before preparation or launch.
        verify_canary(args.state, config)
        # Reject scope/configuration changes before altering the runtime controls.
        request = manifest_request(records, args.workers, config, args.render)
        for name in ('launch-manifest.json', 'launch-intent.json'):
            path = args.state / name
            if path.exists():
                saved = json.loads(path.read_text())
                if (saved.get('request') if name == 'launch-manifest.json' else saved) != request:
                    raise ValueError('Immutable manifest request changed')
        deadline = stop_deadline(args.state, args.stop_at)
        event = multiprocessing.Event()
        processes = []
        with drain_signals() as pending:
            stopped = StopRequested(args.state, deadline, event, pending)
            if stopped():
                print(json.dumps({'launch_status': 'DRAINED', 'reason': 'stop_requested'}), flush=True)
                return 2
            try:
                saved = prepare_manifest(args.queue, args.state, records, args.workers,
                                         config, args.render, stopped)
            except InterruptedError:
                print(json.dumps({'launch_status': 'DRAINED', 'reason': 'preparation_stopped'}), flush=True)
                return 2
            config_path = args.state / 'runtime-config.json'
            checkpoint_json(config_path, config)
            print(json.dumps({'launch_status': 'READY', 'workers': args.workers,
                              'brands': len(records), 'held_brands': len(saved['request']['held_brand_ids']),
                              'queue_digest': saved['request']['queue_digest'], 'stop_at': deadline}), flush=True)
            policy = normalize_policy(config.get('execution_policy'))
            if policy is not None:
                def sweep(selected, phase, number):
                    children = []
                    try:
                        for name in selected:
                            if stopped():
                                break
                            process = multiprocessing.Process(target=run_manifest_worker,
                                args=(args.state, name, config_path, args.render, deadline, event, phase, number))
                            process.start()
                            children.append(process)
                    finally:
                        if len(children) != len(selected):
                            event.set()
                        for process in children:
                            while process.is_alive():
                                if stopped():
                                    event.set()
                                process.join(.5)
                            process.join()
                    return all(process.exitcode == 0 for process in children)
                report = coordinate_bulk(args.state, config_path, args.render,
                                         sweep=sweep, stop_requested=stopped)
                print(json.dumps({'launch_status': report['status'],
                                  'reason': report['stop_reason']}), flush=True)
                return {'COMPLETE': 0, 'INCOMPLETE': 2, 'ERROR': 1}[report['status']]
            try:
                for number in range(1, args.workers + 1):
                    if stopped():
                        break
                    process = multiprocessing.Process(target=run_manifest_worker,
                        args=(args.state, f'W{number}', config_path, args.render, deadline, event))
                    process.start()
                    processes.append(process)
            finally:
                # A partial start must drain its children before releasing launch.lock.
                if len(processes) != args.workers:
                    event.set()
                for process in processes:
                    while process.is_alive():
                        if stopped():
                            event.set()
                        process.join(.5)
                    process.join()
        failed = any(process.exitcode != 0 for process in processes)
        status = 'WORKER_FAILED' if failed else 'DRAINED' if stopped() else 'WORKERS_FINISHED'
        print(json.dumps({'launch_status': status}), flush=True)
        return 1 if failed else 2 if stopped() else 0


def is_kent(record):
    host = urlsplit(str(record.get('domain') or '')).hostname or ''
    return (storefront_family(host) == 'wearkent.com' or str(record.get('brand_id')) == '24790'
            or str(record.get('slug', '')).casefold() == 'kent'
            or str(record.get('brand', '')).casefold() == 'kent')


def canary_record(record, retry_limit=None, execution_policy=None):
    """Give a fresh canary bounded static retries before validated alternatives.

    Store.create checkpoints this policy once. Resuming an existing run uses its
    saved policy and counters, regardless of later queue or CLI defaults.
    """
    policy = normalize_policy(execution_policy) or {}
    result = {**record, 'force_rescan': True}
    if retry_limit is not None:
        result['authority_retry_limit'] = retry_limit
    result.setdefault('authority_retry_limit', policy.get('authority_retry_limit', 2))
    result.setdefault('authority_probe_retry_limit', policy.get('authority_probe_retry_limit', 1))
    limit, probe = result['authority_retry_limit'], result['authority_probe_retry_limit']
    if type(limit) is not int or type(probe) is not int or not 1 <= limit <= 12 or not 0 <= probe <= limit:
        raise ValueError('Canary retry policy requires 1-12 static retries and 0-limit probe retries')
    return result


def print_canary_diagnostics(run):
    """Emit only scanner outcome fields, never the run/configuration payload."""
    fields = ('status', 'scanner_status', 'discovery_complete', 'discovery_reconciliation',
              'catalogue_discovered', 'catalogue_confirmed', 'processed', 'technical_failures',
              'unaccounted', 'integrity_status', 'publish_eligible', 'publication_blockers', 'error')
    diagnostics = {field: run.get(field) for field in fields}
    diagnostics['canonical_score'] = (run.get('canonical') or {}).get('score')
    print(json.dumps(diagnostics), flush=True)


def save_canary_result(state, run):
    """Persist a small public outcome; never copy configuration or raw evidence."""
    fields = ('brand', 'brand_id', 'htt_id', 'slug', 'scan_run_id', 'status',
              'scanner_status', 'discovery_complete', 'discovery_reconciliation',
              'catalogue_discovered', 'catalogue_confirmed', 'processed',
              'report_processed', 'scored', 'excluded', 'technical_failures',
              'unaccounted', 'integrity_status', 'publish_eligible',
              'publication_blockers', 'htt_publish_status', 'htt_readback_status',
              'idealwrld_sync_status', 'idealwrld_readback_status',
              'app_catalogue_status', 'app_report_status', 'parity_status',
              'fully_complete', 'completion_status', 'canary_delivery_attempts',
              'canary_delivery_retry_at', 'error', 'defer_reason', 'scan_next_eligible_at', 'scan_visit',
              'audit_export_status', 'audit_export_error',
              'audit_export_path', 'audit_export_manifest_sha256',
              'audit_export_catalogue_status', 'audit_export_sitemap_status')
    result = {field: run.get(field) for field in fields}
    result['canonical_score'] = (run.get('canonical') or {}).get('score')
    result['scanner_fingerprint'] = run.get('scanner_fingerprint')
    state = Path(state)
    temporary = state / 'canary-result.json.tmp'
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    temporary.replace(state / 'canary-result.json')
    return result


def deliver_canary(store, run_id, config, attempts=8, retry_seconds=15):
    """Allow asynchronous HTT readback/sync to settle without another crawl/POST.

    Only an acknowledged HTT publication is polled. Ambiguous writes and permanent
    preflight failures retain their existing operator-recovery behavior. The next
    readback deadline survives interruption, and each invocation is bounded.
    """
    if not 1 <= attempts <= 20 or not 1 <= retry_seconds <= 300:
        raise ValueError('Canary delivery requires 1-20 attempts and 1-300 seconds between readbacks')
    run = store.run(run_id)
    for attempt in range(attempts):
        deadline = float(run.get('canary_delivery_retry_at') or 0)
        while (remaining := deadline - time.time()) > 0:
            time.sleep(min(30, remaining))
        store.update(run_id, canary_delivery_attempts=int(run.get('canary_delivery_attempts') or 0) + 1,
                     canary_delivery_retry_at=0)
        run = publish_one(store, run_id, config, config['publication_lock'])
        if (run.get('fully_complete') or run.get('htt_publish_status') != 'PASS'
                or run.get('error') not in {'HTT READBACK PARITY FAIL', 'DOWNSTREAM PARITY FAIL'}):
            return run
        store.update(run_id, canary_delivery_retry_at=time.time() + retry_seconds,
                     status='DELIVERY_DEFERRED' if attempt + 1 == attempts else 'SYNC_PENDING')
        run = store.run(run_id)
    return run



def validate_saved_policy(run, config):
    """Never attest a new configured policy against evidence from an old policy."""
    if normalize_policy(run.get('execution_policy')) != normalize_policy(config.get('execution_policy')):
        raise ValueError('Saved execution_policy differs from configuration; a fresh canary is required')
    # Removing or nulling a policy is also a policy change, not permission to
    # attest bounded evidence against an unbounded/unaudited configuration.
    saved_audit = run.get('sitemap_audit_policy')
    requested_audit = config.get('sitemap_audit_policy')
    if ((normalize_audit_policy(saved_audit) if saved_audit is not None else None)
            != (normalize_audit_policy(requested_audit) if requested_audit is not None else None)):
        raise ValueError('Saved sitemap_audit_policy differs from configuration; a fresh canary is required')


def attest_canary(store, run_id, config):
    run = store.run(run_id)
    from htt_publish import assert_publishable
    assert_publishable(run)
    validate_saved_policy(run, config)
    verify_saved_audit(store, run)
    if (is_kent(run) or run.get('manual_mode') or run.get('reused_evidence', 0)
            or run.get('delivery_mode') != 'production'
            or run.get('htt_publish_status') != 'PASS' or run.get('htt_readback_status') != 'PASS'
            or run.get('parity_status') != 'PASS' or not run.get('fully_complete')
            or not run.get('catalogue_confirmed', 0)):
        raise ValueError('Fresh non-Kent production canary and truthful parity required')
    return {'run_id': run_id, 'brand_id': run['brand_id'], 'htt_id':run['htt_id'],
            'score': run['canonical']['score'],
            'catalogue_confirmed':run['catalogue_confirmed'], 'processed':run['processed'],
            'report_processed':run['report_processed'], 'excluded':run.get('excluded', 0),
            'htt_publish_status':run['htt_publish_status'], 'htt_readback_status':run['htt_readback_status'],
            'parity_status':run['parity_status'], 'fully_complete':True,
            'scanner_fingerprint': pipeline_fingerprint(), 'config_hash': digest(config)}


def verify_canary(state, config):
    state = Path(state)
    proof = json.loads((state / 'canary-proof.json').read_text())
    # Four workers verify this checkpoint together; serialize readers before
    # opening SQLite instead of making three fail on a nonblocking store lock.
    with publication_lock(state / 'canary.db.lock'):
        store = Store(state / 'canary.db')
        try:
            if proof != attest_canary(store, proof['run_id'], config):
                raise ValueError('Canary proof does not match this deployment/configuration')
        finally:
            store.close()


def run_worker(*args, stop_requested=None):
    results = worker(*args, stop_requested=stop_requested)
    if any(row.get('worker_error') for row in results):
        raise SystemExit(1)



def _save_schedule(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def deferred_report(state, workers, now=None):
    """Small durable report, including held failures and future eligible times."""
    rows = []
    for index in range(1, workers + 1):
        store = Store(Path(state) / f'W{index}.db')
        try:
            for brand, run_id in store.queue():
                run = store.run(run_id) if run_id else brand
                if run.get('fully_complete') and run.get('audit_export_status') != 'FAIL':
                    continue
                disposition, eligible = revisit_state(run, now)
                rows.append({'brand_id': run.get('brand_id'), 'brand': run.get('brand'),
                             'scan_run_id': run_id, 'status': run.get('status', 'PENDING'),
                             'reason': run.get('defer_reason') or run.get('error'),
                             'disposition': disposition, 'next_eligible_at': eligible,
                             'scan_visit': run.get('scan_visit'),
                             'scanner_status': run.get('scanner_status'),
                             'htt_publish_status': run.get('htt_publish_status'),
                             'delivery_retry_at': run.get('delivery_retry_at'),
                             'audit_export_status': run.get('audit_export_status'),
                             'audit_export_error': run.get('audit_export_error'),
                             'audit_export_path': run.get('audit_export_path')})
        finally:
            store.close()
    return rows


def coordinate_bulk(state, config_path, render=False, sweep=None, clock=None, sleep=None, stop_requested=None):
    """Global first-pass barrier, then bounded due-only visits on the same shards.

    The wait allowance is cumulative and durable, not renewed by restarting the
    command. Per-run visit deadlines and retry counters remain authoritative.
    """
    state = Path(state)
    clock, sleep = clock or time.time, sleep or time.sleep
    config = json.loads(Path(config_path).read_text())
    policy = normalize_policy(config.get('execution_policy'))
    if policy is None:
        raise ValueError('Deferred coordinator requires execution_policy')
    names = [f'W{i}' for i in range(1, policy['workers'] + 1)]
    identity = {'scanner_fingerprint': pipeline_fingerprint(), 'execution_policy': policy,
                'config_hash': digest(config),
                'queue_digest': (state / 'queue-digest').read_text()}
    path = state / 'bulk-schedule.json'
    if path.exists():
        schedule = json.loads(path.read_text())
        if any(schedule.get(key) != value for key, value in identity.items()):
            raise ValueError('Saved bulk policy/source/queue cannot change')
    else:
        schedule = {**identity, 'first_pass_complete': False, 'revisit_round': 1,
                    'wait_seconds_reserved': 0, 'wait_reserved_until': 0}
        _save_schedule(path, schedule)

    def run_sweep(selected, phase, number):
        if sweep is not None:
            return sweep(selected, phase, number)
        processes = [multiprocessing.Process(target=run_worker,
                     args=(state, name, config_path, render, True, phase, number)) for name in selected]
        for process in processes:
            process.start()
        for process in processes:
            process.join()
        return all(process.exitcode == 0 for process in processes)

    def first_done(name):
        checkpoint = state / f'{name}.first-pass.json'
        if not checkpoint.exists():
            return False
        proof = json.loads(checkpoint.read_text())
        if any(proof.get(key) != identity[key] for key in ('scanner_fingerprint', 'execution_policy')):
            raise ValueError('First-pass checkpoint does not match this execution policy/source')
        return True

    if not schedule['first_pass_complete']:
        pending = [name for name in names if not first_done(name)]
        if stop_requested is not None and stop_requested():
            schedule['stop_reason'] = 'stop_requested'
        elif pending and not run_sweep(pending, 'first', None):
            schedule['stop_reason'] = 'worker_error'
        elif not all(first_done(name) for name in names):
            if stop_requested is not None and stop_requested():
                schedule['stop_reason'] = 'stop_requested'
            else:
                raise ValueError('First-pass worker exited without a durable barrier checkpoint')
        else:
            schedule.update(first_pass_complete=True, first_pass_completed_at=clock())
        _save_schedule(path, schedule)

    stop_reason = schedule.get('stop_reason', 'worker_error') if not schedule['first_pass_complete'] else 'revisit_ceiling'
    while schedule['first_pass_complete'] and schedule['revisit_round'] <= policy['max_revisit_passes']:
        if stop_requested is not None and stop_requested():
            stop_reason = 'stop_requested'
            break
        number = schedule['revisit_round']
        rows = deferred_report(state, policy['workers'], clock())
        candidates = []
        for row in rows:
            visit = row.get('scan_visit') or {}
            if row['disposition'] not in {'due', 'waiting'}:
                continue
            if int(visit.get('number', 0)) < number or (int(visit.get('number', 0)) == number and visit.get('state') == 'ACTIVE'):
                candidates.append(row)
        if not candidates:
            schedule['revisit_round'] += 1
            _save_schedule(path, schedule)
            continue
        if any(row['disposition'] == 'due' for row in candidates):
            if not run_sweep(names, 'revisit', number):
                stop_reason = 'worker_error'
                break
            after = deferred_report(state, policy['workers'], clock())
            if stop_requested is not None and stop_requested():
                stop_reason = 'stop_requested'
                break
            if digest(after) == digest(rows):
                stop_reason = 'worker_no_progress'
                break
            continue
        deadline = min(row['next_eligible_at'] for row in candidates)
        now = clock()
        additional = max(0, deadline - max(now, schedule['wait_reserved_until']))
        if schedule['wait_seconds_reserved'] + additional > policy['max_coordinator_wait_seconds']:
            stop_reason = 'coordinator_wait_budget'
            break
        schedule['wait_seconds_reserved'] += additional
        schedule['wait_reserved_until'] = deadline
        _save_schedule(path, schedule)
        while (remaining := deadline - clock()) > 0:
            if stop_requested is not None and stop_requested():
                stop_reason = 'stop_requested'
                break
            sleep(min(1 if stop_requested is not None else 30, remaining))
        if stop_reason == 'stop_requested':
            break
    rows = deferred_report(state, policy['workers'], clock())
    schedule.update(stop_reason=stop_reason, last_report_at=clock())
    _save_schedule(path, schedule)
    outcome = ('ERROR' if stop_reason in {'worker_error', 'worker_no_progress'} else
               'COMPLETE' if schedule['first_pass_complete'] and not rows else 'INCOMPLETE')
    report = {'status': outcome, 'first_pass_complete': schedule['first_pass_complete'], 'stop_reason': stop_reason,
              'revisit_round': schedule['revisit_round'], 'workers': policy['workers'],
              'wait_seconds_reserved': schedule['wait_seconds_reserved'], 'remaining': rows}
    _save_schedule(state / 'deferred-report.json', report)
    print(json.dumps(report), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('canary', 'launch', 'launch-manifest'))
    parser.add_argument('--queue', required=True, type=Path)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--brand-id')
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, MAX_MANIFEST_WORKERS + 1),
                        help='Explicit manifest worker count; legacy launch remains exactly four')
    parser.add_argument('--stop-at', type=parse_stop_at,
                        help='Manifest UTC deadline, e.g. 2026-10-08T00:00:00Z; retained on restart if omitted')
    parser.add_argument('--authority-retry-limit', type=int, choices=range(1, 13),
                        help='Fresh canary static retry limit (queue value or 2 by default); saved runs retain their policy')
    parser.add_argument('--delivery-attempts', type=int, default=8, choices=range(1, 21))
    parser.add_argument('--delivery-retry-seconds', type=int, default=15, choices=range(1, 301))
    args = parser.parse_args()
    if args.command != 'launch-manifest' and (args.workers != 4 or args.stop_at is not None):
        parser.error('--workers and --stop-at are for launch-manifest only')
    args.state.mkdir(parents=True, exist_ok=True)
    config = json.loads(args.config.read_text())
    policy = normalize_policy(config.get('execution_policy'))
    if policy is not None:
        config['execution_policy'] = policy
    audit_policy = normalize_audit_policy(config['sitemap_audit_policy']) if 'sitemap_audit_policy' in config else None
    if audit_policy is not None:
        config['sitemap_audit_policy'] = audit_policy
    # One local POSIX lock directory shared by all processes on this VM.
    config.setdefault('publication_lock', str((args.state / 'publish.lock').resolve()))
    records = load_queue(args.queue)
    if args.command == 'launch-manifest':
        return launch_manifest(args, records, config)
    config_path = args.state / 'runtime-config.json'

    canary = Store(args.state / 'canary.db')
    try:
        with canary.lock():
            if args.command == 'canary':
                matches = [r for r in records if str(r['brand_id']) == args.brand_id]
                if len(matches) != 1 or is_kent(matches[0]):
                    raise ValueError('Select one existing non-Kent stable brand_id')
                record = with_audit_policy(with_policy(canary_record(matches[0], args.authority_retry_limit, policy), policy), audit_policy)
                existing = dict((str(r['brand_id']), rid) for r, rid in canary.queue()).get(str(record['brand_id']))
                if existing:
                    validate_saved_policy(canary.run(existing), config)
                # Do not replace saved runtime configuration when the requested
                # canary policy cannot truthfully attest this existing run.
                config_path.write_text(json.dumps(config))
                production_preflight(record, config)
                canary.enqueue([record])
                # Resume a crash safely; never resubmit an ambiguous publication.
                phase = 'revisit' if existing and (canary.run(existing).get('scan_visit') or {}).get('state') == 'PAUSED' else 'first'
                run = scan_one(canary, record, existing, BrowserRenderer() if args.render else None,
                               domain_lock_directory=config.get('domain_lock_directory'), phase=phase)
                run = export_visit_audit(canary, run['scan_run_id'], args.state)
                save_canary_result(args.state, run)
                if run.get('audit_export_status') == 'FAIL':
                    raise RuntimeError('AUDIT EXPORT FAILED; saved scan evidence is retained')
                visit = run.get('scan_visit') or {}
                if (run.get('status') == 'DEFERRED' and visit.get('state') == 'PAUSED'
                        and run.get('defer_reason') in PAUSE_REASONS):
                    receipt = {'status': 'INCOMPLETE', 'run_status': 'DEFERRED',
                               'scan_run_id': run['scan_run_id'],
                               'defer_reason': run['defer_reason'],
                               'scan_next_eligible_at': run.get('scan_next_eligible_at'),
                               'scan_visit': visit, 'audit_export_status': run.get('audit_export_status'),
                               'audit_export_path': run.get('audit_export_path')}
                    print(json.dumps(receipt), flush=True)
                    return 2
                from htt_publish import assert_publishable
                try:
                    assert_publishable(run)
                except ValueError:
                    print_canary_diagnostics(run)
                    raise
                try:
                    run = deliver_canary(canary, run['scan_run_id'], config,
                                         args.delivery_attempts, args.delivery_retry_seconds)
                finally:
                    save_canary_result(args.state, canary.run(run['scan_run_id']))
                proof = attest_canary(canary, run['scan_run_id'], config)
                proof_path = args.state / 'canary-proof.json.tmp'
                proof_path.write_text(json.dumps(proof, indent=2) + '\n')
                proof_path.replace(args.state / 'canary-proof.json')
                print(json.dumps(proof))
                return 0
            proof = json.loads((args.state / 'canary-proof.json').read_text())
            if proof != attest_canary(canary, proof['run_id'], config):
                raise ValueError('Canary proof does not match this deployment/configuration')
            config_path.write_text(json.dumps(config))
    finally:
        canary.close()
    validate_legacy_queue(records)
    # Only the CLI prepares fresh shards. Existing state is resumed without reset.
    workers = policy['workers'] if policy else 4
    shard_paths = [args.state / f'W{i}.db' for i in range(1, workers + 1)]
    existing_shards = list(args.state.glob('W*.db'))
    if any(path not in shard_paths for path in existing_shards):
        raise ValueError('Worker count cannot reassign existing durable shards')
    if not any(p.exists() for p in shard_paths):
        # Keep all policy-enabled network work inside a saved visit budget.
        prepare(args.queue, args.state, workers, resolve=policy is None, execution_policy=policy,
                sitemap_audit_policy=audit_policy)
        (args.state / 'queue-digest').write_text(digest(records))
    elif not all(p.exists() for p in shard_paths) or (args.state / 'queue-digest').read_text() != digest(records):
        raise ValueError('Incomplete or changed durable shards; refusing reassignment')
    if policy is not None:
        with publication_lock(args.state / 'bulk-coordinator.lock'):
            report = coordinate_bulk(args.state, config_path, args.render)
        return {'COMPLETE': 0, 'INCOMPLETE': 2, 'ERROR': 1}[report['status']]
    processes = [multiprocessing.Process(target=run_worker, args=(args.state, f'W{i}', config_path, args.render, True))
                 for i in range(1, 5)]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    return 0 if all(p.exitcode == 0 for p in processes) else 1


if __name__ == '__main__':
    raise SystemExit(main())
