"""Shared positive-evidence gate for discovered catalogue rows."""
import json
import re
from urllib.parse import urlparse


PRODUCT_ID_FIELDS = ("product_id", "platform_product_id", "variant_id", "platform_variant_id")
SKU_FIELDS = ("sku", "product_code", "product_code_standard")
DENIED_PAGE_TYPES = {
    "account", "blog", "category", "collection", "editorial", "footer", "lookbook",
    "navigation", "recommendation", "search", "static", "store_locator", "promotion", "promo", "homepage",
}
DENIED_PATH = re.compile(
    r"/(?:account|accounts|blogs?|categor(?:y|ies)|collections|lookbooks?|pages|search|"
    r"store-locator|stores)(?:/|$)", re.I,
)
DETAIL_PATH = re.compile(r"/(?:products?|p)/[^/]+/?$", re.I)
LISTING_TITLE = re.compile(
    r"\b(?:top\s+picks?|shop\s+the\s+look|lookbook|editorial|recommendations?|"
    r"collection|category|categories|search\s+results?|best\s+sellers?|new\s+arrivals?|"
    r"best\s+(?:clothes|outfits|styles)\b.*\bseason)\b",
    re.I,
)
CATEGORY_TITLE = re.compile(
    r"\b(?:ropa|clothing|clothes|apparel)\b.{0,90}\b(?:women|men|mujeres|hombres|"
    r"training|entrenamiento|sportswear|deportiv[oa])\b",
    re.I,
)
INVALID_VALUES = {"", "0", "false", "none", "null", "n/a", "na", "undefined"}


def _text(value):
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _present(value):
    return _text(value).casefold() not in INVALID_VALUES


def _objects(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def product_objects(row):
    """Only a root product or a graph product tied to this row, never recommendations."""
    for key in ("product_json", "commerce_json", "product"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                continue
        if isinstance(value, dict) and isinstance(value.get("product"), dict):
            value = value["product"]
        if isinstance(value, dict) and "@graph" not in value:
            candidates = [value]
        elif isinstance(value, dict):
            candidates = value.get("@graph", [])
        else:
            candidates = value if isinstance(value, list) else []
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            obj_url = obj.get("url") or obj.get("@id")
            row_url = row.get("url") or row.get("source_url")
            if obj_url and row_url and str(obj_url).split("#")[0].rstrip("/") != str(row_url).split("#")[0].rstrip("/"):
                continue
            if len(candidates) > 1 and not (obj_url and row_url):
                continue
            yield obj


def _product_type(value):
    values = value if isinstance(value, (list, tuple)) else [value]
    return any(_text(v).lower().rstrip("/").rsplit("/", 1)[-1] == "product" for v in values)


def _has_product_schema(row):
    return _product_type(row.get("schema_type") or row.get("@type") or row.get("type")) or any(
        _product_type(obj.get("@type") or obj.get("type")) for obj in product_objects(row))


def _has_commerce_product_json(row):
    for obj in product_objects(row):
        has_name = _present(obj.get("name") or obj.get("title"))
        has_product_data = any(_present(obj.get(key)) for key in ("sku", "price", "product_id", "productid"))
        has_product_data = has_product_data or bool(obj.get("variants") or obj.get("offers"))
        if has_name and has_product_data:
            return True
    return False


def _has_platform_id(row):
    return any(_present(row.get(key)) for key in PRODUCT_ID_FIELDS)


def _has_sku(row):
    handle = _text(row.get("handle")).strip("/").casefold()
    for key in SKU_FIELDS:
        value = _text(row.get(key))
        if not _present(value):
            continue
        if value.strip("/").casefold() == handle:
            continue
        if re.fullmatch(r"[a-z0-9]+(?:[-_/][a-z0-9]+)*", value, re.I) and "/" in value:
            continue
        return True
    return False


def _page_rejection(row):
    for key in ("page_type", "content_type", "template_type", "resource_type"):
        value = re.sub(r"[\s-]+", "_", _text(row.get(key)).casefold())
        if value in DENIED_PAGE_TYPES:
            return "non_product_page_type"

    title = _text(row.get("title") or row.get("name"))
    if LISTING_TITLE.search(title) or CATEGORY_TITLE.search(title):
        return "navigation_or_listing_title"

    url = _text(row.get("url") or row.get("source_url"))
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/")
    if DENIED_PATH.search(path) and not DETAIL_PATH.search(path):
        return "non_product_page_url"
    return ""


def confirm_product(row):
    """Return ``(confirmed, reason)``; a handle or unknown URL is never evidence."""
    if not isinstance(row, dict):
        return False, "invalid_row"
    page_reason = _page_rejection(row)
    if page_reason:
        return False, page_reason

    if _has_platform_id(row):
        return True, ""
    if _has_sku(row):
        return True, ""
    if _has_product_schema(row):
        return True, ""
    if _has_commerce_product_json(row):
        return True, ""

    url = _text(row.get("url") or row.get("source_url"))
    parsed = urlparse(url)
    has_price = _present(row.get("price") or row.get("price_min") or row.get("price_max"))
    has_availability = _present(row.get("availability") or row.get("stock_status"))
    if (parsed.scheme in ("http", "https") and parsed.netloc
            and DETAIL_PATH.search(parsed.path or "") and has_price and has_availability
            and _present(row.get("title"))):
        return True, ""
    return False, "missing_positive_product_evidence"


def filter_confirmed_products(rows):
    """Return confirmed rows and a reason-count map for discovery candidates rejected."""
    confirmed = []
    rejected = {}
    for row in rows:
        accepted, reason = confirm_product(row)
        if accepted:
            confirmed.append(row)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    return confirmed, rejected