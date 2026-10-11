"""Bounded sitemap capture and offline catalogue evidence export.

Collection uses only the caller's guarded HTTP client, checkpoint store and
visit budget. Export opens SQLite read-only and never contacts a storefront.
Neither operation grants publication eligibility or changes product results.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from catalogue_discovery import FetchError, Response, catalogue_products, crawl_origin
from scan_contract import canonical_url


DEFAULT_POLICY = {'mode': 'captured_only', 'audit_required': False,
                  'max_documents': 1000, 'max_total_bytes': 134_217_728,
                  'max_urls': 1_000_000, 'max_response_bytes': 8_000_000,
                  'max_export_bytes': 268_435_456}
SAFE_HEADERS = {'content-type', 'content-length', 'etag', 'last-modified', 'date', 'cache-control', 'x-wp-totalpages'}
RUN_FIELDS = ('scan_run_id', 'brand_id', 'htt_id', 'brand', 'domain', 'cpt', 'department',
              'census_unit', 'started_at', 'completed_at', 'scanner_fingerprint', 'scanner_version',
              'reader_version', 'standard_version', 'catalogue_snapshot_id', 'discovery_complete',
              'discovery_reconciliation', 'catalogue_discovered', 'catalogue_confirmed',
              'duplicate_count', 'discovery_rejected', 'scanner_status', 'integrity_status')


def normalize_audit_policy(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError('Invalid sitemap_audit policy keys')
    policy = {**DEFAULT_POLICY, **value}
    if policy['mode'] not in {'captured_only', 'cross_check'} or type(policy['audit_required']) is not bool:
        raise ValueError('Invalid sitemap_audit mode or audit_required')
    if policy['audit_required'] and policy['mode'] != 'cross_check':
        raise ValueError('Required sitemap audit needs cross_check mode')
    for key, maximum in (('max_documents', 100_000), ('max_total_bytes', 2_147_483_648),
                         ('max_urls', 10_000_000), ('max_response_bytes', 8_000_000),
                         ('max_export_bytes', 2_147_483_648)):
        if type(policy[key]) is not int or not 1 <= policy[key] <= maximum:
            raise ValueError('Invalid sitemap_audit.' + key)
    return policy


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _public_url(value):
    if not isinstance(value, str):
        raise FetchError('malformed_sitemap')
    try:
        parsed = urlsplit(value)
        parsed.port  # Reject malformed ports even for links that are not fetched.
    except ValueError:
        raise FetchError('unsafe_url') from None
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise FetchError('unsafe_url')
    return value


def _product_branch(url):
    return bool(re.search(r'(?:^|[-_.])products?(?:[-_.]|$)', urlsplit(url).path.rsplit('/', 1)[-1], re.I))


def _first_party(base, url):
    return (urlsplit(base).hostname or '').lower().removeprefix('www.') == (urlsplit(url).hostname or '').lower().removeprefix('www.')


def _reason(value):
    allowed = {'site_pass_budget', 'domain_backoff', 'worker_backoff', 'audit_interrupted',
               'audit_document_limit', 'audit_response_limit', 'audit_byte_limit', 'audit_url_limit',
               'audit_checkpoint_mismatch', 'sitemap_urlset_not_captured', 'malformed_sitemap',
               'sitemap_unavailable', 'bad_sitemap_response', 'unsafe_url', 'offsite_url',
               'blocked', 'timeout', 'connection_error', 'authority_retry_exhausted'}
    return value if isinstance(value, str) and (value in allowed or re.fullmatch(r'http_[1-5][0-9]{2}', value)) else None


def parse_sitemap(text, max_urls=1_000_000):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise FetchError('malformed_sitemap')
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        raise FetchError('malformed_sitemap') from None
    kind = root.tag.rsplit('}', 1)[-1]
    if kind not in {'sitemapindex', 'urlset'}:
        raise FetchError('malformed_sitemap')
    entries = []
    for entry in root:
        if entry.tag.rsplit('}', 1)[-1] != ('sitemap' if kind == 'sitemapindex' else 'url'):
            raise FetchError('malformed_sitemap')
        loc = next((child.text for child in entry if child.tag.rsplit('}', 1)[-1] == 'loc'), None)
        loc = _public_url(loc.strip() if isinstance(loc, str) else loc)
        entries.append({'url': loc, 'lastmod': next((child.text for child in entry
                       if child.tag.rsplit('}', 1)[-1] == 'lastmod'), None)})
        if len(entries) > max_urls:
            raise FetchError('audit_url_limit')
    return kind, entries


def _robots_roots(text):
    roots, seen = [], set()
    for line in text.splitlines():
        if line.strip().casefold().startswith('sitemap:'):
            value = line.split(':', 1)[1].strip().split()
            if not value:
                raise FetchError('malformed_sitemap')
            url = _public_url(value[0])
            if url not in seen:
                roots.append(url)
                seen.add(url)
    return roots


def collect_sitemap_audit(http, store, run_id, *, check_budget=None, policy=None):
    """Checkpoint a product-sitemap graph without changing the confirmed census.

    http must be the caller's authority/SSRF-guarded client. check_budget
    is normally VisitBudget.check. Exceptions propagate after an audit-only
    checkpoint; retry/domain state belongs to the supplied HTTP client/caller.
    Revisit resumes the same pending URL and never resets any HTTP counters.
    """
    selected = normalize_audit_policy(policy)
    run = store.run(run_id)
    state = run.get('sitemap_audit')
    if state:
        if state.get('policy') != selected:
            raise ValueError('Saved sitemap audit policy cannot change on revisit')
        state = json.loads(_encode(state))
    else:
        origin = crawl_origin(http.base)
        state = {'version': 1, 'policy': selected, 'scope': 'product_sitemap_branches',
                 'origin': origin, 'status': 'NOT_REQUESTED', 'started_at': _now(),
                 'pending': [{'url': origin + '/sitemap.xml', 'role': 'sitemap', 'parent': None},
                             {'url': origin + '/robots.txt', 'role': 'robots', 'parent': None}],
                 'documents': [], 'edges': [], 'failures': [], 'total_bytes': 0, 'url_count': 0}
    if selected['mode'] == 'captured_only':
        store.update(run_id, sitemap_audit=state)
        return state
    check = check_budget or (lambda: None)
    def save(status, reason=None):
        state.update(status=status, reason=reason, updated_at=_now())
        store.update(run_id, sitemap_audit=state)
    try:
        for document in state['documents']:
            check()
            cached = store.page(run_id, document['requested_url'])
            if (not cached or not isinstance(cached.get('text'), str)
                    or cached.get('url') != document['response_url']
                    or _sha(cached['text'].encode('utf-8')) != document['sha256']):
                raise FetchError('audit_checkpoint_mismatch')
    except FetchError as exc:
        save('INCOMPLETE', exc.state)
        raise
    except Exception as exc:
        save('PAUSED', _reason(getattr(exc, 'reason', None)) or 'audit_interrupted')
        raise
    if state['status'] == 'COMPLETE':
        return state
    completed = {entry['requested_url'] for entry in state['documents']}
    while state['pending']:
        task = state['pending'][0]
        url = task['url']
        if url in completed:
            state['pending'].pop(0)
            continue
        if len(completed) >= selected['max_documents']:
            save('INCOMPLETE', 'audit_document_limit')
            return state
        save('RUNNING')  # Commit the cursor before budget/network operations.
        try:
            check()
            http.validate(url)
            cached = store.page(run_id, url)
            response = Response(**cached) if cached else http.get(url)
            if not isinstance(response, Response):
                raise FetchError('bad_sitemap_response')
            http.validate(response.url)
            raw = response.text.encode('utf-8')
            if len(raw) > selected['max_response_bytes']:
                save('INCOMPLETE', 'audit_response_limit')
                return state
            if state['total_bytes'] + len(raw) > selected['max_total_bytes']:
                save('INCOMPLETE', 'audit_byte_limit')
                return state
            # Save even malformed XML for diagnosis; never treat it as complete.
            if not cached:
                store.save_page(run_id, url, response)
            check()
            if task['role'] == 'robots':
                kind, entries = 'robots', [{'url': root, 'lastmod': None} for root in _robots_roots(response.text)]
            else:
                kind, entries = parse_sitemap(response.text, selected['max_urls'])
            if state['url_count'] + len(entries) > selected['max_urls']:
                save('INCOMPLETE', 'audit_url_limit')
                return state
            for entry in entries:
                check()
                entry['first_party'] = _first_party(state['origin'], entry['url'])
            if kind in {'sitemapindex', 'robots'}:
                first_party = [entry['url'] for entry in entries if entry['first_party']]
                product_roots = [url for url in first_party if _product_branch(url)]
                # Robots may advertise independent market indexes. Audit all
                # first-party roots; product-branch selection applies within indexes.
                targets = set(first_party if kind == 'robots' else product_roots or first_party)
                scheduled = {pending['url'] for pending in state['pending']}
                new_edges, new_tasks = [], []
                for entry in entries:
                    check()
                    target = entry['url']
                    chosen = target in targets
                    new_edges.append({'parent': url, 'child': target, 'selected': chosen,
                                      'first_party': entry['first_party']})
                    if chosen and target not in completed and target not in scheduled:
                        new_tasks.append({'url': target, 'role': 'sitemap', 'parent': url})
                        scheduled.add(target)
                state['edges'].extend(new_edges)
                state['pending'].extend(new_tasks)
            state['documents'].append({'requested_url': url, 'response_url': response.url,
                                       'kind': kind, 'sha256': _sha(raw), 'bytes': len(raw),
                                       'entry_count': len(entries), 'observed_at': _now(),
                                       'from_cache': bool(cached)})
            completed.add(url)
            state['total_bytes'] += len(raw)
            state['url_count'] += len(entries)
            state['pending'].pop(0)
            save('RUNNING')
        except FetchError as exc:
            if task['parent'] is None and exc.state in {'http_404', 'http_410'}:
                # Missing default sitemap or robots is an explicit absence.
                # Other queued roots remain subject to their normal guards.
                state['failures'].append({'url': url, 'state': exc.state})
                state['pending'].pop(0)
                save('RUNNING')
                continue
            save('INCOMPLETE', exc.state)
            raise
        except Exception as exc:
            # VisitBudget.ScanPaused is intentionally not converted into a
            # catalogue failure. Its caller owns scheduling and status changes.
            save('PAUSED', _reason(getattr(exc, 'reason', None)) or 'audit_interrupted')
            raise
    if not any(doc['kind'] == 'urlset' for doc in state['documents']):
        save('INCOMPLETE', 'sitemap_urlset_not_captured')
    else:
        save('COMPLETE')
    return state


def _document_kind(key, response):
    if key.startswith('rendered:'):
        return None
    requested = key.removeprefix('catalogue-json:')
    path = urlsplit(requested).path
    if path == '/products.json':
        return 'shopify'
    if path.endswith('/wp-json/wc/store/v1/products'):
        return 'woocommerce'
    if path.endswith('/robots.txt'):
        return 'robots'
    text = response.get('text', '')
    if path.lower().endswith('.xml') or re.search(r'<(?:\w+:)?(?:sitemapindex|urlset)\b', text[:2048]):
        return 'sitemap'
    return None


def _snapshot(db, run_id, max_bytes):
    row = db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown scan_run_id')
    run = json.loads(row[0])
    inventory, confirmed, checksum, discovery_errors, inventory_bytes = [], set(), hashlib.sha256(), [], 0
    for identity, raw, saved_hash, item_raw, result_raw in db.execute(
            'SELECT identity,discovery,discovery_hash,item,result FROM products WHERE run_id=? ORDER BY identity', (run_id,)):
        discovery, item = json.loads(raw), json.loads(item_raw)
        result = json.loads(result_raw) if result_raw else {}
        expected_hash = _sha(_encode({key: value for key, value in discovery.items()
                                    if key not in {'source_endpoint', 'fetchedAt', 'discovery_source'}}).encode())
        if expected_hash != saved_hash:
            discovery_errors.append(identity)
        checksum.update(_encode([identity, saved_hash]).encode())
        source_url = discovery.get('url')
        if source_url:
            confirmed.add(canonical_url(source_url))
        inventory.append({'identity': identity, 'discovery_hash': saved_hash,
                          'product_id': discovery.get('product_id') or discovery.get('platform_product_id'),
                          'variant_id': discovery.get('variant_id'), 'sku': discovery.get('sku'),
                          'title': discovery.get('title'), 'original_url': source_url,
                          'current_url': item.get('url'), 'source_endpoint': discovery.get('source_endpoint'),
                          'discovery_source': discovery.get('discovery_source'),
                          'terminal_state': result.get('terminal_state'),
                          'result_product_id': result.get('product_id')})
        inventory_bytes += len(_encode(inventory[-1]).encode()) + 1
        if inventory_bytes > max_bytes:
            raise ValueError('Audit export byte limit exceeded')
    checkpoints = {doc['requested_url']: doc for doc in (run.get('sitemap_audit') or {}).get('documents', [])}
    documents, sources, total = [], {}, 0
    for key, data in db.execute('SELECT url,data FROM pages WHERE run_id=? ORDER BY url', (run_id,)):
        response = json.loads(data)
        if key.startswith('commerce-pair:'):
            pair=response.get('pair')
            if not isinstance(pair,list) or len(pair)!=2 or not response.get('context_id') or not response.get('checked_at'):
                raise ValueError('Invalid commercial pair checkpoint')
            for index, capture in enumerate(pair):
                text=capture.get('text')
                if not isinstance(text,str) or not isinstance(json.loads(text),dict):
                    raise ValueError('Invalid commercial response')
                raw=text.encode('utf-8');total+=len(raw)
                if total>max_bytes:raise ValueError('Audit export byte limit exceeded')
                sha=_sha(raw);filename='responses/'+sha+'.json';sources[filename]=raw
                documents.append({'cache_key':key,'requested_url':capture['url'],'response_url':capture['url'],
                    'kind':'commerce_cart' if index==0 else 'commerce_product','sha256':sha,'bytes':len(raw),
                    'file':filename,'capture_source':'browser_http_response','representation':'decoded_utf8_text',
                    'context_id':response['context_id'],'checked_at':response['checked_at'],
                    'headers':{name.lower():value for name,value in capture.get('headers',{}).items() if name.lower() in SAFE_HEADERS}})
            continue
        kind = _document_kind(key, response)
        if kind is None:
            continue
        text = response.get('text')
        if not isinstance(text, str):
            raise ValueError('Invalid saved response text')
        raw = text.encode('utf-8')
        total += len(raw)
        if total > max_bytes:
            raise ValueError('Audit export byte limit exceeded')
        digest = _sha(raw)
        requested = key.removeprefix('catalogue-json:')
        document = {'cache_key': key, 'requested_url': requested, 'response_url': response.get('url'),
                    'kind': kind, 'sha256': digest, 'bytes': len(raw),
                    'representation': 'decoded_utf8_text',
                    'headers': {name.lower(): value for name, value in response.get('headers', {}).items()
                                if name.lower() in SAFE_HEADERS}}
        if requested in checkpoints:
            checkpoint = checkpoints[requested]
            document['checkpoint_sha256'] = checkpoint.get('sha256')
            document['checkpoint_matches'] = (checkpoint.get('sha256') == digest
                                               and checkpoint.get('response_url') == response.get('url'))
        if key.startswith('catalogue-json:'):
            evidence = response.get('provenance') or {}
            document['capture_source'] = 'browser_http_response'
            document['provenance_valid'] = (response.get('url') == requested
                and evidence == {'capture_source': 'browser_http_response', 'requested_url': requested,
                                 'adapter': kind, 'http_status': 200, 'response_sha256': digest})
        else:
            document['capture_source'] = 'static_http_response'
        try:
            if kind == 'sitemap':
                document['kind'], document['entries'] = parse_sitemap(text)
            elif kind == 'robots':
                document['entries'] = [{'url': url} for url in _robots_roots(text)]
            else:
                products = catalogue_products(Response(response.get('url'), text, response.get('headers', {})), kind)
                document['product_count'] = len(products)
                document['page'] = parse_qs(urlsplit(requested).query).get('page', [None])[0]
        except FetchError as exc:
            document['parse_error'] = exc.state
        suffix = '.xml' if document['kind'] in {'sitemap', 'sitemapindex', 'urlset'} else '.txt' if kind == 'robots' else '.json'
        filename = 'responses/' + digest + suffix
        document['file'] = filename
        sources[filename] = raw
        documents.append(document)
    return run, inventory, confirmed, checksum.hexdigest(), discovery_errors, documents, sources


def _coverage(documents, confirmed):
    sitemaps = {doc['requested_url']: doc for doc in documents
                if doc['kind'] in {'sitemap', 'sitemapindex', 'urlset'}}
    aliases = {doc.get('response_url'): doc['requested_url'] for doc in sitemaps.values()}
    edges, sitemap_urls, missing, linked, external = [], set(), set(), set(), set()
    successors, robot_roots = {}, []
    for doc in documents:
        if doc['kind'] not in {'sitemapindex', 'robots'}:
            continue
        values = [entry['url'] for entry in doc.get('entries', [])]
        first_party = {url for url in values if _first_party(doc['requested_url'], url)}
        selected = [url for url in first_party if _product_branch(url)] or first_party
        if doc['kind'] == 'robots':
            selected = first_party
            robot_roots.extend(selected)
        selected = set(selected)
        successors[doc['requested_url']] = selected
        for target in values:
            resolved = aliases.get(target, target)
            chosen = target in selected
            edges.append({'parent': doc['requested_url'], 'child': target, 'selected': chosen,
                          'captured': resolved in sitemaps, 'first_party': target in first_party})
            linked.add(resolved)
    roots = (set(sitemaps) - linked) | {aliases.get(url, url) for url in robot_roots}
    pending, visited, malformed, urlsets = list(roots), set(), False, 0
    while pending:
        url = pending.pop()
        url = aliases.get(url, url)
        if url in visited:
            continue
        visited.add(url)
        doc = sitemaps.get(url)
        if doc is None:
            missing.add(url)
            continue
        if doc.get('parse_error') or doc.get('checkpoint_matches') is False:
            malformed = True
        elif doc['kind'] == 'urlset':
            urlsets += 1
            for entry in doc.get('entries', []):
                target = sitemap_urls if _first_party(doc['requested_url'], entry['url']) else external
                target.add(canonical_url(entry['url']))
        pending.extend(successors.get(url, []))
    status = 'NOT_CAPTURED' if not sitemaps else 'INCOMPLETE' if missing or malformed or not urlsets else 'COMPLETE'
    return {'status': status, 'scope': 'captured_product_sitemap_branches', 'roots': sorted(roots),
            'graph': edges, 'unfetched_selected_links': sorted(missing),
            'sitemap_url_count': len(sitemap_urls), 'confirmed_url_count': len(confirmed),
            'external_leaf_urls': sorted(external),
            'matched_urls': sorted(sitemap_urls & confirmed),
            'sitemap_only_urls': sorted(sitemap_urls - confirmed),
            'catalogue_only_urls': sorted(confirmed - sitemap_urls),
            'comparison_status': 'NOT_AVAILABLE' if status != 'COMPLETE' else
                                 'MATCH' if sitemap_urls == confirmed else 'DIFFERENCE'}


def _catalogue_capture(documents, inventory, coverage):
    kinds = {row.get('discovery_source') for row in inventory}
    commerce = kinds & {'shopify', 'woocommerce'}
    if not commerce:
        return {'status': coverage['status'], 'basis': 'captured_sitemap_graph'}
    if len(commerce) != 1:
        return {'status': 'INCOMPLETE', 'basis': 'mixed_commerce_adapters'}
    kind = next(iter(commerce))
    pages = {}
    for doc in documents:
        if doc['kind'] != kind:
            continue
        if doc.get('parse_error') or doc.get('provenance_valid') is False:
            return {'status': 'INCOMPLETE', 'basis': 'invalid_catalogue_page'}
        page = doc.get('page')
        if not isinstance(page, str) or not page.isdigit() or int(page) < 1:
            return {'status': 'INCOMPLETE', 'basis': 'invalid_page_identity'}
        number = int(page)
        if number in pages and pages[number]['sha256'] != doc['sha256']:
            return {'status': 'INCOMPLETE', 'basis': 'conflicting_catalogue_pages'}
        pages[number] = doc
    if not pages or set(pages) != set(range(1, max(pages) + 1)):
        return {'status': 'INCOMPLETE', 'basis': 'missing_catalogue_page'}
    final = pages[max(pages)]
    empty_end = final.get('product_count') == 0
    explicit_end = kind == 'woocommerce' and str(final['headers'].get('x-wp-totalpages')) == str(max(pages))
    complete = (empty_end or explicit_end) and all(pages[n].get('product_count', 0) > 0 for n in pages if n != max(pages))
    return {'status': 'COMPLETE' if complete else 'INCOMPLETE',
            'basis': 'empty_terminal_page' if empty_end else 'explicit_page_count' if explicit_end else 'no_exhaustion_evidence',
            'adapter': kind, 'pages': sorted(pages)}


def export_audit(db_path, run_id, output, *, max_export_bytes=268_435_456):
    """Atomically export a consistent saved snapshot. No writes to the DB."""
    if type(max_export_bytes) is not int or max_export_bytes <= 0:
        raise ValueError('max_export_bytes must be a positive integer')
    db_path, output = Path(db_path).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Audit output already exists; choose a new snapshot directory')
    if not db_path.is_file():
        raise ValueError('Authority checkpoint does not exist')
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        run, inventory, confirmed, digest, errors, documents, sources = _snapshot(db, run_id, max_export_bytes)
        candidates = [dict(zip(('ordinal', 'identity', 'reason'), row)) for row in db.execute(
            'SELECT ordinal,identity,reason FROM candidates WHERE run_id=? ORDER BY ordinal', (run_id,))]
        event = db.execute('SELECT MAX(sequence) FROM events WHERE run_id=?', (run_id,)).fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        retries = [dict(zip(('url', 'attempts', 'retry_at', 'state', 'total_429'), row)) for row in db.execute(
            'SELECT url,attempts,retry_at,state,total_429 FROM authority_retries WHERE run_id=? ORDER BY url',
            (run_id,))] if 'authority_retries' in tables else []
    coverage = _coverage(documents, confirmed)
    audit = run.get('sitemap_audit') or {}
    missing_checkpoints = sorted({doc['requested_url'] for doc in audit.get('documents', [])}
                                 - {doc['requested_url'] for doc in documents})
    checkpoint_ok = not missing_checkpoints and all(doc.get('checkpoint_matches', True) for doc in documents)
    coverage.update(checkpoint_matches=checkpoint_ok, missing_checkpoint_documents=missing_checkpoints)
    cross_check_pending = (audit.get('policy', {}).get('mode') == 'cross_check'
                           and audit.get('status') != 'COMPLETE')
    coverage['collection_status'] = audit.get('status', 'NOT_RECORDED')
    if not checkpoint_ok or cross_check_pending:
        coverage.update(status='INCOMPLETE', comparison_status='NOT_AVAILABLE')
    capture = _catalogue_capture(documents, inventory, coverage)
    expected = run.get('catalogue_snapshot_id')
    integrity = not errors and (expected is None or digest == expected)
    browser_ok = all(doc.get('provenance_valid', True) for doc in documents)
    manifest = {'schema_version': 1, 'exported_at': _now(), 'snapshot_event_sequence': event,
                'network_requests': 0, 'run': {key: run.get(key) for key in RUN_FIELDS},
                'catalogue': {'confirmed_products': len(inventory), 'confirmed_urls': len(confirmed),
                              'snapshot_sha256': digest, 'snapshot_matches': integrity,
                              'invalid_discovery_identities': errors,
                              'capture': capture,
                              'discovery_complete': run.get('discovery_complete') is True,
                              'reconciliation': run.get('discovery_reconciliation'),
                              'status': 'COMPLETE' if run.get('discovery_complete') is True
                                  and run.get('discovery_reconciliation') == 'PASS' and integrity and browser_ok
                                  and capture['status'] == 'COMPLETE' else 'INCOMPLETE'},
                'sitemap': coverage, 'documents': documents, 'retry_history': retries,
                'audit_collection': {**{key: audit.get(key) for key in ('version', 'scope', 'origin', 'status',
                                     'started_at', 'updated_at', 'total_bytes', 'url_count')},
                                     'reason': _reason(audit.get('reason')),
                                     'policy': {key: audit.get('policy', {}).get(key) for key in DEFAULT_POLICY},
                                     'pending': [{key: task.get(key) for key in ('url', 'role', 'parent')}
                                                 for task in audit.get('pending', [])],
                                     'failures': [{'url': entry.get('url'), 'state': _reason(entry.get('state'))}
                                                  for entry in audit.get('failures', [])]},
                'candidate_url_limit': 'Rejected candidates have no saved URL; consult captured source documents.',
                'retention': 'Export does not delete or expire source evidence.'}
    sources['products.jsonl'] = ''.join(_encode(item) + '\n' for item in inventory).encode()
    sources['candidates.jsonl'] = ''.join(_encode(item) + '\n' for item in candidates).encode()
    manifest['files'] = {name: {'sha256': _sha(data), 'bytes': len(data)} for name, data in sorted(sources.items())}
    sources['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    sources['SHA256SUMS'] = ''.join(_sha(data) + '  ' + name + '\n' for name, data in sorted(sources.items())).encode()
    if sum(map(len, sources.values())) > max_export_bytes:
        raise ValueError('Audit export byte limit exceeded')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + output.name + '.', dir=output.parent))
    try:
        for name, data in sources.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        staging.rename(output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**manifest, 'export': {'manifest_path': str(output / 'manifest.json'), 'output': str(output),
                                   'manifest_sha256': _sha(sources['manifest.json']),
                                   'file_count': len(sources), 'bytes': sum(map(len, sources.values()))}}


def verify_export(output, expected_run_id=None, expected_fingerprint=None, *, max_export_bytes=268_435_456):
    """Verify an existing export, including the manifest's complete member hashes.

    The returned manifest hash can be pinned in the worker's attestation. Missing
    or changed evidence is an error; verification never repairs or recrawls it.
    """
    if type(max_export_bytes) is not int or max_export_bytes <= 0:
        raise ValueError('max_export_bytes must be a positive integer')
    output = Path(output).absolute()
    if output.is_symlink() or not output.is_dir():
        raise ValueError('Invalid audit output directory')
    total, members, product_count = 0, {}, 0
    def read_member(name):
        nonlocal total
        if (not isinstance(name, str) or not name or '\\' in name
                or any(part in {'', '.', '..'} for part in name.split('/')) or name.startswith('/')):
            raise ValueError('Unsafe audit member path')
        target = output
        for part in name.split('/'):
            target /= part
            if target.is_symlink():
                raise ValueError('Symlink in audit member path')
        try:
            info = target.stat()
        except OSError:
            raise ValueError('Missing audit member') from None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Audit member is not a regular file')
        if total + info.st_size > max_export_bytes:
            raise ValueError('Audit export byte limit exceeded')
        with target.open('rb') as stream:
            data = stream.read(max_export_bytes - total + 1)
        total += len(data)
        if total > max_export_bytes:
            raise ValueError('Audit export byte limit exceeded')
        return data
    try:
        checksums = read_member('SHA256SUMS').decode('utf-8')
        for line in checksums.splitlines():
            match = re.fullmatch(r'([a-f0-9]{64})  (.+)', line)
            if not match:
                raise ValueError('Invalid audit checksum list')
            digest, name = match.groups()
            if name in members or name == 'SHA256SUMS':
                raise ValueError('Duplicate audit checksum member')
            data = read_member(name)
            if _sha(data) != digest:
                raise ValueError('Audit member checksum mismatch')
            members[name] = {'sha256': digest, 'bytes': len(data)}
            if name == 'manifest.json':
                manifest = json.loads(data)
            elif name == 'products.jsonl':
                for line in data.decode('utf-8').splitlines():
                    if not isinstance(json.loads(line), dict):
                        raise ValueError('Invalid audit inventory row')
                    product_count += 1
        if not {'manifest.json', 'products.jsonl', 'candidates.jsonl'} <= members.keys():
            raise ValueError('Missing required audit members')
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
            raise ValueError('Invalid audit manifest')
        bound = {name: info for name, info in members.items() if name != 'manifest.json'}
        if manifest.get('files') != bound:
            raise ValueError('Audit manifest member mismatch')
        run = manifest.get('run') or {}
        if expected_run_id is not None and run.get('scan_run_id') != expected_run_id:
            raise ValueError('Audit run identity mismatch')
        if expected_fingerprint is not None and run.get('scanner_fingerprint') != expected_fingerprint:
            raise ValueError('Audit scanner fingerprint mismatch')
        for document in manifest.get('documents', []):
            if members.get(document.get('file')) != {'sha256': document.get('sha256'), 'bytes': document.get('bytes')}:
                raise ValueError('Audit document hash mismatch')
        if manifest['catalogue']['confirmed_products'] != product_count:
            raise ValueError('Audit inventory count mismatch')
        return {'scan_run_id': run.get('scan_run_id'), 'scanner_fingerprint': run.get('scanner_fingerprint'),
                'manifest_path': str(output / 'manifest.json'),
                'manifest_sha256': members['manifest.json']['sha256'],
                'product_count': product_count,
                'catalogue_status': manifest['catalogue']['status'], 'sitemap_status': manifest['sitemap']['status'],
                'file_count': len(members) + 1, 'bytes': total}
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError('Invalid audit evidence structure') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-export-bytes', type=int, default=268_435_456)
    args = parser.parse_args(argv)
    result = export_audit(args.db, args.run_id, args.output, max_export_bytes=args.max_export_bytes)
    print(json.dumps({'scan_run_id': args.run_id, 'catalogue_status': result['catalogue']['status'],
                      'sitemap_status': result['sitemap']['status'], 'output': str(args.output)}, sort_keys=True))


if __name__ == '__main__':
    main()
