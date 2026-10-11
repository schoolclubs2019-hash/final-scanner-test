"""AWS Lambda adapter for the authoritative Throne composition reader.

The scoring implementation remains in throne_api.py/throne_read.py.  This file
only validates the public HTTP boundary and translates Lambda Function URL
events into the reader's existing response shape.
"""
import base64
import hmac
import json
import os
import re
from urllib.parse import urlparse

from throne_api import brand_figure, brand_groups, read_one, scanner_fingerprint
import textile_policy as TP
from throne_read import READER_VERSION
from department_readers import detect_department, read_department
from confirmed_product import product_objects
from text_normalizer import normalize_row


MAX_ITEMS = 300
MAX_AGGREGATE_ITEMS = 5000
MAX_BODY_BYTES = 5_500_000
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
FIELD_LIMITS = {
    "department": 100,
    "cpt": 100,
    "gtin": 20,
    "fetch_status": 80,
    "default_currency": 12,
    "regular_price": 100,
    "compare_at_price": 100,
    "sale_price": 100,
    "sku": 300,
    "title": 1000,
    "handle": 1000,
    "body_html": 200_000,
    "description": 200_000,
    "material": 10_000,
    "composition": 10_000,
    "ingredients": 30_000,
    "inci": 30_000,
    "page_text": 200_000,
    "tags": 10_000,
    "url": 4000,
    "collection": 300,
    "category": 300,
    "product_type": 300,
    "market": 50,
    "locale": 50,
    "source_endpoint": 4000,
    "product_id": 100,
    "platform_product_id": 100,
    "variant_id": 100,
    "platform_variant_id": 100,
    "product_code": 300,
    "product_code_standard": 300,
    "schema_type": 100,
    "page_type": 100,
    "content_type": 100,
    "template_type": 100,
    "resource_type": 100,
    "product_json": 200_000,
    "commerce_json": 200_000,
    "availability": 100,
    "stock_status": 100,
    "price": 100,
    "price_min": 100,
    "price_max": 100,
    "currency": 12,
}


def response(status, payload):
    try:
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        status = 500
        encoded = json.dumps({"error": "The reader could not serialize its result."})
    if len(encoded.encode()) > MAX_BODY_BYTES:
        status = 413
        encoded = json.dumps({"error": "Result exceeds the response limit. Use smaller read batches or include_rows=false for catalogue aggregation."})
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "access-control-allow-origin": "*",
            "access-control-allow-headers": "authorization,content-type,x-throne-install-id",
            "access-control-allow-methods": "GET,POST,OPTIONS",
        },
        "body": encoded,
    }


def headers(event):
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def server_authorised(event):
    incoming = headers(event)
    key = os.environ.get("THRONE_API_KEY", "")
    bearer = incoming.get("authorization", "")
    return bool(key and hmac.compare_digest(bearer, f"Bearer {key}"))


def authorised(event):
    incoming = headers(event)
    return server_authorised(event) or bool(CLIENT_ID.fullmatch(incoming.get("x-throne-install-id", "")))


def body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw)
    elif isinstance(raw, str):
        raw = raw.encode()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("Request is too large.")
    payload = json.loads(raw or b"{}")
    if not isinstance(payload, dict):
        raise ValueError("The request body must be an object.")
    return payload


def valid_url(value):
    if not value:
        return True
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def clean_items(value, max_items=MAX_ITEMS):
    if not isinstance(value, list) or not value:
        raise ValueError("items must be a non-empty list.")
    if len(value) > max_items:
        raise ValueError(f"A reader batch may contain at most {max_items} products.")
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Every item must be an object.")
        row = {}
        for field, limit in FIELD_LIMITS.items():
            value = item.get(field)
            if field in ("product_json", "commerce_json") and isinstance(value, (dict, list)):
                text = json.dumps(value, separators=(",", ":"))
            else:
                text = str(value or "")
            if len(text.encode()) > limit:
                raise ValueError(f"{field} is too long.")
            row[field] = text
        if not valid_url(row["url"]):
            raise ValueError("Every source URL must be HTTP or HTTPS.")
        cleaned.append(row)
    return cleaned



# === HTT CANONICAL TEXTILE LAMBDA PATCH 2026.09 ===
_htt_original_clean_items = clean_items

def clean_items(value, max_items=MAX_ITEMS):
    cleaned=_htt_original_clean_items(value,max_items)
    for out,raw in zip(cleaned,value):
        for product in product_objects(out):
            for target, source in (("title", "name"), ("description", "description"),
                                   ("material", "material"), ("ingredients", "ingredients"),
                                   ("sku", "sku"), ("gtin", "gtin")):
                candidate = product.get(source)
                if not out.get(target) and isinstance(candidate, (str, int, float)):
                    if len(str(candidate).encode()) > FIELD_LIMITS[target]:
                        raise ValueError(f"Structured {target} is too long.")
                    out[target] = str(candidate)
            properties = product.get("additionalProperty") or []
            if isinstance(properties, dict):
                properties = [properties]
            if isinstance(properties, list):
                for prop in properties:
                    if isinstance(prop, dict) and str(prop.get("name") or "").lower() in {"composition", "material", "fabric composition"}:
                        candidate = prop.get("value")
                        if not out.get("material") and isinstance(candidate, str):
                            if len(candidate.encode()) > FIELD_LIMITS["material"]:
                                raise ValueError("Structured material is too long.")
                            out["material"] = candidate
        original_composition = out.get('composition', '')
        out.update(normalize_row(out))
        out['raw_original'] = normalize_row(raw)['raw_original']
        out['evidence_sources'] = raw.get('evidence_sources', [])
        if 'component_weights' in raw:
            weights = raw['component_weights']
            if not isinstance(weights, dict) or len(weights) > 32 or len(json.dumps(weights).encode()) > 4096:
                raise ValueError('component_weights must be a bounded object of published percentages.')
            out['component_weights'] = weights

        if original_composition != out.get('composition', '') and raw.get('composition_raw') in (None, ''):
            out['composition_raw'] = original_composition
        from price_capture import capture_price
        out.update(capture_price(raw))
        out['variant_prices'] = []
        out['fetchedAt']=str(raw.get('fetchedAt') or '')[:80]
        out['reader_how']=str(raw.get('reader_how') or '')[:80]
        out['reader_band']=str(raw.get('reader_band') or '')[:40]
        out['filtered']=bool(raw.get('filtered'))
        out['exclusion_reason']=str(raw.get('exclusion_reason') or '')[:80]
    return cleaned
# === END HTT CANONICAL TEXTILE LAMBDA PATCH ===

def handler(event, _context):
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "GET").upper()
    path = (event.get("rawPath") or request.get("path") or "/").rstrip("/") or "/"

    if method == "OPTIONS":
        return response(204, {})
    if method == "GET" and path == "/v1/standard":
        from throne_standard import STANDARD
        return response(200, STANDARD)
    if method == "GET" and path == "/v1/health":
        return response(200, {
            "ok": True,
            "scanner": "throne_read",
            "reader": scanner_fingerprint(),
            "throne_standard_version": TP.POLICY_VERSION,
            "scanner_version": TP.SCANNER_VERSION,
            "reader_version": READER_VERSION,
            "service": "aws-lambda",
        })
    if method != "POST" or path not in ("/v1/read", "/v1/aggregate"):
        return response(404, {"error": "Not found"})
    if not authorised(event):
        return response(401, {"error": "Bad or missing reader identity"})

    try:
        trusted = server_authorised(event)
        is_aggregate = path == "/v1/aggregate"
        if is_aggregate and not trusted:
            return response(403, {"error": "Brand aggregation requires server-verified evidence."})
        limit = MAX_AGGREGATE_ITEMS if is_aggregate else MAX_ITEMS
        payload = body(event)
        mode = payload.get('mode', 'AUTHORITY_CENSUS' if trusted else 'CONSUMER_LOOKUP')
        if mode not in {'AUTHORITY_CENSUS', 'CONSUMER_LOOKUP'}:
            return response(400, {'error': 'Unknown scanner mode'})
        if mode == 'AUTHORITY_CENSUS' and not trusted:
            return response(403, {'error': 'Authority census requires server authentication'})
        if not trusted and len(payload.get('items') or []) > 1:
            return response(403, {'error': 'Consumer checks are limited to one product; request a canonical HTT lookup or queue an authority refresh'})
        discovered_items = clean_items(payload.get("items"), limit)
        from scan_contract import scan
        result = scan(discovered_items, payload, trusted)
        if is_aggregate and payload.get("include_rows") is False:
            result["read_count"] = len(result["read"])
            result["read"] = []
            result["report_notes_count"] = len(result["report_notes"])
            result["report_notes"] = [note for note in result["report_notes"] if "sku" not in note]
        return response(200, result)
    except (ValueError, json.JSONDecodeError) as error:
        return response(400, {"error": str(error)})
    except Exception:
        return response(500, {"error": "The reader could not complete this batch."})


def effective_filtered(item, department):
    if not item.get("filtered"):
        return False
    if item.get("exclusion_reason") == "not-apparel" and department != "clothing":
        return False
    return True
