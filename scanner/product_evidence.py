"""Product-owned text boundaries shared by textile and ingredient readers."""
import re
from html.parser import HTMLParser

# Discard attribution to another product, even if its percentage is explicit.
CROSS_SELL = re.compile(
    r"\b(?:pairs?\s+(?:(?:this|it|well)\s+)?with|style\s+(?:it\s+)?with|wear\s+(?:it\s+)?with|"
    r"model\s+(?:wears?|is\s+wearing)|complete\s+(?:the|your)\s+look|you\s+may\s+also\s+like|related\s+products?|"
    r"recommended\s+(?:products?|for\s+you)|customers\s+also\s+bought)\b[^\n.!?]*(?:[.!?]|$)", re.I)
NOISE = re.compile(r"(?:recommend|related[-_ ]?products|cross[-_ ]?sell|upsell|reviews?|newsletter|footer|navigation|journal|seo[-_ ]?(?:text|copy))", re.I)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class ProductText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        excluded = tag in {"script", "style", "template", "noscript", "nav", "footer", "aside"} or bool(
            NOISE.search(" ".join(attrs.get(k, "") or "" for k in ("class", "id", "aria-label"))))
        if tag not in VOID:
            self.stack.append((tag, excluded or any(x[1] for x in self.stack)))
        if tag in {"p", "div", "li", "br", "section", "h2", "h3", "dt", "dd"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        self.parts.append("\n")

    def handle_data(self, data):
        if not any(x[1] for x in self.stack):
            self.parts.append(data)


def product_text(value):
    parser = ProductText()
    parser.feed(str(value or ""))
    parser.close()
    return CROSS_SELL.sub(" ", " ".join(parser.parts)).strip()


def component_evidence(item):
    """Audit declarations independently; never assign garment component weights."""
    from throne_read import COMP_LABEL, _seg_pairs
    from text_normalizer import normalize_text, canonical_materials
    records = []
    sources = item.get('evidence_sources') or [
        {'source': key, 'raw_original': item[key]}
        for key in ('composition', 'material', 'body_html', 'description', 'page_text') if item.get(key)]
    for source in sources:
        raw = str(source.get('raw_original') or '')
        normalized = normalize_text(product_text(raw))
        labels = list(COMP_LABEL.finditer(normalized))
        for index, label in enumerate(labels):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(normalized)
            declaration = normalized[label.end():end].strip()
            pairs = _seg_pairs(declaration)
            total = sum(pct for pct, _ in pairs)
            materials = []
            for pct, name in pairs:
                token = (canonical_materials(f'{pct}% {name}') or [{'material': name, 'class': None}])[0]
                materials.append({'material': name, 'normalized_material': token['material'],
                                  'classification': token['class'], 'percentage': pct})
            records.append({
                'source': source.get('source'), 'raw_original': raw,
                'raw_declaration': declaration, 'normalized': normalized,
                'component': re.sub(r'[:\-\s]+$', '', label.group()).lower(),
                'materials': materials,
                'percentage_total': total,
                'unknown_remainder': max(0, 100 - total),
                'status': 'evidence_error' if total > 100 else 'complete' if total == 100 else 'partial'})
    return records
