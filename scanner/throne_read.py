#!/usr/bin/env python3
"""
Throne Standard ACCURATE FREE reader (no LLM, zero Claude usage).
Reads a product's own composition and product-specific description. Material classes come from the
canonical machine-readable Throne Standard.
"""
import math
import re
import unicodedata
from throne_standard import MATERIAL_TERMS

READER_VERSION = "1.3.2"
_MATERIAL_CLASS = {
    term: ("semi" if material_class == "semi_synthetic" else material_class)
    for term, (_canonical, material_class) in MATERIAL_TERMS.items()
}
PLANT = {term for term, material_class in _MATERIAL_CLASS.items() if material_class == "plant"}
ANIMAL = {term for term, material_class in _MATERIAL_CLASS.items() if material_class == "animal"}
SYN = {term for term, material_class in _MATERIAL_CLASS.items() if material_class == "synthetic"}
SEMI = {term for term, material_class in _MATERIAL_CLASS.items() if material_class == "semi"}
FIBRE = set(_MATERIAL_CLASS)
# Hides and furs are materials in their own right: FULL_SCAN_RULES.md classes them animal,
# so a leather jacket, skirt or tote is banded rather than dropped as a non-textile good.
MATERIAL_NON_FIBRE = {'leather', 'suede'}

# non-textile product classes (homeware / footwear / accessories with no clothing fabric) —
# excluded from the clothing plant %, so a brand-wide fibre tagline in a product's description
# ('...woven from Italian linen') can't count a glass carafe or a leather shoe as plant.
# Genuinely NON-FIBRE objects only (no textile to rate) -> excluded from the clothing %.
# Footwear is NOT here: leather/suede shoes are rated by material (leather = animal), per the
# "exclude nothing, rate by the standard" rule — excluding them would inflate the plant %.
NONTEXTILE = re.compile(r'\b(?:ceramic|terracotta|porcelain|stoneware|mug|cup|tumbler|drinkware|'
    r'dinnerware|vase|bowl|plate|spoon|incense|pitcher|jar|carafe|decanter|glassware|goblet|'
    r'stemware|fl[uû]te|glass|candle|soap|shampoo|lotion|balm|oil|perfume|bottle|flask|'
    r'jewel|jewellery|jewelry|ring|necklace|earring|bracelet|brooch|pendant|cufflink|'
    r'sunglass|sunglasses|eyewear|watch|'
    # 'print' ONLY as wall/art print — in fashion 'block print', 'floral print', 'cotton print'
    # is a PATTERN, not a non-textile good (a 'Cotton Block Print' nightie is apparel)
    r'book|journal|notebook|poster|(?:art|photo|wall|framed|canvas|fine[\s-]?art|gicl[ée]e?)[\s-]?print|'
    r'card|sticker|magnet|keychain|keyring|'
    r'gift\s*(?:card|bag)|e-?gift|voucher|'
    # Bedding and home textiles use the same purity reader as clothing.
    # jewellery beyond the basics
    r'hoop|bangle|anklet|charm|bead|'
    # toys / plush
    r'teddy|stuffed\s*(?:toy|animal)|plush|lovey|snuggle|rattle|doll|'
    # packaging, dyes, raw craft goods
    # NB: no bare 'cord' (corduroy caps) and no 'sliver' (product names like "Instapump Sliver")
    r're-?zip|packaging|mailer|pigment|dye\s*kit|yarn|twine|bungee\s*cord|webbing|roving|'
    r'shipping\s*kit|return\s*kit|compost|take-?back|'
    # home fragrance / body care
    r'diffuser|fragrance|perfume|parfum|cologne|sunscreen|spf|sun\s*protection|deodorant|scrub|serum|'
    # checkout line-items and services (not products at all)
    r'gift\s*note|gift\s*wrap|shipping\s*protection|insurance|donation|donate|tariff|tarrif|'
    # services, memberships and promo entries (not products)
    r'mending\s*club|repair\s*(?:club|kit|service)|membership|subscription|free\s+\w*\s*gift|free\s+pair|'
    # small accessories that are not garments
    r'teardrop|hair\s*stick|hairstick|hair\s*clip|scrunchie\s*set|'
    # non-garment add-ons seen as unresolved SKUs (SKU-by-SKU pass, 1 Sep 2026) — APPAREL guard protects real garments
    r'pencil\s*(?:pouch|case)|makeup\s*pouch|lint\s*roller|fit\s*tape|boob\s*tape|body\s*tape|'
    r'replacement\s*strap|choker|tarjeta\s*regalo|insect\s*repellent|repellent\s*treatment|'
    r'carbon\s*(?:offset|footprint)|offset\s*my\s*order|route\s*(?:package\s*)?protection|package\s*protection|'
    r'additional\s*fee|extra\s*fee|personalization|personalisation|swatch|sample\s*pack|'
    r'free\s*returns|store\s*credit|deposit|surcharge)s?\b', re.I)
# garment words — if a title names a garment it is apparel, never non-textile homeware
APPAREL = re.compile(r'\b(?:shirt|tee|t-shirt|dress|sock|pant|trouser|short|jacket|coat|hoodie|'
    r'sweater|sweatshirt|legging|bra|brief|boxer|underwear|skirt|top|blouse|jumpsuit|romper|'
    r'poncho|glove|hat|beanie|scarf|cardigan|jean|robe|pyjama|pajama|swimwear|swimsuit|bikini|'
    r'jumper|knitwear|tank|vest|onesie|bodysuit|kimono|overall|dungaree|slipper\s*sock|'
    r'nightie|nightgown|nightdress|nightshirt|tunic|gown|caftan|kaftan|sarong|playsuit|camisole)s?\b', re.I)
# textile / fabric product words — a fabric store's product (e.g. "Paper Yarn Cotton Stretch Twill
# Fabric") is a textile, not raw craft yarn.  The NONTEXTILE yarn match must not fire when the
# title also names a woven structure, fibre, or fabric type.
FABRIC = re.compile(r'\b(?:fabric|cloth|textile|twill|canvas|denim|chambray|voile|chiffon|satin|'
    r'velvet|tulle|organza|taffeta|poplin|crepe|georgette|muslin|calico|gauze|'
    r'woven|knit(?:ted|ted)?|jersey|rib(?:bed|bing)|fleece|terry|velour|'
    r'linho|toile|stoff|tissu|stoffe)\b', re.I)

# Report scope is an identity decision, not a material-scoring inference.
# Accessories remain accounted catalogue products, but they never enter the
# clothing textile denominator or block publication for missing composition.
NONMERCHANDISE = re.compile(
    r'\b(?:gift\s*(?:card|certificate|voucher|bag)|e-?gift|voucher|store\s*credit|'
    r'donation|donate|shipping\s*protection|insurance|membership|subscription|'
    r'additional\s*fee|extra\s*fee|deposit|surcharge)\b', re.I)
ACCESSORY_SPECIFIC = re.compile(
    r'\b(?:laundry|wash(?:ing)?|garment|lingerie|shoe)\s+(?:bag|bags|pouch|pouches)\b|'
    r'\b(?:bra\s+)?pads?\b|\b(?:bra|breast|bust)\s+inserts?\b|'
    r'\breplacement\s+straps?\b|\b(?:fit|boob|body)\s+tape\b', re.I)
ACCESSORY_GENERAL = re.compile(
    r'\b(?:accessor(?:y|ies)|bags?|totes?|backpacks?|purses?|pouches?|clutches?|wallets?|'
    r'belts?|card\s*holders?|keychains?|keyrings?|scrunchies?|hair\s*(?:clips?|ties?|bands?|sticks?)|'
    r'headbands?|hats?|beanies?|caps?|scar(?:f|ves)|gloves?|mittens?|neck\s*gaiters?)\b', re.I)
CORE_GARMENT = re.compile(
    r'\b(?:shirt|tee|t-?shirt|dress|sock|pant|trouser|short|jacket|coat|hoodie|sweater|'
    r'sweatshirt|legging|bra|brief|boxer|underwear|skirt|top|blouse|jumpsuit|romper|'
    r'cardigan|jean|robe|pyjama|pajama|swimwear|swimsuit|bikini|jumper|knitwear|tank|'
    r'vest|onesie|bodysuit|kimono|overall|dungaree|slipper\s*sock|nightie|nightgown|'
    r'nightdress|nightshirt|tunic|gown|caftan|kaftan|sarong|playsuit|camisole)s?\b', re.I)


def product_scope(title='', handle='', product_type='', category=''):
    """Return (scope, reason) from product identity only.

    Textile products are eligible for the clothing report. Accessory and
    nontextile products remain in catalogue reconciliation but are explicitly
    out of scope for the textile score/report.
    """
    identity = ' '.join(str(value or '') for value in (title, handle, product_type, category))
    if NONMERCHANDISE.search(identity):
        return 'nontextile', 'non_merchandise_identity'
    if ACCESSORY_SPECIFIC.search(identity):
        return 'accessory', 'accessory_identity'
    if ACCESSORY_GENERAL.search(identity) and not CORE_GARMENT.search(identity):
        return 'accessory', 'accessory_identity'
    if NONTEXTILE.search(identity) and not CORE_GARMENT.search(identity) and not FABRIC.search(identity):
        return 'nontextile', 'nontextile_identity'
    return 'textile', 'eligible_textile_identity'

NEG   = re.compile(r'\b(?:no|non|without|free\s+of|free\s+from|zero|not\s+made\s+(?:with|from|of))\b[\s-]*([a-z]+)|\b([a-z]+)[\s-]*free\b', re.I)
COMP  = re.compile(r"(?:materials?|composition|fabric|fibre|fiber|content|made\s+(?:of|from|with)|"
    r"crafted\s+from|knit\s+from|woven\s+from|shell)\s*[:\-]?\s*([a-z0-9 ,.%/&':-]{3,90})", re.I)

# Non-fibre materials, so a product with no textile composition still names what it IS made of
# (jewellery -> metal, homeware -> ceramic/wood/glass, bags -> leather, cases -> plastic). Read from
# the same stated-material prose; never guessed. Used only for display, not for the fibre score.
MATERIAL_WORDS = re.compile(r'\b(925\s*sterling\s*silver|sterling\s*silver|recycled\s*silver|silver|'
    r'gold\s*vermeil|gold[- ]?plated|gold[- ]?filled|rose\s*gold|yellow\s*gold|white\s*gold|vermeil|solid\s*gold|gold|'
    r'platinum|titanium|stainless\s*steel|surgical\s*steel|steel|brass|bronze|copper|pewter|alumini?um|'
    r'porcelain|stoneware|earthenware|terracotta|ceramic|enamel|clay|'
    r'borosilicate\s*glass|crystal|glass|'
    r'reclaimed\s*wood|solid\s*wood|oak|walnut|teak|beech|birch|maple|bamboo|rattan|cane|cork|wood|'
    r'full[- ]?grain\s*leather|genuine\s*leather|vegan\s*leather|leather|suede|'
    r'recycled\s*plastic|acrylic|silicone|resin|rubber|pvc|plastic|'
    r'marble|granite|slate|stone|concrete|jute|seagrass|wax|soy\s*wax|beeswax)\b', re.I)

def read_material(text):
    """The stated NON-fibre material for a product with no textile composition (metal for jewellery,
    ceramic/wood/glass for homeware, leather for bags, plastic for cases). Read from a 'material: X'
    clause or a material word actually present in the copy; '' when the brand states nothing."""
    if not text: return ''
    t = re.sub(r'<[^>]+>', ' ', text)
    m = COMP.search(t)
    if m:
        val = re.sub(r'\s+', ' ', m.group(1)).strip(" .,:-")
        if len(val) >= 3 and not val.isdigit(): return val[:70]
    m2 = MATERIAL_WORDS.search(t)
    if m2: return re.sub(r'\s+', ' ', m2.group(1)).strip().lower()
    return ''
# Related-product sections — cut here so a SUGGESTED item's composition can't be read as this
# product's. Only STRONG, unambiguous markers: bare "recommend" ("we recommend sizing up") and
# "more from" fire inside ordinary copy and were cutting the page BEFORE the real composition,
# hiding it (No Nasties read 100% organic cotton as unclear). Strong markers alone are enough.
_STOP = re.compile(r'you\s+may\s+also|related\s+products?|you\s+might\s+also|complete\s+the\s+look|'
    r'shop\s+the\s+look|customers\s+also(?:\s+(?:bought|viewed|liked))?|similar\s+(?:products|items)|'
    r'"productid"|"graphqlid"|productrecommendations', re.I)
_PACKAGING_SECTION = re.compile(r'\b(?:packing\s+and\s+shipping|packaging(?:\s+(?:and|&)\s+shipping)?|shipping\s+and\s+packaging)\b', re.I)

def _without_packaging_section(text):
    plain = re.sub(r'<[^>]+>', ' ', text or '')
    match = _PACKAGING_SECTION.search(plain)
    return plain[:match.start()] if match else plain

SCRIPT = re.compile(r'<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)>', re.I | re.S)
# "elastic waistband / elasticated cuff / elastic drawstring" is a CONSTRUCTION feature, not the
# elastane FIBRE — a pure-linen pant with an elastic waist is still 100% plant. Bare "elastic" is
# in SYN (real labels do write "5% elastic"), so neutralise it ONLY when a garment-part noun
# follows, before fibre extraction. A genuine "elastane"/"spandex"/"5% elastic" composition is
# untouched and still counts. (MagicLinen BREMER/MORCOTE were mis-read synthetic on this.)
ELASTIC_TRIM = re.compile(r'\belasti(?:c|cated|cised|cized)\b[\s-]*'
    r'(?:waist\s*bands?|waistbands?|waists?|bands?|draw\s*strings?|drawstrings?|draw\s*cords?|'
    r'cuffs?|hems?|closures?|gussets?|loops?|straps?|openings?|backs?|ankles?|legs?|'
    r'bindings?|trims?|edges?|panels?|inserts?|sides?)', re.I)
# More ambiguous words that are NOT the fibre in these contexts. Each would otherwise wrongly demote
# a plant garment (or mis-band it). Neutralised BEFORE fibre extraction; a genuine composition
# ("100% silk", "goose down", "camel hair", "5% elastane") is left intact and still counts.
# "down"/"feather" are fibres ONLY in a fill/insulation context (down jacket, goose down, 20% down,
# down-filled). Bare "down" is a collar/preposition/style ("button-down", "sits down", a tank dress
# that says "down to the knee") and bare "feather" is usually a style name ("Feather Pump") or a
# design detail ("feather edge"). So COUNT down/feather only with a positive context; else strip.
DOWNFEATHER_FIBRE = re.compile(r'\b(?:down|feather)[\s-]?(?:fill|filled|feathers?|insulat\w*|padding|proof)\b'
    r'|\b(?:goose|duck)[\s-]?(?:down|feather)\b|\bdown\s+(?:jacket|coat|vest|parka|gilet|puffer)\b'
    r'|\d{1,3}(?:\.\d+)?\s*%\s*(?:down|feather)\b|\b(?:down|feather)\s*\d{1,3}(?:\.\d+)?\s*%', re.I)
BARE_DOWNFEATHER = re.compile(r'\b(?:downs?|feathers?)\b', re.I)
BAMBOO_TRIM  = re.compile(r'\bbamboo\s*(?:buttons?|charcoal|handles?|lining|clasp|zips?|beads?)\b', re.I)
CAMEL_COLOUR = re.compile(r'\bcamel\b(?!\s*(?:hair|wool|fib))', re.I)          # 'camel' colour, not camel hair
SILK_SIMILE  = re.compile(r'\bsilk(?:[\s-]?(?:like|soft|touch|feel|finish|effect|smooth)|y)\b', re.I)  # 'silky', 'silk-like'
# 'cashmere-soft organic cotton' — cashmere as a TEXTURE simile, not the fibre (No Nasties knits
# every organic-cotton top 'cashmere-soft'). Real 'cashmere' / '100% cashmere' is left untouched.
CASHMERE_SIMILE = re.compile(r'\bcashmere[\s-]?(?:like|soft|touch|feel|finish|effect|smooth)\b'
    r'|\bsoft\s+as\s+cashmere\b', re.I)
RUBBER_TRIM  = re.compile(r'\brubber\s*(?:soles?|out[\s-]?soles?|grips?|bands?|buttons?|tips?|'
    r'coat(?:ed|ing)|logo|patch|sole\s*unit)\b', re.I)                          # rubber sole/buttons, not fibre
FAUX_LEATHER = re.compile(r'\b(?:faux|vegan|synthetic|imitation|pu|eco|plant[\s-]?based)[\s-]*(?:leather|suede)\b'
    r'|\bleather(?:ette|[\s-]?look)\b|\bsuede[\s-]?look\b|\bmicro[\s-]?suede\b', re.I)  # = synthetic, not animal

def _deambig(t):
    """Strip/normalise ambiguous fibre-word contexts so construction, colour and simile terms are
    not mistaken for fibres. Order matters: faux-leather -> synthetic BEFORE any leather match.
    Then EU fibre words -> English so the rest of the reader sees one vocabulary."""
    t = ELASTIC_TRIM.sub(' ', t)
    t = FAUX_LEATHER.sub(' polyurethane ', t)   # vegan/faux leather is synthetic (PU), never animal
    t = BAMBOO_TRIM.sub(' ', t)                 # bamboo button/charcoal/handle, not bamboo fabric
    t = SILK_SIMILE.sub(' ', t)
    t = CASHMERE_SIMILE.sub(' ', t)             # 'cashmere-soft cotton' -> cotton, not cashmere
    t = RUBBER_TRIM.sub(' ', t)
    t = CAMEL_COLOUR.sub(' ', t)
    if not DOWNFEATHER_FIBRE.search(t):         # no down/feather fill context -> not a fibre here
        t = BARE_DOWNFEATHER.sub(' ', t)
    return t


# A stated elastane percentage ("5% elastane", "elastane 5%") = quantified, no ambiguity to flag.
_ELAST_PCT = re.compile(r'\d{1,3}(?:\.\d+)?\s*%\s*[a-z ]{0,20}elast|elast[a-z]*\s*\d{1,3}(?:\.\d+)?\s*%', re.I)

def flag_notes(title, body_html='', tags='', page_text='', composition=''):
    """TRANSPARENCY flags, not score changes. The score reads the stated composition; this records
    suspicions for the reader: (a) an 'elastic waistband' with no elastane %; (b) a non-plant fibre
    NAMED in the copy that is NOT in the stated composition (a leather trim, a satin lining, a care
    note or a simile), which we do not count but surface so nothing is hidden."""
    raw = f"{title} {re.sub(r'<[^>]+>', ' ', body_html or '')} {tags}"
    notes = []
    if ELASTIC_TRIM.search(raw) and not _ELAST_PCT.search(raw):
        notes.append('Mentions an elastic waistband or cuff but does not state a percentage or '
                     'confirm it is elastane, so a small unquantified elastane content is possible.')
    txt = _strip_neg(_deambig(raw)).lower()
    in_comp = set(re.split(r'[^a-z]+', (composition or '').lower()))
    seen = [w for w in sorted(ANIMAL | SYN | SEMI)
            if w != 'elastic' and w not in in_comp and re.search(r'\b' + re.escape(w) + r'\b', txt)]
    if seen:
        notes.append('The description mentions ' + ', '.join(seen) + ', which is not in the stated '
                     'composition (likely a trim, lining or care note); it is not counted in the score.')
    return notes


def exact_composition(title, body_html='', tags='', page_text=''):
    """The EXACT stated fibre composition, verbatim — e.g. '95% cotton, 5% elastane' or
    '100% organic cotton'; '' when the brand publishes none. This is exactly what `classify` read to
    assign the band — SAME read, so the printed composition and the band can never disagree."""
    _band, comp, _how = classify(title, '', page_text, body_html, tags)
    return comp


def _cat(w):
    w = w.strip().lower()
    # Accent folding is a deterministic alias lookup against the one Standard.
    if w not in FIBRE:
        w = ''.join(c for c in unicodedata.normalize('NFKD', w) if not unicodedata.combining(c))
    cat = ('plant' if w in PLANT else 'animal' if w in ANIMAL else
           'syn' if w in SYN else 'semi' if w in SEMI else None)
    if cat is None and len(w) > 3 and w.endswith('s') and not w.endswith('ss'):
        # Brands write the plural as freely as the singular ('100% polyesters', 'lined in
        # leathers'). Resolving the singular keeps those rows readable instead of dropping a
        # composition the brand did publish. Only ever a fallback - the singular decides.
        s2 = w[:-1]
        cat = ('plant' if s2 in PLANT else 'animal' if s2 in ANIMAL else
               'syn' if s2 in SYN else 'semi' if s2 in SEMI else None)
    return cat


def _worst(cats):
    if 'animal' in cats: return 'animal'
    if 'syn' in cats:    return 'synthetic'
    if 'semi' in cats:   return 'semi'
    if 'plant' in cats:  return 'plant'
    return None


# "Latex-Spandex-Elastane-Free" / "spandex, elastane and latex free": a RUN of FIBRE words before
# "free" — negate every fibre in the run. The run may contain ONLY fibre words: 'wool mulesing-free'
# (mulesing is a farming practice, not a fibre) must NOT swallow the 'wool' that precedes it, and
# 'paraben-free' / 'cruelty free' are never fibre runs either.
_FWORDS = '|'.join(sorted((w for w in FIBRE if ' ' not in w), key=len, reverse=True))
_FREE_RUN = re.compile(r'\b((?:(?:%s)[\s,/&+-]+){1,5}(?:%s)?)[\s-]*free\b' % (_FWORDS, _FWORDS), re.I)

def _strip_neg(t):
    def repl_run(m):
        run = re.sub(r'[a-z]+', lambda w: ' ' if w.group(0).lower() in FIBRE else w.group(0), m.group(1), flags=re.I)
        return run + ' free'
    t = _FREE_RUN.sub(repl_run, t)
    def repl(m):
        for g in m.groups():
            if g and g in FIBRE: return ' '
        return m.group(0)
    return NEG.sub(repl, t)


# a % whose following word is a price, not a material ('50% off cotton', '30% sale')
PCT_STOP = re.compile(r'^(?:(?:off(?![\s-]?white))|sale|save|discount|login|shop|free|extra(?![\s-]+fine\b)|up|code|voucher)\b', re.I)
PCT_CONTEXT = re.compile(r'^\s*(?:less|more|lower|reduc(?:ed|tion)|saving|biodegradable|traceable|renewable|recyclable|carbon|water|energy|waste)\b', re.I)
PCT_APPROX = re.compile(r'(?:about|approx(?:imately)?|up\s+to|as\s+much\s+as|between|range)\s*$', re.I)
OMITTED_COMPONENT = re.compile(r'\b(?:excluding|except(?:ing)?|not\s+including)\s+(?:the\s+)?(?:trim|trims|lining|linings|thread|threads|decoration|decorations|coating|coatings|fill|filling|hardware)\b', re.I)
NONPLANT_COMPONENT = re.compile(r'\b(?:with|plus|and|including|features?)\s+(?:an?\s+)?(leather|suede|polyurethane|polyester|nylon|acrylic|polyamide|pvc|rubber|latex|viscose|rayon|modal|lyocell|tencel|wool|silk|cashmere|down|feather)\s+(trim|patch|coating|lining|membrane|fill|sole|panel|thread)\b', re.I)
EXTRA_COMPONENT_PCT = re.compile(r'\b(?:lining|shell|self|outer|upper|sole|trim|rib|fill|filling|padding|embroidery|fabric\s*\d*)\s*[:\-]?\s*[^.;]{0,90}?\d{1,3}(?:\.\d+)?\s*%', re.I)
PCT_NUM = re.compile(r'(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*%')
WORD = re.compile(r'[a-zà-ÿ]+')
# A share clause written WITHOUT '%' signs as a slash-ratio: '29 Hemp/69 Organic Cotton/2 Spandex'
# or '60 Cotton / 40 Polyester'. The numbers sit against the fibre word, so a promo, a price, a
# style number or a care ratio ('Ships in 2-4 days') never matches. Runs of 2-6 shares; each share
# must name a fibre; the shares are accepted only when they close on ~100, exactly like a % clause.
RATIO_SEG = re.compile(r'(?<!\d)\d{1,3}(?:\.\d+)?[\s-]+[a-zà-ÿ]+(?:[\s-]+[a-zà-ÿ]+){0,5}', re.I)

def visible(html):
    html = SCRIPT.sub(' ', html or '')
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))


def _majority(pairs):
    """Composition band. A garment is a single-fibre-class garment only when every stated share
    belongs to that class. Any cross-class composition remains blended; a 95/5 cotton/elastane
    garment must never become 100% plant merely because cotton is the majority."""
    shares, tot = {}, 0.0
    for n, fib in pairs:
        c = _cat(fib)
        if c:
            shares[c] = shares.get(c, 0) + n
            tot += n
    if not shares or tot <= 0:
        return None
    if len(shares) == 1:
        top = next(iter(shares))
        return 'synthetic' if top == 'syn' else top
    return 'blended'


def _ratio_clause(text):
    """Read a composition written WITHOUT '%' signs as slash shares: '29 Hemp/69 Organic Cotton/2
    Spandex' or '60 Cotton / 40 Polyester'. Each share must name a fibre, every share is a number
    against a fibre word (so a promo, a price, a style number or a care ratio like '2-4 days' never
    matches), and the shares are accepted only when they close on ~100 - exactly the evidence bar of
    a % clause, so a partial ratio is 'incomplete', never guessed. Returns (comp, pairs) or None."""
    candidates = []
    gap = re.compile(r'\s*/\s*')
    pos = 0
    while pos < len(text):
        m = RATIO_SEG.match(text, pos)
        if not m:
            pos += 1
            continue
        body = m.group(0)
        nm = re.match(r'\d{1,3}(?:\.\d+)?', body)
        num = float(nm.group(0))
        rest = body[nm.end():]
        fibre = fstart = None
        for w in WORD.finditer(rest):
            if _cat(w.group(0)):
                fibre, fstart = w.group(0), m.start() + nm.end() + w.start()
                break
        if not fibre:
            pos = m.end()
            continue
        candidates.append((m.start(), m.end(), num, fibre, fstart))
        pos = m.end()

    i = 0
    while i < len(candidates):
        j = i
        total = 0.0
        while j + 1 < len(candidates) and gap.fullmatch(text[candidates[j][1]:candidates[j + 1][0]]):
            total += candidates[j][2]
            j += 1
        total += candidates[j][2]
        run = candidates[i:j + 1]
        if len(run) >= 2 and abs(total - 100.0) <= 0.05:
            comp = re.sub(r'\s+', ' ', text[run[0][0]:run[-1][4] + len(run[-1][3])]).strip(' ,;:/')
            return comp, [(n, fib) for _, _, n, fib, _ in run]
        i = j + 1
    return None


def _non_composition_percentage(text, match):
    """Apply identical promo/statistic/range filters to parsing and total validation."""
    return bool(
        PCT_STOP.search(text[match.end():match.end() + 24].lstrip())
        or PCT_CONTEXT.search(text[match.end():match.end() + 40])
        or PCT_APPROX.search(text[max(0, match.start() - 24):match.start()])
        or re.search(r'\d\s*[-–]\s*$', text[max(0, match.start() - 8):match.start()])
    )


def _composition(text, label_ok=True):
    """The fibres the brand PUBLISHES as this product's composition — and nothing else.
    Order: the percentage list ('95% cotton, 5% elastane' / 'Cotton 95%, Elastane 5%'), then (only
    when label_ok) an explicit composition/material field ('Material: Organic Linen', 'Made from
    linen'). A fibre word that sits OUTSIDE that statement (a leather trim, a size chart, a
    'spandex-free' tag, a style name) is never read.

    `label_ok=False` reads ONLY percentages — used on the shared PAGE HTML, where a bare fibre
    label is often page chrome (a nav 'Fabric' menu, a 'linen / wool / cotton' filter, a footer)
    that is NOT this product's composition: Linen Fox's fabric-type menu made 583 linen garments
    read 'linen, wool, cotton' -> animal. A stated percentage is the one per-product signal.

    Every 'N%' is paired with ONE fibre — the word immediately before the number ('cotton 95%',
    'organic cotton 95%') or the first fibre after it ('95% cotton', '95% organic cotton'). A fibre
    sitting between two %s belongs to the % that FOLLOWS it, so '50% cotton 50% polyester' never
    pairs the middle 'cotton' to both numbers.

    Returns (kind, composition, band) — ONE read. The composition is the brand's stated clause
    ('95% Cotton, 5% Elastane'), so the caller can NEVER derive the band from one read and print
    the composition from a different one. kind: 'pct' | 'label' | None."""
    norm = _strip_neg(_deambig(text))
    low = norm.lower()
    # A component-itemised garment is read BEFORE the percentage pass: each component carries its
    # own 100%-based clause, so a flat pass would pair them together and total 200%.
    combined = _combine_components(norm)
    if combined:
        ccomp, cband, _n = combined
        return 'components', ccomp, cband
    pairs, span_lo, span_hi, claimed = [], None, None, []
    seen = set()
    def _free(lo, hi):
        return not any(lo < ch and hi > cl for cl, ch in claimed)
    for m in PCT_NUM.finditer(low):
        if pairs and m.start() - span_hi > 40:
            break                                # a 2nd % 40+ chars away is a DIFFERENT clause
                                                 # (care/wash text), not this composition list
        n = float(m.group(1))
        if _non_composition_percentage(low, m):
            continue
        if n < 0:
            return 'invalid', norm, None
        # fibre BEFORE the number: 'cotton 95%' (the word touching the number, so it must BE a
        # fibre — '100% Capri', a brand phrase, never pairs). Belongs to this % only if the fibre
        # wasn't already claimed by an earlier % ('50% cotton 50%' keeps cotton for the 2nd %).
        pre = re.search(r'([a-zà-ÿ]+)\s*$', low[:m.start()])
        before = pre.group(1) if pre else ''
        if _cat(before):
            # the fibre must belong to the '%' — NOT the last word of a previous sentence that
            # happens to be a fibre ('...consider sizing down 85% Organic Cotton' -> 'down' is not
            # the fibre of 85%). Accept only when the fibre is clause-initial or has at most ONE
            # modifier word before it ('Organic cotton 95%'); separators reset the count.
            pos, mid = pre.start(), 0
            while pos > 0:
                w2 = re.search(r'([a-zà-ÿ]+)\s*$', low[:pos])
                if not w2:
                    break
                if _cat(w2.group(1)):
                    break
                mid += 1
                if mid >= 2:
                    break
                pos = w2.start()
            if mid >= 2:
                before = ''
        else:
            before = ''
        if before and _free(pre.start(), m.start()):
            if (n, before) in seen:
                break                        # the SAME %-fibre again = a duplicated spec clause
            seen.add((n, before))
            pairs.append((n, before))
            claimed.append((pre.start(), m.start()))
            if span_lo is None: span_lo = pre.start()
            span_hi = m.end()
            continue
        # fibre AFTER the number: '95% cotton' — one of the first 6 words, so modifier runs like
        # '50% post consumer recycled wool' pair, but a fibre 6+ words later ('100% Capri signature
        # charm Upper in reinforced Linen' — a brand phrase + a separate clause) is never a composition.
        after = None
        for i, w in enumerate(WORD.finditer(re.split(r'[0-9%]', low[m.end():m.end() + 80], maxsplit=1)[0])):
            if i > 4:
                break
            if _cat(w.group(0)):
                after = w
                break
        if after and _free(m.start(), m.end() + after.end()):
            if (n, after.group(0)) in seen:
                break                        # the SAME %-fibre again = a duplicated spec clause
            seen.add((n, after.group(0)))
            pos = m.end() + after.start()
            pairs.append((n, after.group(0)))
            claimed.append((m.start(), m.end() + after.end()))
            if span_lo is None: span_lo = m.start()
            span_hi = pos + len(after.group(0))
            continue
    if pairs:
        # Unknown quantified terms must not disappear behind an otherwise complete 100% clause.
        extra_tail = re.split(r"[.;!?]|\b(?:care|washing|delivery)\b", norm[span_hi:span_hi + 40], maxsplit=1, flags=re.I)[0]
        prefix_start = max(0, span_lo - 40)
        prefix = norm[prefix_start:span_lo]
        boundaries = list(re.finditer(r'[;!?]|(?<!\d)\.(?!\d)|\b(?:composition|materials?|fabric)\s*:', prefix, re.I))
        clause_start = prefix_start + boundaries[-1].end() if boundaries else prefix_start
        clause = norm[clause_start:span_hi] + extra_tail
        numbers = [float(m.group(1)) for m in PCT_NUM.finditer(clause)
                   if not _non_composition_percentage(clause.lower(), m)]
        if len(numbers) > len(pairs):
            return ('invalid' if sum(numbers) > 100.05 else 'incomplete'), clause.strip(), None
    # A composition is evidence only when the captured fibre shares account for the whole item.
    # Reject isolated percentages ("95% cotton" with the other 5% missing), marketing statistics,
    # and truncated clauses instead of silently treating the visible majority as the full garment.
    if pairs and sum(n for n, _ in pairs) > 100.05:
        return 'invalid', re.sub(r'\s+', ' ', norm[span_lo:span_hi]).strip(), None
    if pairs and abs(sum(n for n, _ in pairs) - 100.0) <= 0.05:
        comp = re.sub(r'\s+', ' ', norm[span_lo:span_hi]).strip(' ,;:/')
        tail = norm[span_hi:span_hi+140]
        cover = re.match(r'\s*(?:(?:knitted|knit|woven)\s+)?cover\s+fabric\b', tail, re.I)
        if cover:
            # A complete cover declaration says nothing about the pad/fill.
            return 'incomplete', comp + cover.group(0), None
        omitted = OMITTED_COMPONENT.search(tail)
        if omitted:
            return 'incomplete', comp + ' ' + omitted.group(0), None
        # A second component appeared but the component pass above did not accept it: some
        # component is partial or does not close on 100%. Component weights are not published, so
        # these percentages still cannot form one valid garment composition. Never silently accept
        # the first component.
        extra = EXTRA_COMPONENT_PCT.search(tail)
        if extra:
            return 'incomplete', (comp + '; ' + extra.group(0)).strip(), None
        return 'pct', comp, _majority(pairs)
    # A '15% The Hemp' promo caught by the %-loop is not a full composition. A slash-ratio clause
    # ('29 Hemp/69 Organic Cotton/2 Spandex', no % signs, adds to ~100) is a complete published
    # composition - it outranks any incomplete %-catch, never a full one.
    ratio = _ratio_clause(low)
    if ratio:
        comp, rpairs = ratio
        return 'pct', comp, _majority(rpairs)
    if pairs:
        comp = re.sub(r'\s+', ' ', norm[span_lo:span_hi]).strip(' ,;:/')
        return 'incomplete', comp, None
    # Bare "Material: cotton" statements are not full compositions. They may describe a shell,
    # main fabric or marketing material while omitting elastane, lining, trim or other fibres.
    return None, '', None


# A labelled component requires a colon/dash or an immediately following percentage: 'Lining: 100% polyester' is a component
# specification, while 'the lining is cut on the bias' is copy.
COMP_LABEL = re.compile(r'\b(?:shell|lining|linings|outer|upper|body|sleeve|sole|trim|trims|'
                        r'fill|filling|padding|embroidery|rib|contrast|pocketing|outsole|insole|self|straps?|hardware|coating|insulation|fabric\s*\d*)\b\s*(?::|-(?=\s|\d)|(?=\d+(?:\.\d+)?\s*%))', re.I)


def _seg_pairs(seg):
    """(percentage, fibre) pairs inside one composition clause, same fibre-preference rules as
    composition_shares(). Used to keep the REAL fibre names when components are combined."""
    text = _strip_neg(_deambig(seg or '')).lower()
    marks = list(PCT_NUM.finditer(text))
    pairs, claimed = [], set()
    for idx, m in enumerate(marks):
        n = float(m.group(1))
        stop = marks[idx+1].start() if idx + 1 < len(marks) else min(len(text), m.end() + 80)
        start = marks[idx-1].end() if idx else 0
        pre = re.search(r'([a-zà-ÿ]+)\s*$', text[start:m.start()])
        if pre and _cat(pre.group(1)) and start + pre.start() not in claimed:
            pairs.append((n, pre.group(1)))
            claimed.add(start + pre.start())
            continue
        for i, word in enumerate(WORD.finditer(text[m.end():stop])):
            if i > 4:
                break
            position = m.end() + word.start()
            if _cat(word.group(0)) and position not in claimed:
                pairs.append((n, word.group(0)))
                claimed.add(position)
                break
    return pairs


def component_declarations(text):
    """Keep each published component clause separate, including an unnamed main fabric."""
    marks = list(COMP_LABEL.finditer(text or ''))
    if not marks:
        return []
    declarations = []
    head = text[:marks[0].start()].strip(' ,;:-')
    if head and PCT_NUM.search(head) and not any(
            _non_composition_percentage(head.lower(), m) for m in PCT_NUM.finditer(head)):
        declarations.append(('main', head))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        label = re.sub(r'[:\-]\s*$', '', mark.group()).strip().lower()
        declarations.append((label, text[mark.end():end].strip(' ,;:-')))
    return declarations


def _combine_components(text):
    """Recognize a component declaration without inventing garment weights or flattening it."""
    parts = component_declarations(text)
    if len(parts) < 2:
        return None
    pairs = [pair for _, clause in parts for pair in _seg_pairs(clause)]
    band = _majority(pairs) if pairs and all(_clause_shares(clause) for _, clause in parts) else None
    return (text.strip(), band or 'unclear', len(parts))


def _clause_shares(comp):
    text = _strip_neg(_deambig(comp or '')).lower()
    pairs = _seg_pairs(text)
    percentages = list(PCT_NUM.finditer(text))
    if percentages and len(pairs) != len(percentages):
        return {}
    if not pairs:
        ratio = _ratio_clause(text)
        pairs = ratio[1] if ratio else []
    if (not pairs or any(n < 0 or n > 100 for n, _ in pairs)
            or abs(sum(n for n, _ in pairs) - 100.0) > 0.05):
        return {}
    out = {'plant': 0.0, 'animal': 0.0, 'synthetic': 0.0, 'semi': 0.0}
    for n, fibre in pairs:
        category = _cat(fibre)
        if category:
            out['synthetic' if category == 'syn' else category] += n
    return out


def composition_shares(comp, component_weights=None):
    """Quantify complete evidence only. Component vectors require explicit weights or identity.

    Weights, when supplied by the matched product source, are percentages of the whole product.
    A class vector invariant across all complete components needs no assumed physical weights.
    """
    parts = component_declarations(comp or '')
    if len(parts) < 2:
        return _clause_shares(parts[0][1] if parts else comp)
    vectors = [_clause_shares(clause) for _, clause in parts]
    if not all(vectors):
        return {}
    if component_weights is not None:
        if not isinstance(component_weights, dict) or set(component_weights) != {label for label, _ in parts}:
            return {}
        try:
            weights = [float(component_weights[label]) for label, _ in parts]
        except (ValueError, TypeError):
            return {}
        if any(not math.isfinite(w) or w < 0 or w > 100 for w in weights) or abs(sum(weights) - 100) > 0.05:
            return {}
        return {key: sum(v[key] * w / 100 for v, w in zip(vectors, weights)) for key in vectors[0]}
    if all(vector == vectors[0] for vector in vectors[1:]):
        return dict(vectors[0])
    return {}


def _labelled(low):
    """Composition read from an EXPLICIT 'Composition:/Material:/Fabric:/Fibre content:' label,
    wherever it sits on the page. The label is unambiguous — a related-products carousel shows names
    and prices, never a composition field — so it needs NO related-products cut. This fixes pages
    that render a 'Shop the look' section BEFORE the product's own spec (No Nasties put its
    'Composition 100% organic cotton' after that heading, so a blunt cut hid it). Returns the
    composition ONLY with a stated percentage — a bare 'Fabric: linen/wool/cotton' menu is chrome,
    not composition.

    A label is accepted only if the composition comes from the capture's FIRST '%' — a capture whose
    first % has no fibre ('Materials: 100% biodegradable, cruelty free, no toxic chemicals 100%
    cruelty-free wool…' is a brand blurb, the real spec 'Material Main Fabric: 100% Wool' is its own
    label) is prose before the statement, so the NEXT label is tried."""
    components = _combine_components(low)
    if components:
        return "components", components[0], components[1]
    candidates = []
    for m in re.finditer(r'(?=(' + COMP.pattern + r'))', low, re.I):
        whole = re.sub(r'\s+', ' ', m.group(1))
        # 'Shell' is both a valid single-clause label and a component label. When the page goes on
        # to label a second component, the shell alone is the WORSE read, so skip it and let the
        # component pass read the whole itemised garment.
        if [c for c in COMP_LABEL.finditer(low[m.start():m.start() + 200])][1:]:
            continue
        kind, comp, band = _composition(whole)
        if kind == 'pct' and comp:
            whole_pct = PCT_NUM.search(whole)
            idx = whole.find(comp)
            if idx < 0:
                continue
            if whole_pct and not (whole_pct.start() >= idx and whole_pct.end() <= idx + len(comp)):
                continue                     # a % BEFORE the composition is unpaired -> prose blurb
            candidates.append((kind, comp, band))
    if candidates:
        first = candidates[0]
        if any(_composition_conflict(first[1], first[2], other[1], other[2]) for other in candidates[1:]):
            return 'conflict', ' [conflicts with] '.join(c[1] for c in candidates), None
        return first
    return None, '', None


# A material counts as this product's own only when a CONSTRUCTION cue introduces it and no other
# product's noun follows it. 'tote bag in woven nylon' / 'with two slim leather handles' are the
# product's own materials. 'with our wool coat' / 'with the linen blazer' are a DIFFERENT garment.
PROSE_CUE = re.compile(r'\b(?:in|with|of|from|made|crafted|using|constructed|featuring|contains)\b', re.I)
FOREIGN_NOUN = re.compile(r'\b(?:coat|jacket|blazer|cardigan|sweater|jumper|knitwear|dress|skirt|'
                          r'shorts?|trousers|pants|shirt|blouse|tee|t-?shirt|top|gown|jumpsuit|romper|'
                          r'scarf|beanie|hat|socks?|gloves|belt|bag|tote|backpack|purse|clutch|'
                          r'wallet|shoes?|boots?|sandals?|sneakers?)\b', re.I)
# A PART of this product is fine: it names the same object.
PART_NOUN = re.compile(r'\b(?:handles?|trim|trimming|body|shell|lining|straps?|bands?|panels?|'
                       r'collar|cuffs?|hem|sleeves?|pockets?|hardware|buttons?|zips?|zipper|'
                       r'overlay|casing|base)\b', re.I)
# A fabric-structure noun is never another product, so a material qualifying it is attributable
# even with no construction cue ('cotton gabardine', 'silk charmeuse').
PROSE_FABRIC = re.compile(r'\b(?:gabardine|charmeuse|seersucker|corduroy|boucl[eé]|herringbone|'
                          r'poplin|twill|canvas|denim|chambray|chiffon|satin|velvet|jersey|'
                          r'organza|taffeta|crepe|muslin|gauze|voile|tulle|pinstripe|plaid|'
                          r'tartan|knit|woven|weave|brocade|gingham|lawn)\b', re.I)
# Derived from the category sets themselves so the prose vocabulary can never drift from them.
# A hand-listed subset had already drifted: 'lambswool' is in ANIMAL but was missing here, and
# \bwool\b cannot match inside 'lambswool'. Longest first so 'polyamide' wins over 'poly'.
_prose_words = sorted({w for w in (FIBRE | MATERIAL_NON_FIBRE) if ' ' not in w and w.isalpha()},
                      key=len, reverse=True)
PROSE_MATERIAL = re.compile(r'\b(' + '|'.join(_prose_words) + r')\b', re.I)
APPEARANCE_ONLY = re.compile(r'\b(?:silk[\s-]?(?:like|y|soft|feel|finish|effect)|'
    r'cashmere[\s-]?(?:soft|like|feel|finish|effect)|wool[\s-]?like|leather[\s-]?look|'
    r'suede[\s-]?effect|suede[\s-]?look|denim[\s-]?(?:look|like)|linen[\s-]?inspired|'
    r'softness\s+of\s+cashmere)\b', re.I)
COMPONENT_MATERIAL_LABEL = re.compile(
    r'\b(body|shell|lining|outer|upper|sole|trim|filling|fill|padding|insulation|'
    r'straps?|hardware|coating)\s*[:\-]\s*', re.I)


def prose_materials(description):
    """Materials a PER-PRODUCT description states the product is MADE of, with no percentages.

    Some brands publish no composition field at all and describe the material instead: The Row's
    'tote bag in woven nylon with two slim leather handles' is a complete, unambiguous statement of
    what that bag is made of, and refusing it left a real product permanently unscorable.

    The input is the caller's responsibility and must be a per-product description from a
    structured catalogue record - never scraped page HTML. That distinction is the whole safety
    argument: a product description cannot contain a navigation menu, and page chrome is exactly
    what produced the 'fabric menu' reads this engine already refuses elsewhere. Materials are
    deliberately NOT taken from the title, so a product NAME can never become a composition.

    Discernment rules:
      - a CONSTRUCTION cue must introduce the material ('in woven nylon', 'made from cotton'), or
        the material must qualify a fabric-structure noun ('cotton gabardine');
      - a material followed by another product's noun is refused ('with our wool coat');
      - ALL OR NOTHING: if any material cannot be attributed, none are used. Half a material list
        is worse than none - it banded a wool coat as 100% plant from its lining alone.

    Leather and suede count as materials in their own right: FULL_SCAN_RULES.md classes them as
    animal, and a leather jacket, skirt or tote is genuinely made of leather. NONTEXTILE is for
    metal, ceramic, wood and plastic, not for hide. Breading animal is also the conservative
    direction - the worst class cannot flatter a score.

    Returns (composition, band) or None. No percentages or relative material weights are inferred;
    the returned composition explicitly says percentages were not stated.
    """
    text = re.sub(r'\s+', ' ', _strip_neg(_deambig(APPEARANCE_ONLY.sub(' ',
        re.sub(r'<[^>]+>', ' ', description or ''))))).strip()
    if not text:
        return None
    text = ' '.join(re.split(r'(?<=[.!?])\s+', text)[0:3])   # the product summary, not the marketing tail
    found, seen, ambiguous = [], set(), False
    for m in PROSE_MATERIAL.finditer(text):
        w = m.group(1).lower()
        cat = _cat(w)
        if not cat or w in seen:
            continue
        back = text[max(0, m.start() - 28):m.start()]
        cue = None
        for cm in PROSE_CUE.finditer(back):
            cue = cm                                    # nearest cue wins
        if not cue:
            if PROSE_FABRIC.match(text[m.end():].lstrip()):
                pass                                    # attributable on the strength of the noun
            else:
                ambiguous = True
                continue
        elif FOREIGN_NOUN.search(back[cue.end():]):
            ambiguous = True
            continue                                    # another product inside the cue's scope
        ahead = text[m.end():m.end() + 28].lstrip()
        if FOREIGN_NOUN.match(ahead) and not PART_NOUN.match(ahead):
            ambiguous = True
            continue                                    # the material heads a DIFFERENT product
        seen.add(w)
        found.append((w, 'syn' if cat == 'syn' else cat))
    if not found or ambiguous:
        return None
    classes = {c for _, c in found}
    only = next(iter(classes)) if len(classes) == 1 else None
    band = ('blended' if only is None
            else 'synthetic' if only == 'syn' else only)
    materials = []
    for word, _category in found:
        qualifier = re.search(r'\b(?:organic|recycled|regenerated|reclaimed)\s+$', text[:text.lower().find(word)], re.I)
        material = f'{qualifier.group(0).strip()} {word}' if qualifier else word
        materials.append(material)
    return (' and '.join(materials) + ' (percentages not stated)', band)


def explicit_description_materials(description):
    """Retain a later explicit construction claim without inferring percentages.

    Long product descriptions can put 'Made of ...' after the introductory
    sentences read by prose_materials. Other named fibres must not disappear.
    """
    text = re.sub(r'\s+', ' ', _strip_neg(_deambig(APPEARANCE_ONLY.sub(' ', description or '')))).strip()
    statements = [sentence for sentence in re.split(r'(?<=[.!?])\s+', text)
                  if re.match(r'^(?:made|crafted|constructed)\s+(?:of|from|in|using)\b', sentence, re.I)]
    if not statements or len(statements) > 3:
        return None
    declaration = ' '.join(statements)
    all_terms = {m.group(1).lower() for m in PROSE_MATERIAL.finditer(text)}
    declaration_terms = {m.group(1).lower() for m in PROSE_MATERIAL.finditer(declaration)}
    if not all_terms <= declaration_terms:
        return None
    return prose_materials(declaration)


def component_materials(text):
    """Read explicit, product-owned component labels without assigning component weights."""
    clean = re.sub(r'\s+', ' ', _strip_neg(_deambig(APPEARANCE_ONLY.sub(' ',
        re.sub(r'<[^>]+>', ' ', text or ''))))).strip()
    labels = list(COMPONENT_MATERIAL_LABEL.finditer(clean))
    if not labels:
        return None
    parts = []
    categories = set()
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(clean)
        segment = re.split(r'[.;]', clean[label.end():end], maxsplit=1)[0]
        matches = list(PROSE_MATERIAL.finditer(segment))
        if not matches:
            return None
        material_names = []
        for match in matches:
            word = match.group(1).lower()
            category = _cat(word)
            if category:
                categories.add('synthetic' if category == 'syn' else category)
                if word not in material_names:
                    material_names.append(word)
        if not material_names:
            return None
        parts.append(f'{label.group(1).lower()}: {" and ".join(material_names)}')
    band = next(iter(categories)) if len(categories) == 1 else 'blended'
    return '; '.join(parts) + ' (percentages not stated)', band


def _composition_conflict(first_composition, first_band, second_composition, second_band):
    first_parts, second_parts = component_declarations(first_composition), component_declarations(second_composition)
    if len(first_parts) > 1 or len(second_parts) > 1:
        first_map, second_map = dict(first_parts), dict(second_parts)
        if set(first_map) != set(second_map) or len(first_map) != len(first_parts) or len(second_map) != len(second_parts):
            return True
        return any(_composition_conflict(first_map[label], first_band, second_map[label], second_band)
                   for label in first_map)
    first_materials = _quantified_materials(first_composition)
    second_materials = _quantified_materials(second_composition)
    if first_materials and second_materials:
        return any(abs(first_materials.get(term, 0) - second_materials.get(term, 0)) > 0.05
                   for term in first_materials.keys() | second_materials.keys())
    first_shares = composition_shares(first_composition)
    second_shares = composition_shares(second_composition)
    if first_shares and second_shares:
        return any(abs(first_shares.get(key, 0) - second_shares.get(key, 0)) > 0.05
                   for key in ('plant', 'animal', 'synthetic', 'semi'))
    return first_band != second_band


def _quantified_materials(composition):
    pairs = _seg_pairs(composition)
    if not pairs or abs(sum(value for value, _term in pairs) - 100.0) > 0.05:
        ratio = _ratio_clause(_strip_neg(_deambig(composition or '').lower()))
        pairs = ratio[1] if ratio else []
    if not pairs or abs(sum(value for value, _term in pairs) - 100.0) > 0.05:
        return {}
    materials = {}
    for value, term in pairs:
        canonical = MATERIAL_TERMS.get(term.lower(), (term.lower(), None))[0]
        materials[canonical] = materials.get(canonical, 0.0) + value
    return materials


def _strong_source_conflict(composition, band, page_text):
    page = _deambig(re.sub(r'\s+', ' ', page_text or ''))
    kind, page_composition, page_band = _labelled(_strip_neg(page.lower()))
    if kind == 'conflict':
        return page_composition
    if kind in ('pct', 'components') and page_composition and _composition_conflict(
            composition, band, page_composition, page_band):
        return page_composition
    return None


def _complete_partial_from_page(partial, page_text, other_text=''):
    """A complete page declaration may extend matching, quantified partial data."""
    pairs = _seg_pairs(partial)
    if (not pairs or len(pairs) != len(list(PCT_NUM.finditer(partial)))
            or not 0 < sum(value for value, _ in pairs) < 99.95
            or COMP_LABEL.search(partial) or OMITTED_COMPONENT.search(partial)):
        return None
    page = _deambig(_without_packaging_section(page_text))
    kind, complete, band = _labelled(_strip_neg(page.lower()))
    if kind == 'conflict':
        return 'unclear', complete, 'evidence-conflict'
    if kind is None:
        cut = _STOP.search(page)
        kind, complete, band = _composition(page[:cut.start()] if cut else page, label_ok=False)
    if kind == 'invalid':
        # A deeper read with an impossible declared total is an evidence error,
        # not permission to retain the earlier partial declaration as its only
        # outcome. Preserve the invalid clause and the normal conflict gate.
        return 'unclear', complete, 'invalid-composition-total'
    if kind != 'pct':
        return None
    materials = _quantified_materials(complete)
    other_kind, other_comp, other_band = _composition(other_text)
    if other_kind in {'pct', 'components'} and _composition_conflict(complete, band, other_comp, other_band):
        return 'unclear', f'{complete} [conflicts with product data: {other_comp}]', 'evidence-conflict'
    if other_kind == 'incomplete':
        other_pairs = _seg_pairs(other_comp)
        if (not other_pairs or len(other_pairs) != len(list(PCT_NUM.finditer(other_comp)))
                or not 0 < sum(value for value, _ in other_pairs) < 99.95
                or COMP_LABEL.search(other_comp) or OMITTED_COMPONENT.search(other_comp)):
            return None
        pairs += other_pairs
    for value, term in pairs:
        canonical = MATERIAL_TERMS.get(term.lower(), (term.lower(), None))[0]
        if abs(materials.get(canonical, 0) - value) > 0.05:
            return 'unclear', f'{partial} [conflicts with page: {complete}]', 'evidence-conflict'
    return band, complete, 'page'


def classify(title, handle, page_text, body_html='', tags='', own_description=None, product_material=None):
    """Read ONLY the composition the brand publishes, and bucket by it. No inference, no assumption:
    a fibre word that is not part of the published composition (a trim, a tag, a size chart, a
    product name) is never counted.

    Returns (band, composition, how) — the band and the composition come from the SAME read, so
    they can never disagree. `composition` is the exact stated composition clause; '' when the brand
    publishes none (band is then 'unclear' or 'nontextile'). A cross-class composition is 'blended';
    exact category percentages are calculated separately from the same accepted clause."""
    from product_evidence import product_text
    title = title or ''
    body_html = product_text(body_html)
    page_text = product_text(page_text)
    own_description = product_text(own_description)
    product_material = product_text(product_material)
    # TAGS ARE NOT A COMPOSITION SOURCE. They are merchandising taxonomy, and taxonomy leaks
    # material words: The Row tags a belt "DIV_MEN'S SMALL LEATHER GOODS", which put 'leather' in
    # the fabric field of a SKU the brand never gave a composition for - inflating the count of
    # disclosed products and putting a department name into a score. Only the product's own title
    # and body text are read here. Tags still feed flag_notes(), which reports rather than scores.
    own = _deambig(_without_packaging_section(f"{body_html or ''} . {own_description or ''}"))
    own_stop = _STOP.search(own)
    if own_stop: own = own[:own_stop.start()]

    scope, _scope_reason = product_scope(title, handle)
    if scope == 'accessory':
        return 'accessory', '', 'accessory'
    if scope == 'nontextile':
        return 'nontextile', read_material(own + ' ' + (page_text or '')), 'nontextile'

    # bags / wallets / homeware / candles / jewellery have no clothing fabric to rate — but still name
    # the real material (metal, ceramic, wood, leather, plastic) so the SKU table is never blank.
    # A product that names a fabric structure (wildcard: even a 'yarn' component product like
    # 'Paper Yarn Cotton Stretch Twill Fabric') is a textile and is rated by what it states.
    if NONTEXTILE.search(title + ' ' + (handle or '')) and not APPAREL.search(title) and not FABRIC.search(title + ' ' + (handle or '')):
        return 'nontextile', read_material(own + ' ' + (page_text or '')), 'nontextile'

    if product_material:
        kind, comp, band = _composition(str(product_material))
        if kind == 'components':
            body_kind, body_comp, body_band = _composition(own)
            if body_kind in ('pct', 'components') and _composition_conflict(comp, band, body_comp, body_band):
                return 'unclear', f'{comp} [conflicts with product data: {body_comp}]', 'evidence-conflict'
            conflict = _strong_source_conflict(comp, band, page_text)
            if conflict:
                return 'unclear', f'{comp} [conflicts with page: {conflict}]', 'evidence-conflict'
            return band, comp, 'components'
        if kind == 'pct':
            body_kind, body_comp, body_band = _composition(own)
            if body_kind in ('pct', 'components') and _composition_conflict(comp, band, body_comp, body_band):
                return 'unclear', f'{comp} [conflicts with product data: {body_comp}]', 'evidence-conflict'
            conflict = _strong_source_conflict(comp, band, page_text)
            if conflict:
                return 'unclear', f'{comp} [conflicts with page: {conflict}]', 'evidence-conflict'
            return band, comp, 'product-material'
        if kind == 'invalid':
            return 'unclear', comp, 'invalid-composition-total'
        if kind == 'incomplete':
            return _complete_partial_from_page(comp, page_text, own) or ('unclear', comp, 'incomplete')
        if kind is None and any(not _non_composition_percentage(str(product_material).lower(), match)
                                for match in PCT_NUM.finditer(str(product_material).lower())):
            # An explicitly quantified but unknown material cannot disappear
            # merely because another source supplies a complete known blend.
            return 'unclear', str(product_material), 'incomplete'
        own_kind, own_comp, own_band = _composition(own)
        own_how = 'own'
        if own_kind not in ('pct', 'components'):
            own_kind, own_comp, own_band = _labelled(page_text.lower())
            own_how = 'page'
        if own_kind in ('pct', 'components'):
            summary = {MATERIAL_TERMS.get(m.group(1).lower(), (m.group(1).lower(), None))[0]
                       for m in PROSE_MATERIAL.finditer(str(product_material))}
            declared = {MATERIAL_TERMS.get(m.group(1).lower(), (m.group(1).lower(), None))[0]
                        for m in PROSE_MATERIAL.finditer(own_comp)}
            if summary and summary <= declared:
                conflict = _strong_source_conflict(own_comp, own_band, page_text)
                if conflict:
                    return 'unclear', f'{own_comp} [conflicts with page: {conflict}]', 'evidence-conflict'
                return own_band, own_comp, 'components' if own_kind == 'components' else own_how
            if summary:
                return 'unclear', f'{product_material} [conflicts with product data: {own_comp}]', 'evidence-conflict'
        component_read = component_materials(str(product_material))
        if component_read:
            comp, band = component_read
            return band, comp, 'component-materials'
        prose = prose_materials('Made from ' + str(product_material))
        if prose:
            comp, band = prose
            return band, comp, 'product-material'

    # 1) the published composition in the product's OWN data
    kind, comp, band = _composition(own)
    if kind == 'components':
        conflict = _strong_source_conflict(comp, band, page_text)
        if conflict:
            return 'unclear', f'{comp} [conflicts with page: {conflict}]', 'evidence-conflict'
        return band, comp, 'components'
    if kind == 'pct':
        conflict = _strong_source_conflict(comp, band, page_text)
        if conflict:
            return 'unclear', f'{comp} [conflicts with page: {conflict}]', 'evidence-conflict'
        component = NONPLANT_COMPONENT.search(own)
        if component:
            return 'blended', comp + '; ' + component.group(0) + ' (percentage not stated)', 'component'
        return band, comp, 'own'
    if kind == 'invalid':
        return 'unclear', comp, 'invalid-composition-total'
    if kind == 'incomplete':
        return _complete_partial_from_page(comp, page_text) or ('unclear', comp, 'incomplete')

    component_read = component_materials(own)
    if component_read:
        comp, band = component_read
        return band, comp, 'component-materials'

    # 1b) some sites publish the composition as a TAG ("100% Wool"), so tags are still consulted -
    # but ONLY when they carry an explicit percentage. A bare material word in a tag is taxonomy,
    # not a composition: The Row tags a belt "DIV_MEN'S SMALL LEATHER GOODS", and reading that as a
    # composition invented a disclosure AND put a department name in the fabric field. Requiring a
    # percentage keeps the real disclosures and rejects the taxonomy.
    if tags:
        tkind, tcomp, tband = _composition(_deambig(re.sub(r'<[^>]+>', ' ', tags)))
        if tkind == 'components':
            return tband, tcomp, 'components'
        if tkind == 'pct':
            return tband, tcomp, 'own'

    # 2) the published composition on the product PAGE (some brands put it in a tab, not the feed)
    page = _deambig(re.sub(r'\s+', ' ', page_text or ''))
    page = _without_packaging_section(page)
    plow = _strip_neg(page.lower())
    # 2a) an EXPLICIT composition/material label anywhere on the page — unambiguously THIS product's
    kind, comp, band = _labelled(plow)
    if kind == 'conflict':
        return 'unclear', comp, 'evidence-conflict'
    if kind == 'components':
        return band, comp, 'components'
    if kind == 'pct':
        return band, comp, 'page'
    if kind == 'invalid':
        return 'unclear', comp, 'invalid-composition-total'
    if kind == 'incomplete':
        return 'unclear', comp, 'incomplete'
    # 2b) otherwise a bare 'N% fibre' (percentages ONLY — no bare page label, which is often nav/
    #     filter chrome), and only BEFORE any related-products section
    cut = _STOP.search(page)
    kind, comp, band = _composition(page[:cut.start()] if cut else page, label_ok=False)
    if kind == 'components':
        return band, comp, 'components'
    if kind == 'invalid':
        return 'unclear', comp, 'invalid-composition-total'
    if kind == 'incomplete':
        # Without this branch a partial composition returned band=None, which reached the scanner
        # as an unknown band and was relabelled 'invalid-classification' - implying a malfunction
        # where the reader had simply declined to band a composition it could not complete.
        return 'unclear', comp, 'incomplete'
    if kind:
        return band, comp, 'page'

    # 3) no composition statement anywhere. If the caller supplied a PER-PRODUCT description from a
    # structured catalogue record, read what it says the product is MADE of. Scoped to that field
    # alone: page text and the product name can never supply a material.
    if own_description:
        description = _without_packaging_section(own_description)
        prose = prose_materials(description) or explicit_description_materials(description)
        if prose:
            pcomp, pband = prose
            return pband, pcomp, 'prose-materials'

    # 4) still nothing -> name the real material where the brand states one
    if NONTEXTILE.search(title + ' ' + (handle or '')):
        return 'nontextile', read_material(own + ' ' + page), 'nontextile'
    # No composition was published, so none is reported. A bare material word in a description with
    # no percentage ("a leather belt", "with packable hood, interior snaps") is not a composition
    # statement, and attaching one to a row we are calling undisclosed asserts something the brand
    # never said. It also leaked into the report as a material for 124 SKUs. The row stays
    # undisclosed and the SKU table shows no fabric, which is the honest state.
    return 'unclear', '', 'none'
