"""Deterministic cleanup for stored catalogue text before eligibility and scoring."""
import html
import re
import unicodedata

from throne_standard import MATERIAL_TERMS


_ESCAPE = re.compile(r"\\?u([0-9a-fA-F]{4})(?![0-9a-fA-F])")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_PERCENT = re.compile(r"(?P<pct>\d+(?:\.\d+)?)\s*%\s*(?P<material>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ -]*)", re.I)
_MATERIALS = sorted(MATERIAL_TERMS.items(), key=lambda item: len(item[0]), reverse=True)


def _decode_literal_escapes(value):
    return _ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), value)


def _fold(value):
    return "".join(char for char in unicodedata.normalize("NFKD", value)
                    if not unicodedata.combining(char)).casefold()


def normalize_text(value, *, strip_html=False):
    """Repair only deterministic encoding and presentation corruption."""
    text = "" if value is None else str(value)
    text = _decode_literal_escapes(text)
    text = html.unescape(text)
    if strip_html:
        text = _TAG.sub(" ", text)
    text = text.replace("**", "")
    if any(marker in text for marker in ("Ã", "Â", "â€", "â€™", "â€œ", "â€“")):
        try:
            repaired = text.encode("latin1").decode("utf-8")
            if "�" not in repaired:
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return _SPACE.sub(" ", text).strip()


def canonical_materials(composition):
    """Return only Standard-supported percentage/material tokens."""
    tokens = []
    for match in _PERCENT.finditer(normalize_text(composition)):
        material_text = match.group("material").strip(" ,.;:/")
        material_folded = _fold(material_text)
        matched = None
        for alias, (canonical, material_class) in _MATERIALS:
            if _fold(alias) == material_folded:
                matched = (canonical, material_class)
                break
        if matched:
            tokens.append({
                "percentage": float(match.group("pct")),
                "material": matched[0],
                "class": matched[1],
            })
    return tokens


def canonical_composition(composition):
    """Build a scoring-only composition string from supported Standard tokens."""
    tokens = canonical_materials(composition)
    # A scoring projection must never drop unknown fibres, qualifiers or components.
    text = normalize_text(composition)
    matches = list(_PERCENT.finditer(text))
    if len(tokens) != len(matches) or not matches or re.search(r"(?:^|[\s,;])[+-]\d", text):
        return ""
    remainder = _PERCENT.sub("", text)
    if re.sub(r"[\s,;./]", "", remainder):
        return ""
    return ", ".join(f"{token['percentage']:g}% {token['material']}" for token in tokens)


def normalize_row(row):
    """Clean display fields and add scoring tokens without changing IDs or URLs."""
    cleaned = dict(row or {})
    cleaned['raw_original'] = dict(cleaned.get('raw_original') or {
        key: cleaned[key] for key in ('title', 'name', 'composition', 'fabric', 'material',
                                      'description', 'page_text', 'body_html', 'tags') if key in cleaned})
    for key in ("title", "name", "composition", "fabric", "material", "description", "page_text", "tags"):
        if key in cleaned:
            cleaned[key] = normalize_text(cleaned[key], strip_html=key in {"composition", "fabric", "material"})

    title = cleaned.get("title", "")
    composition = cleaned.get("composition") or cleaned.get("fabric") or cleaned.get("material") or ""
    first_percent = re.search(r"\d+(?:\.\d+)?\s*%", title)
    if first_percent and composition:
        prefix = title[:first_percent.start()].rstrip(" -:;,")
        if prefix and normalize_text(composition).casefold().startswith(title[first_percent.start():].strip().casefold()[:8]):
            cleaned["title"] = prefix

    cleaned['normalized'] = {key: cleaned[key] for key in cleaned['raw_original'] if key in cleaned}
    cleaned["composition_clean"] = composition
    cleaned["canonical_materials"] = canonical_materials(composition)
    cleaned["canonical_composition"] = canonical_composition(composition)
    return cleaned


def has_text_corruption(row):
    values = (row.get("title", ""), row.get("composition", ""), row.get("fabric", ""), row.get("material", ""))
    return any(re.search(r"(?:\\?u00[0-9a-fA-F]{2}|\\?u201[0-9a-fA-F]|&amp;|&#\d+;|�)", str(value)) for value in values)


def validate_clean_rows(rows):
    """Return publication-blocking text defects; an empty list is publishable."""
    errors = []
    for index, row in enumerate(rows or []):
        if has_text_corruption(row):
            errors.append({"row": index, "reason": "text_encoding_corruption"})
        title = normalize_text(row.get("title", ""))
        composition = normalize_text(row.get("composition") or row.get("fabric") or row.get("material") or "")
        if re.search(r"\d+(?:\.\d+)?\s*%", title) and not composition:
            errors.append({"row": index, "reason": "title_composition_concatenation"})
    return errors