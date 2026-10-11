"""CONSUMER_LOOKUP: canonical HTT data only; never imports a crawler or reader."""
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

CONSUMER_LOOKUP = 'CONSUMER_LOOKUP'
AUTHORITY_CENSUS = 'AUTHORITY_CENSUS'


def current_result(result, htt_id, max_age_days=30, now=None):
    if not isinstance(result, dict) or result.get('htt_id') != htt_id or result.get('publication_status') != 'verified':
        return False
    if result.get('score') is None or not result.get('scan_run_id') or not result.get('standard_version'):
        return False
    try:
        age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(result['scan_date'].replace('Z','+00:00'))
        return timedelta(0) <= age <= timedelta(days=max_age_days)
    except (KeyError, ValueError, TypeError):
        return False


def lookup(htt_id, slug, transport, htt_api, enqueue, sku=None, max_age_days=30):
    """At most two GETs. Queue misses centrally; never accept client evidence/scores.

    enqueue is an authenticated host backend callback, not a browser-supplied URL.
    The API adapter must normalize its response to the canonical projection.
    """
    base = htt_api.rstrip('/') + '/brands/' + quote(slug, safe='')
    try:
        result = transport.request(base)
        if current_result(result, htt_id, max_age_days):
            if sku is not None:
                product = transport.request(base + '/skus/' + quote(sku, safe=''))
                if (isinstance(product, dict) and product.get('htt_id') == htt_id and product.get('sku') == sku
                        and product.get('scan_run_id') == result['scan_run_id']):
                    return {'mode': CONSUMER_LOOKUP, 'status': 'CURRENT', 'result': product, 'authority_scan_started': False}
            else:
                return {'mode': CONSUMER_LOOKUP, 'status': 'CURRENT', 'result': result, 'authority_scan_started': False}
    except (OSError, ValueError, RuntimeError):
        pass
    enqueue(htt_id, 'canonical_result_missing_stale_or_mismatched')
    return {'mode': CONSUMER_LOOKUP, 'status': 'UPDATE_REQUIRED', 'refresh_status': 'QUEUED',
            'result': None, 'authority_scan_started': False}
