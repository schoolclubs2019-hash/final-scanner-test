"""Endpoint adapters from the owner-supplied production architecture.

All endpoints are configurable. This module constructs contracts, never writes.
External source/configuration locations are evidence pointers, not inspected code.
"""
from urllib.parse import urlsplit
import re
from aws_reader_lambda import MAX_AGGREGATE_ITEMS, MAX_BODY_BYTES
from publication_parity import scanner_metadata
from scanner_store import encode, discovery_hash
from throne_standard import STANDARD


def base_url(value):
    parsed = urlsplit(value or '')
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Base URL must be an absolute credential-free HTTP(S) URL')
    return value.rstrip('/')


def consumer_routes(bases):
    """Explicit templates, without assuming unknown response field names."""
    routes = {}
    if bases.get('htt_api'):
        htt = base_url(bases['htt_api'])
        routes.update(htt_api=htt + '/brands/{slug}', chrome_brand=htt + '/brands/{slug}',
                      chrome_cache=htt + '/scan-cache?site={domain}')
    # Actual app catalogue/report URLs are configured independently of these defaults.
    if bases.get('app_catalogue'):
        routes['app_catalogue'] = base_url(bases['app_catalogue']) + '/brands/{slug}'
    if bases.get('app_report'):
        routes['app_report'] = base_url(bases['app_report']) + '/report?id={idealwrld_id}'
    if bases.get('idealwrld_api'):
        routes['idealwrld'] = base_url(bases['idealwrld_api']) + '/report?id={idealwrld_id}'
    for client in ('shopify', 'wix'):
        if bases.get(client + '_api'):
            base = base_url(bases[client + '_api'])
            routes[client + '_brand'] = base + '/brands/{slug}'
            routes[client + '_sku'] = base + '/brands/{slug}/skus/{sku}'
            if client == 'shopify': routes[client + '_cache'] = base + '/scan-cache?site={domain}'
    return routes


def _style_with_commerce(style):
    commercial = style.get('commercial')
    if not isinstance(commercial, dict):
        return style
    variants = commercial.get('variants') or []
    selected = [v for v in variants if str(v.get('variant_id') or '') == str(style.get('variant_id') or '')]
    # A product minimum price is not the price of the legacy row's selected SKU.
    exact = len(selected) == 1 and selected[0].get('sku', '') == style.get('sku', '')
    style['price'] = selected[0].get('price') if exact else None
    style['currency'] = selected[0].get('currency', '') if exact else ''
    style['price_scope'] = 'variant' if exact else 'see_exact_variants'
    style['composition_scope'] = 'product'
    style['variant_composition_scope'] = 'not_established'
    return style


def publication_style(product):
    """Attach a real, unambiguous commerce SKU without changing a census row.

    The processed item may replace source_endpoint with the product-page URL
    after HTML/render capture. Commerce identity therefore comes from the
    immutable discovery snapshot when available; that snapshot is exactly what
    proved the product belonged to the enumerated Shopify catalogue.
    """
    item = product['item']
    discovery = product.get('discovery') or item
    style = {**item, **(product.get('result') or {})}
    if style.get('sku'):
        return _style_with_commerce(style)
    identity_source = 'discovery.commerce_json.variants[0].sku'
    if discovery.get('discovery_source') == 'shopify':
        obj = discovery.get('commerce_json')
        if not isinstance(obj, dict) or not obj.get('id'):
            return _style_with_commerce(style)
        product_id = str(obj['id'])
        if str(discovery.get('product_id')) != product_id or str(style.get('product_id')) != product_id:
            return _style_with_commerce(style)
        source = urlsplit(discovery.get('source_endpoint', ''))
        target = urlsplit(discovery.get('url') or item.get('url', ''))
        host = lambda value: (value.hostname or '').removeprefix('www.')
        if not host(source) or host(source) != host(target) or source.path.rstrip('/') != '/products.json':
            return _style_with_commerce(style)
        if not obj.get('handle') or not target.path.rstrip('/').endswith('/products/' + str(obj['handle'])):
            return _style_with_commerce(style)
    elif discovery.get('discovery_source') == 'product_sitemap':
        from catalogue_discovery import FetchError, Response, product_json_identity, product_json_probe_url
        from scan_contract import canonical_url
        obj = discovery.get('product_lookup_json')
        endpoint = discovery.get('product_lookup_source_endpoint')
        response_hash = discovery.get('product_lookup_response_sha256')
        if (not isinstance(obj, dict) or not isinstance(endpoint, str) or not endpoint
                or not isinstance(response_hash, str)
                or not re.fullmatch(r'[0-9a-f]{64}', response_hash)
                or product.get('discovery_hash') != discovery_hash(discovery)
                or endpoint != product_json_probe_url(discovery)
                or any(item.get(key) != discovery.get(key) for key in (
                    'product_lookup_json', 'product_lookup_source_endpoint', 'product_lookup_response_sha256'))
                or canonical_url(style.get('url')) != canonical_url(discovery.get('url'))):
            return _style_with_commerce(style)
        try:
            # The immutable snapshot digest binds the parsed JSON and original
            # response hash. Re-encoding here only revalidates identity/schema;
            # it is never substituted for the original HTTP response hash.
            verified = product_json_identity(discovery, Response(endpoint, encode(obj), {}), endpoint)
        except (FetchError, TypeError, ValueError):
            return _style_with_commerce(style)
        product_id = verified['platform_product_id']
        if str(style.get('product_id')) != product_id:
            return _style_with_commerce(style)
        identity_source = 'discovery.product_lookup_json.variants[0].sku'
    else:
        return _style_with_commerce(style)
    variants = obj.get('variants')
    if not isinstance(variants, list) or len(variants) != 1 or not isinstance(variants[0], dict):
        return _style_with_commerce(style)
    variant = variants[0]
    sku = variant.get('sku')
    if (not variant.get('id') or not isinstance(sku, str) or not sku.strip()
            or (variant.get('product_id') is not None and str(variant['product_id']) != product_id)
            or (style.get('variant_id') and str(style['variant_id']) != str(variant['id']))):
        return _style_with_commerce(style)
    style['sku'] = sku.strip()
    style['sku_identity_source'] = {
        'source': identity_source, 'product_id': product_id,
        'variant_id': str(variant['id']), 'census_unit': 'product',
    }
    if identity_source == 'discovery.product_lookup_json.variants[0].sku':
        style['sku_identity_source'].update(source_endpoint=endpoint, response_sha256=response_hash,
                                            discovery_hash=product['discovery_hash'])
    return _style_with_commerce(style)

def publication_plan(run, products, bases):
    canonical = run.get('canonical')
    if not canonical:
        raise ValueError('Run has no canonical calculation')
    styles = []
    excluded = {'excluded_accessory': 0, 'excluded_nontextile': 0}
    for product in products:
        # Preserve every catalogue row internally, but only publish textile-report
        # rows. Out-of-scope products are identified in the durable result and
        # reconciliation counts, never emitted as report styles.
        style = publication_style(product)
        state = style.get('terminal_state')
        if style.get('report_included') is False or state in excluded:
            if state in excluded:
                excluded[state] += 1
            continue
        styles.append(style)
    reports = run['report'].get('department_reports', [])
    identified_total = int(run.get('catalogue_confirmed') or run.get('processed') or len(styles))
    excluded_total = excluded['excluded_accessory'] + excluded['excluded_nontextile']
    payload = {
        'source': 'durable-catalogue-worker', 'brandName': run['brand'], 'brandSlug': run.get('slug', ''),
        'category': run.get('department', 'auto'), 'department': run.get('department', 'auto'),
        'report_cpt': run['cpt'], 'site': run['domain'], 'scannedAt': canonical['scan_date'],
        'styles': styles, 'canonicalResult': canonical,
        'catalogue': {'complete': run.get('discovery_complete') is True, 'confirmed_total': len(styles),
                      'identified_total': identified_total, 'excluded_total': excluded_total,
                      'excluded_accessory': excluded['excluded_accessory'],
                      'excluded_nontextile': excluded['excluded_nontextile']},
        'discoveryCandidatesCount': run.get('catalogue_discovered'), 'discoveryRejectedReasons': run.get('discovery_rejected', {}),
        'scannerFingerprint': run['scanner_fingerprint'], 'scanRunId': run['scan_run_id'],
        'htt_id': run['htt_id'], 'htt_source_id': run['brand_id'], 'htt_source_post_type': run['cpt'],
        'commercialCaptureRequired':run.get('require_commerce_capture') is True,
        'scannerOwnedMetadata': scanner_metadata(canonical),
        'reportDestination': reports[0]['report_destination'] if len(reports) == 1 else None,
        'publicationCopy': reports[0].get('publication_copy') if len(reports) == 1 else None,
    }
    blockers = list(run.get('publication_blockers', []))
    skus = [style['sku'] for style in styles if style.get('sku')]
    if len(skus) != len(set(skus)):
        blockers.append('SKU_IDENTITY_CONFLICT')
    if len(styles) > MAX_AGGREGATE_ITEMS or len(encode(payload).encode()) > MAX_BODY_BYTES:
        blockers.append('LEGACY_INTAKE_TRANSPORT_LIMIT_REQUIRES_GENERATION_ADAPTER')
    blockers.extend(['LIVE_WRITES_DISABLED', 'ATOMIC_INTAKE_CONTRACT_NOT_VERIFIED'])
    return {'mode': 'DRY_RUN', 'request_allowed': False, 'scan_run_id': run['scan_run_id'],
            'intake_url': base_url(bases['htt_api']) + '/scans' if bases.get('htt_api') else None,
            'intake_payload': payload, 'blockers': blockers, 'consumer_routes': consumer_routes(bases),
            'idealwrld_push_receiver': base_url(bases['idealwrld_api']) + '/standard-sync' if bases.get('idealwrld_api') else None,
            'push_payload_schema_status': 'NOT TESTED: external push/receiver source not present',
            'sync_contract': STANDARD['sync_contract'], 'parity_status': 'NOT TESTED'}
