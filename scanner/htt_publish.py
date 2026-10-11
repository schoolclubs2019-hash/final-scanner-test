"""Identity-bound client for the existing HTT POST /scans contract.

HTT owns materialization, cache invalidation and IdealWrld sync. This client
never posts to IdealWrld, creates a publisher protocol, deletes or clears data.
"""
import os
import json
import urllib.request
from urllib.parse import urlsplit, quote

from htt_adapters import base_url, publication_plan, publication_style
from publication_parity import ContractError, StagingTransport, parity_check, SURFACES, FIELDS, SKU_FIELDS
from throne_api import scanner_fingerprint
from throne_read import READER_VERSION
from textile_policy import SCANNER_VERSION
from throne_standard import VERSION
from scanner_store import now

DEFAULT_API = 'https://holdthethrone.com/wp-json/htt/v1'
DEFAULT_READER = 'https://htt-reader-3647d4a8.eastus.cloudapp.azure.com'


def settings(config=None):
    config = config or {}
    # The key is never copied into configuration, the database or diagnostics.
    if not os.environ.get('HTT_API_KEY'):
        raise ContractError('PUBLISH CONFIGURATION MISSING')
    api = base_url(os.environ.get('HTT_API_URL') or config.get('bases', {}).get('htt_api') or DEFAULT_API)
    reader = base_url(os.environ.get('HTT_READER_URL') or config.get('bases', {}).get('reader') or DEFAULT_READER)
    if any(urlsplit(url).scheme != 'https' for url in (api, reader)):
        raise ContractError('Production API and reader require HTTPS')
    return api, reader


def production_config(config, api, reader):
    config = dict(config)
    config.update(environment='read-only', target_environment='production', htt_api=api)
    config['allowed_hosts'] = sorted(set(config.get('allowed_hosts', [])) | {urlsplit(api).hostname, urlsplit(reader).hostname})
    # The deployed HTT APIs expose the full canonical result inside an envelope.
    # Check visible consumer fields where available as well as the stored result;
    # a stale/withheld public score must not pass just because canonical.score does.
    brand_fields = {field:'canonical.' + field for field in FIELDS}
    brand_fields.update({'brand_id':'reportId', 'brand':'name', 'htt_id':'htt_id', 'score':'score',
                    'score_status':'scoreStatus', 'publication_status':'publicationStatus',
                    'sku_count':'skusRead', 'coverage':'coverageRate', 'disclosure_rate':'disclosureRate',
                    'scanner_version':'scannerVersion',
                    'reader_version':'readerVersion', 'standard_version':'throneStandardVersion'})
    cache_fields = {field:'result.canonical.' + field for field in FIELDS}
    cache_fields.update({'brand_id':'result.reportId', 'brand':'result.brandName',
                        'htt_id':'result.htt_id', 'score':'result.brand.score',
                        'score_status':'result.scoreStatus', 'publication_status':'result.brand.status',
                        'scanner_version':'result.scannerVersion', 'reader_version':'result.readerVersion',
                        'standard_version':'result.standardVersion'})
    sku_fields = {field:'canonical.' + field for field in SKU_FIELDS}
    sku_fields.update({'score':'score', 'sku':'sku', 'product_id':'productId',
                       'terminal_state':'terminalState'})
    surfaces = dict(config.get('surfaces', {}))
    def apply_default(name, spec):
        previous = surfaces.get(name)
        if not previous:
            surfaces[name] = spec
            return
        # Existing Azure configurations already contain these routes. Upgrade
        # only their recognized old default fields, rather than leaving a stale
        # explicit configuration immune to the corrected adapter. Custom routes
        # and explicit records_path projections remain under their own contract.
        if previous.get('url') != spec['url'] or previous.get('records_path'):
            return
        fields = dict(previous.get('fields', {}))
        legacy = {'catalogue_total':'catalogueTotal', 'scan_date':'checkedAt'}
        for field, destination in spec.get('fields', {}).items():
            old = fields.get(field)
            recognized = {field, 'canonical.' + field, 'result.canonical.' + field,
                          legacy.get(field), brand_fields.get(field), cache_fields.get(field),
                          sku_fields.get(field)}
            if old is None or old in recognized:
                fields[field] = destination
        surfaces[name] = {**previous, 'fields':fields}
    for name in ('htt_api','chrome_brand','shopify_brand','wix_brand'):
        apply_default(name, {'url':api+'/brands/{slug}', 'fields':brand_fields})
    for name in ('chrome_cache','shopify_cache'):
        apply_default(name, {'url':api+'/scan-cache?site={domain}', 'fields':cache_fields})
    for name in ('shopify_sku','wix_sku'):
        apply_default(name, {'url':api+'/brands/{slug}/skus/{sku}', 'fields':sku_fields})
    stored = surfaces.get('htt_stored', {})
    if stored.get('url') == api + '/brands/{slug}/canonical' and not stored.get('token_env'):
        surfaces['htt_stored'] = {**stored, 'token_env':'HTT_API_KEY'}
    client_configuration = {name:dict(spec) for name, spec in config.get('client_configuration', {}).items()}
    for name in ('shopify', 'wix'):
        if client_configuration.get(name):
            client_configuration[name].setdefault('expected_api_url', api)
            client_configuration[name].setdefault('expected_reader_url', reader)
    config['client_configuration'] = client_configuration
    config['surfaces'] = surfaces
    return config


class HTTReadback(StagingTransport):
    def request(self, url, body=None, token_env=None, **kwargs):
        if body is not None:
            raise ContractError('Readback cannot write')
        if token_env and (token_env != 'HTT_API_KEY' or urlsplit(url).hostname != urlsplit(self.config['htt_api']).hostname):
            raise ContractError('Only HTT authentication is permitted on the Mac')
        value = super().request(url, token_env=token_env, **kwargs)
        def identity_types(obj):
            if isinstance(obj, dict):
                return {k: str(v) if k in {'id','reportId','report_id','brand_id','product_id','htt_source_id'}
                        and isinstance(v, int) and not isinstance(v, bool) else identity_types(v) for k,v in obj.items()}
            if isinstance(obj, list): return [identity_types(v) for v in obj]
            return obj
        return identity_types(value)


class HTTIntake:
    def __init__(self, api, readback):
        self.api, self.readback = api, readback

    def stage_commerce(self, store, run_id):
        import hashlib
        from scanner_store import encode
        run = store.run(run_id)
        required = run.get('require_commerce_capture') is True
        if not required and run.get('capture_commerce') is not True:
            return
        for product in store.iter_products(run_id):
            style = publication_style(product)
            if style.get('terminal_state') in {'excluded_accessory','excluded_nontextile'}:
                continue
            commercial = style.get('commercial')
            if not isinstance(commercial, dict) or commercial.get('variant_census_complete') is not True:
                if not required:
                    continue
                raise ContractError('COMMERCIAL CAPTURE INCOMPLETE')
            digest = hashlib.sha256(encode(commercial).encode()).hexdigest()
            body = {'brand_id':run['brand_id'],'htt_id':run['htt_id'],'scan_run_id':run_id,
                    'product_id':str(style.get('product_id') or ''),'source_url':style['url'],
                    'commercial':commercial,'commercial_sha256':digest}
            self.stage_product_commerce(run['slug'], body, digest)

    def stage_product_commerce(self, slug, body, digest):
        previous = -1
        for _ in range(501):
            receipt = self._post('/brands/'+quote(slug,safe='')+'/commerce-stage',body)
            if receipt.get('product_id') != body['product_id'] or receipt.get('source_digest') != digest:
                raise ContractError('COMMERCIAL STAGING RECEIPT MISMATCH')
            if receipt.get('status') == 'staged':
                return receipt
            count = receipt.get('captured_images')
            if receipt.get('status') != 'staging' or type(count) is not int or count <= previous:
                raise ContractError('COMMERCIAL STAGING DID NOT PROGRESS')
            previous = count
        raise ContractError('COMMERCIAL STAGING REQUEST BOUND REACHED; saved evidence retained')

    def _post(self, path, payload):
        key = os.environ.get('HTT_API_KEY')
        if not key:
            raise ContractError('PUBLISH CONFIGURATION MISSING')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        request = urllib.request.Request(self.api+path,data=json.dumps(payload,allow_nan=False).encode(),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=150) as response:
                return json.loads(response.read(8_000_001))
        except Exception:
            raise ContractError('COMMERCIAL STAGING FAILED; saved evidence retained') from None

    def submit(self, payload, run_id):
        key = os.environ.get('HTT_API_KEY')
        if not key:
            raise ContractError('PUBLISH CONFIGURATION MISSING')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        request = urllib.request.Request(self.api+'/scans', data=json.dumps(payload, allow_nan=False).encode(),
            headers={'Authorization':'Bearer '+key, 'Content-Type':'application/json',
                     'Accept':'application/json', 'Idempotency-Key':run_id}, method='POST')
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=180) as response:
                data = response.read(8_000_001)
                if len(data) > 8_000_000: raise ValueError('response too large')
                value = json.loads(data)
                if not isinstance(value, dict): raise ValueError('intake response must be an object')
                return value
        except (OSError, ValueError):
            # A lost response is ambiguous; never retry the POST blindly.
            raise ContractError('HTT SUBMISSION OUTCOME UNKNOWN; verify readback before retry') from None


def reader_preflight(transport, api, reader):
    expected = {'reader_version':READER_VERSION, 'scanner_version':SCANNER_VERSION,
                'throne_standard_version':VERSION, 'reader':scanner_fingerprint()}
    observed = {}
    for name, url in (('deployed',reader+'/v1/health'), ('htt',api+'/health')):
        try:
            response = transport.request(url)
            observed[name] = {field:response.get(field) for field in expected}
        except (ContractError, AttributeError):
            observed[name] = {'unavailable':True}
    return {'status':'PASS' if all(value == expected for value in observed.values()) else 'FAIL',
            'local':expected, **observed}


def require_readback_configuration(config):
    missing = [name for name in SURFACES if not config.get('surfaces', {}).get(name, {}).get('url')]
    missing += [name+'_configuration' for name in ('shopify', 'wix')
                if not config.get('client_configuration', {}).get(name)]
    if missing:
        raise ContractError('READBACK CONFIGURATION MISSING: ' + ', '.join(missing))
    invalid = []
    website = config['surfaces']['htt_website']
    if website.get('format') != 'html' or not website.get('script_id'):
        invalid.append('htt_website requires HTML and its canonical script_id')
    from pathlib import Path
    for name in ('shopify', 'wix'):
        spec = config['client_configuration'][name]
        if (not (spec.get('url') or spec.get('control_plane_file')) or not spec.get('expected_api_url')
                or (spec.get('reader_required', True) and not spec.get('expected_reader_url'))):
            invalid.append(name + ' configuration route and expected service URLs')
        if spec.get('control_plane_file'):
            path = Path(spec['control_plane_file']).expanduser()
            if not path.is_absolute() or not path.is_file():
                invalid.append(name + ' control-plane attestation file unavailable')
    if invalid:
        raise ContractError('READBACK CONFIGURATION INVALID: ' + ', '.join(invalid))


def require_listing_identity(record, actual):
    if (not isinstance(actual, dict) or not record.get('slug')
            or str(actual.get('reportId')) != str(record['brand_id'])
            or actual.get('slug') != record['slug']
            or str(actual.get('site') or '').rstrip('/') != record['domain'].rstrip('/')
            or str(actual.get('htt_id')) != str(record['htt_id'])):
        raise ContractError('HTT LISTING IDENTITY MISMATCH')


def require_active_standard(actual):
    # HTT health proxies Lambda. The WordPress option is a separate publication
    # gate, reported on the listing even while its previous score is stale.
    if actual.get('currentThroneStandardVersion') != VERSION:
        raise ContractError('HTT STANDARD SYNCHRONIZATION REQUIRED')


def production_preflight(record, config):
    """Validate cheap provider prerequisites before spending a catalogue crawl."""
    api, reader = settings(config)
    live_config = production_config(config, api, reader)
    require_readback_configuration(live_config)
    transport = HTTReadback(live_config)
    health = reader_preflight(transport, api, reader)
    if health['status'] != 'PASS':
        raise ContractError('READER DEPLOYMENT REQUIRED')
    actual = transport.request(api + '/brands/' + quote(str(record.get('slug') or ''), safe=''))
    require_listing_identity(record, actual)
    require_active_standard(actual)
    return {'status':'PASS', 'brand_id':str(record['brand_id']),
            'htt_id':str(record['htt_id']), 'reader_parity':health}


def assert_publishable(run):
    if (run.get('mode') != 'AUTHORITY_CENSUS' or run.get('unaccounted') != 0 or run.get('integrity_status') != 'PASS'
            or run.get('discovery_reconciliation') != 'PASS' or not run.get('discovery_complete')
            or not run.get('publish_eligible') or run.get('publication_blockers')
            or run.get('technical_failures') != 0 or run.get('scanner_status') != 'SCANNER_COMPLETE'):
        raise ContractError('PUBLICATION PREFLIGHT FAILED')
    canonical = run.get('canonical') or {}
    report_total = run.get('report_processed')
    if report_total is None:
        report_total = (run.get('report') or {}).get('report_read_count')
    excluded_total = int(run.get('excluded') or 0)
    if (type(report_total) is not int or report_total < 1
            or canonical.get('score') is None
            or any(canonical.get(key) != report_total
                   for key in ('catalogue_total', 'processed_total', 'sku_count'))
            or run.get('catalogue_confirmed') != run.get('processed')
            or report_total + excluded_total != run.get('processed')):
        raise ContractError('CANONICAL RECONCILIATION FAILED')

    if any(canonical.get(key) != run.get(key) for key in ('brand_id', 'htt_id', 'slug', 'domain', 'scan_run_id', 'scanner_fingerprint')):
        raise ContractError('CANONICAL IDENTITY MISMATCH')
    from scanner_pipeline import pipeline_fingerprint
    if run.get('scanner_fingerprint') != pipeline_fingerprint():
        raise ContractError('SAVED AUTHORITY FINGERPRINT MISMATCH')
    expected = {'reader_version':READER_VERSION, 'scanner_version':SCANNER_VERSION, 'standard_version':VERSION}
    if (any(run.get(k) != value or canonical.get(k) != value for k,value in expected.items())
            or (run.get('report') or {}).get('scanner_fingerprint') != scanner_fingerprint()):
        raise ContractError('SAVED CANONICAL READER MISMATCH; rescan required')


def selected_products(store, run_id, maximum=3):
    """Verify product census rows by their immutable product IDs, never fake SKUs."""
    rows, seen = [], set()
    for product in store.iter_products(run_id):
        row = publication_style(product)
        discovery = product.get('discovery') or {}
        identity = str(discovery.get('product_id') or '')
        if (not identity or str(row.get('product_id') or '') != identity
                or row.get('terminal_state') != 'scored'):
            continue
        if identity in seen:
            raise ContractError('PRODUCT READBACK IDENTITY AMBIGUOUS')
        seen.add(identity)
        if len(rows) < maximum:
            rows.append(row)
    return rows


def selected_skus(store, run_id, maximum=3):
    # Use the identical identity projection sent to HTT, and verify scored rows.
    rows = []
    for product in store.iter_products(run_id):
        row = publication_style(product)
        if row.get('sku') and row.get('terminal_state') == 'scored' and len(rows) < maximum:
            rows.append(row)
            if len(rows) == maximum:
                break
    return rows



def readback_sample(store, run_id, config):
    product_routes = config.get('product_census_readback') is True and all(
        '{product_id}' in config.get('surfaces', {}).get(name, {}).get('url', '')
        for name in ('shopify_sku', 'wix_sku'))
    return selected_products(store, run_id) if product_routes else selected_skus(store, run_id)


def publish(store, run_id, config, api, reader, transport, intake):
    # All entry points share one lock, including standalone saved-run delivery.
    from authority_batch import publication_lock
    from pathlib import Path
    lock_path = config.get('publication_lock') or Path(store.path).parent / 'publish.lock'
    with publication_lock(lock_path):
        return _publish(store, run_id, config, api, reader, transport, intake)


def _publish(store, run_id, config, api, reader, transport, intake):
    run = store.run(run_id)
    assert_publishable(run)
    selected = [record for record, _ in store.queue() if str(record['brand_id']) == run['brand_id']]
    if len(selected) != 1 or any(str(selected[0].get(key)) != str(run.get(key))
                                for key in ('brand_id', 'htt_id', 'slug', 'domain', 'cpt')):
        raise ContractError('IMMUTABLE QUEUE IDENTITY MISMATCH')
    store.update(run_id, fully_complete=False, completion_status='INCOMPLETE', completed_at=None, delivery_mode='production')
    health = reader_preflight(transport, api, reader)
    store.update(run_id, reader_parity=health)
    if health['status'] != 'PASS':
        raise ContractError('READER DEPLOYMENT REQUIRED')
    require_readback_configuration(config)
    plan = publication_plan(run, store.iter_products(run_id), {'htt_api':api})
    if len(plan['intake_payload']['styles']) != run.get('report_processed'):
        raise ContractError('PERSISTED REPORT RECONCILIATION FAILED')
    catalogue = plan['intake_payload'].get('catalogue') or {}
    if (catalogue.get('identified_total') != run.get('catalogue_confirmed')
            or catalogue.get('excluded_total') != run.get('excluded')):
        raise ContractError('PERSISTED CATALOGUE RECONCILIATION FAILED')
    blockers = [b for b in plan['blockers'] if b not in {'LIVE_WRITES_DISABLED','ATOMIC_INTAKE_CONTRACT_NOT_VERIFIED'}]
    if blockers:
        raise ContractError('PUBLICATION PREFLIGHT FAILED: '+', '.join(blockers))
    # Authority attestations accompany evidence; the existing HTT intake revalidates it.
    payload = plan['intake_payload']
    payload['scannerFingerprint'] = scanner_fingerprint()
    payload['authorityFingerprint'] = run['scanner_fingerprint']
    canonical = run['canonical']
    sample = readback_sample(store, run_id, config)
    if not sample:
        raise ContractError('SKU READBACK IDENTITY MISSING')
    required_htt = ('htt_stored','htt_website','htt_api','chrome_cache','shopify_sku')
    try:
        if run.get('htt_publish_status') != 'PASS':
            if run.get('intake_state') in {'SENDING','UNKNOWN','ACKNOWLEDGED'}:
                verification = parity_check(canonical, config, transport, sample, required_htt)
                if verification['status'] != 'PASS':
                    raise ContractError('HTT SUBMISSION OUTCOME UNVERIFIED; no duplicate POST sent')
                store.update(run_id, htt_publish_status='PASS', intake_state='ACKNOWLEDGED')
            else:
                before = transport.request(api+'/brands/'+quote(run['slug'],safe=''))
                require_listing_identity(run, before)
                require_active_standard(before)
                store.update(run_id, previous_public_result=before)
                if run.get('require_commerce_capture') is True:
                    intake.stage_commerce(store, run_id)
                # Staging cannot activate the report. Only an attempted intake POST
                # creates an uncertain publication outcome that forbids a repeat POST.
                store.update(run_id, intake_state='SENDING')
                receipt = intake.submit(payload, run_id)
                report_id = receipt.get('reportId', receipt.get('report_id'))
                if receipt.get('status') != 'published' or str(report_id) != str(run['brand_id']):
                    store.update(run_id, intake_state='UNKNOWN')
                    raise ContractError('HTT DID NOT CONFIRM THE EXPECTED PUBLISHED LISTING')
                store.update(run_id, intake_state='ACKNOWLEDGED', htt_publish_status='PASS',
                             htt_receipt={'status':'published','reportId':report_id}, idealwrld_sync_status='IW_SYNC_PENDING')
        htt = parity_check(canonical, config, transport, sample, required_htt)
        store.update(run_id, htt_readback_status=htt['status'], htt_parity=htt)
        if htt['status'] != 'PASS':
            raise ContractError('HTT READBACK PARITY FAIL')
        store.update(run_id, publication_status='HTT_PUBLISHED')
        # HTT owns the sync trigger and secret. This client only GETs downstream data.
        result = parity_check(canonical, config, transport, sample)
        iw = result['surfaces']['idealwrld']['status']
        store.update(run_id, parity=result, parity_status=result['status'],
                     idealwrld_readback_status=iw, idealwrld_sync_status='IW_SYNC_VERIFIED' if iw == 'PASS' else 'IW_SYNC_PENDING',
                     app_catalogue_status=result['surfaces']['app_catalogue']['status'],
                     app_report_status=result['surfaces']['app_report']['status'])
        if result['status'] != 'PASS':
            raise ContractError('DOWNSTREAM PARITY FAIL')
        store.update(run_id, status='COMPLETE', fully_complete=True, completion_status='FULLY_COMPLETE',
                     ecosystem_status='ECOSYSTEM_PROPAGATED', completed_at=now(), error=None)
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        latest = store.run(run_id)
        store.update(run_id, status='SYNC_PENDING' if latest.get('htt_publish_status') == 'PASS' else 'RETRY',
                     intake_state='UNKNOWN' if latest.get('intake_state') == 'SENDING' else latest.get('intake_state'),
                     fully_complete=False, completion_status='INCOMPLETE', completed_at=None,
                     parity_status='FAIL', error=str(exc) if isinstance(exc, ContractError) else type(exc).__name__)
    return store.run(run_id)
