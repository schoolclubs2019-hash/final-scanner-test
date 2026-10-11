#!/usr/bin/env python3
"""Universal Throne scan API and shared textile implementation.

Production requests enter through ``aws_reader_lambda.py``. The local HTTP
server delegates its complete public contract to that same handler, so local
clients and integration tests cannot drift into a second scanner.

Endpoints
    GET  /v1/health
    POST /v1/read
    POST /v1/aggregate

The reader derives every material result on the server. Successfully read
products without usable composition are `UNDISCLOSED`; technical failures are
separate and never inherit a client-submitted score or status.
"""
import argparse
import json
import os
import re
import sys
from functools import lru_cache
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import throne_read as R  # noqa: E402
import textile_policy as TP  # noqa: E402

HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def scanner_fingerprint():
    """First 16 hex of the complete canonical scanner package's SHA-256.

    Every production route, department reader, shared Standard rule and
    extraction module contributes to this value. A health check therefore
    detects drift anywhere in the single deployed scanner, not just in the
    textile reader.
    """
    import hashlib
    digest = hashlib.sha256()
    for name in (
        "aws_reader_lambda.py",
        "confirmed_product.py",
        "product_evidence.py",
        "evidence_ai_rules.py",
        "evidence_ai_gate.py",
        "price_capture.py",
        "brand_capture.py",
        "scan_contract.py",
        "row_stream.py",
        "publication_copy.py",
        "text_normalizer.py",
        "department_capabilities.py",
        "department_readers.py",
        "inci_read.py",
        "ingredient_extract.py",
        "ingredient_read.py",
        "textile_policy.py",
        "throne_api.py",
        "throne_read.py",
        "throne_standard.py",
    ):
        digest.update(name.encode("utf-8"))
        digest.update((HERE / name).read_bytes())
    digest.update(TP.STANDARD_VERSION.encode())
    from throne_standard import STANDARD_PATH
    digest.update(STANDARD_PATH.read_bytes())
    for path in sorted((HERE / "inci_data").glob("*.yml")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
API_KEY = [REDACTED], "")


def validate_bind(host, api_key):
    if host not in ("127.0.0.1", "localhost", "::1") and not api_key:
        raise ValueError("THRONE_API_KEY is required when binding the reader API beyond loopback.")

def read_one(item):
    """One product, read exactly as the batch scanner reads it."""
    scope, scope_reason = R.product_scope(
        item.get("title") or "",
        item.get("handle") or "",
        item.get("product_type") or "",
        item.get("category") or "",
    )
    scoring_composition = item.get("canonical_composition") or item.get("composition") or item.get("material") or ""
    if scope == "accessory":
        band, comp, how = "accessory", "", "accessory"
    elif scope == "nontextile":
        band, comp, how = "nontextile", R.read_material(
            " ".join(str(item.get(k) or "") for k in ("description", "body_html", "page_text", "material"))
        ), "nontextile"
    else:
        band, comp, how = R.classify(
            item.get("title") or "",
            item.get("handle") or "",
            item.get("page_text") or "",
            item.get("body_html") or "",
            item.get("tags") or "",
            own_description=item.get("description") or None,
            product_material=scoring_composition or None,
        )
        if band == "nontextile":
            scope, scope_reason = "nontextile", "reader_nontextile_identity"
        elif band == "accessory":
            scope, scope_reason = "accessory", "reader_accessory_identity"
    if item.get("composition") and item.get("material"):
        other_kind, other_comp, other_band = R._composition(item["material"])
        if other_kind in ("pct", "components") and comp and band not in ("unclear", "nontextile"):
            if R._composition_conflict(comp, band, other_comp, other_band):
                band, comp, how = "unclear", f"{comp} [conflicts with material: {other_comp}]", "evidence-conflict"
    # Evidence needs a stable source. Commerce apps can supply their platform
    # product record ID when Wix/Shopify has not exposed an absolute storefront
    # URL; that record is still auditable server-side and is never invented by
    # the scanner.
    if band not in ("unclear", "nontextile", "accessory") and not (item.get("url") or item.get("source_endpoint")):
        band, how = "unclear", "missing-source-url"

    shares = None
    if band not in ("unclear", "nontextile", "accessory") and comp:
        try:
            shares = R.composition_shares(comp, item.get("component_weights"))
        except Exception:
            shares = None

    sku_score = None
    if shares and band not in ("unclear", "nontextile", "accessory", "undisclosed"):
        sku_score = TP.score_for(shares["plant"])
        sku_score = TP.animal_floor(sku_score, {band: 1})

    # Preserve the field actually selected, especially when a bare material summary
    # was superseded by a compatible complete product description or page declaration.
    if how == "page":
        source_field = "page_text"
    elif how == "own":
        source_field = next((key for key in ("body_html", "description", "tags") if item.get(key)), "body_html")
    elif how == "components":
        from product_evidence import product_text
        source_field = next((key for key in ("composition", "material", "body_html", "description", "tags", "page_text")
                             if comp.casefold().strip(" .") in product_text(item.get(key)).casefold()), "composition")
    else:
        source_field = "composition" if item.get("composition") else "material" if item.get("material") else "description"
    raw_text = (item.get("raw_original") or {}).get(source_field, item.get(source_field) or "")
    return {
        "sku": item.get("sku"),
        "collection": item.get("collection"),
        "band": band,
        "composition": comp or None,
        "how": how,
        "shares": shares,
        "component_weights": item.get("component_weights"),
        "source_inputs": {key: item[key] for key in ("composition", "canonical_composition", "material", "body_html",
            "description", "page_text", "tags", "url", "source_endpoint", "product_id", "variant_id", "sku",
            "raw_original", "component_weights") if key in item},
        "score": sku_score,
        # A SKU receives plant-evidence credit only when its stated composition is wholly plant.
        "passes": band == "plant",
        "product_scope": scope,
        "scope_reason": scope_reason,
        "report_included": scope == "textile" and band not in ("nontextile", "accessory"),
        "evidence_record": {
            "source_type": "product_page" if how == "page" else "structured_product_data",
            "source_url": item.get("url") or None,
            "source_endpoint": item.get("source_endpoint") or item.get("url") or "",
            "source_field": source_field,
            "raw_text": raw_text,
            "accepted_text": comp,
            "parsed_materials": sorted({
                R.MATERIAL_TERMS.get(match.group(1).lower(), (match.group(1).lower(), None))[0]
                for match in R.PROSE_MATERIAL.finditer(comp or "")
            }),
            "parsed_percentages": shares or {},
            "rejection_reason": ("Conflicting strong sources; review required." if how == "evidence-conflict"
                                 else f"Technical evidence read failure: {how}" if how.startswith("unread") or how == "missing-source-url"
                                 else "No usable product-specific composition evidence."
                                 if band in ("undisclosed", "unclear") else None),
            "captured_at": now(),
            "reader_version": R.READER_VERSION,
            "scanner_version": TP.SCANNER_VERSION,
            "throne_standard_version": TP.STANDARD_VERSION,
        },
    }


def brand_figure(rows, category=None):
    """Delegate all brand scoring to the canonical Standard policy."""
    return TP.api_brand_figure(rows, category=category)



# === HTT CANONICAL TEXTILE API PATCH 2026.09 ===
_htt_original_read_one = read_one

def read_one(item):
    out=_htt_original_read_one(item)
    if out.get('band')=='unclear' and out.get('how')=='none':
        out['band']='undisclosed'; out['how']='undisclosed'
    out.update({'title':item.get('title') or '', 'handle':item.get('handle') or '', 'url':item.get('url') or '',
                'price':item.get('price'),
                'price_min':item.get('price_min'),
                'price_max':item.get('price_max'),
                'variant_prices':item.get('variant_prices') if isinstance(item.get('variant_prices'),list) else [],
                'currency':item.get('currency') or '',
                'fetchedAt':item.get('fetchedAt') or ''})
    out.update({'category':item.get('category') or '', 'product_type':item.get('product_type') or ''})
    ann=TP.annotate_row(out,raw=item,category=item.get('category'))
    out.update(ann)
    if ann.get("composition_partial") or ann.get("evidence_state") != "scoreable":
        out["score"] = None
        out["passes"] = None
    for k in ('evidence_state','scoreable','evidence_source','evidence_notes','score_reason',
              'scoring_model','swimwear_natural_fibre_pct','swimwear_score'):
        if k in ann: out[k]=ann[k]
    return out

def brand_figure(rows, category=None):
    return TP.api_brand_figure(rows, category=category)
# === END HTT CANONICAL TEXTILE API PATCH ===

def brand_groups(rows, category=None):
    """Per-collection brand figures, only when every product carries the
    collection label the brand itself publishes. A store that mixes clothing
    with homeware gets one honest figure per collection instead of one blended
    range. Groups are disjoint: each product belongs to exactly one label, so
    group figures partition the catalogue without double counting."""
    by_collection = {}
    for r in rows:
        label = (r.get("collection") or "").strip()
        if not label:
            return []
        by_collection.setdefault(label, []).append(r)
    if len(by_collection) < 2:
        return []
    groups = []
    for label, group_rows in by_collection.items():
        figure = brand_figure(group_rows, category=category)
        groups.append({
            "collection": label,
            "count": len(group_rows),
            "figure": figure,
        })
    groups.sort(key=lambda g: (-g["figure"]["assessed"], g["collection"].lower()))
    return groups


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not API_KEY:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {API_KEY}"

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _universal(self, method, payload=None):
        # Import lazily: aws_reader_lambda imports the shared functions above,
        # and importing it before this module finishes would create a cycle.
        from aws_reader_lambda import handler as universal_handler
        event = {
            "rawPath": self.path.split("?", 1)[0],
            "requestContext": {"http": {"method": method, "path": self.path}},
            "headers": {key: value for key, value in self.headers.items()},
            "body": json.dumps(payload or {}),
            "isBase64Encoded": False,
        }
        result = universal_handler(event, None)
        try:
            response_body = json.loads(result.get("body") or "{}")
        except json.JSONDecodeError:
            response_body = {"error": "The universal scanner returned an invalid response."}
        return self._send(int(result.get("statusCode") or 500), response_body)

    def do_GET(self):
        return self._universal("GET")

    def do_POST(self):
        try:
            payload = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "Body is not JSON"})
        return self._universal("POST", payload)

    def do_OPTIONS(self):
        return self._universal("OPTIONS")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    try:
        validate_bind(args.host, API_KEY)
    except ValueError as error:
        ap.error(str(error))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Throne scan API on http://{args.host}:{args.port}")
    print(f"  reader:    throne_read.classify")
    print(f"  auth:      {'Bearer (configured)' if API_KEY else 'none (set THRONE_API_KEY)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
