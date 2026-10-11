#!/usr/bin/env python3
"""Durable A→Z worker. Safe by default; explicit singleton publication uses the existing HTT intake."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from row_stream import RowStream
from delta_refresh import prepare_delta
from scan_policy import ScanPaused

from catalogue_discovery import (Discovery, FetchError, PublicHTTP, Response, capture,
                                 catalogue_products, confirm_candidate, is_challenge_page, product_json_probe_url)
from confirmed_product import confirm_product
from scan_contract import read_products, aggregate_rows
from scanner_store import Store, DatabaseSchemaError, digest, now, identity, discovery_hash
from throne_api import scanner_fingerprint
from throne_read import READER_VERSION
from throne_standard import VERSION
from publication_parity import ContractError, StagingTransport, parity_check, propagate

from textile_policy import SCANNER_VERSION
CANARY_KINDS = {'story', 'outerknown', 'shopify', 'rendered', 'large'}

RESCORE_DEPARTMENTS = {'clothing', 'bedding'}


def authority_rescore_department(record):
    """Return the textile department eligible for a new authority score.

    Clothing, including swimwear, and bedding are rescored.
    Other HTT verticals retain their existing published scores.
    """
    department = str(record.get('department') or '').strip().casefold().replace('_', '-')
    cpt = str(record.get('cpt') or '').strip().casefold().replace('_', '-')

    if cpt and cpt not in {'clothes', 'bedding'}:
        return None
    if department in {'clothing', 'clothes', 'apparel', 'textile', 'swimwear'} or cpt == 'clothes':
        return 'clothing'
    if department in {'bedding', 'home-textiles', 'home textiles'} or cpt == 'bedding':
        return 'bedding'
    return None



def pipeline_fingerprint():
    h = hashlib.sha256(scanner_fingerprint().encode())
    for name in ('catalogue_discovery.py', 'scanner_pipeline.py', 'scanner_store.py', 'publication_parity.py', 'rendered_evidence.py', 'htt_adapters.py', 'delta_refresh.py', 'consumer_lookup.py', 'authority_http.py', 'htt_publish.py', 'authority_batch.py', 'production_worker.py', 'checkpoint_io.py', 'storefront.py', 'public_suffix_list.dat', 'scan_policy.py', 'catalogue_audit.py', 'commerce_capture.py', 'evidence_ai_rules.py', 'evidence_ai_gate.py'):
        h.update(Path(__file__).with_name(name).read_bytes())
    return h.hexdigest()


def catalogue_change(previous_total, items, previous_items=None, max_drop=0.30, minimum_previous=20):
    if not 0 <= max_drop < 1 or minimum_previous < 1 or previous_total < 0:
        raise ValueError('Invalid anomaly policy or previous count')
    current = len(items)
    difference = current - previous_total
    percent = difference / previous_total * 100 if previous_total else None
    anomaly = previous_total >= minimum_previous and current < previous_total * (1 - max_drop)
    changes = {'previous_total': previous_total, 'new_total': current, 'absolute_difference': abs(difference),
               'percentage_difference': percent, 'anomaly': anomaly,
               'new_products': None, 'removed_products': None, 'changed_urls': None}
    if previous_items is not None:
        def key(row):
            return str(row.get('product_id') or row.get('platform_product_id') or row['url'])
        old, new = {key(x): x['url'] for x in previous_items}, {key(x): x['url'] for x in items}
        changes.update(new_products=sorted(new.keys() - old.keys()), removed_products=sorted(old.keys() - new.keys()),
                       changed_urls=[{'id': k, 'old': old[k], 'new': new[k]} for k in sorted(new.keys() & old.keys()) if old[k] != new[k]])
    return changes


def recover_rendered_confirmation(store, run_id, url, probe_url=None):
    """Clear only this URL's recovered static failure; retain durable retry history."""
    run = store.run(run_id)
    failure = run.get('authority_retry') or {}
    recovered_urls = {url} | ({probe_url} if probe_url else set())
    if (failure.get('url') not in recovered_urls or failure.get('state') != 'EXHAUSTED'
            or run.get('status') != 'FAILED' or run.get('error') != 'authority_retry_exhausted'):
        return
    # Store recovery by URL; rewriting a growing run JSON map on every product
    # is quadratic for large catalogues and risks losing earlier evidence.
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO rendered_recovery VALUES(?,?,?)',
                         (run_id, url, json.dumps({**failure, 'confirmation_source': 'rendered_html'})))
    count = store.db.execute('SELECT COUNT(*) FROM rendered_recovery WHERE run_id=?',
                             (run_id,)).fetchone()[0]
    store.update(run_id, status='RUNNING', error=None, authority_retry=None,
                 rendered_recoveries_count=count)


def recoverable_product_transition(store, run_id, renderer):
    """Resume only a saved unfinished static-to-browser product transition."""
    run = store.run(run_id)
    pending, failure = run.get('product_recovery_pending'), run.get('authority_retry')
    if (not run.get('discovery_complete') or not callable(renderer)
            or not isinstance(pending, dict) or pending.get('channel') != 'rendered_html'
            or not isinstance(pending.get('url'), str)
            or not isinstance(failure, dict) or failure.get('state') != 'EXHAUSTED'
            or failure.get('url') != pending.get('url')
            or run.get('error') != 'authority_retry_exhausted'
            or run.get('htt_publish_status') == 'PASS'):
        return False
    row = store.db.execute(
        'SELECT item,result,discovery,discovery_hash FROM products WHERE run_id=? AND identity=?',
        (run_id, pending.get('identity'))).fetchone()
    if not row or row[1] is not None or row[3] != pending.get('discovery_hash'):
        return False
    try:
        item, original = json.loads(row[0]), json.loads(row[2])
        if (item.get('url') != pending.get('url') or identity(original) != pending.get('identity')
                or discovery_hash(original) != row[3]):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    saved = store.db.execute(
        'SELECT attempts,state FROM authority_retries WHERE run_id=? AND url=?',
        (run_id, pending['url'])).fetchone()
    if not saved or saved[1] != 'EXHAUSTED' or saved[0] != failure.get('attempts'):
        return False
    rendered = store.db.execute(
        'SELECT state FROM authority_retries WHERE run_id=? AND url=?',
        (run_id, 'rendered:' + pending['url'])).fetchone()
    return not rendered or rendered[0] != 'EXHAUSTED'


def capture_sitemap_for_run(store, run_id, http, budget=None):
    """Save an independent sitemap cross-check under the same source limits.

    Optional audit errors cannot replace scanner results, erase active failures,
    or turn missing sitemap evidence into proof. HTTP retry/domain rows remain
    shared while audit HTTP outcome fields are kept under sitemap_http.
    """
    from catalogue_audit import collect_sitemap_audit, normalize_audit_policy
    from authority_http import AuthorityHTTP
    run = store.run(run_id)
    value = run.get('sitemap_audit_policy')
    if value is None and run.get('execution_policy') is None:
        return True
    policy = normalize_audit_policy(value)
    if isinstance(http, AuthorityHTTP):
        class AuditStore:
            db = store.db

            def run(self, selected):
                saved = store.run(selected)
                return {**saved, **(saved.get('sitemap_http') or {})}

            def update(self, selected, **changes):
                saved = store.run(selected)
                store.update(selected, sitemap_http={**(saved.get('sitemap_http') or {}), **changes})

        http = AuthorityHTTP(http.http, AuditStore(), run_id,
                             retry_limit=http.limit, probe_retry_limit=http.probe_limit,
                             backoff_base=http.backoff_base, backoff_cap=http.cap,
                             clock=http.clock, sleep=http.sleep, jitter=http.jitter, budget=budget)
    try:
        audit = collect_sitemap_audit(http, store, run_id,
                                     check_budget=budget.check if budget is not None else None,
                                     policy=policy)
    except ScanPaused:
        raise
    except FetchError as exc:
        if budget is not None and exc.retryable:
            budget.check()
        if not policy['audit_required']:
            return True
        status = 'RETRY' if exc.retryable else 'REPAIR_REQUIRED'
        if exc.state == 'domain_backoff':
            deadline = float((store.run(run_id).get('sitemap_http') or {}).get('worker_retry_at') or 0)
            if budget is not None:
                raise ScanPaused('domain_backoff', deadline) from None
            status = 'DEFERRED'
            store.update(run_id, defer_reason='domain_backoff', worker_retry_at=deadline)
        store.update(run_id, status=status, error='sitemap_audit_' + exc.state,
                     publish_eligible=False, integrity_status='PENDING')
        return False
    if policy['audit_required'] and audit.get('status') != 'COMPLETE':
        store.update(run_id, status='REPAIR_REQUIRED', error='sitemap_audit_incomplete',
                     publish_eligible=False, integrity_status='PENDING')
        return False
    return True


def run_brand(store, run_id, http, renderer=None, retry=False, manual=None, budget=None):
    run = store.run(run_id)
    capture_transport = run.get('capture_transport', 'auto')
    if capture_transport not in {'auto', 'browser'}:
        raise ValueError('Unsupported capture_transport')
    if capture_transport == 'browser' and not callable(renderer):
        raise ValueError('Browser capture requires the configured renderer')
    if budget is not None and renderer is not None:
        from rendered_evidence import BrowserRenderer
        if isinstance(renderer, BrowserRenderer):
            renderer.budget = budget
    if run.get('execution_policy') is not None and budget is None:
        raise ValueError('Saved execution_policy requires a worker-managed scan visit')
    if budget is not None and budget.run_id != run_id:
        raise ValueError('Visit budget belongs to a different run')
    def check_budget():
        store.check_capacity()
        if budget is not None:
            budget.check()
    check_budget()
    if run.get('mode') != 'AUTHORITY_CENSUS':
        raise ValueError('Only AUTHORITY_CENSUS can enumerate a catalogue')
    if authority_rescore_department(run) not in RESCORE_DEPARTMENTS:
        store.update(run_id, status='REPAIR_REQUIRED', scanner_status='HELD',
                     publish_eligible=False, publication_blockers=['incompatible_authority_department'],
                     error='AUTHORITY RESCORING DISABLED FOR THIS DEPARTMENT; retain existing published score')
        return store.run(run_id)
    if run['scanner_fingerprint'] != pipeline_fingerprint():
        raise ValueError('Scanner changed: create a new immutable run with --force-rescan')
    if run['status'] == 'COMPLETE' or run.get('htt_publish_status') == 'PASS' or run.get('canonical') and not retry:
        return run
    if isinstance(http, PublicHTTP):
        from authority_http import AuthorityHTTP
        http.budget = budget
        policy = budget.policy if budget is not None else {}
        def durable(client):
            return AuthorityHTTP(client, store, run_id,
                                 retry_limit=int(run.get('authority_retry_limit', policy.get('authority_retry_limit', 12))),
                                 probe_retry_limit=int(run.get('authority_probe_retry_limit', policy.get('authority_probe_retry_limit', 1))),
                                 backoff_base=float(run.get('authority_backoff_base', policy.get('authority_backoff_base_seconds', 5))),
                                 backoff_cap=float(run.get('authority_backoff_cap', policy.get('authority_backoff_cap_seconds', 300))),
                                 budget=budget)
        try:
            resolution = run.get('storefront_resolution')
            if resolution:
                http.allowed_hosts.update(resolution['allowed_hosts'])
                http.base = resolution['crawl_origin']
                http.validate(http.base)
            else:
                if capture_transport == 'browser':
                    # Explicit per-run transport selection. The same durable
                    # domain cooldown still owns every browser request.
                    client = durable(http)
                    resolution = http.resolve(get=lambda url: Response(url, client.render(url, renderer), {}))
                else:
                    resolution = http.resolve(get=durable(http).probe)
                accepted_probe = {}
                current = store.run(run_id)
                failure = current.get('authority_retry') or {}
                if (resolution.get('probe_state') == 'authority_retry_exhausted'
                        and failure.get('url') == http.configured_url
                        and failure.get('state') == 'EXHAUSTED'
                        and current.get('error') == 'authority_retry_exhausted'):
                    # resolve() explicitly accepted an unavailable homepage as
                    # a nonfatal origin probe. Keep its durable retry history,
                    # but do not carry FAILED into a later catalogue request.
                    accepted_probe = {'status': 'RUNNING', 'error': None, 'authority_retry': None}
                store.update(run_id, storefront_resolution=resolution,
                             crawl_origin=resolution['crawl_origin'], **accepted_probe)
        except FetchError as exc:
            if exc.retryable:
                check_budget()
            store.update(run_id, status='DEFERRED' if exc.state == 'domain_backoff' else 'RETRY' if exc.retryable else 'REPAIR_REQUIRED',
                         error=exc.state, integrity_status='FAIL')
            return store.run(run_id)
        http = durable(http)
    if budget is not None and renderer is not None:
        from rendered_evidence import BrowserRenderer
        if isinstance(renderer, BrowserRenderer):
            renderer.budget = budget
    # Keep the raw JSON capability before the HTML renderer is wrapped below.
    # Explicit class methods prevent dynamic mocks from inventing this channel.
    fetch_json = renderer.fetch_json if callable(getattr(type(renderer), 'fetch_json', None)) else None
    fetch_sequence = renderer.fetch_json_sequence if callable(getattr(type(renderer), 'fetch_json_sequence', None)) else None

    def require_render_budget():
        check_budget()
        limit = run.get('render_page_limit')
        if limit is not None:
            count = store.db.execute(
                "SELECT COUNT(*) FROM pages WHERE run_id=? AND (url LIKE 'rendered:%' OR url LIKE 'catalogue-json:%')",
                (run_id,)).fetchone()[0]
            if count >= int(limit):
                raise FetchError('render_budget_exhausted')

    if renderer:
        render_page = renderer
        def renderer(url):
            check_budget()
            http.validate(url)
            key = 'rendered:' + url
            cached = store.page(run_id, key)
            if cached:
                return cached['text']
            # One bounded render per distinct URL, cached across interruption.
            # An optional operator budget may hold a run, never truncate it.
            require_render_budget()
            # Use the explicit authority protocol only when the client class
            # implements it; dynamic mocks must not fabricate a render method.
            authority_render = getattr(type(http), 'render', None)
            html = http.render(url, render_page) if callable(authority_render) else render_page(url)
            if is_challenge_page(html, url):
                # A transient HTTP 200 challenge is failed evidence. Caching it
                # would make every later resume replay the same failed page.
                raise FetchError('blocked', True)
            store.save_page(run_id, key, Response(url, html, {}))
            return html
    payload = {'department': run.get('department', 'auto'), 'report_cpt': run['cpt'], 'brandName': run['brand']}
    if not run['discovery_complete']:
        class SnapshotHTTP:
            base = http.base

            def validate(self, url):
                # Delegate explicitly instead of storing a callable as a class
                # attribute, which can be rebound as a descriptor and receive
                # an unexpected SnapshotHTTP self argument.
                return http.validate(url)

            def _fetch(self, url, method):
                check_budget()
                cached = store.page(run_id, url)
                if cached:
                    return Response(**cached)
                response = method(url)
                store.save_page(run_id, url, response)
                return response

            def get(self, url):
                return self._fetch(url, http.get)

            def probe(self, url):
                return self._fetch(url, getattr(http, 'probe', http.get))

            def catalogue(self, url, kind, probe=False):
                check_budget()
                def validated(response, browser=False):
                    if not isinstance(response, Response):
                        raise FetchError('bad_catalogue_response')
                    if browser and response.url != url:
                        raise FetchError('catalogue_response_identity_mismatch')
                    catalogue_products(response, kind)
                    return response

                def provenance(response):
                    return {'capture_source': 'browser_http_response', 'requested_url': url,
                            'adapter': kind, 'http_status': 200,
                            'response_sha256': hashlib.sha256(response.text.encode('utf-8')).hexdigest()}

                def record_recovery(evidence):
                    current = store.run(run_id)
                    pending = current.get('catalogue_recovery_pending')
                    clear_pending = ({'catalogue_recovery_pending': None}
                                     if isinstance(pending, dict) and pending.get('url') == url else {})
                    recoveries = dict(current.get('catalogue_browser_recoveries') or {})
                    changed = recoveries.get(url) != evidence
                    recoveries[url] = evidence
                    failure = current.get('authority_retry') or {}
                    if (failure.get('url') == url and failure.get('state') == 'EXHAUSTED'
                            and current.get('error') == 'authority_retry_exhausted'):
                        # Retain the static EXHAUSTED row and its counters. Only
                        # the active failure is recovered by valid same-URL JSON.
                        store.update(run_id, status='RUNNING', error=None, authority_retry=None,
                                     catalogue_browser_recoveries=recoveries,
                                     **clear_pending)
                    elif changed or clear_pending:
                        store.update(run_id, catalogue_browser_recoveries=recoveries,
                                     **clear_pending)

                def static_response(response):
                    response = validated(response)
                    pending = store.run(run_id).get('catalogue_recovery_pending')
                    if isinstance(pending, dict) and pending.get('url') == url:
                        store.update(run_id, catalogue_recovery_pending=None)
                    return response

                cached = store.page(run_id, url)
                if cached:
                    return static_response(Response(**cached))
                key = 'catalogue-json:' + url
                cached = store.page(run_id, key)
                if cached:
                    try:
                        response = validated(Response(cached['url'], cached['text'], cached['headers']), browser=True)
                        evidence = provenance(response)
                        if cached.get('provenance') != evidence:
                            raise FetchError('catalogue_browser_checkpoint_mismatch')
                    except (KeyError, TypeError, ValueError, AttributeError):
                        raise FetchError('catalogue_browser_checkpoint_mismatch') from None
                    record_recovery(evidence)
                    return response
                if fetch_json is not None:
                    # Static exhaustion commits FAILED before control returns
                    # here. Checkpoint the eligible alternate channel first so
                    # a killed worker can resume that precise transition.
                    store.update(run_id, catalogue_recovery_pending={'url': url, 'adapter': kind})
                def browser_response():
                    if fetch_json is None:
                        raise FetchError('renderer_not_configured')
                    require_render_budget()
                    def read_json(requested_url):
                        return validated(fetch_json(requested_url), browser=True)
                    authority_render = getattr(type(http), 'render', None)
                    response = http.render(url, read_json) if callable(authority_render) else read_json(url)
                    evidence = provenance(response)
                    data = {'url': response.url, 'text': response.text, 'headers': response.headers,
                            'provenance': evidence}
                    with store.db:
                        store.db.execute('INSERT OR IGNORE INTO pages VALUES(?,?,?)',
                                         (run_id, key, json.dumps(data, ensure_ascii=False, allow_nan=False)))
                    record_recovery(evidence)
                    return response
                if capture_transport == 'browser':
                    return browser_response()
                try:
                    method = getattr(http, 'probe', http.get) if probe else http.get
                    response = method(url)
                except FetchError as exc:
                    if fetch_json is None or exc.state not in {'blocked', 'http_429', 'authority_retry_exhausted'}:
                        raise
                    return browser_response()
                response = static_response(response)
                store.save_page(run_id, url, response)
                return response
        discovery = Discovery(SnapshotHTTP(), census_unit=run.get('census_unit', 'product'))
        rejected, candidates, duplicates = Counter(), 0, 0
        seen_candidates = set()
        try:
            for candidate in manual if manual is not None else discovery.enumerate(run.get('adapter', 'auto')):
                check_budget()
                candidates += 1
                item = dict(candidate)
                accepted, reason = confirm_product(item)
                if not accepted and reason == 'missing_positive_product_evidence':
                    # Capture identity independently: even an extraction failure must
                    # leave a confirmed Product in the census with a technical result.
                    try:
                        http.validate(item['url'])
                        recovered_static = False
                        cached_render = store.page(run_id, 'rendered:' + item['url']) if renderer else None
                        if cached_render and manual is None:
                            # Resume confirmation from the same durable evidence, even
                            # when the static retry record is already EXHAUSTED.
                            response = Response(item['url'], '', {})
                            recovered_static = True
                        else:
                            try:
                                response = discovery.get(item['url'])
                            except FetchError as exc:
                                if (manual is not None or not (renderer or product_json_probe_url(item)) or
                                        exc.state not in {'blocked', 'http_429', 'authority_retry_exhausted'}):
                                    raise
                                # An empty response supplies no identity. The existing
                                # plausible-detail, challenge and positive-evidence gates
                                # must all pass before this alternate channel recovers it.
                                item, accepted, reason = confirm_candidate(
                                    item, Response(item['url'], '', {}), renderer,
                                    product_lookup=discovery.probe)
                                if not accepted:
                                    # Keep the static EXHAUSTED row, but report why
                                    # the alternate evidence channel did not recover.
                                    raise FetchError('rendered_product_identity_unresolved' if renderer
                                                     else 'product_json_identity_unresolved') from exc
                                recovered_static = True
                                response = None
                        if response is not None:
                            item, accepted, reason = confirm_candidate(
                                item, response, renderer,
                                product_lookup=None if manual is not None or cached_render else discovery.probe)
                        if recovered_static and not accepted:
                            raise FetchError('rendered_product_identity_unresolved')
                        if accepted and item.get('confirmation_source') == 'rendered_html':
                            recover_rendered_confirmation(
                                store, run_id, item['url'], (item.get('confirmation_probe') or {}).get('url'))
                    except FetchError as exc:
                        # Stale URLs and ambiguous identity are accounted rejections.
                        # Transient, blocked and security failures still hold discovery.
                        if exc.retryable or exc.state not in {'http_404', 'http_410', 'ambiguous_product_identity'}:
                            raise
                        accepted, reason = False, exc.state
                if not accepted:
                    rejected[reason] += 1
                    with store.db:
                        store.db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?)', (run_id, candidates, None, reason))
                    continue
                key = identity(item)
                duplicates += key in seen_candidates
                seen_candidates.add(key)
                store.add(run_id, item)
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?)', (run_id, candidates, key, 'confirmed'))
            items = RowStream(lambda: (p['discovery'] for p in store.iter_products(run_id)), store.product_count(run_id))
            previous = store.previous_snapshot(run)
            previous_total = store.product_count(previous['scan_run_id']) if previous else int(run.get('existing_catalogue_count') or 0)
            change = catalogue_change(previous_total, items, run.get('previous_products'),
                                      float(run.get('max_catalogue_drop', .30)), int(run.get('anomaly_minimum_previous', 20)))
            snapshot = store.discovery_digest(run_id)
            store.update(run_id, discovery_complete=True, catalogue_snapshot_id=snapshot,
                         catalogue_previous=change['previous_total'], catalogue_discovered=candidates,
                         catalogue_confirmed=len(items), discovery_rejected=dict(rejected), duplicate_count=duplicates,
                         catalogue_change=change, manual_mode=manual is not None,
                         discovery_reconciliation='PASS' if candidates == len(items) + sum(rejected.values()) + duplicates else 'FAIL',
                         rejected_candidates=sum(rejected.values()) + duplicates,
                         discovery_outcomes=discovery.outcomes)
        except (FetchError, ValueError, TypeError) as exc:
            if isinstance(exc, FetchError):
                if exc.retryable:
                    check_budget()
                status = 'DEFERRED' if exc.state == 'domain_backoff' else 'FAILED' if exc.state == 'authority_retry_exhausted' else ('RETRY' if exc.retryable else 'REPAIR_REQUIRED')
            else:
                status = 'REPAIR_REQUIRED'
            store.update(run_id, status=status, error=str(exc), integrity_status='FAIL',
                         discovery_outcomes=discovery.outcomes)
            return store.run(run_id)
    check_budget()
    if not store.run(run_id).get('delta_prepared'):
        prepare_delta(store, run_id, force=run.get('force_rescan', False))
    # Database cursors read at most 300 records; commit every completed product.
    for product in store.iter_products(run_id):
        if product['result'] and not (retry and product['retryable']):
            continue
        check_budget()
        item = product['item']
        retryable = False
        try:
            if item.get('manual_evidence_verified') is True or item.get('_reuse_raw_evidence') is True:
                # Explicit repair evidence goes through exactly the same reader.
                from text_normalizer import normalize_row
                item = normalize_row(item)
                item['fetch_status'] = 'ok'
            else:
                recovered_static = False
                recovered_json = False
                if renderer:
                    # Checkpoint before static retries can mark the run FAILED.
                    store.update(run_id, product_recovery_pending={
                        'identity': product['identity'], 'url': item['url'],
                        'discovery_hash': product['discovery_hash'], 'channel': 'rendered_html'})
                try:
                    http.validate(item['url'])
                    cached_render = store.page(run_id, 'rendered:' + item['url'])
                    if cached_render and (item.get('confirmation_source') == 'rendered_html'
                                          or item.get('platform_product_id') is not None):
                        response = Response(**cached_render)
                        recovered_static = True
                    elif capture_transport == 'browser':
                        response = Response(item['url'], renderer(item['url']), {})
                        item = {**item, 'confirmation_source': 'rendered_html'}
                        recovered_static = True
                    else:
                        response = http.get(item['url'])
                        store.save_page(run_id, item['url'], response)
                except FetchError as exc:
                    if (exc.state in {'blocked', 'http_429', 'authority_retry_exhausted'}
                            and item.get('product_lookup_source_endpoint')):
                        endpoint = item['product_lookup_source_endpoint']
                        cached_json = store.page(run_id, endpoint)
                        if (not cached_json or hashlib.sha256(cached_json['text'].encode()).hexdigest()
                                != item.get('product_lookup_response_sha256')):
                            raise FetchError('product_json_checkpoint_mismatch') from None
                        item = {**item, 'page_fetch_status': exc.state}
                        response = Response(**cached_json)
                        recovered_json = True
                    elif exc.state in {'blocked', 'http_429', 'authority_retry_exhausted'} and renderer:
                        # Catalogue identity is already established independently
                        # from positive product evidence. Render once
                        # for product-owned evidence instead of letting an exhausted
                        # static route abort or permanently poison the brand run.
                        item = {**item, 'page_fetch_status': exc.state,
                                'confirmation_source': 'rendered_html'}
                        rendered = renderer(item['url'])
                        response = Response(item['url'], rendered, {})
                        recovered_static = True
                    elif exc.state == 'blocked':
                        # Commerce adapters can carry sufficient first-party evidence
                        # even when direct HTML is blocked.
                        item = {**item, 'page_fetch_status': 'blocked'}
                        response = Response(item['url'], '', {})
                    else:
                        raise
                try:
                    item = capture(item, response, None if recovered_static else renderer,
                                   run.get('department', 'auto'), rendered_response=recovered_static,
                                   product_json_response=recovered_json)
                except FetchError as exc:
                    if recovered_static and exc.state == 'render_required':
                        raise FetchError('extractor_unresolved') from None
                    raise
                if recovered_static:
                    recover_rendered_confirmation(store, run_id, item['url'])
            if not item.get('_reuse_raw_evidence'):
                item['fetchedAt'] = now()
            rows, _ = read_products([item], payload, True)
            row = rows[0]
            if run.get('independent_ai_review') is True and row.get('scoreable') is True:
                from evidence_ai_gate import verify_item
                row = verify_item(item, row, html=response.text if not (item.get('manual_evidence_verified') is True or item.get('_reuse_raw_evidence') is True) else '')
            if (run.get('require_commerce_capture') is True or run.get('capture_commerce') is True) and row.get('terminal_state') not in {'excluded_accessory','excluded_nontextile'}:
                from commerce_capture import capture_available_commerce
                executor = http.render if callable(getattr(type(http), 'render', None)) else None
                commercial, missing = capture_available_commerce(item, fetch_sequence, executor, store, run_id,
                    required=run.get('require_commerce_capture') is True)
                if commercial:
                    item['commercial'] = commercial
                    row['commercial'] = commercial
                else:
                    item['commercial_missing_reason'] = row['commercial_missing_reason'] = missing
        except ScanPaused:
            # The worker checkpoints this visit; the product remains pending.
            raise
        except FetchError as exc:
            if exc.state == 'domain_backoff':
                # Keep this product and the remainder pending for a deliberate resume.
                # Aggregating here would replace DEFERRED with permanent failures.
                store.update(run_id, status='DEFERRED', error=exc.state,
                             defer_reason=exc.state, publish_eligible=False)
                return store.run(run_id)
            if exc.retryable:
                check_budget()
            item = {**item, 'fetch_status': exc.state}
            retryable = exc.retryable
            row = read_products([item], payload, True)[0][0]
        except Exception:
            # Preserve a row even for a parser bug. Exception text can contain provider secrets.
            item = {**item, 'fetch_status': 'reader_error'}
            retryable = True
            row = read_products([item], payload, True)[0][0]
        store.save_product(run_id, product['identity'], item, row, retryable)
        pending = store.run(run_id).get('product_recovery_pending')
        if isinstance(pending, dict) and pending.get('identity') == product['identity']:
            store.update(run_id, product_recovery_pending=None)
    check_budget()
    if not capture_sitemap_for_run(store, run_id, http, budget):
        return store.run(run_id)
    products = RowStream(lambda: store.iter_products(run_id), store.product_count(run_id))
    rows = store.result_rows(run_id)
    store.update(run_id, phase='AGGREGATING')
    counts = Counter(r['terminal_state'] for r in rows)
    unaccounted = len(products) - len(rows)
    run = store.run(run_id)
    payload['catalogue'] = {'complete': run['discovery_complete'], 'confirmed_total': len(products)}
    stats = {'discovery_candidates_count': run['catalogue_discovered'],
             'discovery_rejected_count': sum(run['discovery_rejected'].values()),
             'discovery_rejected_reasons': run['discovery_rejected'], 'duplicate_count': run['duplicate_count']}
    report = aggregate_rows(rows, payload, True, stats, compact=True)
    blockers = list(report['brand']['publication_blockers'])
    if run['catalogue_change']['anomaly']:
        blockers.append('CATALOGUE_DISCOVERY_ANOMALY')
    if run.get('manual_mode') and run.get('manual_catalogue_complete') is not True:
        blockers.append('MANUAL_CATALOGUE_COMPLETENESS_UNVERIFIED')
    if unaccounted or run.get('discovery_reconciliation') != 'PASS':
        blockers.append('SCAN_INTEGRITY_FAIL')
    ai_eligible=ai_passed=0
    for r in rows:
        if r.get('scoreable') is True or r.get('independent_review') is not None:
            ai_eligible+=1
            ai_passed+=int((r.get('independent_review') or {}).get('pass') is True)
    if run.get('independent_ai_review') is True and ai_passed!=ai_eligible:
        blockers.append('INDEPENDENT_EVIDENCE_REVIEW_INCOMPLETE')
    store.update(run_id, independent_review_eligible=ai_eligible, independent_review_passed=ai_passed)
    commerce_coverage = {'required':run.get('require_commerce_capture') is True,
                         'eligible_products':0,'captured_products':0,'variants':0,
                         'sku_disclosed':0,'prices_available':0,'product_image_sources':0}
    for r in rows:
        if r.get('terminal_state') in {'excluded_accessory','excluded_nontextile'}:
            continue
        commerce_coverage['eligible_products'] += 1
        commercial = r.get('commercial') or {}
        if commercial.get('variant_census_complete'):
            commerce_coverage['captured_products'] += 1
        variants = commercial.get('variants') or []
        commerce_coverage['variants'] += len(variants)
        commerce_coverage['sku_disclosed'] += sum(v.get('sku_status')=='disclosed' for v in variants)
        commerce_coverage['prices_available'] += sum(v.get('price_status')=='available' for v in variants)
        commerce_coverage['product_image_sources'] += len(commercial.get('product_images') or [])
    if commerce_coverage['required'] and commerce_coverage['eligible_products'] != commerce_coverage['captured_products']:
        blockers.append('COMMERCIAL_CAPTURE_INCOMPLETE')
    store.update(run_id, commerce_coverage=commerce_coverage)
    brand = report['brand']
    technical = sum(counts[k] for k in ('technical_failure', 'blocked', 'timeout', 'reader_error', 'http_404', 'product_removed'))
    review = sum(counts[k] for k in ('category_unresolved', 'evidence_conflict', 'partial_composition', 'manual_review_required'))
    report_total = int(report.get('report_read_count') or 0)
    excluded_accessory = counts['excluded_accessory']
    excluded_nontextile = counts['excluded_nontextile']
    excluded_total = excluded_accessory + excluded_nontextile
    scan_date = now()
    canonical = {'brand_id': run['brand_id'], 'htt_id': run['htt_id'], 'brand': run['brand'], 'domain': run['domain'],
                 'slug': run.get('slug', ''), 'idealwrld_id': run.get('idealwrld_id'), 'score': brand.get('score') if not blockers else None,
                 'score_status': brand['status'], 'publication_status': 'verified' if not blockers else 'blocked',
                 'catalogue_total': report_total, 'eligible_total': brand.get('eligible_total'), 'sku_count': report_total,
                 'catalogue_identified_total': len(products),
                 'excluded_accessory_count': excluded_accessory,
                 'excluded_nontextile_count': excluded_nontextile,
                 'out_of_scope_count': excluded_total,
                 'product_count': store.db.execute(
                     "SELECT COUNT(DISTINCT COALESCE(NULLIF(json_extract(result,'$.product_id'),''),json_extract(result,'$.url'))) "
                     "FROM products WHERE run_id=? AND json_extract(result,'$.terminal_state') "
                     "NOT IN ('excluded_nontextile','excluded_accessory')", (run_id,)).fetchone()[0],
                 'processed_total': report_total,
                 'census_unit': run.get('census_unit', 'product'),
                 'scored_count': counts['scored'], 'undisclosed_count': counts['undisclosed'],
                 'technical_failure_count': technical, 'coverage': brand.get('coverage'),
                 'disclosure_rate': brand.get('disclosureRate'), 'scan_date': scan_date,
                 'scanner_version': run['scanner_version'], 'reader_version': run['reader_version'],
                 'standard_version': run['standard_version'], 'scan_run_id': run_id,
                 'catalogue_snapshot_id': run['catalogue_snapshot_id'], 'scanner_fingerprint': run['scanner_fingerprint']}
    for field in ('composition_plant_share_pct', 'composition_coverage_pct', 'composition_assessed_count',
                  'composition_missing_count', 'composition_score_scope',
                  'catalogue_plant_share_lower_bound', 'catalogue_plant_share_upper_bound'):
        canonical[field] = brand.get(field)
    for field in ('swimwear_score', 'swimwear_natural_fibre_pct',
                  'swimwear_eligible_count', 'swimwear_scoreable_count',
                  'swimwear_undisclosed_count', 'swimwear_technical_count'):
        canonical[field] = None
    # Persist only the compact report here; evidence/SKU rows remain in SQLite.
    report.pop('read', None)
    report.pop('report_notes', None)
    report.pop('groups', None)
    store.update(run_id, report=report, canonical=canonical, processed=len(rows), report_processed=report_total,
                 scored=counts['scored'], undisclosed=counts['undisclosed'], technical_failures=technical,
                 excluded=excluded_total, excluded_accessory=excluded_accessory,
                 excluded_nontextile=excluded_nontextile,
                 review_required=review, unaccounted=unaccounted, terminal_counts=dict(counts), canonical_score=canonical['score'],
                 integrity_status='PASS' if unaccounted == 0 and run['discovery_reconciliation'] == 'PASS' else 'FAIL', publish_eligible=not blockers,
                 publication_blockers=blockers, status='SCANNER_COMPLETE' if not blockers else 'REPAIR_REQUIRED',
                 scanner_status='SCANNER_COMPLETE' if not unaccounted and run['discovery_reconciliation'] == 'PASS' and not run['catalogue_change']['anomaly'] else 'INCOMPLETE',
                 scanner_completed_at=now(), phase='SCANNER_COMPLETE' if not unaccounted and run['discovery_reconciliation'] == 'PASS' and not run['catalogue_change']['anomaly'] else 'INCOMPLETE',
                 error=', '.join(blockers) or None)
    return store.run(run_id)


def require_canaries(store, config=None):
    passed = set()
    for brand, run_id in store.queue():
        if run_id:
            run = store.run(run_id)
            if run['status'] == 'COMPLETE' and run['scanner_fingerprint'] == pipeline_fingerprint() and run.get('parity_status') == 'PASS' and run.get('propagation_contract_hash') == digest(config or {}):
                kind = brand.get('canary_kind')
                if kind != 'large' or run.get('processed', 0) > 500:
                    passed.add(kind)
    if not CANARY_KINDS <= passed:
        raise ValueError('A→Z blocked: required canaries incomplete: ' + ', '.join(sorted(CANARY_KINDS - passed)))


def console(run, index=None):
    print('═' * 42)
    print((f'[{index}] ' if index else '') + run['brand'])
    print('MODE:', run.get('mode', 'AUTHORITY_CENSUS'))
    print('PREVIOUS CATALOGUE:', run.get('catalogue_previous'))
    print('DELTA:', json.dumps(run.get('delta_counts', {})))
    print('Required fresh reads:', run.get('required_fresh_reads'))
    print('Reused trusted evidence:', run.get('reused_evidence'))
    for label, key in [('Discovered', 'catalogue_discovered'), ('Confirmed', 'catalogue_confirmed'), ('Processed', 'processed'),
                       ('Scored', 'scored'), ('Undisclosed', 'undisclosed'), ('Technical failures', 'technical_failures'),
                       ('Excluded', 'excluded'), ('Review required', 'review_required'), ('Unaccounted', 'unaccounted'),
                       ('Integrity', 'integrity_status'), ('Canonical score', 'canonical_score'), ('HTT write', 'htt_publish_status'),
                       ('HTT readback', 'htt_readback_status'), ('IdealWrld sync', 'idealwrld_sync_status'),
                       ('IdealWrld readback', 'idealwrld_readback_status'), ('App catalogue', 'app_catalogue_status'),
                       ('App report', 'app_report_status'), ('ALL SURFACES PARITY', 'parity_status')]:
        print(f'{label:.<28} {run.get(key, "NOT TESTED")}')
    print('SCANNER:', run.get('scanner_status', 'INCOMPLETE'))
    print('PUBLICATION:', run.get('publication_status', 'NOT AUTHORIZED'))
    print('ECOSYSTEM:', run.get('ecosystem_status', 'NOT TESTED'))
    print('FINAL STATUS:', 'VERIFIED + PUBLISHED (' + run.get('delivery_mode', 'staging').upper() + ')' if run['status'] == 'COMPLETE' else 'INCOMPLETE: ' + run['status'])
    if run.get('error'):
        print('Reason:', run['error'])


def unresolved_report(store, run_id, limit=20):
    """Bounded actionable diagnostics; never dump configuration or credentials."""
    samples, total = [], 0
    for product in store.iter_products(run_id):
        row, item = product['result'] or {}, product['item']
        if row.get('terminal_state') in {'scored', 'excluded_nontextile', 'duplicate'}:
            continue
        total += 1
        if len(samples) < limit:
            samples.append({'title': item.get('title'), 'url': item.get('url'),
                'terminal_state': row.get('terminal_state'), 'how': row.get('how'),
                'composition': row.get('composition'), 'fetch_status': item.get('fetch_status'),
                'page_fetch_status': item.get('page_fetch_status')})
    return {'scan_run_id': run_id, 'unresolved_count': total, 'samples': samples}


def select_queue(queue, brand=None, start_letter=None, start_brand=None):
    rows = list(queue)
    if brand:
        selector = str(brand).casefold()
        by_id = [(record, run_id) for record, run_id in rows
                 if str(record.get('brand_id', '')).casefold() == selector]
        if by_id:
            rows = by_id
        else:
            rows = [(record, run_id) for record, run_id in rows
                    if selector in {str(record.get('brand', '')).casefold(),
                                    str(record.get('slug', '')).casefold()}]
            if len(rows) > 1:
                raise ValueError('Ambiguous brand selector; use stable brand_id')
    return [(record, run_id) for record, run_id in rows
            if (not start_letter or record['brand'].casefold() >= start_letter.casefold())
            and (not start_brand or record['brand'].casefold() >= start_brand.casefold())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--queue', type=Path, help='HTT brand records JSON array, stable IDs required')
    parser.add_argument('--queue-url', help='Configured staging HTT paginated queue contract')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--mode', choices=['AUTHORITY_CENSUS','CONSUMER_LOOKUP'], default='AUTHORITY_CENSUS')
    parser.add_argument('--dry-run', action='store_true', help='Preview queue only; no discovery or publication')
    parser.add_argument('--brand', help='Exact stable brand_id, brand name, or slug')
    parser.add_argument('--htt-id')
    parser.add_argument('--sku')
    parser.add_argument('--start-letter')
    parser.add_argument('--start-brand')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='New immutable delta run')
    parser.add_argument('--canaries', action='store_true')
    parser.add_argument('--force-rescan', action='store_true')
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--run-id')
    parser.add_argument('--manual', type=Path, help='Complete manual evidence array; partial inputs remain blocked')
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--publish', action='store_true', help='Publish one explicit verified existing HTT listing through /scans; requires HTT_API_KEY')
    parser.add_argument('--authority-retry-limit', type=int, help='Durable per-operation 429 retry ceiling (default 12)')
    parser.add_argument('--publish-staging', action='store_true', help='Local contract simulation only; all live writes disabled')
    parser.add_argument('--dry-run-publication', metavar='SCAN_RUN_ID', help='Export existing /scans intake plan without sending requests')
    parser.add_argument('--parity-check', metavar='SCAN_RUN_ID')
    args = parser.parse_args()
    if args.publish:
        from htt_publish import settings
        try:
            settings()  # Missing credential fails before DB/network/scanning.
            if args.mode != 'AUTHORITY_CENSUS' or not args.brand or args.publish_staging or args.canaries or args.dry_run:
                raise ContractError('--publish requires one explicit existing HTT brand in AUTHORITY_CENSUS mode')
            if args.resume and args.run_id and (args.refresh or args.force_rescan or args.manual or args.retry or args.queue or args.queue_url):
                raise ContractError('Saved publication resume cannot modify or retry the catalogue')
        except ContractError as exc:
            print(str(exc))
            return 1
    try:
        store = Store(args.db)
    except DatabaseSchemaError as exc:
        print(str(exc))
        return 1
    config = json.loads(args.config.read_text()) if args.config else {}
    try:
        with store.lock():
            live = None
            if args.publish:
                from htt_publish import settings, production_config, reader_preflight, HTTIntake, HTTReadback
                api, reader = settings(config)
                live_config = production_config(config, api, reader)
                live_transport = HTTReadback(live_config)
                health = reader_preflight(live_transport, api, reader)
                if health['status'] != 'PASS':
                    print(json.dumps(health))
                    raise ContractError('READER DEPLOYMENT REQUIRED')
                live = (api, reader, live_config, live_transport, HTTIntake(api, live_transport))
            if args.queue:
                store.enqueue(json.loads(args.queue.read_text()))
            if args.queue_url:
                transport = StagingTransport(config)
                url, brands, seen = args.queue_url, [], set()
                while url:
                    if url in seen:
                        raise ValueError('Queue pagination cycle')
                    seen.add(url)
                    page = transport.request(url, token_env=config.get('queue_token_env'))
                    brands.extend(page['records'])
                    url = page.get('next')
                    if url is None and page.get('complete') is not True:
                        raise ValueError('Queue enumeration not complete')
                store.enqueue(brands)
            if args.mode == 'CONSUMER_LOOKUP':
                if not args.htt_id or not args.brand:
                    raise ValueError('Consumer lookup requires --htt-id and --brand slug')
                from consumer_lookup import lookup
                result = lookup(args.htt_id, args.brand, StagingTransport(config), config['bases']['htt_api'], store.enqueue_refresh, args.sku)
                print(json.dumps(result))
                return 0 if result['status'] == 'CURRENT' else 1
            if args.dry_run:
                print(json.dumps({'mode':'AUTHORITY_CENSUS','dry_run':True,'writes':'NOT AUTHORIZED',
                                  'brands':[brand for brand,_ in select_queue(store.queue(), args.brand, args.start_letter, args.start_brand)]}))
                return 0
            if args.status:
                print(json.dumps(store.summary()))
                return 0
            if args.dry_run_publication:
                from htt_adapters import publication_plan
                from rescore_catalogues import atomic_json
                run = store.run(args.dry_run_publication)
                plan = publication_plan(run, store.products(args.dry_run_publication), config.get('bases', {}))
                destination = args.db.with_suffix('.publication-plan.json')
                atomic_json(destination, plan)
                print('DRY RUN ONLY:', str(destination))
                return 1
            if args.parity_check:
                run = store.run(args.parity_check)
                if config.get('target_environment') == 'production':
                    from htt_publish import HTTReadback, readback_sample
                    rows = readback_sample(store, args.parity_check, config)
                    transport = HTTReadback(config)
                else:
                    rows = store.result_rows(args.parity_check)
                    transport = StagingTransport(config)
                result = parity_check(run['canonical'], config, transport, rows)
                if not run.get('publish_eligible') or run.get('htt_publish_status') != 'PASS' or run.get('idealwrld_sync_status') != 'IW_SYNC_VERIFIED':
                    result['status'] = 'FAIL'
                    result['publication_prerequisites'] = 'NOT TESTED'
                store.update(args.parity_check, parity=result, parity_status=result['status'])
                if result['status'] != 'PASS':
                    store.update(args.parity_check, status='SYNC_PENDING' if run.get('htt_publish_status') == 'PASS' else run['status'], completed_at=None, fully_complete=False, ecosystem_status='INCOMPLETE', completion_status='INCOMPLETE')
                print(json.dumps(result, indent=2))
                return 0 if result['status'] == 'PASS' else 1
            if not args.canaries and not args.run_id and not args.brand:
                require_canaries(store, config)
            renderer = None
            if args.render:
                from rendered_evidence import BrowserRenderer
                renderer = BrowserRenderer()
            selected = [(brand, run_id) for brand, run_id in select_queue(store.queue(), args.brand, args.start_letter, args.start_brand)
                        if authority_rescore_department(brand) in RESCORE_DEPARTMENTS
                        and (not args.canaries or brand.get('canary_kind') in CANARY_KINDS)
                        and (not args.run_id or run_id == args.run_id)]
            if args.publish and (len(selected) != 1 or str(selected[0][0]['brand_id']) != str(args.brand)):
                raise ContractError('--publish requires exactly one existing HTT queue record')
            if not selected:
                raise ValueError('No matching queue records')
            completed_runs = []
            for index, (brand, run_id) in enumerate(selected, 1):
                if not run_id or args.force_rescan or args.refresh:
                    brand = {**brand, 'force_rescan': args.force_rescan}
                    if args.authority_retry_limit is not None:
                        brand['authority_retry_limit'] = args.authority_retry_limit
                    run_id = store.create(brand, pipeline_fingerprint(), SCANNER_VERSION, READER_VERSION, VERSION)['scan_run_id']
                if args.publish and args.resume and args.run_id:
                    # Delivery-only retry of an explicitly selected immutable scan.
                    # publish() verifies its canonical package, integrity and eligibility.
                    run = store.run(run_id)
                else:
                    try:
                        http = PublicHTTP(brand['domain'], requests_per_second=float(brand.get('requests_per_second', 2)))
                        run = run_brand(store, run_id, http, renderer, args.retry or args.resume,
                                        json.loads(args.manual.read_text()) if args.manual else None)
                    except (FetchError, ValueError, ContractError) as exc:
                        # A bad brand must not terminate the unrelated catalogue.
                        # Explicit publication remains fail-fast to protect its contract.
                        if args.publish or args.publish_staging:
                            raise
                        store.update(run_id,
                                     status='RETRY' if isinstance(exc, FetchError) and exc.retryable else 'REPAIR_REQUIRED',
                                     scanner_status='INCOMPLETE' if isinstance(exc, FetchError) and exc.retryable else 'HELD',
                                     integrity_status=None if isinstance(exc, FetchError) and exc.retryable else 'FAIL',
                                     publish_eligible=False,
                                     error=getattr(exc, 'state', str(exc)))
                        run = store.run(run_id)
                        console(run, f'{index}/{len(selected)}')
                        print(json.dumps(unresolved_report(store, run_id), indent=2))
                        completed_runs.append(run)
                        store.export(args.db.with_suffix('.manifest.json'))
                        continue
                if live:
                    from htt_publish import publish
                    api, reader, live_config, live_transport, intake = live
                    try:
                        run = publish(store, run_id, live_config, api, reader, live_transport, intake)
                    except ContractError:
                        console(store.run(run_id), f'{index}/{len(selected)}')
                        print(json.dumps(unresolved_report(store, run_id), indent=2))
                        raise
                if args.publish_staging and run.get('publish_eligible'):
                    rows = store.result_rows(run_id)
                    run = propagate(store, run_id, config, StagingTransport(config), rows)
                console(run, f'{index}/{len(selected)}')
                if not run.get('publish_eligible'):
                    print(json.dumps(unresolved_report(store, run_id), indent=2))
                completed_runs.append(run)
                store.export(args.db.with_suffix('.manifest.json'))
            expected_status = 'COMPLETE' if args.publish or args.publish_staging else 'SCANNER_COMPLETE'
            return 0 if completed_runs and all(run['status'] == expected_status for run in completed_runs) else 1
    except (ValueError, ContractError, FetchError) as exc:
        print('INCOMPLETE:', str(exc))
        return 1
    finally:
        store.export(args.db.with_suffix('.manifest.json'))
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
