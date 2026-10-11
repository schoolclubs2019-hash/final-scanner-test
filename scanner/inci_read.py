#!/usr/bin/env python3
"""Throne Standard INCI reader — the beauty/skincare analogue of the food ingredient reader.
Classifies each cosmetic ingredient as natural (raw plant / mineral), synthetic (petrochemical,
sulfate, silicone, PEG, synthetic preservative/fragrance/colour — an isolate, not a whole ingredient),
or animal. It also REJECTS the non-ingredient garbage the previous beauty scan swallowed off Shopify
product pages (price/options/customer JSON, marketing tags like "Vegan", "Orangutan Safe"), so the
natural% is counted over REAL ingredients only.

    classify_inci(name)  -> 'natural' | 'synthetic' | 'animal' | 'skip' | 'unknown'
    score_inci(product, ingredients_text_or_list) -> canonical per-SKU record (beauty schema-compatible)
"""
import re, os

HERE = os.path.dirname(os.path.abspath(__file__))

# --- Biodizionario cosmetic-ingredient catalog: INCI name -> hazard 0 (safe/natural) .. 4 (synthetic
#     /harmful). The authoritative lookup that replaces regex guessing; ~5000 exact names + substring
#     patterns (peg-, siloxane, glycol -> flagged). https://github.com/costajob/inci_score (Biodizionario)
def _load_catalog():
    cat = {}
    try:
        with open(os.path.join(HERE, 'inci_data', 'catalog.yml'), encoding='utf-8') as source:
            for line in source:
                m = re.match(r'^(.*):\s*([0-4])\s*$', line.rstrip('\n'))
                if m:
                    cat[m.group(1).strip().strip('"\'').lower()] = int(m.group(2))
    except OSError:
        pass
    return cat

def _load_hazards():
    out = []
    try:
        with open(os.path.join(HERE, 'inci_data', 'hazards.yml'), encoding='utf-8') as source:
            for line in source:
                m = re.match(r'^(.+):\s*([0-4])\s*$', line.rstrip('\n'))
                if m and not m.group(1).startswith('-'):
                    out.append((m.group(1).strip().strip('"\'').lower(), int(m.group(2))))
    except OSError:
        pass
    return out

CATALOG = _load_catalog()
HAZARDS = _load_hazards()

def hazard(name):
    """Biodizionario hazard for an INCI name: exact match, then substring patterns. None if unknown."""
    t = re.sub(r'\s+', ' ', (name or '').strip().strip('"').lower())
    if t in CATALOG:
        return CATALOG[t]
    t2 = re.sub(r'\s*\([^)]*\)', '', t).strip()            # drop "(common name)" and retry
    if t2 in CATALOG:
        return CATALOG[t2]
    for pat, h in HAZARDS:
        if pat in t:
            return h
    return None

# ---- garbage: Shopify/JSON/marketing text that is NOT a cosmetic ingredient -------------------
# structural JSON/price/customer noise — always garbage
SKIP_STRUCT = re.compile(r'^\s*$|price\s*:|compareatprice|hasonly|available\s*:|options\s*:|'
    r'"?position"?\s*:|"?values"?|relatedproducts|collections?$|page(_type)?\s*:|customer\s*:|'
    r'email\s*:|first_name|last_name|\bnull\b|\btrue\b|\bfalse\b|productsof|^\W*e\)\s*;|'
    r'^[\[\]{}();]+$|^ingredient:?\s*$|full\s*ingredient|^\d+$|^si$|^dipheny$', re.I)
# a STANDALONE marketing tag or product-type phrase (whole string) — NOT "organic <plant>" (a real ingredient)
SKIP_TAG = re.compile(r'^"?(vegan|cruelty[\s-]?free|organic|natural|unscented|scented|clean|non[\s-]?toxic|'
    r'soy[\s-]?free|gluten[\s-]?free|sulfate[\s-]?free|paraben[\s-]?free|orangutan[\s-]?safe|'
    r'plastic[\s-]?free|handmade|small[\s-]?batch|default\s*title)"?\]?\s*$|'
    r'\b(moistur\w+|hydrating|nourishing|lotion|cleanser|shampoo|conditioner|serum|soap|scrub|'
    r'balm|mask|toner|deodorant)\b[a-z\s]*"?\]?\s*$|for\s+(sensitive|dry|oily|normal)\s+skin|'
    r'made\s*safe|effective\s+as|based\s+on\s+ing|as\s+possible|'
    r'(natural|organic|gentle|nourishing|hydrating|non[\s-]?irritating|moisturi\w+|foaming|creamy|'
    r'luxurious|soothing|scented)\s+(bubble\s*bath|body\s*wash|body\s*butter|hand\s*wash|face\s*wash|'
    r'shower\s*gel|body\s*lotion|hand\s*cream|lip\s*balm|shampoo|conditioner|soap|scrub|cleanser|'
    r'wash|bath|lotion|cream|oil|serum|butter)', re.I)
SKIP_PROSE = re.compile(r'^(?:unless\s+unscented|a\s+little\s+love\s+for\s+your\s+skin|'
    r'a\s+signature\s+blend\s+including|a\s+sigaturel?\s+blend\s+including)\b', re.I)
# ingredients are often written "Organic X" / "Raw X" / "Cold-Pressed X" — strip the prefix, keep X
PREFIX = re.compile(r'^\**\s*(organic|raw|cold[\s-]?pressed|unrefined|virgin|fair[\s-]?trade|'
    r'pure|natural|steam[\s-]?distilled|purified|certified\s+organic)\s+', re.I)

# ---- animal-derived (a plant-based platform docks these) --------------------------------------
ANIMAL = re.compile(r'\bcera\s*alba|beeswax|\bmel\b|(?<![a-z])honey|lanolin|carmine|\bci\s*75470|'
    r'tallow|collagen|\bkeratin|snail|silk|serica|milk|lactis|\bhoney\b|shellac|guanine|squalane\s*\(animal', re.I)

# Disclosures that are sufficiently clear for ORIGIN even though they are not formal INCI names.
# Their naming quality is recorded separately as 'naturally-derived' or 'common-name'.
NATURAL_DERIVED_GROUP = re.compile(r'^(?:plant[\s-]*based\s+surfactants?|coconut[\s-]*derived\s+cleansers?|'
    r'(?:a\s+)?(?:floral\s+)?essential\s+oil\s+blend)$', re.I)
COMMON_NATURAL = re.compile(r'^(?:orange|parsley|sugar|fine[\s-]*grade\s+sand)$', re.I)
TRADE_SYNTHETIC = re.compile(r'^liquid\s+germall(?:®|\s)*plus$', re.I)

# ---- synthetic / isolate / petrochemical -> FLAGGED -------------------------------------------
SYNTH = re.compile(
    r'sulfate|sulphate|laureth|lauryl\s*sulf|\bpeg-?\d|\bppg-?\d|polyethylene\s*glycol|'
    r'phenoxyethanol|paraben|methyl\w*isothiazolinone|\bedta\b|carbomer|acrylate|acrylamide|'
    r'triethanolamine|diethanolamine|\bdea\b|\bmea\b|\btea\b|cocamidopropyl|cocamide|'
    r'propylene\s*glycol|butylene\s*glycol|petrolatum|paraffin|mineral\s*oil|microcrystalline\s*wax|'
    r'\bbht\b|\bbha\b|sodium\s*benzoate|potassium\s*sorbate|benzyl\s*(alcohol|benzoate|salicylate)|'
    r'polysorbate|peg\b|dimethicone|\w+(cone|silane|siloxane|siloxy)\b|silica\s*dimethyl|'
    r'parfum|fragrance|\bci\s*(?!77\d)\d{4,5}|fd&c|d&c|(?:blue|red|yellow|green)\s*\d|'
    r'oxybenzone|octinoxate|avobenzone|homosalate|octocrylene|phthalate|formaldehyde|'
    r'toluene|\bpvp\b|styrene|quaternium|polyquaternium|laureth|isopropyl\s*myristate|'
    r'ethylhexyl|butylphenyl|methylpropional|hydroxyisohexyl|alcohol\s*denat|denatured\s*alcohol|'
    r'isopropyl\s*(myristate|palmitate|isostearate)|myristyl\s*myristate|isononyl|isononanoate|'
    r'diazolidinyl|imidazolidinyl|\bdmdm\b|hydantoin|iodopropynyl|butylcarbamate|methylchloro|'
    r'isothiazolinone|\burea\b|ceteareth|steareth|laureth|trideceth|cocamide|isohexadecane|'
    r'c\d{2}-\d{2}\s|hydrogenated\s*poly|trimethyl|dimethiconol|phenyl\s*trimethicone|'
    # derived surfactants / emulsifiers / esters / isolate preservatives — NOT raw (Throne Standard docks)
    r'glucoside|betaine|isethionate|sarcosinate|sulfosuccinate|taurate|glutamate\b|'
    r'cocoate|palmate|kernelate|\boleate\b|\blaurate\b|cocoyl|lauroyl|olivoyl|'
    r'triglyceride|caprylyl\s*glycol|ethylhexylglycerin|hydroxyacetophenone|phenethyl\s*alcohol|'
    r'panthenol|behentrimonium|behenyl|phytate|gluconate|polyglyceryl|glyceryl\s*(stearate|caprylate)|'
    r'ceteareth|cetearyl\s*alcohol|cetyl\s*alcohol|stearyl\s*alcohol|stearic\s*acid|hydrolyzed|'
    r'sodium\s*(cocoyl|lauroyl|stearoyl|methyl)|potassium\s*(cocoate|palmate|stearate)|'
    r'sodium\s*hydroxide|potassium\s*hydroxide|disodium|trisodium|synthetic|nylon-?\d|'
    # makeup/skincare derived + silicones + film-formers + glass fillers + fragrance isolates
    r'silsesquioxane|\bmalate\b|adipate|borosilicate|stearalkonium|disteardimonium|pentaerythrityl|'
    r'undecane|tridecane|dodecane|hexanediol|methylpropanediol|propanediol|dicaprylyl|'
    r'tribehenin|behenin|olivate|ascorbate|hyaluron|\bferment\b|saccharomyces|'
    r'palmitoyl|peptide|pinene|linalyl|isoeugenyl|acetyloctahydronaphthalene|nicotinate|'
    r'limonene|linalool|geraniol|citronellol|coumarin|eugenol|citral', re.I)

# ---- natural: raw plant / mineral -------------------------------------------------------------
# a Latin botanical binomial with a (Common Name) is the strongest natural signal in INCI
BOTANICAL = re.compile(r'[a-z]{4,}\s+[a-z]{3,}[a-z()\s]*\((?:[a-z ]+)\)\s*'
    r'(oil|butter|extract|water|juice|powder|wax|seed|leaf|flower|fruit|root|bark|kernel|peel)', re.I)
NATURAL = re.compile(
    r'\baqua\b|water/eau|(?<![a-z])water(?![a-z])|glycerin|glycerol|'
    r'(shea|cocoa|coconut|olive|jojoba|argan|almond|avocado|castor|sunflower|rosehip|hemp|'
    r'sesame|grapeseed|macadamia|marula|baobab|moringa|neem|tamanu)\s*(oil|butter|seed)|'
    r'butyrospermum|cocos\s*nucifera|olea\s*europaea|simmondsia|prunus|persea|ricinus|'
    r'helianthus|rosa\s*|argania|aloe|vera|barbadensis|calendula|chamomil|matricaria|lavandula|'
    r'rosmarinus|mentha|citrus\s+\w+\s+peel|camellia|hibiscus|hamamelis|arnica|avena\s*sativa|'
    r'essential\s*oil|hydrosol|floral\s*water|\bclay\b|kaolin|bentonite|rhassoul|illite|'
    r'zinc\s*oxide|iron\s*oxide|titanium\s*dioxide|\bmica\b|\bci\s*77\d{3}|sea\s*salt|magnesium|'
    r'sodium\s*(chloride|bicarbonate)|tocopherol|vitamin\s*e|ascorbic|citric\s*acid|'
    r'xanthan\s*gum|guar|arrowroot|starch|oat\s*|honey(?=\s*extract)|propolis|'
    r'candelilla|carnauba', re.I)


# a plant ingredient in INCI almost always ENDS in a form word ("Persea Gratissima Oil",
# "Theobroma Cacao Seed Butter", "Rosa Damascena Flower Water"). Checked AFTER synth (so "Mineral Oil",
# "PEG-40 ... Castor Oil" are already caught) — high-precision natural signal across the huge botanical space.
FORM_END = re.compile(r'\b(oil|butter|extract|juice|water|eau|wax|powder|seed|kernel|flower|leaf|root|'
    r'bark|peel|fruit|hydrosol|distillate|sap|resin|gum|starch|clay|charcoal|vinegar|puree|pulp)s?\s*\**\s*$', re.I)

# bare common botanicals/minerals written without a form word ("Organic Coconut", "Lavender")
BARE = re.compile(r'^(coconut|jojoba|olive|argan|castor|almond|avocado|shea|cocoa|sunflower|rosehip|'
    r'hemp|sesame|grapeseed|macadamia|marula|baobab|moringa|neem|tamanu|lavender|rosemary|peppermint|'
    r'spearmint|chamomile|calendula|sandalwood|vetiver|patchouli|frankincense|geranium|ylang|bergamot|'
    r'\brose\b|lemongrass|eucalyptus|tea\s*tree|cedarwood|clary\s*sage|helichrysum|myrrh|turmeric|'
    r'ginger|aloe|witch\s*hazel|green\s*tea|cucumber|\boat\b|clay|charcoal|kaolin|mica|zinc|magnesium)\b', re.I)

# raw minerals / water — natural ORIGIN signals the catalog's hazard axis can't give
RAW = re.compile(r'\baqua\b|water/eau|(?<![a-z])water(?![a-z])|\bsea\s*salt\b|(?<![a-z])salt(?![a-z])|'
    r'clay|kaolin|bentonite|rhassoul|illite|\bmica\b|zinc\s*oxide|iron\s*oxide|titanium\s*dioxide|'
    r'\bci\s*77\d{3}|magnesium|charcoal|silt|maris\s*(sal|aqua)', re.I)

def _is_raw(t):
    """raw plant / mineral ORIGIN — the only thing that counts toward the Throne Standard natural%"""
    return bool(BOTANICAL.search(t) or BARE.search(t) or FORM_END.search(t) or RAW.search(t))

def classify_inci(name):
    t = (name or '').strip().strip('"').strip().lower()
    if not t or SKIP_STRUCT.search(t) or SKIP_TAG.search(t) or SKIP_PROSE.search(t) or len(t) < 2 or len(t) > 80:
        return 'skip'
    t = PREFIX.sub('', t)         # strip "organic"/"raw"/"cold-pressed" -> classify the real ingredient
    if ANIMAL.search(t):
        return 'animal'
    if TRADE_SYNTHETIC.search(t):
        return 'synthetic'
    if NATURAL_DERIVED_GROUP.search(t):
        return 'derived'
    if COMMON_NATURAL.search(t):
        return 'natural'
    if _is_raw(t):
        return 'natural'          # raw plant/mineral origin
    if hazard(t) is not None or SYNTH.search(t):
        return 'synthetic'        # a recognised cosmetic ingredient that ISN'T raw = derived/synthetic
    if NATURAL.search(t):
        return 'natural'
    return 'unknown'              # unrecognised -> likely prose/garbage; excluded from the score

def is_flagged(name):
    """A genuine CONCERN (not merely non-raw): Biodizionario hazard 3-4 — silicones, PEG, sulfate esters,
    synthetic fragrance/colour, formaldehyde-releasers, etc. This is the catalog's real strength."""
    h = hazard(PREFIX.sub('', (name or '').strip().strip('"').lower()))
    return h is not None and h >= 3


# a genuine INCI declaration carries these tokens; marketing scent-copy does not
INCI_SIGNAL = re.compile(r'\baqua\b|water/eau|glycerin|glyceryl|cetearyl|cetyl\b|stearyl|stearate|'
    r'sodium\s+\w|potassium\s+\w|tocopherol|panthenol|parfum|phenoxyethanol|butyrospermum|helianthus|'
    r'cocos\s*nucifera|simmondsia|citric\s*acid|xanthan|caprylic|lecithin|'
    r'[a-z]{4,}\s+[a-z]{4,}\s+(?:seed\s+|leaf\s+|flower\s+|fruit\s+)?(?:oil|extract|butter|water)', re.I)

def looks_like_inci(text):
    """True only for a real INCI ingredient list — not scent-note marketing prose."""
    t = (text or '').strip()
    if len(t) < 3:
        return False
    segs = [s.strip() for s in re.split(r'[,;]', t) if s.strip()]
    if not segs:
        return False
    cls = [classify_inci(s) for s in segs]
    recog = sum(1 for c in cls if c in ('natural', 'derived', 'synthetic', 'animal'))
    nat = sum(1 for c in cls if c in ('natural', 'derived'))
    # a genuine list either carries an INCI marker (aqua/glycerin/…) OR is a clearly-botanical
    # blend (pure-oil ranges list only plant names — no aqua) — both beat marketing scent-copy.
    if not INCI_SIGNAL.search(t) and nat / len(segs) < 0.6:
        return False
    return recog / len(segs) >= 0.7          # predominantly recognisable INCI, not scent/marketing prose


VARIABLE_LIST = re.compile(r'(?:\.\.\.|…|\betc\.?\b|\band\s+more\b|\bother\s+ingredients?\b|'
    r'\bmay\s+(?:also\s+)?(?:contain|include)\b|\bcan\s+contain\b|\+\s*/\s*[-−]|'
    r'ingredients?\s+(?:may|can)\s+(?:vary|change)|refer\s+to\s+(?:the\s+)?(?:packaging|label))', re.I)
STRUCTURAL_CONTAMINATION = re.compile(r'(?:"?@type"?|acceptedanswer|mainentity|application/ld\+json|'
    r'"(?:image|url|price|sku|offers|aggregateRating)"\s*:|\\[nrt]|<script|</script)', re.I)


def extract_inci(html):
    """Pull a genuine INCI declaration from cosmetic product HTML, or '' if none is published."""
    import ingredient_extract as X
    return X.extract(html, validate=looks_like_inci, window=2200,
                     reject=lambda candidate: bool(VARIABLE_LIST.search(candidate) or
                                                   STRUCTURAL_CONTAMINATION.search(candidate)))


def split_inci(text):
    if isinstance(text, list):
        items = [str(x) for x in text]
    else:
        text = re.sub(r'\([^)]*\d+%[^)]*\)', '', text or '')   # drop "(2%)" strengths
        items = re.split(r'[,;]|\band\b', text)
    out = []
    for p in items:
        p = re.sub(r'^\s*"?\s*ingredient\s*:?\s*', '', p, flags=re.I)   # strip leaked "Ingredient:" prefix
        p = p.strip().strip('"').strip(' *.')
        if p:
            out.append(p)
    return out


def score_inci(product, ingredients, url=''):
    """Binary per-SKU naturalness, parallel to Clothing. A product is 100% natural only when every
    disclosed ingredient is identified as raw natural. Generic derived-group claims do not
    identify the ingredients and remain unresolved, as do unknowns and incomplete lists.
    Synthetic/animal entries make an assessable product not 100% natural. Naming quality
    remains separate."""
    rows = [(i, classify_inci(i)) for i in split_inci(ingredients)]
    real = [(i, c) for i, c in rows if c != 'skip']
    n = len(real)
    nat = sum(1 for _, c in real if c == 'natural')
    derived = [i for i, c in real if c == 'derived']
    synth = [i for i, c in real if c == 'synthetic']
    ani = [i for i, c in real if c == 'animal']
    unknown = [i for i, c in real if c == 'unknown']
    flagged = [i for i, c in real if c != 'animal' and is_flagged(i)]   # genuine concerns (hazard 3-4)
    assessable = bool(n) and not unknown and len(real) == len(rows) and not derived
    assessable = assessable and not VARIABLE_LIST.search(str(ingredients))
    ingredient_natural_pct = round(100 * nat / n) if assessable else None
    is_100_natural = assessable and not synth and not ani
    natural_pct = (100 if is_100_natural else 0) if assessable else None
    informal = [i for i, _ in real if (NATURAL_DERIVED_GROUP.search(i.strip()) or
                COMMON_NATURAL.search(i.strip()) or TRADE_SYNTHETIC.search(i.strip()))]
    inci_quality = ('formal-inci' if assessable and not informal else
                    'common-name-or-trade-disclosure' if assessable else 'unresolved')
    if not assessable: verdict = 'UNRESOLVED'
    elif is_100_natural: verdict = '100% NATURAL'
    else: verdict = 'NOT 100% NATURAL'
    return {'product': product, 'url': url, 'ingredient_count': n,
            'natural_pct': natural_pct, 'ingredient_natural_pct': ingredient_natural_pct,
            'is_100_natural': is_100_natural if assessable else None,
            'natural_label': verdict, 'inci_quality': inci_quality,
            'informal_ingredient_names': informal[:20], 'flagged': len(set(flagged)),
            'flagged_ingredients': sorted(set(flagged))[:20],
            'animal': sorted(set(ani)), 'synthetic_ingredients': sorted(set(synth))[:20],
            'naturally_derived_ingredients': sorted(set(derived))[:20],
            'unknown_ingredients': sorted(set(unknown))[:20],
            'assessment_status': 'assessed' if assessable else 'unresolved', 'verdict': verdict,
            'garbage_skipped': len(rows) - len(real)}
