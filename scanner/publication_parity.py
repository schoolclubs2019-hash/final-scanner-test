"""Strict publication protocol and fresh all-consumer readbacks. No score arithmetic.

The staging publisher must implement this versioned contract. The legacy /scans
endpoint is not assumed to support staging, CAS promotion or cache invalidation.
"""
import json
import os
from string import Formatter
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit
from html.parser import HTMLParser
from pathlib import Path
from throne_standard import STANDARD


FIELDS = ('brand_id', 'htt_id', 'brand', 'score', 'score_status', 'publication_status',
          'catalogue_total', 'processed_total', 'eligible_total', 'sku_count', 'product_count', 'census_unit', 'scored_count', 'undisclosed_count',
          'technical_failure_count', 'coverage', 'disclosure_rate', 'scan_date',
          'scanner_version', 'reader_version', 'standard_version', 'scan_run_id',
          'composition_plant_share_pct', 'composition_coverage_pct', 'composition_assessed_count',
          'composition_missing_count', 'composition_score_scope',
          'catalogue_plant_share_lower_bound', 'catalogue_plant_share_upper_bound')
SURFACES = ('htt_stored', 'htt_website', 'htt_api', 'chrome_cache', 'chrome_brand',
            'shopify_brand', 'shopify_cache', 'shopify_sku', 'wix_brand', 'wix_sku',
            'idealwrld', 'app_catalogue', 'app_report')
SKU_FIELDS = ('brand_id', 'htt_id', 'scan_run_id', 'product_id', 'sku', 'score', 'terminal_state',
              'scanner_version', 'reader_version', 'standard_version')
HTT_SURFACES = ('htt_stored', 'htt_website', 'htt_api')


class ContractError(ValueError):
    pass


def at(value, path):
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            raise KeyError(path)
        value = value[key]
    return value


def compare(expected, actual, mapping, fields):
    errors = []
    for field in fields:
        try:
            value = at(actual, mapping.get(field, field))
        except (KeyError, TypeError):
            errors.append({'field': field, 'reason': 'MISSING'})
            continue
        # bool must not equal 0/1; null is an intentional value, never a missing key.
        if isinstance(value, bool) != isinstance(expected[field], bool) or value != expected[field]:
            errors.append({'field': field, 'reason': 'MISMATCH', 'expected': expected[field], 'actual': value})
    return errors


class StagingTransport:
    def __init__(self, config):
        if config.get('environment') not in {'staging', 'read-only'}:
            raise ContractError('Use staging simulation or read-only configuration in this repository')
        self.config = config
        self.hosts = set(config.get('allowed_hosts', []))
        if not self.hosts:
            raise ContractError('Staging allowed_hosts required')

    def request(self, url, body=None, token_env=None, idempotency_key=None, response_format="json"):
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        local_simulation = (self.config.get('environment') == 'staging' and self.config.get('allow_local_http') is True
                            and host in {'127.0.0.1', 'localhost', '::1'})
        if body is not None and not local_simulation:
            raise ContractError('Live writes disabled: no authorized staging or production target supplied')
        if host not in self.hosts:
            raise ContractError('Endpoint outside staging allowlist')
        if parsed.username or parsed.password or parsed.fragment:
            raise ContractError('Credentials/fragments prohibited in endpoint URLs')
        if parsed.scheme != 'https' and not (self.config.get('allow_local_http') is True and host in {'127.0.0.1', 'localhost', '::1'}):
            raise ContractError('HTTPS required')
        headers = {'Accept': 'application/json', 'Cache-Control': 'no-cache, no-store', 'Pragma': 'no-cache'}
        if token_env:
            token = os.environ.get(token_env)
            if not token:
                raise ContractError('Configured token environment variable unavailable')
            headers['Authorization'] = 'Bearer ' + token
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        # Never forward an authorization header or a publication write on redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ContractError('Readback too large')
                return raw.decode("utf-8") if response_format == "html" else json.loads(raw)
        except (OSError, ValueError) as exc:
            # Exception URLs may contain secrets; only persist a classification.
            raise ContractError(type(exc).__name__) from None



def scanner_metadata(canonical):
    """Map scanner projection to existing Standard-owned WP keys only."""
    aliases = {'scored_count': 'scoreable_skus', 'technical_failure_count': 'technical_failures',
               'coverage': 'coverage_rate', 'scan_date': 'scanned_at', 'standard_version': 'throne_standard_version'}
    allowed = set(STANDARD['field_ownership']['scanner_owned']['fields'])
    return {aliases.get(k, k): value for k, value in canonical.items() if aliases.get(k, k) in allowed}


def listing_result(html, script_id):
    class Listing(HTMLParser):
        def __init__(self):
            super().__init__(); self.active = False; self.parts = []; self.matches = 0
        def handle_starttag(self, tag, attrs):
            if tag == 'script':
                self.active = dict(attrs).get('id') == script_id
                if self.active: self.matches += 1
        def handle_endtag(self, tag):
            if tag == 'script': self.active = False
        def handle_data(self, text):
            if self.active: self.parts.append(text)
    parser = Listing(); parser.feed(html); parser.close()
    if parser.matches != 1:
        raise ContractError('Listing canonical result absent or ambiguous')
    return json.loads(''.join(parser.parts))


def endpoint(spec, canonical, row=None):
    context = {**canonical, **(row or {})}
    for _, key, _, _ in Formatter().parse(spec['url']):
        if key is not None and context.get(key) in (None, ''):
            raise ContractError('Endpoint identity field unavailable: ' + key)
    return spec['url'].format_map({key: quote(str(value), safe='') for key, value in context.items()})


def readback_record(actual, spec, canonical, row=None):
    """Project a configured envelope/collection using stable listing identity."""
    if spec.get('records_path'):
        actual = at(actual, spec['records_path'])
    if not isinstance(actual, list):
        return actual
    htt_id = canonical.get('htt_id')
    if htt_id in (None, ''):
        raise ContractError('Collection readback requires stable HTT identity')
    fields = spec.get('fields', {})
    identity_path = spec.get('identity_htt_id_field') or fields.get('htt_id', 'htt_id')
    matches = []
    for record in actual:
        try:
            if str(at(record, identity_path)) != str(htt_id):
                continue
            if row is not None and any(
                    row.get(field) in (None, '') or str(at(record, fields.get(field, field))) != str(row[field])
                    for field in ('sku', 'product_id')):
                continue
            matches.append(record)
        except (KeyError, TypeError):
            continue
    if len(matches) != 1:
        raise ContractError('Collection readback identity absent or ambiguous')
    record = matches[0]
    try:
        slug = at(record, spec.get('identity_slug_field', 'slug'))
    except (KeyError, TypeError):
        slug = None
    if slug not in (None, '') and canonical.get('slug') not in (None, '', slug):
        raise ContractError('Collection readback slug identity mismatch')
    return record


def resolve_idealwrld_context(canonical, config, transport):
    """Resolve the IdealWrld listing ID from the app catalogue using stable identity.

    HTT and IdealWrld WordPress post IDs are not interchangeable. When a downstream
    endpoint requires idealwrld_id, resolve it from the configured app catalogue
    even when a saved ID exists. Require stable htt_id and reject a conflicting slug.
    """
    spec = config.get('surfaces', {}).get('app_catalogue') or {}
    url = spec.get('url')
    if not url or '{idealwrld_id}' in url:
        raise ContractError('IdealWrld identity resolver unavailable')
    actual = transport.request(
        endpoint(spec, canonical),
        token_env=spec.get('token_env'),
        response_format=spec.get('format', 'json'),
    )
    actual = readback_record(actual, spec, canonical)
    if not isinstance(actual, dict):
        raise ContractError('IdealWrld catalogue identity invalid')

    identity_path = spec.get('identity_field', 'id')
    try:
        idealwrld_id = at(actual, identity_path)
    except (KeyError, TypeError):
        raise ContractError('IdealWrld listing ID unavailable') from None
    if idealwrld_id in (None, ''):
        raise ContractError('IdealWrld listing ID unavailable')

    htt_path = spec.get('identity_htt_id_field') or spec.get('fields', {}).get('htt_id', 'htt_id')
    slug_path = spec.get('identity_slug_field', 'slug')
    try:
        actual_htt_id = at(actual, htt_path)
    except (KeyError, TypeError):
        actual_htt_id = None
    if actual_htt_id not in (None, ''):
        if str(actual_htt_id) != str(canonical.get('htt_id', '')):
            raise ContractError('IdealWrld htt_id identity mismatch')
    try:
        actual_slug = at(actual, slug_path)
    except (KeyError, TypeError):
        actual_slug = None
    if actual_slug not in (None, ''):
        if str(actual_slug) != str(canonical.get('slug', '')):
            raise ContractError('IdealWrld slug identity mismatch')
    if actual_htt_id in (None, ''):
        raise ContractError('IdealWrld catalogue did not prove stable identity')

    resolved = dict(canonical)
    resolved['idealwrld_id'] = idealwrld_id
    return resolved

def read_client_configuration(spec, canonical, transport):
    """Read deployed client configuration.

    Normal clients use an HTTPS readback endpoint. Wix-managed app backends are
    not anonymously curlable outside a Wix app instance, so production Wix can
    instead use a control-plane attestation produced directly by `wix env pull`
    after the released version is created. Secrets are never copied into it.
    """
    control_plane_file = spec.get('control_plane_file')
    if control_plane_file:
        path = Path(control_plane_file).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ContractError('Control-plane attestation unavailable')
        try:
            actual = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ContractError('Control-plane attestation invalid') from None
        if not isinstance(actual, dict):
            raise ContractError('Control-plane attestation invalid')
        source = spec.get('attestation_source')
        if source and actual.get('source') != source:
            raise ContractError('Control-plane attestation source mismatch')
        release = spec.get('expected_release_version')
        if release and actual.get('release_version') != release:
            raise ContractError('Control-plane release version mismatch')
        return actual
    if not spec.get('url'):
        raise ContractError('Client configuration readback unavailable')
    return transport.request(endpoint(spec, canonical), token_env=spec.get('token_env'))


def parity_check(canonical, config, transport, rows=(), surfaces=SURFACES):
    results = {}
    endpoint_context = dict(canonical)
    downstream_resolved = False
    for name in surfaces:
        spec = config.get('surfaces', {}).get(name)
        if not spec or not spec.get('url'):
            results[name] = {'status': 'NOT CONFIGURED'}
            continue
        try:
            if '{idealwrld_id}' in spec['url'] and not downstream_resolved:
                endpoint_context = resolve_idealwrld_context(canonical, config, transport)
                downstream_resolved = True
            errors = []
            sku_surface = name.endswith('_sku')
            targets = rows if sku_surface else [None]
            # Prefer one complete SKU-indexed collection response. Never issue
            # unbounded per-SKU requests for a large catalogue.
            indexed = None
            if sku_surface and spec.get('collection_url'):
                collection_spec = {**spec, 'url': spec['collection_url']}
                batch = transport.request(endpoint(collection_spec, endpoint_context),
                                          token_env=spec.get('token_env'),
                                          response_format=spec.get('format', 'json'))
                documents = (batch.get('products', batch.get('items', batch.get('rows')))
                             if isinstance(batch, dict) else batch)
                if not isinstance(documents, list):
                    raise ContractError('SKU collection readback must be an array')
                indexed = {}
                for doc in documents:
                    if not isinstance(doc, dict) or not isinstance(doc.get('sku'), str) or not doc['sku']:
                        raise ContractError('SKU collection identity missing')
                    if doc['sku'] in indexed:
                        raise ContractError('Duplicate SKU in collection readback')
                    indexed[doc['sku']] = doc
            elif sku_surface and len(rows) > 100:
                raise ContractError('SKU collection readback required for large catalogue')
            count = 0
            for row in targets:
                count += 1
                if row is not None and '{sku}' in spec['url'] and not row.get('sku'):
                    raise ContractError('Real SKU identity required by consumer endpoint')
                expected = canonical if row is None else {**canonical, **{k: row.get(k) for k in ('product_id', 'sku', 'score', 'terminal_state')}}
                if indexed is not None:
                    if row.get('sku') not in indexed:
                        raise ContractError('Missing SKU in collection readback')
                    actual = indexed[row['sku']]
                else:
                    actual = transport.request(endpoint(spec, endpoint_context, row),
                                               token_env=spec.get('token_env'),
                                               response_format=spec.get('format', 'json'))
                if name == 'htt_website':
                    if spec.get('format') != 'html' or not spec.get('script_id'):
                        raise ContractError('Actual listing HTML readback required')
                    actual = listing_result(actual, spec['script_id'])
                actual = readback_record(actual, spec, canonical, row)
                required = tuple(spec.get('required_fields') or (SKU_FIELDS if sku_surface else FIELDS))
                unknown = [field for field in required if field not in expected]
                if unknown:
                    raise ContractError('Unknown parity field: ' + ', '.join(unknown))
                errors.extend(compare(expected, actual, spec.get('fields', {}), required))
            if not count:
                errors.append({'reason': 'NO_PRODUCTS_VERIFIED'})
            results[name] = {'status': 'MISMATCH' if errors else 'PASS', 'errors': errors, 'checked': count}
        except (ContractError, KeyError, ValueError, TypeError):
            results[name] = {'status': 'FAIL', 'reason': 'readback_unavailable_or_invalid'}
    if surfaces == SURFACES:
        for name in ('shopify', 'wix'):
            spec = config.get('client_configuration', {}).get(name)
            try:
                if not spec:
                    raise ContractError('NOT CONFIGURED')
                actual = read_client_configuration(spec, canonical, transport)
                # Read deployed configuration or a Wix control-plane attestation;
                # never infer production configuration from source defaults.
                expected_environment = config.get('target_environment', 'production' if config['environment'] == 'read-only' else 'staging')
                good = actual.get('environment') == expected_environment and actual.get('fixture_mode') is False
                good = good and bool(actual.get('HTT_API_URL')) and actual['HTT_API_URL'] == spec['expected_api_url']
                if spec.get('reader_required', True):
                    good = good and bool(actual.get('HTT_READER_URL')) and actual['HTT_READER_URL'] == spec['expected_reader_url']
                results[name + '_configuration'] = {'status': 'PASS' if good else 'FIXTURE OR MISCONFIGURED'}
            except (ContractError, KeyError, ValueError, TypeError):
                results[name + '_configuration'] = {'status': 'NOT CONFIGURED'}
    return {'status': 'PASS' if results and all(r['status'] == 'PASS' for r in results.values()) else 'FAIL',
            'environment': config.get('environment'), 'surfaces': results}


def propagate(store, run_id, config, transport, rows):
    run = store.run(run_id)
    canonical = run['canonical']
    if run['integrity_status'] != 'PASS' or not run['publish_eligible']:
        raise ContractError('Canonical publication gate failed')
    from scanner_store import digest
    protocol = config.get('publication', {})
    if protocol.get('contract') != 'htt-generations-v1':
        raise ContractError('Atomic staging publication contract NOT CONFIGURED')
    contract_hash = digest(config)
    saved_hash = run.get('propagation_contract_hash')
    if saved_hash is not None and saved_hash != contract_hash:
        raise ContractError('Publication contract changed during propagation')
    if saved_hash is None:
        store.update(run_id, propagation_contract_hash=contract_hash)

    def write(action, body):
        spec = protocol.get(action)
        if not spec:
            raise ContractError(action + ' NOT CONFIGURED')
        return transport.request(endpoint(spec, canonical), body, spec.get('token_env'), run_id + ':' + action)

    try:
        if run.get('htt_publish_status') != 'PASS':
            # Idempotent begin and chunk uploads; retry after a lost response is safe.
            receipt = write('stage', {'canonical': canonical, 'expected_generation': run.get('previous_generation'),
                                      'report': run['report'], 'contract': 'htt-generations-v1',
                                      'scanner_owned_metadata': scanner_metadata(canonical),
                                      'sync_contract': STANDARD['sync_contract'],
                                      'identity': {'htt_id': canonical['htt_id'], 'htt_source_id': canonical['brand_id'], 'htt_source_post_type': run['cpt']} })
            if receipt.get('scan_run_id') != run_id or receipt.get('status') != 'staged':
                raise ContractError('HTT stage rejected')
            from row_stream import RowStream
            batches = rows.batches(300) if isinstance(rows, RowStream) else (rows[i:i+300] for i in range(0,len(rows),300))
            for batch_number, batch in enumerate(batches):
                index = batch_number * 300
                spec = protocol['products']
                result = transport.request(endpoint(spec, canonical),
                    {'scan_run_id': run_id, 'offset': index, 'rows': batch}, spec.get('token_env'), run_id + ':products:' + str(index))
                if result.get('accepted') != len(batch):
                    raise ContractError('HTT product upload rejected')
            receipt = write('commit', {'scan_run_id': run_id, 'expected_generation': run.get('previous_generation'),
                                       'invalidate_caches': True, 'enqueue_standard_sync': True})
            if (receipt.get('scan_run_id') != run_id or receipt.get('status') != 'published'
                    or receipt.get('atomic') is not True or receipt.get('cache_invalidated') is not True
                    or receipt.get('sync_enqueued') is not True):
                raise ContractError('HTT atomic commit acknowledgement invalid')
            store.update(run_id, htt_publish_status='PASS', status='HTT_PUBLISHED', idealwrld_sync_status='IW_SYNC_PENDING')
        htt = parity_check(canonical, config, transport, rows, HTT_SURFACES)
        store.update(run_id, htt_readback_status=htt['status'], htt_parity=htt)
        if htt['status'] != 'PASS':
            raise ContractError('PUBLICATION PARITY FAILURE')
        store.update(run_id, status='HTT_VERIFIED', publication_status='HTT_PUBLISHED')
        if store.run(run_id).get('idealwrld_sync_status') != 'IW_SYNC_VERIFIED':
            spec = config.get('sync')
            if not spec:
                raise ContractError('IdealWrld sync NOT CONFIGURED')
            receipt = transport.request(endpoint(spec, canonical), {'htt_ids': [canonical['htt_id']], 'dry_run': False},
                                        spec.get('token_env'), run_id + ':sync')
            if receipt.get('ok') is not True or receipt.get('status') != 'success':
                raise ContractError('IdealWrld sync rejected')
            iw = parity_check(canonical, config, transport, rows, ('idealwrld',))
            store.update(run_id, idealwrld_readback_status=iw['status'])
            if iw['status'] != 'PASS':
                raise ContractError('IdealWrld verification failure')
            store.update(run_id, idealwrld_sync_status='IW_SYNC_VERIFIED', status='IW_SYNCED')
        result = parity_check(canonical, config, transport, rows)
        store.update(run_id, parity=result, parity_status=result['status'],
                     app_catalogue_status=result['surfaces']['app_catalogue']['status'],
                     app_report_status=result['surfaces']['app_report']['status'])
        if result['status'] != 'PASS':
            raise ContractError('ALL SURFACES PARITY FAIL')
        from scanner_store import now
        store.update(run_id, status='COMPLETE', completed_at=now(), error=None,
                     ecosystem_status='ECOSYSTEM_PROPAGATED', fully_complete=True, completion_status='FULLY_COMPLETE')
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        current = store.run(run_id)
        store.update(run_id, status='SYNC_PENDING' if current.get('htt_publish_status') == 'PASS' else 'RETRY',
                     idealwrld_sync_status=('IW_SYNC_RETRY' if current.get('idealwrld_sync_status') != 'IW_SYNC_VERIFIED' else 'IW_SYNC_VERIFIED') if current.get('htt_publish_status') == 'PASS' else 'NOT ATTEMPTED',
                     parity_status='FAIL', completed_at=None, fully_complete=False, ecosystem_status='INCOMPLETE', error=str(exc) if isinstance(exc, ContractError) else type(exc).__name__)
    return store.run(run_id)
