#!/usr/bin/env python3
"""Throne Standard INGREDIENT EXTRACTOR — the food analogue of the clothing composition reader.

Its ONE job: pull the genuine ingredient DECLARATION off a brand's own product HTML, and nothing else.
The hard part (learned the same way the fibre reader was): a product page says the word "ingredients"
in many places that are NOT the declaration — marketing ("no artificial ingredients"), JSON-LD prose
("minimum 72% cacao delivers antioxidants"), reviews, related-product blurbs. Reading any of those is
worse than reading nothing, so this module is deliberately strict: it finds an Ingredients LABEL, takes
the visible text that FOLLOWS it (works across tags, for accordion / metafield layouts), trims at the
next section, and only returns it if it genuinely looks like a comma-separated list of foods. If nothing
qualifies it returns '' — the caller then reports "ingredients not published", never a fabricated read.

    extract(html_or_text) -> str      # the ingredient line, or '' if none is genuinely present
    looks_like_list(text) -> bool     # is this text a real ingredient list (not prose)?

Test-locked in test_ingredient_extract.py — behaviour must not regress silently.
"""
import re, html

# ---- unicode / entity repair (same mojibake fix used on web + app) -----------------------------
def _clean(s):
    s = html.unescape(s or '')
    s = re.sub(r'u(00[0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), s)   # u00e9 -> é
    s = s.replace('–', ',').replace('—', ',')                          # en/em dash -> comma
    return re.sub(r'\s+', ' ', s).strip()

def _visible(h):
    """strip tags but KEEP a separator so 'Ingredients</dt><dd>Cacao' -> 'Ingredients Cacao'"""
    h = re.sub(r'(?is)<(script|style|template|noscript)[^>]*>.*?</\1>', ' ', h or '')
    return _clean(re.sub(r'<[^>]+>', ' ', h))

# ---- vocabulary --------------------------------------------------------------------------------
FOOD = re.compile(r'cocoa|cacao|choc|sugar|milk|butter|salt|vanilla|nut|almond|hazelnut|cashew|'
    r'peanut|pistachio|lecithin|syrup|coconut|cream|honey|paste|powder|mass|bean|liquor|nib|'
    r'oat|rice|soy|oil|fruit|berry|cherry|orange|lemon|mint|ginger|cinnamon|caramel|malt|'
    r'wheat|flour|date|raisin|coffee|espresso|maple|agave|stevia|erythritol|inulin|fibre|fiber', re.I)
# words that mark PROSE, not a declaration — if the tail reads like a sentence, reject it
PROSE = re.compile(r'\b(deliver|delivers|provide|provides|maximum|antioxidant|benefit|health|'
    r'crafted|handmade|made with|we (?:use|make|believe|source)|our |simply|perfect|delicious|'
    r'rich|smooth|satisfying|indulg|guilt|treat|enjoy|experience|minimum \d|content\b)', re.I)
# a real declaration begins at this label
# (?![-\w]) so "product-ingredient-details-panel" (a CSS class) is NOT read as an Ingredients label —
# only a real word boundary counts, not a hyphenated class/id name.
ADDITIVE_HINT = r'lecithin|emulsifier|flavou?r|preservative|colou?r|acid|extract|powder|oil|sugar|butter|milk'
LABEL = re.compile(r'(?<![a-z])(?:(?:\d{1,3}\s*%\s*)?(?:natural|full|key|active|inactive)\s+)?(?:ingredients?|inci)(?![-\w])\s*(?:list)?\s*(?:\([^)]{0,30}\))?\s*[:\-–]?\s*', re.I)
# where an ingredient list ENDS (next section on the label / page)
STOP = re.compile(r'\b(allergen|allergy|may contain|contains\s*:|nutrition|nutritional|storage|'
    r'store in|keep (?:in|refrigerated|cool)|best before|use by|net (?:wt|weight)|weight\b|'
    r'per 100\s*g|per serving|reviews?\b|www\.|https?:|©|shipping|delivery|add to (?:cart|bag)|'
    r'no added|dairy[\s-]?free|gluten[\s-]?free|palm[\s-]?oil[\s-]?free|\bvegan\b|\borganic\b\.|'
    r'\bcertified\b|suitable for|\d{1,4}\s*g\b|\d{1,4}\s*oz\b)', re.I)
# ingredient separators seen in the wild: comma, semicolon, ampersand, plus, middot, and, bullet
SEP = re.compile(r'\s*[,;&+·•]\s*|\s+and\s+', re.I)

def looks_like_list(text):
    """True if `text` is a genuine ingredient list, not marketing prose."""
    t = text.strip(' .,;:').strip()
    if len(t) < 3 or len(t) > 400:
        return False
    if re.search(r'(?:\.\.\.|…|\betc\.?\b|\band\s+more\b|\bother\s+ingredients?\b|\bmay\s+(?:also\s+)?include\b)', t, re.I):
        return False                         # explicitly truncated/variable declaration
    if not FOOD.search(t):
        return False
    segs = [s.strip() for s in SEP.split(t) if s.strip()]
    if not segs:
        return False
    short = sum(1 for s in segs if len(s.split()) <= 6)
    # a single-item list ("100% cocoa") is fine only if it's short and food; multi-item needs 70% short
    if len(segs) == 1:
        return len(t.split()) <= 5 and not PROSE.search(t)
    if short < len(segs) * 0.7:
        return False
    # reject if the whole thing reads like a sentence
    if PROSE.search(t) and ',' not in t[:60]:
        return False
    return True

def _tail_after(label_end_text):
    """given the visible text that follows an Ingredients label, isolate the declaration"""
    t = label_end_text.lstrip(' :.-–')
    m = STOP.search(t)
    if m:
        t = t[:m.start()]
    # cut at a sentence end that is clearly the end of the list (". Capital" or "! ")
    t = re.split(r'(?<=[a-z%\)])\.\s+(?=[A-Z])', t)[0]
    return t.strip(' .,;:')

def extract(source, validate=None, window=900, reject=None):
    """Return the ingredient declaration from product HTML (or plain text), or '' if none is real.
    validate(text)->bool decides what counts as a genuine list (default = the food validator;
    pass inci_read.looks_like_inci for cosmetics). window sizes the capture (INCI lists run long)."""
    if not source:
        return ''
    validate = validate or looks_like_list
    raw = source
    # 1) labelled declaration in the HTML — capture the VISIBLE text following each label
    # A declaration ends at its own block boundary. Goodnow Farms writes
    #   "Ingredients</strong>: Cacao Beans, Organic Sugar, Freshly Pressed Cocoa Butter<br>Soy Free | ..."
    # and letting the free-from badge run ("Soy Free | Gluten Free |") glue onto the end made the
    # whole thing fail looks_like_list, so a real, complete declaration was thrown away and the
    # caller fell back to marketing prose. Cut at the first block boundary after the label first.
    # A page says "ingredients" in several places and the FIRST is usually marketing ("ingredients
    # from organic cultivation") or a nav link, while the real declaration sits further down. Collect
    # every candidate and return the BEST one, judged on how much it reads like a list.
    candidates = []
    for m in LABEL.finditer(raw):
        chunk = raw[m.end():m.end() + window]
        # A declaration is often laid out as separate LINES or bullets with no commas:
        #   "ingredients for this bar:<br>SINGLE ORIGIN COCOA BEANS<br>CANE SUGAR<br>COCOA BUTTER"
        # Stripping tags flattens that into one run and it fails looks_like_list, so the whole
        # declaration is discarded. Turn block boundaries into separators BEFORE the tags go.
        chunk = re.sub(r'(?i)<\s*(?:br\s*/?|/p|/li|/div|/h[1-6]|/td|/tr)\s*>', ', ', chunk)
        # The label often sits in its own element ("<h3>Ingredients</h3><p>cocoa butter*, ...").
        # Cutting at the first block boundary would stop on the <p> that OPENS the declaration, so
        # skip the tags between the label and the first visible character before cutting.
        lead = re.match(r'(?:\s|<[^>]*>|[:\-\u2013])*', chunk)
        chunk = chunk[lead.end():] if lead else chunk
        seg = chunk
        cand = _clean(_visible(seg))
        cand = re.sub(r'\s*\|.*$', '', cand)          # trailing "Soy Free | Gluten Free" badges
        # the slice can start mid-markup ('">Ingredients:') or on the label's own colon
        cand = re.sub(r'^[\s">|·•\-]*', '', cand)
        cand = re.sub(r'^(?:(?:\d{1,3}\s*%\s*)?(?:natural|full|key)?\s*ingredients?\b\s*(?:list)?\s*[:\-–]?\s*)+',
                      '', cand, flags=re.I)
        cand = cand.lstrip(': \u2013-')
        # "ingredients for this bar: X, Y" - drop the qualifier fragment before the colon
        cand = re.sub(r'^[^,]{0,40}?:\s*,?\s*', '', cand).lstrip(', ')
        # A parenthetical aside is not part of the list: "Single Origin Cacao Beans (caffeine is
        # naturally present)" is ONE ingredient, but the aside pushed it past the single-item word
        # limit and the whole declaration was discarded. Brackets that hold a comma list or an
        # additive are kept, because those ARE ingredients.
        # Keep a bracket that names ingredients ("(cacao mass)"); drop one that is an aside about
        # the product ("(caffeine is naturally present)"). Prose, not punctuation, is the tell.
        def _keep(m):
            inner = m.group(1).strip()
            aside = re.search(r'\b(?:is|are|was|were|may|can|naturally|present|contains?\s+no|'
                              r'sourced|grown|from\s+our|our\s+own)\b', inner, re.I)
            return ' (' + inner + ')' if (FOOD.search(inner) and not aside) else ' '
        cand = re.sub(r'\s*\(([^)]*)\)', _keep, cand).strip()
        cand = trim_trailer(cand)
        if cand and looks_like_list(cand) and (validate is None or validate(cand)):
            if not (reject and reject(cand)):
                candidates.append(cand)
    if candidates:
        # A declaration is short noun phrases. Process prose ("the chocolate mass is conched, warmed
        # up, shaped") also has commas and food words, so it must be rejected outright rather than
        # merely ranked lower, or it wins on segment count alone.
        def _is_declaration(c):
            segs = [x.strip() for x in SEP.split(c) if x.strip()]
            if not segs:
                return False
            if PROSE.search(c):
                return False
            if sum(len(x.split()) for x in segs) / len(segs) > 5:
                return False                       # long segments mean sentences, not ingredients
            return sum(1 for x in segs if FOOD.search(x)) >= max(1, len(segs) * 0.5)
        # The food test cannot judge an INCI list (Aqua, Tocopherol, Phenoxyethanol are not foods),
        # so it only PROMOTES a clear declaration. When it finds none, fall back to the first
        # candidate, which is the behaviour this function had before.
        real = [c for c in candidates if _is_declaration(c)]
        if real:
            return max(real, key=lambda c: (sum(1 for x in SEP.split(c) if FOOD.search(x)), -len(c)))
        return candidates[0]
    for m in LABEL.finditer(raw):
        vis = _visible('ingredients ' + raw[m.end():m.end() + window])
        # the label can repeat and carry a claim: "> INGREDIENTS 99% NATURAL INGREDIENTS:"
        vis = re.sub(r'^(?:[\s>|·•\-]*(?:\d{1,3}\s*%\s*)?(?:natural|full|key)?\s*ingredients?\b\s*(?:list)?\s*[:\-–]?\s*)+', '', vis, flags=re.I)
        if reject and reject(vis):
            continue
        cand = _clean(_tail_after(vis))
        if validate(cand):
            return cand
    # 2) explicit machine field: "ingredients":"..." in JSON-LD / metafields (validated, so marketing fails)
    for m in re.finditer(r'"ingredients?"\s*:\s*"([^"]{3,%d})"' % (window // 2), raw, re.I):
        raw_cand = _clean(m.group(1))
        if reject and reject(raw_cand):
            continue
        cand = _clean(_tail_after(raw_cand))
        if validate(cand):
            return cand
    # 3) Shopify renders the declaration into an UNLABELLED metafield block, so no label precedes it.
    #    Take the longest block that reads like a declaration and let validate() reject the rest.
    best = ''
    for m in re.finditer(r'<[^>]*metafield-multi_line_text_field[^>]*>(.*?)</[a-z]+>', raw, re.S | re.I):
        cand = _clean(_visible(m.group(1)))
        if reject and reject(cand):
            continue
        cand = re.sub(r'^(?:[\s>|·•\-]*(?:\d{1,3}\s*%\s*)?(?:natural|full|key)?\s*ingredients?\b\s*(?:list)?\s*[:\-–]?\s*)+', '', cand, flags=re.I)
        if len(cand) > len(best) and validate(cand):
            best = cand
    if best:
        return best
    return ''

# A declaration is followed on many packs by a legend and spec block:
#   "cane sugar* * = ingredients from organic farming ** = from biodynamic agriculture
#    Characteristics Cocoa: 85% minimum"
# Left attached it survives looks_like_list (it still contains food words) and lands in the score as
# a pile of unrecognised tokens, dragging a clean 100% list down. Cut the declaration free.
TRAILER = re.compile(
    r'\s*(?:'
    r'\*+\s*=|'                                        # "* = ingredients from organic farming"
    r'\bcharacteristics\b|\bnutrition(?:al)?\b|\bnutritional\s+values?\b|\benergy\b|\bkcal\b|'
    r'\bper\s+100\s*g\b|\bcocoa\s*:\s*\d|'
    r'\ballergen|\bmay\s+contain\b|\bstorage\b|\bstore\s+(?:in|at)\b|\bbest\s+before\b|'
    r'\bnet\s+(?:wt|weight)\b|\bshelf\s+life\b|'
    r'\bingredients?\s+from\s+organic\b|\bfrom\s+biodynamic\b|'
    r'\bvegan\s+and\b|\bgluten[- ]free\b|\bsuitable\s+for\b|'          # pack badges
    r'\bquantity\b|\badd\s+to\s+(?:cart|bag)\b|\bsold\s+out\b|\bsubscribe\b|\bshare\b|'  # shop UI
    r'\bdirections?\b|\bhow\s+to\s+use\b|\busage\b|\bdescription\b|\breviews?\b|'          # next section
    r'\brelated\b|\bdelivery\b|\breturns\b|\byou\s+may\s+also\b|\bcustomers\s+also\b'
    r').*$', re.I)


def trim_trailer(text):
    """Remove the organic-marker legend and nutrition/spec block that follows a declaration."""
    # keep the per-ingredient organic asterisks: they are part of the declaration as printed, and
    # split_ingredients() already strips them before classification
    return TRAILER.sub('', text or '').strip(' ,.;')
