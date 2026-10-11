"""Project identity-validated Shopify Ajax evidence without guessing commerce data.

The transport must capture product and cart in the same anonymous market context.
Amounts retain the original API integer and the explicit /100 Ajax convention.
Product-level images are not asserted to be variant-specific images.
"""
from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import urlsplit, urljoin, urlencode


class CommerceEvidenceError(ValueError):
    pass


def capture_available_commerce(item, fetch_sequence, request_executor, store, run_id, required=True):
    """Missing commerce never changes a valid composition calculation."""
    from catalogue_discovery import FetchError
    try:
        return capture_product_commerce(item, fetch_sequence, request_executor, store, run_id), None
    except FetchError as exc:
        if required or exc.state == 'domain_backoff':
            raise
        return None, exc.state


def _sha(response):
    return hashlib.sha256(response.text.encode()).hexdigest()


def _image(value, product_url):
    if isinstance(value, dict):
        value = value.get('src') or value.get('url')
    if not isinstance(value, str) or not value:
        return None
    result = urljoin(product_url, value)
    parsed = urlsplit(result)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise CommerceEvidenceError('image_source_invalid')
    return result


def _money(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CommerceEvidenceError('ajax_price_invalid')
    return format(Decimal(value) / 100, '.2f')


def shopify_ajax_capture(item, product_response, cart_response, checked_at, context_id):
    """The caller validates product identity before this commercial projection."""
    url = item['url']; parsed = urlsplit(url)
    if not checked_at or not context_id:
        raise CommerceEvidenceError('market_context_missing')
    root = parsed.path.split('/products/', 1)
    if len(root) != 2 or parsed.query or parsed.fragment:
        raise CommerceEvidenceError('product_endpoint_invalid')
    endpoint = url.rstrip('/') + '.js'
    cart_endpoint = parsed.scheme + '://' + parsed.netloc + root[0] + '/cart.js'
    if product_response.url != endpoint or cart_response.url != cart_endpoint:
        raise CommerceEvidenceError('commercial_response_identity_mismatch')
    obj, cart = product_response.json(), cart_response.json()
    pid = str(item.get('product_id') or item.get('platform_product_id') or '')
    if (not pid or str(obj.get('id')) != pid or obj.get('handle') != root[1].rstrip('/')
            or urljoin(url, obj.get('url', '')) != url.rstrip('/')):
        raise CommerceEvidenceError('commercial_product_identity_mismatch')
    code = cart.get('currency')
    if not isinstance(code, str) or not re.fullmatch('[A-Z]{3}', code):
        raise CommerceEvidenceError('presentment_currency_unresolved')
    source = {'endpoint':endpoint,'response_sha256':_sha(product_response),
              'currency_endpoint':cart_endpoint,'currency_response_sha256':_sha(cart_response),
              'checked_at':checked_at,'context_id':context_id,'price_divisor':100,
              'market':'anonymous storefront presentment','locale_path':root[0] or '/'}
    variants = obj.get('variants')
    if not isinstance(variants, list) or not variants:
        raise CommerceEvidenceError('variant_census_missing')
    rows, seen = [], set()
    for v in variants:
        if not isinstance(v, dict):
            raise CommerceEvidenceError('variant_schema_invalid')
        vid = str(v.get('id') or '')
        if not re.fullmatch('[1-9][0-9]*',vid) or vid in seen:
            raise CommerceEvidenceError('variant_identity_ambiguous')
        if v.get('product_id') is not None and str(v['product_id']) != pid:
            raise CommerceEvidenceError('variant_product_mismatch')
        seen.add(vid)
        sku = v.get('sku')
        if sku is not None and not isinstance(sku,str):
            raise CommerceEvidenceError('sku_schema_invalid')
        opts = v.get('options')
        if opts is None:
            opts = [v[k] for k in ('option1','option2','option3') if v.get(k) is not None]
        if not isinstance(opts,list) or any(not isinstance(o,str) for o in opts):
            raise CommerceEvidenceError('variant_options_invalid')
        image = _image(v.get('featured_image'),url)
        price = _money(v.get('price'))
        rows.append({'product_id':pid,'variant_id':vid,'sku':sku or '',
                     'sku_status':'disclosed' if sku else 'not_disclosed','options':opts,
                     'title':v.get('title') or '', 'price':price,'currency':code,
                     'compare_at_price':_money(v.get('compare_at_price')),
                     'raw_price':v.get('price'),'price_status':'available' if price is not None else 'not_disclosed',
                     'availability':'in_stock' if v.get('available') is True else
                                    'out_of_stock' if v.get('available') is False else 'not_disclosed',
                     'source_url':url+'?'+urlencode({'variant':vid}),
                     'image_source_url':image,'image_scope':'variant' if image else None,
                     'observation':source})
    images = []
    for raw in obj.get('images') or []:
        image = _image(raw,url)
        if image and image not in [v['source_url'] for v in images]:
            images.append({'source_url':image,'scope':'product','product_id':pid,
                           'alt':raw.get('alt') if isinstance(raw,dict) else None,'observation':source})
    # The documented Ajax ceiling is 250. Hitting it is not exhaustion proof.
    return {'variants':rows,'variant_count':len(rows),'variant_census_complete':len(rows)<250,
            'variant_census_status':'complete' if len(rows)<250 else 'adapter_limit_unverified',
            'product_images':images,'commerce_observation':source}


def capture_product_commerce(item, fetch_sequence, request_executor, store, run_id):
    """Checkpoint the whole validated pair; never replay half a currency context."""
    import uuid
    from catalogue_discovery import Response, FetchError
    from scanner_store import now, encode
    obj = item.get('commerce_json') or item.get('product_lookup_json') or {}
    if not isinstance(obj,dict) or not obj.get('variants'):
        raise FetchError('commerce_adapter_required')
    url=item['url']; parsed=urlsplit(url); root=parsed.path.split('/products/',1)
    if len(root)!=2 or parsed.query or parsed.fragment:
        raise FetchError('commerce_adapter_required')
    key='commerce-pair:'+url
    saved=store.page(run_id,key)
    try:
        if saved:
            pair=saved['pair']; checked_at=saved['checked_at'];context=saved['context_id']
            cart_response,product_response=[Response(**r) for r in pair]
        else:
            if fetch_sequence is None:
                raise FetchError('commerce_transport_required')
            cart_url=parsed.scheme+'://'+parsed.netloc+root[0]+'/cart.js'
            cart_response,product_response=fetch_sequence([cart_url,url.rstrip('/')+'.js'],request_executor)
            checked_at=now();context=str(uuid.uuid4())
        result=shopify_ajax_capture(item,product_response,cart_response,checked_at,context)
        # Discovery and Ajax must enumerate the same variants before publication.
        expected={str(v['id']) for v in obj['variants'] if isinstance(v,dict) and v.get('id')}
        actual={v['variant_id'] for v in result['variants']}
        if expected!=actual:
            raise CommerceEvidenceError('variant_census_changed_during_capture')
        if not result['variant_census_complete']:
            raise CommerceEvidenceError('variant_adapter_limit_unverified')
        if not saved:
            data={'pair':[{'url':r.url,'text':r.text,'headers':r.headers} for r in (cart_response,product_response)],
                  'checked_at':checked_at,'context_id':context}
            with store.db:
                store.db.execute('INSERT OR IGNORE INTO pages VALUES(?,?,?)',(run_id,key,encode(data)))
        return result
    except CommerceEvidenceError as exc:
        raise FetchError(str(exc)) from None
