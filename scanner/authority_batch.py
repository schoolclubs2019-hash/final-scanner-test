#!/usr/bin/env python3
"""Durable domain-affine batch runner for authority scans and verified HTT publication."""
import argparse
import os
import fcntl
import json
import time
from datetime import datetime, timezone
from uuid import uuid4
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from catalogue_discovery import FetchError, PublicHTTP, storefront_family
from catalogue_audit import export_audit, normalize_audit_policy, verify_export
from publication_parity import ContractError
from rendered_evidence import BrowserRenderer
from scanner_pipeline import (
    SCANNER_VERSION,
    pipeline_fingerprint,
    recoverable_product_transition,
    run_brand,
)
from scanner_store import Store
from checkpoint_io import checkpoint_json
from scan_policy import (ScanPaused, PAUSE_REASONS, begin_visit, finish_visit,
                         normalize_policy, pause_visit, revisit_state, with_policy)
from throne_read import READER_VERSION
from throne_standard import VERSION



def with_audit_policy(record, policy=None):
    """Attach only to fresh input; an existing Store run remains authoritative."""
    if 'sitemap_audit_policy' not in record and policy is None:
        return dict(record)
    value = record.get('sitemap_audit_policy', policy)
    return {**record, 'sitemap_audit_policy': normalize_audit_policy(value)}


def audit_snapshot_path(state, relative):
    """Resolve an owned relative snapshot without accepting symlink redirects."""
    state, relative = Path(state).absolute(), Path(relative)
    if state.is_symlink() or relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Invalid audit snapshot path')
    candidate = state
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('Audit snapshot cannot contain symlinks')
    if not candidate.resolve().is_relative_to(state.resolve()):
        raise ValueError('Audit snapshot must remain inside the state directory')
    return candidate


def verify_saved_audit(store, run):
    """Bind opted-in canary proof to the actual immutable export on disk."""
    if run.get('execution_policy') is None and run.get('sitemap_audit_policy') is None:
        return None
    if run.get('audit_export_status') != 'PASS' or not run.get('audit_export_manifest_sha256'):
        raise ValueError('Opted-in canary requires a successful saved-evidence audit export')
    relative = run.get('audit_export_path')
    if not isinstance(relative, str) or not relative:
        raise ValueError('Saved audit export path is unavailable')
    output = audit_snapshot_path(Path(store.path).parent, relative)
    policy = normalize_audit_policy(run.get('sitemap_audit_policy'))
    proof = verify_export(output, expected_run_id=run['scan_run_id'],
                          expected_fingerprint=run['scanner_fingerprint'],
                          max_export_bytes=policy['max_export_bytes'])
    if proof['manifest_sha256'] != run['audit_export_manifest_sha256']:
        raise ValueError('Saved audit export manifest hash changed')
    if policy['audit_required'] and (proof['sitemap_status'] != 'COMPLETE'
            or (run.get('sitemap_audit') or {}).get('status') != 'COMPLETE'):
        raise ValueError('Required sitemap audit is incomplete; publication remains held')
    return proof


def export_visit_audit(store, run_id, state):
    """Export and verify saved evidence after a visit, including paused visits."""
    run = store.run(run_id)
    if run.get('execution_policy') is None and run.get('sitemap_audit_policy') is None:
        return run
    visit = run.get('scan_visit') or {}
    try:
        if Path(str(run_id)).name != str(run_id) or str(run_id) in {'.', '..'}:
            raise ValueError('Invalid audit run directory')
        policy = normalize_audit_policy(run.get('sitemap_audit_policy'))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        relative = Path('audits') / str(run_id) / f'visit-{visit.get("number", 0)}-{stamp}-{uuid4().hex[:12]}'
        output = audit_snapshot_path(state, relative)
        export_audit(store.path, run_id, output, max_export_bytes=policy['max_export_bytes'])
        verified = verify_export(output, expected_run_id=run_id,
                                 expected_fingerprint=run['scanner_fingerprint'],
                                 max_export_bytes=policy['max_export_bytes'])
        store.update(run_id, audit_export_status='PASS', audit_export_error=None,
                     audit_export_path=str(relative),
                     audit_export_manifest_sha256=verified['manifest_sha256'],
                     audit_export_catalogue_status=verified['catalogue_status'],
                     audit_export_sitemap_status=verified['sitemap_status'])
    except Exception as exc:
        # A saved export is separate from truthful catalogue/sitemap completeness.
        store.update(run_id, audit_export_status='FAIL', audit_export_error=type(exc).__name__,
                     audit_export_path=None, audit_export_manifest_sha256=None,
                     audit_export_catalogue_status=None, audit_export_sitemap_status=None)
    return store.run(run_id)


def host_key(record):
    host = (urlsplit(str(record.get('crawl_origin') or record.get('domain') or '')).hostname or '').casefold().removeprefix('www.')
    if not host:
        raise ValueError('Queue record has no valid public hostname')
    return storefront_family(host)


def record_weight(record):
    try:
        return max(1, int(record.get('existing_catalogue_count') or 1))
    except (TypeError, ValueError):
        return 1


def load_queue(path):
    value = json.loads(Path(path).read_text())
    records = value.get('queue') if isinstance(value, dict) else value
    if not isinstance(records, list) or not records:
        raise ValueError('Authority queue must be a non-empty array or an object with queue[]')
    required = ('brand_id', 'htt_id', 'brand', 'domain', 'cpt')
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Authority queue entries must be objects')
        missing = [key for key in required if not record.get(key)]
        if missing:
            raise ValueError('Queue record missing ' + ', '.join(missing))
        brand_id = str(record['brand_id'])
        if brand_id in seen:
            raise ValueError('Duplicate queue brand_id ' + brand_id)
        seen.add(brand_id)
        host_key(record)
    return records


def domain_affine_shards(records, workers=4):
    if workers < 1:
        raise ValueError('workers must be positive')
    groups = {}
    for record in records:
        groups.setdefault(host_key(record), []).append(record)

    shards = [[] for _ in range(workers)]
    weights = [0 for _ in range(workers)]
    domains = [set() for _ in range(workers)]

    ranked = sorted(
        ((sum(record_weight(record) for record in rows), host, rows)
         for host, rows in groups.items()),
        key=lambda item: (-item[0], item[1]),
    )
    for weight, host, rows in ranked:
        index = min(range(workers), key=lambda i: (weights[i], i))
        shards[index].extend(rows)
        weights[index] += weight
        domains[index].add(host)

    owner = {}
    for index, names in enumerate(domains):
        for host in names:
            if host in owner:
                raise AssertionError('Hostname assigned to more than one shard: ' + host)
            owner[host] = index

    for shard in shards:
        shard.sort(key=lambda record: (
            record_weight(record),
            str(record.get('brand', '')).casefold(),
            str(record.get('brand_id', '')),
        ))
    return shards, weights


def prepare(queue_path, state_dir, workers=4, reset=False, resolve=False, execution_policy=None, sitemap_audit_policy=None, stop_requested=None):
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    policy = normalize_policy(execution_policy)
    records = [with_audit_policy(with_policy(record, policy), sitemap_audit_policy) for record in load_queue(queue_path)]
    if resolve:
        for record in records:
            if stop_requested is not None and stop_requested():
                raise InterruptedError('Preparation stopped before creating durable shards')
            try:
                resolution = PublicHTTP(record['domain']).resolve()
            except FetchError as exc:
                record.pop('crawl_origin', None)
                record.pop('storefront_resolution', None)
                record['storefront_resolution_error'] = exc.state
            else:
                record.pop('storefront_resolution_error', None)
                record['crawl_origin'] = resolution['crawl_origin']
                record['storefront_resolution'] = resolution
    if stop_requested is not None and stop_requested():
        raise InterruptedError('Preparation stopped before creating durable shards')
    shards, weights = domain_affine_shards(records, workers)

    for index, records_for_worker in enumerate(shards, 1):
        name = f'W{index}'
        shard_path = state / f'{name}.json'
        db_path = state / f'{name}.db'
        if db_path.exists() and not reset:
            store = Store(db_path)
            try:
                if store.queue():
                    raise ValueError(f'{db_path} already contains durable work; use a new state directory or --reset only after verifying no publication must be preserved')
            finally:
                store.close()
        if reset:
            for suffix in ('', '-wal', '-shm', '.lock'):
                candidate = Path(str(db_path) + suffix)
                if candidate.exists():
                    candidate.unlink()
        shard_path.write_text(json.dumps(records_for_worker, ensure_ascii=False))
        store = Store(db_path)
        try:
            store.enqueue(records_for_worker)
        finally:
            store.close()

    return {
        'total': len(records),
        'workers': workers,
        'counts': [len(shard) for shard in shards],
        'weights': weights,
    }


def queue_run(store, brand_id):
    matches = [(record, run_id) for record, run_id in store.queue()
               if str(record.get('brand_id')) == str(brand_id)]
    if len(matches) != 1:
        raise ValueError('Stable brand_id must select exactly one queue record')
    return matches[0]


def retryable_products(store, run_id):
    return any(product.get('retryable') for product in store.iter_products(run_id))


def scan_one(store, brand, run_id=None, renderer=None, domain_lock_directory=None, phase='first'):
    from hashlib import sha256
    # All four processes live on one Azure VM and share this state directory.
    # This also protects against accidental overlapping queues with separate DBs.
    key = sha256(host_key(brand).encode()).hexdigest()
    lock_directory = Path(domain_lock_directory) if domain_lock_directory else Path(store.path).parent / 'domains'
    with publication_lock(lock_directory / (key + '.lock')):
        return _scan_one(store, brand, run_id, renderer, phase)


def _scan_one(store, brand, run_id=None, renderer=None, phase='first'):
    if run_id is None:
        run_id = store.create(
            with_audit_policy(with_policy({**brand, 'independent_ai_review': os.environ.get('HTT_INDEPENDENT_AI_REVIEW') == '1'}, None)),
            pipeline_fingerprint(),
            SCANNER_VERSION,
            READER_VERSION,
            VERSION,
        )['scan_run_id']

    run = store.run(run_id)
    if run.get('status') == 'COMPLETE' or run.get('htt_publish_status') == 'PASS':
        return run
    if run.get('scanner_status') == 'SCANNER_COMPLETE' and not retryable_products(store, run_id):
        return run

    if run.get('execution_policy') is not None:
        return _scan_one_policy(store, run_id, renderer, phase)

    status = run.get('status')
    if status == 'DEFERRED' and run.get('defer_reason') == 'domain_backoff':
        # A server's future Retry-After must release this worker. Once it is due,
        # an explicit worker/canary restart may resume that same immutable run.
        # Exhausted or permanent deferrals remain terminal and budgets persist.
        attempts = int(run.get('worker_attempts', 0))
        if (float(run.get('worker_retry_at', 0)) > time.time()
                or attempts >= int(run.get('worker_retry_limit', 3))):
            return run
        store.update(run_id, status='RETRY', worker_attempts=attempts + 1,
                     defer_reason=None, error=None)
        run = store.run(run_id)
        status = run['status']
    if status == 'FAILED':
        pending = run.get('catalogue_recovery_pending')
        failure = run.get('authority_retry')
        recoverable_catalogue = (
            not run.get('discovery_complete')
            and isinstance(pending, dict) and pending.get('adapter') in {'shopify', 'woocommerce'}
            and bool(pending.get('url')) and isinstance(failure, dict)
            and failure.get('url') == pending['url'] and failure.get('state') == 'EXHAUSTED'
            and run.get('error') == 'authority_retry_exhausted'
            and callable(getattr(type(renderer), 'fetch_json', None))
        )
        if not (recoverable_catalogue or recoverable_product_transition(store, run_id, renderer)):
            return run
        # Only resume a checkpointed static-to-browser transition. The existing
        # immutable source, worker ceiling and both channel budgets still apply;
        # an exhausted rendered:<URL> failure cannot match this static URL.
        status = 'RETRY'
    if status == 'DEFERRED':
        return run
    if status == 'REPAIR_REQUIRED' and not retryable_products(store, run_id):
        return run

    retry = status in {'RUNNING', 'RETRY', 'REPAIR_REQUIRED'}
    if int(run.get('worker_attempts', 0)) >= int(run.get('worker_retry_limit', 3)):
        store.update(run_id, status='DEFERRED', defer_reason=run.get('error') or 'retry_ceiling')
        return store.run(run_id)
    deadline = float(run.get('worker_retry_at', 0))
    while (remaining := deadline - time.time()) > 0:
        time.sleep(min(30, remaining))
    try:
        http = PublicHTTP(
            run['domain'],
            requests_per_second=float(run.get('requests_per_second', 2)),
        )
    except FetchError as exc:
        store.update(run_id, status='DEFERRED' if exc.retryable else 'REPAIR_REQUIRED',
                     defer_reason=exc.state if exc.retryable else None, error=exc.state,
                     integrity_status='FAIL', publish_eligible=False,
                     worker_retry_at=time.time() + 300 if exc.retryable else 0)
        return store.run(run_id)
    run = run_brand(store, run_id, http, renderer, retry=retry)

    while run.get('status') != 'DEFERRED' and (run.get('status') in {'RUNNING', 'RETRY'} or retryable_products(store, run_id)):
        attempts = int(run.get('worker_attempts', 0)) + 1
        deadline = time.time() + min(300, 5 * 2 ** min(attempts, 6))
        store.update(run_id, worker_attempts=attempts, worker_retry_at=deadline)
        if attempts >= int(run.get('worker_retry_limit', 3)):
            store.update(run_id, status='DEFERRED', defer_reason=run.get('error') or 'transient_scan_failure')
            return store.run(run_id)
        time.sleep(max(0, deadline - time.time()))
        run = run_brand(store, run_id, http, renderer, retry=True)

    return run


def _recoverable_catalogue(run, renderer):
    pending, failure = run.get('catalogue_recovery_pending'), run.get('authority_retry')
    return (not run.get('discovery_complete') and isinstance(pending, dict)
            and pending.get('adapter') in {'shopify', 'woocommerce'} and bool(pending.get('url'))
            and isinstance(failure, dict) and failure.get('url') == pending['url']
            and failure.get('state') == 'EXHAUSTED' and run.get('error') == 'authority_retry_exhausted'
            and callable(getattr(type(renderer), 'fetch_json', None)))


def _scan_one_policy(store, run_id, renderer, phase):
    """One bounded visit; cooperative pauses never spend failure retry counters."""
    run = store.run(run_id)
    policy = normalize_policy(run['execution_policy'])
    if run.get('scanner_fingerprint') != pipeline_fingerprint():
        raise ValueError('Scanner changed: create a new immutable run with --force-rescan')

    def held():
        if (run.get('scan_visit') or {}).get('state') == 'ACTIVE':
            return finish_visit(store, run_id)
        return run

    if (run.get('status') == 'FAILED' and not _recoverable_catalogue(run, renderer)
            and not recoverable_product_transition(store, run_id, renderer)):
        return held()
    if run.get('status') == 'DEFERRED' and run.get('defer_reason') not in PAUSE_REASONS:
        return held()
    if run.get('status') == 'REPAIR_REQUIRED' and not retryable_products(store, run_id):
        return held()
    if int(run.get('worker_attempts', 0)) >= int(run.get('worker_retry_limit', 3)):
        return held()
    budget = begin_visit(store, run_id, phase)
    if budget is None:
        return run
    if isinstance(renderer, BrowserRenderer):
        renderer = BrowserRenderer(timeout_ms=policy['browser_timeout_ms'], allow_local=renderer.allow_local)
    try:
        budget.check()
        budget.wait_until(float(run.get('worker_retry_at') or 0), 'worker_backoff')
        store.update(run_id, status='RETRY', defer_reason=None)
        try:
            http = PublicHTTP(run['domain'], requests_per_second=float(run.get('requests_per_second', 2)),
                              timeout=policy['http_timeout_seconds'], retries=policy['http_retries'], budget=budget)
        except FetchError as exc:
            if not exc.retryable:
                store.update(run_id, status='REPAIR_REQUIRED', error=exc.state,
                             integrity_status='FAIL', publish_eligible=False)
                return finish_visit(store, run_id)
            attempts = int(run.get('worker_attempts', 0)) + 1
            deadline = time.time() + 300
            store.update(run_id, worker_attempts=attempts, worker_retry_at=deadline, error=exc.state,
                         integrity_status='FAIL', publish_eligible=False)
            if attempts >= int(run.get('worker_retry_limit', 3)):
                store.update(run_id, status='DEFERRED', defer_reason=exc.state)
                return finish_visit(store, run_id)
            raise ScanPaused('worker_backoff', deadline) from None
        retry = bool(run.get('scan_visit')) or run.get('status') in {'RUNNING', 'RETRY', 'REPAIR_REQUIRED', 'DEFERRED'}
        while True:
            budget.check()
            run = run_brand(store, run_id, http, renderer, retry=retry, budget=budget)
            if run.get('status') == 'DEFERRED' and run.get('defer_reason') == 'domain_backoff':
                raise ScanPaused('domain_backoff', float(run.get('worker_retry_at') or 0))
            transient = (run.get('status') in {'RUNNING', 'RETRY'} or retryable_products(store, run_id))
            if run.get('status') in {'DEFERRED', 'FAILED'} or not transient:
                return finish_visit(store, run_id)
            attempts = int(run.get('worker_attempts', 0)) + 1
            deadline = time.time() + min(300, 5 * 2 ** min(attempts, 6))
            store.update(run_id, worker_attempts=attempts, worker_retry_at=deadline)
            if attempts >= int(run.get('worker_retry_limit', 3)):
                store.update(run_id, status='DEFERRED', defer_reason=run.get('error') or 'transient_scan_failure')
                return finish_visit(store, run_id)
            budget.wait_until(deadline, 'worker_backoff')
            retry = True
    except ScanPaused as pause:
        return pause_visit(store, run_id, pause)


@contextmanager
def publication_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def publish_one(store, run_id, config, lock_path):
    run = store.run(run_id)
    if run.get('htt_publish_status') != 'PASS' and (
        run.get('scanner_status') != 'SCANNER_COMPLETE'
        or not run.get('publish_eligible')
    ):
        return run

    if normalize_audit_policy(run.get('sitemap_audit_policy'))['audit_required']:
        verify_saved_audit(store, run)
    config = {**config, 'publication_lock': str(lock_path)}
    from htt_publish import (
        HTTIntake,
        HTTReadback,
        production_config,
        publish,
        reader_preflight,
        settings,
    )
    api, reader = settings(config)
    live_config = production_config(config, api, reader)
    transport = HTTReadback(live_config)
    health = reader_preflight(transport, api, reader)
    if health.get('status') != 'PASS':
        raise ContractError('READER DEPLOYMENT REQUIRED')
    intake = HTTIntake(api, transport)
    try:
        return publish(store, run_id, live_config, api, reader, transport, intake)
    except ContractError as exc:
        store.update(run_id, status='PUBLICATION_HELD', publication_status='HELD',
                     error=str(exc), fully_complete=False)
        return store.run(run_id)

def status_row(run):
    canonical = run.get('canonical') or {}
    return {
        'brand': run.get('brand'),
        'brand_id': run.get('brand_id'),
        'run_id': run.get('scan_run_id'),
        'score': canonical.get('score'),
        'scanner_status': run.get('scanner_status'),
        'publish_eligible': bool(run.get('publish_eligible')),
        'htt_publish_status': run.get('htt_publish_status'),
        'htt_readback_status': run.get('htt_readback_status'),
        'idealwrld_sync_status': run.get('idealwrld_sync_status'),
        'app_report_status': run.get('app_report_status'),
        'parity_status': run.get('parity_status'),
        'status': run.get('status'),
        'error': run.get('error'),
        'defer_reason': run.get('defer_reason'),
        'next_eligible_at': run.get('scan_next_eligible_at') or run.get('delivery_retry_at') or run.get('worker_retry_at'),
        'scan_visit': run.get('scan_visit'),
        'audit_export_status': run.get('audit_export_status'),
        'audit_export_error': run.get('audit_export_error'),
        'audit_export_path': run.get('audit_export_path'),
        'audit_export_manifest_sha256': run.get('audit_export_manifest_sha256'),
        'audit_export_catalogue_status': run.get('audit_export_catalogue_status'),
        'audit_export_sitemap_status': run.get('audit_export_sitemap_status'),
    }


def worker(state_dir, name, config_path, render=False, publish=False, phase='first', revisit_round=None, stop_requested=None):
    state = Path(state_dir)
    db_path = state / f'{name}.db'
    config = json.loads(Path(config_path).read_text()) if config_path else {}
    if publish:
        from production_worker import verify_canary
        verify_canary(state, config)
    store = Store(db_path)
    policy = normalize_policy(config.get('execution_policy'))
    audit_policy = normalize_audit_policy(config['sitemap_audit_policy']) if 'sitemap_audit_policy' in config else None
    renderer = BrowserRenderer() if render else None
    results = []
    ownership = store.lock()
    ownership.__enter__()
    drained = False
    try:
        for brand, run_id in store.queue():
            # Drain between brands: never interrupt a crawl checkpoint or an
            # in-flight publication whose result could otherwise be ambiguous.
            if stop_requested is not None and stop_requested():
                drained = True
                print(json.dumps({'worker': name, 'worker_status': 'DRAINED',
                                  'next_brand_id': str(brand['brand_id'])}), flush=True)
                break
            try:
                brand = with_audit_policy(with_policy(brand, policy), audit_policy)
                if phase == 'revisit':
                    if run_id is None:
                        continue
                    saved = store.run(run_id)
                    visit = saved.get('scan_visit') or {}
                    if (revisit_state(saved)[0] != 'due' or (revisit_round is not None
                            and (int(visit.get('number', 0)) > revisit_round
                                 or (int(visit.get('number', 0)) == revisit_round and visit.get('state') != 'ACTIVE')))):
                        continue
                run = scan_one(store, brand, run_id, renderer, domain_lock_directory=config.get('domain_lock_directory'), phase=phase)
                run = export_visit_audit(store, run['scan_run_id'], state)
                if run.get('audit_export_status') == 'FAIL':
                    raise RuntimeError('AUDIT EXPORT FAILED; saved scan evidence is retained')
                if publish and (
                    run.get('htt_publish_status') == 'PASS'
                    or (
                        run.get('scanner_status') == 'SCANNER_COMPLETE'
                        and run.get('publish_eligible')
                    )
                ):
                    run = publish_one(store, run['scan_run_id'], config, config.get('publication_lock') or state / 'publish.lock')
                    if run.get('status') in {'RUNNING', 'RETRY', 'SYNC_PENDING'}:
                        store.update(run['scan_run_id'], status='DELIVERY_DEFERRED',
                                     delivery_retry_at=time.time() + 300,
                                     defer_reason=run.get('error') or 'delivery_pending')
                        run = store.run(run['scan_run_id'])
                row = status_row(run)
            except Exception as exc:
                current = None
                try:
                    _, current_id = queue_run(store, brand['brand_id'])
                    current = store.run(current_id) if current_id else None
                except Exception:
                    current = None
                row = status_row(current or brand)
                row['worker_error'] = type(exc).__name__
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                # A failed brand must not starve subsequent independent brands.
                # The worker still exits nonzero and first-pass checkpoint is withheld.
                continue
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        if policy is not None and phase == 'first' and not drained and not any(row.get('worker_error') for row in results):
            checkpoint = state / f'{name}.first-pass.json'
            checkpoint_json(checkpoint, {'scanner_fingerprint': pipeline_fingerprint(),
                                         'execution_policy': policy, 'completed_at': time.time()})
        return results
    finally:
        ownership.__exit__(None, None, None)
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    prep = sub.add_parser('prepare')
    prep.add_argument('--queue', required=True, type=Path)
    prep.add_argument('--state', required=True, type=Path)
    prep.add_argument('--workers', type=int, default=4)
    prep.add_argument('--reset', action='store_true')
    prep.add_argument('--resolve-storefronts', action='store_true')

    run = sub.add_parser('worker')
    run.add_argument('--state', required=True, type=Path)
    run.add_argument('--name', required=True)
    run.add_argument('--config', type=Path)
    run.add_argument('--render', action='store_true')
    run.add_argument('--publish', action='store_true')

    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(args.queue, args.state, args.workers, args.reset, args.resolve_storefronts), indent=2))
        return 0
    results = worker(args.state, args.name, args.config, args.render, args.publish)
    failed = [row for row in results if row.get('worker_error')]
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
