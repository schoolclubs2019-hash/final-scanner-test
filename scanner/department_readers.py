"""Department adapters inside the one universal Throne scanner.

The authoritative clothing, beauty, food and hotel rules are all shipped from
this scanner directory and exposed through the same two HTTP routes. No client
or department has a separate reader service.
"""
from __future__ import annotations

import inci_read
import ingredient_extract
import ingredient_read
from department_capabilities import capability, CAPABILITIES
from product_evidence import product_text
from price_capture import capture_price
from throne_standard import STANDARD
import re
from row_stream import selected


ALIASES = {
    "clothes": "clothing", "apparel": "clothing", "shoes": "clothing", "textile": "clothing",
    "cosmetics": "beauty", "skincare": "beauty", "body-bath": "beauty", "body_bath": "beauty",
    "grocery": "food", "groceries": "food", "restaurant": "food", "chocolate": "food",
    "cacao": "food", "cocoa": "food", "hotel": "hotels", "stay": "hotels", "stays": "hotels",
    "phone": "electronics", "tech": "electronics", "laptops": "electronics",
    "veg-boxes": "food", "home-textiles": "bedding", "swimwear": "clothing",
    "homewares": "homeware", "housewares": "homeware", "home": "homeware",
}
SIGNALS = {
    "beauty": r"\b(?:skincare|serum|cleanser|moisturi[sz]er|toner|face oil|shampoo|conditioner|mascara|lipstick|foundation|concealer|deodorant|perfume|parfum|fragrance|body wash|body lotion|inci)\b",
    "food": r"\b(?:food|grocery|snack|chocolate|cacao|coffee|tea|beverage|drink|juice|olive oil|spice|flour|oats?|rice|quinoa|lentils?|beans?|ingredients|nutrition)\b",
    "hotels": r"\b(?:hotel|resort|eco lodge|ecolodge|hostel|guesthouse|guest house|bed and breakfast|room|suite|villa|bungalow|accommodation|book a stay|nightly rate)\b",
    "homeware": r"\b(?:homeware|housewares|home goods|bedding|towel|curtain|cushion|pillow|tableware|cookware|furniture)\b",
    "clothing": r"\b(?:clothing|apparel|garment|shirt|t-?shirt|tee|dress|skirt|trousers|pants|jeans|shorts|underwear|bra|jacket|coat|sweater|jumper|hoodie|sock|shoe|fabric|textile|cotton|linen|wool|polyester|elastane|viscose|composition)\b",
}


def canonical_department(value):
    key = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    key = ALIASES.get(key, key)
    return key if key in CAPABILITIES else "auto"


def route_product(item, requested="auto"):
    """Resolve each SKU independently. A brand hint cannot overrule product identity."""
    for key in ("cpt", "department", "product_type", "category"):
        value = canonical_department(item.get(key))
        if value != "auto":
            return value, "product_" + key
    # Product identity only; ingredients/materials/recommendations do not define department.
    text = " ".join(str(item.get(key) or "") for key in ("title", "product_type", "category"))
    identity_patterns = {
        "beauty": r"\b(?:serum|cleanser|moisturi[sz]er|shampoo|conditioner|mascara|lipstick|deodorant|perfume|body wash|body lotion|face oil|soap)\b",
        "food": r"\b(?:chocolate|cacao|coffee|tea|juice|snack|olive oil|flour|oats|rice|quinoa|lentils|beans)\b",
        "bedding": r"\b(?:bedding|bed sheet|bed linen|duvet|pillowcase|pillow|cushion|comforter|quilt|bedspread|napkin|tablecloth|placemat|towel|curtain|blanket|rug)\b",
        "electronics": r"\b(?:iphone|smartphone|laptop|computer|headphones|tablet)\b",
        "candles": r"\b(?:candle|candles|reed diffuser)\b",
        "jewellery": r"\b(?:necklace|earrings?|bracelet|jewellery|jewelry)\b",
        "clothing": r"\b(?:shirt|t-?shirt|tee|dress|skirt|trousers|pants|jeans|shorts|underwear|bra|jacket|coat|sweater|jumper|hoodie|sock|socks|shoes?|swimsuit|swimwear|bikini|scarf)\b",
    }
    # Structured type/breadcrumb outrank title. Compound garment nouns outrank
    # the incidental word "blanket", within the same identity source only.
    for key in ("product_type", "category", "breadcrumb", "title"):
        evidence = str(item.get(key) or "")
        if re.search(r"\b(?:blanket\s+(?:shirt|jacket|scarf)|shirt\s+jacket)\b", evidence, re.I):
            return "clothing", "specific_" + key
        matches = [dep for dep, pattern in identity_patterns.items() if re.search(pattern, evidence, re.I)]
        if len(matches) == 1:
            return matches[0], "product_identity"
        if len(matches) > 1:
            return "unknown", "ambiguous_product_category"
    hint = canonical_department(requested)
    if hint != "auto":
        return hint, "request_department"
    if item.get("inci"):
        return "beauty", "explicit_inci_field"
    # An unambiguous material in a title is a weaker last-resort textile signal.
    if re.search(r"\b(?:cotton|linen|wool|polyester|elastane|viscose)\b", text, re.I):
        return "clothing", "textile_title"
    return "unknown", "category_not_identified"


def detect_department(items, requested="auto"):
    departments = {route_product(item, requested)[0] for item in items}
    return next(iter(departments)) if len(departments) == 1 else "mixed"


def _source(item):
    return "\n".join(str(item.get(key) or "") for key in (
        "ingredients", "inci", "composition", "material", "description", "body_html", "page_text"
    ))


def _base(item):
    return {
        **capture_price(item),
        "sku": item.get("sku", ""),
        "title": item.get("title", ""),
        "handle": item.get("handle", ""),
        "url": item.get("url", ""),

    }


def _ingredient_declaration(value):
    return re.sub(r"^(?:full\s+)?(?:ingredients?|inci)\s*:?\s*", "",
                  product_text(value).strip(), flags=re.I).strip()


def read_beauty(item):
    row = _base(item)
    declared = item.get("inci") or item.get("ingredients")
    ingredients = ""
    if declared:
        candidate = _ingredient_declaration(declared)
        if not inci_read.VARIABLE_LIST.search(candidate) and not inci_read.STRUCTURAL_CONTAMINATION.search(candidate):
            ingredients = candidate
    else:
        ingredients = inci_read.extract_inci(product_text(item.get("description") or item.get("body_html") or item.get("page_text")))
    declarations = []
    for key in ("inci", "ingredients"):
        if item.get(key):
            declarations.append(_ingredient_declaration(item[key]))
    for key in ("description", "body_html", "page_text"):
        candidate = inci_read.extract_inci(product_text(item.get(key)))
        if candidate:
            declarations.append(candidate)
    normalized = {tuple(x.casefold().strip() for x in inci_read.split_inci(value)) for value in declarations}
    if len(normalized) > 1:
        row.update({"band": "unresolved", "composition": ingredients, "how": "evidence-conflict",
                    "assessment_status": "unresolved", "evidence_status": "evidence-conflict",
                    "evidence_state": "technical_failure", "natural_pct": None, "score": None, "passes": None})
        return row
    if not ingredients:
        row.update({
            "band": "unresolved", "composition": "", "how": "not-disclosed",
            "assessment_status": "unresolved", "evidence_status": "no_inci_found",
            "natural_pct": None, "score": None, "passes": None,
        })
        return row
    scored = inci_read.score_inci(row["title"], ingredients, url=row["url"])
    row.update(scored)
    row.update({
        "band": "unresolved" if scored.get("natural_pct") is None else "natural" if scored.get("is_100_natural") else "mixed",
        "composition": ingredients,
        "how": "own",
        "evidence_status": "assessed" if scored.get("assessment_status") == "assessed" else "unknown_ingredients",
        "score": _score_from_pct(scored.get("natural_pct")),
        "passes": scored.get("is_100_natural"),
    })
    return row


def read_food(item):
    row = _base(item)
    ingredients = str(item.get("ingredients") or "").strip() or ingredient_extract.extract(product_text(_source(item)))
    if not capability("food")["scoring"]:
        row.update({
            "band": "evidence", "composition": ingredients,
            "ingredients_evidence": ingredients, "how": "ingredient-capture",
            "assessment_status": "captured_for_validation",
            "evidence_status": "captured_for_validation", "natural_pct": None,
            "score": None, "passes": None,
        })
        return row
    if not ingredients:
        row.update({
            "band": "unresolved", "composition": "", "how": "not-disclosed",
            "assessment_status": "unresolved", "evidence_status": "no_ingredients_found",
            "natural_pct": None, "score": None, "passes": None,
        })
        return row
    scored = ingredient_read.score_sku(row["title"], ingredients, url=row["url"])
    natural = scored.get("natural_pct")
    row.update(scored)
    row.update({
        "band": "natural" if natural == 100 else "mixed" if natural is not None else "unresolved",
        "composition": ingredients,
        "how": "own" if natural is not None else "unreadable",
        "assessment_status": "assessed" if natural is not None else "unresolved",
        "evidence_status": "assessed" if natural is not None else "unreadable_ingredients",
        "passes": natural == 100 if natural is not None else None,
    })
    return row


def read_hotel(item):
    """Capture property evidence without inventing a hotel score.

    Published hotel results are loaded from Hold The Throne before this route is
    used. A new property can therefore be enumerated and submitted for review,
    but it receives no made-up score while a canonical hotel rubric is absent.
    """
    row = _base(item)
    row.update({
        "band": "evidence", "composition": "", "how": "page-evidence",
        "assessment_status": "captured_for_review", "evidence_status": "captured_for_review",
        "natural_pct": None, "score": None, "passes": None,
    })
    return row


def read_homeware(item):
    row = _base(item)
    material = str(item.get("material") or item.get("composition") or "").strip()
    row.update({
        "band": "evidence", "composition": material, "material_evidence": material,
        "how": "material-capture", "assessment_status": "captured_for_validation",
        "evidence_status": "captured_for_validation", "source_url": row["url"],
        "natural_pct": None, "score": None, "passes": None,
    })
    return row


def _score_from_pct(value):
    if value is None:
        return None
    return next((int(band["score"]) for band in STANDARD["scoring"]["score_bands"]
                 if value >= band["minimum_percent"]), None)


def _brand(rows, department, trusted):
    department_capability = capability(department)
    assessed = selected(rows, lambda row: row.get("natural_pct") is not None)
    unresolved = len(rows) - len(assessed)
    if department == "beauty":
        assessed = selected(assessed, lambda row: type(row.get("passes")) is bool)
        unresolved = len(rows) - len(assessed)
    rate = (100 * sum(row["passes"] is True for row in assessed) / len(assessed)
            if department == "beauty" and assessed else
            sum(row["natural_pct"] for row in assessed) / len(assessed) if assessed else None)
    average = round(rate, 1) if rate is not None else None
    score = None
    if trusted and assessed and not unresolved and department_capability["scoring"]:
        if department == "food":
            values = selected(assessed, lambda row: row.get("score") is not None)
            score = max(1, min(5, round(sum(row["score"] for row in values) / len(values)))) if values else None
        else:
            score = _score_from_pct(rate)
            if department == "beauty" and score == 5 and any(row.get("animal") for row in assessed):
                score = 4

    if department in ("hotels", "homeware") or not department_capability["scoring"]:
        headline = "Product evidence captured for validation"
        qualification = f"{department.title()} scoring and publication remain validation-locked; evidence is captured without a score."
    elif department == "hotels":
        headline = "Property evidence captured for review"
        qualification = "Existing verified hotel results load instantly. New property evidence is submitted for review; no score is guessed."
    elif average is None:
        headline = "Ingredients not disclosed"
        qualification = f"No complete {department} ingredient declaration could be verified from these product pages."
    else:
        headline = (f"{average:g}% of assessed products meet the ingredient purity rule" if department == "beauty"
                    else f"{average:g}% average natural ingredient origin across assessed products")
        qualification = (
            f"Ingredient declarations were assessed for {len(assessed)} of {len(rows)} products. "
            f"{unresolved} unresolved product{'s' if unresolved != 1 else ''} remain visible and were not guessed."
        )

    return {
        "department": department,
        "category": department,
        "score": score,
        "naturalPct": average,
        "assessed": len(assessed),
        "unresolved": unresolved,
        "filtered": 0,
        "status": "captured_for_validation" if not department_capability["scoring"] else "verified" if trusted and score is not None and not unresolved else "partial_scan" if assessed else "not_assessed",
        "statusLabel": "Captured for validation" if not department_capability["scoring"] else "Verified result" if trusted and score is not None else "Evidence pending server verification" if not trusted else "Not assessed",
        "headline": headline,
        "qualification": qualification,
        "scoreBasis": ("evidence capture only" if not department_capability["scoring"] else
                       "share of products whose complete ingredient lists meet the purity rule; not ingredient weight" if department == "beauty" else
                       "average of per-product ingredient scores; natural origin rate is an average, not product pass share" if department == "food" else "evidence capture only"),
    }


READERS = {"beauty": read_beauty, "food": read_food, "homeware": read_homeware, "hotels": read_hotel}
SCANNER_LABELS = {
    "beauty": "Universal Throne scanner · beauty rules",
    "food": "Universal Throne scanner · food rules",
    "homeware": "Universal Throne scanner · homeware evidence capture",
    "hotels": "Universal Throne scanner · hotel rules",
}


def read_department(items, department, trusted=False):
    reader = READERS.get(department, read_homeware)
    rows = [reader(item) for item in items]
    for row in rows:
        row["department"] = department
        row["scanner"] = SCANNER_LABELS.get(department, "Universal Throne scanner · " + department + " evidence capture")
        row["authoritative"] = bool(trusted)
        row["capability_state"] = capability(department)["state"]
        if not trusted or not capability(department)["scoring"]:
            row["score"] = None
            row["passes"] = None
    eligible = [row for row, item in zip(rows, items) if not (item.get("filtered") and item.get("exclusion_reason") != "not-apparel")]
    brand = _brand(eligible, department, trusted)
    brand["scanner"] = SCANNER_LABELS.get(department, "Universal Throne scanner · " + department + " evidence capture")
    brand["capability_state"] = capability(department)["state"]
    brand["publishing_enabled"] = capability(department)["publishing"]
    return {"read": rows, "brand": brand, "groups": []}
