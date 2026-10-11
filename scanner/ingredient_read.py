#!/usr/bin/env python3
"""Throne Standard INGREDIENT reader. Classifies each ingredient as natural (whole plant/mineral),
additive/chemical (isolates, emulsifiers, E-numbers, synthetic flavour/colour), or animal — the food
analogue of the fibre reader. 100% transparency: every ingredient is named and bucketed, nothing hidden."""
import re

# whole plant / mineral / simple natural foods — the only things that count as pure
NATURAL = (r'cocoa(?:\s*(?:mass|butter|solids|powder|nibs|liquor|beans?))?|cacao|cane sugar|raw sugar|'
    r'(?:dark|plain)?\s*chocolate|couverture|'
    r'coconut sugar|coconut|sugar\b|dates?|honey|maple|vanilla(?:\s*(?:bean|extract|pods?))?|sea salt|salt\b|'
    r'nuts?|almonds?|hazelnuts?|cashews?|peanuts?|pistachios?|fruit|raisins?|cherries|orange|mint|'
    r'oats?|rice\b|flour|water|agua|eau|acqua|wasser|lemon|ginger|cinnamon|spices?|herbs?|olive oil|milk|cream|butter\b|'
    r'coffee(?:\s*beans?)?|cacao\s*beans?|tea\b|rooibos|matcha|chicory|rapeseed\s*oil|sunflower\s*oil|sesame\s*oil|avocado\s*oil|linseed\s*oil|flaxseed|hemp\s*seed|cardamom|sumac|black\s*pepper|peppercorns?|turmeric|cumin|coriander|paprika|oregano|thyme|rosemary|basil|chilli|chili|cayenne|cloves?|nutmeg|fennel|anise|bay\s*leaf|garlic|onion|quinoa|buckwheat|millet|amaranth|teff|barley|rye|spelt|lentils?|chickpeas?|beans?|peas\b|apricots?|figs?|prunes?|cranberr(?:y|ies)|blueberr(?:y|ies)|blackberr(?:y|ies)|strawberr(?:y|ies)|raspberr(?:y|ies)|mulberr(?:y|ies)|goji|acai|mango|banana|apple|pear|peach|plum|grape|melon|papaya|pineapple|passionfruit|lime|seeds?|pumpkin\s*seed|chia|sesame|sunflower\s*seed|walnuts?|pecans?|brazil\s*nuts?|macadamia|olives?|tomato|carrot|beetroot|spinach|kale|mushrooms?|seaweed|kelp|nori|spirulina|agave|molasses|jaggery|panela|rapadura|lime|grapefruit|bergamot|elderflower|hibiscus|chamomile|peppermint|lavender|'
    # --- multilingual core (DE/FR/ES/IT/PT): chocolate is mostly cocoa + sugar + cocoa butter ---
    r'kakao|cacau|zucker|az[uú]car|a[çc][uú]car|sucre|zucchero|rohrohr|rohrzucker|vollrohr|'
    r'haseln[uü]ss|avellana|noisette|nocciol|amande|mandel|almendra|am[êe]ndoa|'
    r'vanille|vainilla|baunilha|vaniglia|salz|meersalz|\bsal\b|\bsel\b|\bsale\b|hafer|avena|kaffee|caff[èe]')
ANIMAL  = (r'\bmilk|cream|butter\b|whey|casein|lactose|honey|egg|gelatin|carmine|shellac|beeswax\b|'
    r'milch|milchpulver|\bleche\b|\blait\b|\blatte\b|\bleite\b|sahne|\brahm\b|panna|\bnata\b|molke|'
    r'honig|miel|\bmel\b|\buovo\b|\bhuevo\b|\boeuf\b|\bei\b|milchschokolade')
# isolates / additives / synthetics — the Throne Standard docks these (not a whole food)
ADDITIVE = (r'\bemulsifier|lecithins?|\bpgpr\b|mono-?\s*and\s*di-?glycerides?|glycerides?|'
    r'flavou?ring|flavou?r\b|vanillin|artificial|\bcolou?r(?:ing|ant)?s?\b|preservatives?|'
    r'antioxidants?|acidity regulator|stabili[sz]er|thickener|maltodextrin|glucose syrup|'
    r'corn syrup|invert sugar|dextrose|\bhydrogenated|palm oil|palm kernel|soy protein isolate|'
    r'sucralose|aspartame|acesulfame|\be\d{3}\b|sorbitol|carrageenan|xanthan|guar gum|'
    r'sodium |potassium |calcium |mono[\s-]?sodium|msg\b|cream of tartar')

# plant fats read as "butter" but are whole plant foods, NOT dairy (incl. non-English cocoa butter)
PLANT_FAT = (r'(?:cocoa|cacao|shea|nut|almond|peanut|cashew|seed|coconut|mango|sunflower)\s*butter|'
    r'kakaobutter|beurre de cacao|manteca de cacao|burro di cacao|manteiga de cacau|cacaoboter')
# plant "milks" are whole-plant, NOT dairy — must be checked BEFORE the animal 'milk' rule
PLANT_MILK = (r'(?:rice|oat|coconut|almond|soy|soya|hazelnut|cashew|hemp|pea|potato|tiger\s*nut|'
    r'macadamia|walnut|quinoa|spelt)\s*-?\s*milk|coconut\s*(?:cream|powder|milk)|cream(?:ed)?\s*coconut|'
    r'cream\s*of\s*coconut|reismilch|hafermilch|kokosmilch|kokos')

# ------------------------------------------------------------- junk guard
# Some stored SKU "ingredients" are not ingredient lists at all: the scraper captured page furniture.
# Real examples found 9 Sep 2026: 'html" class="pp">Baking ingredients Spreads Grains and grain
# products Legumes', 'Beans Origin Regenerative Impact', and raw JSON-LD. Scoring those produced
# nonsense in BOTH directions, including a 42% MILK chocolate reading 100% natural / 5 of 5.
# A capture failure must never become a verdict.
JUNK = (r'<[a-z/][^>]*>|class\s*=|"@type"|priceSpecification|valueAddedTaxIncluded|ListItem|'
        r'\.html|https?://|\{\s*"|\bskip to content\b|\badd to cart\b|\bshop now\b|'
        # bare HTML ATTRIBUTES survive tag-stripping and are a sure sign of a scrape that grabbed
        # markup: Amano's stored "ingredients" ended '... vanilla." title="Amano_NF_FILES-04"
        # data-caption="" data-src="', and that stray "butter" nearly delisted a real brand.
        r'\bdata-[a-z-]+\s*=|\btitle\s*=\s*"|\balt\s*=\s*"|\bsrc\s*=\s*"|\bhref\s*=\s*"|"\s*>')


# Prose is not an ingredient list. A re-scan on 9 Sep 2026 captured a customer REVIEW ("and I must
# say, wow! A premium chocolate bar") and scored it 100% natural / 5 of 5, because it contained a
# comma and no markup. An ingredient list names things; it does not speak.
PROSE = (r"\b(?:i|we|you|my|our|your)\b|\b(?:must say|wow|love|loved|delicious|tastes?|flavour is|"
         r"perfect|amazing|favourite|favorite|recommend|highly|arrived|ordered|bought|gift|"
         r"treat|enjoy(?:ed)?|beautiful|lovely|excellent)\b|[!?]|\.\.\.")


def looks_like_prose(text):
    t = (text or '').strip()
    if re.search(PROSE, t, re.I):
        return True
    # a list is short noun phrases; a sentence has verbs and long runs between commas
    parts = [x.strip() for x in t.split(',') if x.strip()]
    if parts and sum(len(x.split()) for x in parts) / len(parts) > 6:
        return True
    return False


def looks_like_junk(text):
    """True when the stored text is page furniture rather than an ingredient list."""
    t = (text or '').strip()
    if len(t) < 3:
        return True
    if re.search(JUNK, t, re.I):
        return True
    if looks_like_prose(t):
        return True
    # a nav strip is title-case words with no commas. But a real single ingredient can be written
    # that way too ("Single Origin Cacao Beans"), so only treat it as a nav strip when it names
    # nothing we recognise as food.
    if ',' not in t and len(t.split()) >= 4:
        caps = sum(1 for w in t.split() if w[:1].isupper())
        if caps >= max(3, len(t.split()) * 0.6):
            # "Single Origin Cacao Beans" is a real ingredient; "Beans Origin Regenerative Impact"
            # is a nav strip that happens to contain one food word. The difference is the RATIO of
            # words that name food, not whether any single one does.
            # descriptors ("Organic", "Roasted") are neither evidence for nor against; drop them so
            # "Organic Roasted Cacao Nibs" is judged on "Cacao Nibs"
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]+", t) if not is_descriptor(w)]
            food = sum(1 for w in words
                       if re.search(NATURAL, w, re.I) or re.search(ANIMAL, w, re.I) or re.search(ADDITIVE, w, re.I))
            if not words or food / len(words) < 0.4:
                return True
    return False


# --------------------------------------------------------------- confidence
# ROOT CAUSE of the 9 Sep 2026 mis-scoring: natural_pct was natural/TOTAL, and anything the
# vocabulary could not name returned 'unknown' and sat in the denominator. So a word we simply did
# not know cost a brand exactly as much as a word we knew was bad, and pure cinnamon scored 50%
# because "Cinnamomum loureiroi" was unrecognised. Our vocabulary gap became the brand's penalty.
#
# The Throne Standard's own discipline is the clothing reader's: better unclear than a guess. So the
# score is now taken over what we can actually NAME, and when too little is recognised we return no
# verdict at all instead of a low one.
MIN_KNOWN = 0.5          # at least half the ingredient slots must be recognised to score at all

# Latin botanical names live inside brackets ("(Cinnamomum loureiroi)"), which split_ingredients
# already drops, so no separate rule is needed. A shape-based test was tried and removed: it matched
# "Royal cinnamon" as a binomial and deleted a real ingredient.


# ------------------------------------------------------------- descriptors
# Words that describe an ingredient or its provenance but are NOT ingredients. Validation over 1,460
# real stored SKU lists (9 Sep 2026) showed these were being counted in the denominator, which pushed
# genuinely whole-food products into MOSTLY SYNTHETIC. They are dropped before anything is counted.
DESCRIPTOR = (r'^(?:certified\s+)?organic$|^single[\s-]?ingredient$|^unrefined$|^cold[\s-]?(?:pressed|extracted)$|'
    r'^fair\s*trade$|^fairtrade$|^non[\s-]?gmo$|^raw$|^vegan$|^natural$|^usda\s+organic$|'
    r'^(?:medium|dark|light)\s+roast(?:ed)?$|^roasted$|^shade[\s-]?grown$|^sun[\s-]?dried$|^dried$|'
    r'^unsalted$|^unfiltered$|^grown$|^single\s+origin$|^gluten[\s-]?free$|^dairy[\s-]?free$|'
    r'^100%?$|^whole$|^pure$|^stone[\s-]?ground$|^small\s+batch$|^hand[\s-]?picked$|^wild[\s-]?(?:grown|harvested)$|'
    # provenance, not composition
    r'^(?:colombia|honduras|peru|brazil|ethiopia|guatemala|nicaragua|mexico|sicily|italy|spain|greece|'
    r'france|portugal|turkey|india|vietnam|indonesia|ecuador|madagascar|ghana|tanzania|kenya|uganda)$|'
    r'^(?:leccino|frantoio|moraiolo|coratina|picual|arbequina|koroneiki)$|'
    r'^sprouted$|^bulk$|^hand[\s-]?harvested$|^extra\s+virgin$|^estate[\s-]?grown.*$|^with$|'
    r'^ground$|^whole$|^chopped$|^sliced$|^flakes?$|^milled$|^crushed$|^toasted$|^blanched$|^shelled$|^pitted$|^halves$|^pieces$|^granules$|^fine$|^coarse$|^dry$|^fresh$|^pure$|^grade\s+\w+.*$|^unroasted$|^activated$|^sustainably\s+sourced$')


def is_descriptor(ing):
    return bool(re.match(DESCRIPTOR, (ing or '').strip().strip('*.()[]% ').lower()))


# ---------------------------------------------------------------- negation
# A clause like "no artificial colours", "free from palm oil" or "contains no milk" states what is
# ABSENT. Reading those as present made a product that says it has no additives score MOSTLY
# SYNTHETIC, and made "contains no milk" read as animal, which would wrongly disqualify a vegan
# product. Negated clauses are removed before classification and reported separately.
NEGATION = (r'\bfree\s+from\s+[^.;]*|\bfree\s+of\s+[^.;]*|\bwithout\s+[^.;]*|'
            r'\bcontains?\s+no\s+[^.;]*|\bno\s+(?:added\s+)?[^.;,]*|\bnever\s+[^.;,]*|'
            r'\bzero\s+[^.;,]*|\b[a-z-]+\s*-\s*free\b')


def strip_negations(text):
    """Return (text_without_negated_clauses, list_of_absent_clauses)."""
    t = text or ''
    absent = [m.group(0).strip() for m in re.finditer(NEGATION, t, re.I)]
    clean = re.sub(NEGATION, ' ', t, flags=re.I)
    return clean, absent


def classify_ingredient(ing):
    t = ing.lower().strip()
    if not t: return None
    if re.search(ADDITIVE, t):                    # additive/chemical wins (an isolate is not a whole food)
        return 'chemical'
    if re.search(PLANT_FAT, t):                   # cocoa/shea/nut butter = plant, before the dairy check
        return 'natural'
    if re.search(PLANT_MILK, t):                  # rice/oat/coconut milk = plant, before the dairy check
        return 'natural'
    if re.search(ANIMAL, t):
        return 'animal'
    if re.search(NATURAL, t):
        return 'natural'
    return 'unknown'


# A bracketed sub-list inherits its parent: "Cacao (Paste, Butter)" means cacao paste and cacao
# BUTTER, which is plant. Read bare, that "Butter" looks like dairy and would wrongly mark a vegan
# brand as containing milk — and on 9 Sep 2026 it nearly delisted Giddy Yo Yo on that basis.
AMBIGUOUS_CHILD = re.compile(r'^(?:butter|paste|powder|mass|liquor|solids?|nibs?|oil|fat|extract|'
                             r'cream|milk|sugar)$', re.I)


def expand_bracketed(text):
    """Rewrite "Cacao (Paste, Butter)" as "cacao paste, cacao butter" so children keep their parent."""
    def _fix(m):
        parent = m.group(1).strip()
        inner = m.group(2)
        parts = [x.strip() for x in re.split(r'[,;]', inner) if x.strip()]
        out = []
        for x in parts:
            core = re.sub(r'[^A-Za-z ]', '', x).strip()
            out.append(f'{parent} {x}' if AMBIGUOUS_CHILD.match(core) else x)
        return parent + ', ' + ', '.join(out)
    return re.sub(r'([A-Za-z][A-Za-z \-]{2,}?)\s*\(([^)]*)\)', _fix, text or '')

def split_ingredients(text):
    text = expand_bracketed(text or '')
    text = re.sub(r'\([^)]*%\)', '', text)                    # drop "(70%)" percentages
    text = re.sub(r'may contain.*$', '', text, flags=re.I)          # allergen advisory isn't an ingredient
    # Bracketed text after an ingredient is provenance or method, not more ingredients. Validation on
    # real data showed "100% extra virgin olive oil (Koroneiki olives, PDO Kolymvari, cold extracted)"
    # being split into five fake ingredients and scored 33% MOSTLY SYNTHETIC. Brackets are dropped
    # UNLESS they name an actual additive, which must still be caught.
    # A bracket may hold the REAL ingredient list ("70% dark chocolate (cocoa mass, sugar)") or mere
    # provenance ("(Koroneiki olives, PDO Kolymvari)"). Flatten it into the list when it names
    # ingredients (a comma list, or an additive); drop it only when it is a short provenance phrase.
    def _bracket(m):
        inner = m.group(1).strip()
        if re.search(ADDITIVE, inner, re.I) or ',' in inner:
            return ', ' + inner + ', '
        return ' '
    text = re.sub(r'\(([^)]*)\)', _bracket, text)
    # a stated "100% <thing>" is a single-ingredient product, exactly as the clothing reader treats
    # a prose percentage only when it states 100% of one fibre
    m = re.match(r'\s*100\s*%?\s+(.+)$', text.strip())
    if m and not re.search(ADDITIVE, m.group(1), re.I):
        first = re.split(r'[,;.]', m.group(1))[0].strip()
        if first:
            text = first
    parts = re.split(r'[,;&+·•]|\band\b', text)
    out = []
    for p in parts:
        p = re.sub(r'[\*#\d.°†‡•]+', '', p).strip(' .:-')   # strip organic markers (°) + footnote glyphs
        if len(p) >= 2 and not re.match(r'^\s*$', p):
            out.append(p)
    return out

# canonical short labels so the customer-facing flag reads "milk, honey" — not scraped fragments
ANIMAL_CANON = [('milk', r'milk|milch|leche|lait|latte|leite|lactose|casein|milchschokolade'),
    ('cream', r'\bcream|sahne|rahm|panna|\bnata\b'), ('butter', r'\bbutter\b'),
    ('honey', r'honey|honig|miel|\bmel\b'), ('egg', r'\begg|uovo|huevo|oeuf|\bei\b'),
    ('whey', r'whey|molke'), ('gelatine', r'gelatin'), ('carmine', r'carmine'),
    ('shellac', r'shellac'), ('beeswax', r'beeswax')]

def _canon_animal(ing):
    t = ing.lower()
    for label, pat in ANIMAL_CANON:
        if re.search(pat, t):
            return label
    # No canonical match: fall back to the ANIMAL term that actually fired, not the first word of the
    # fragment. "caramel* filling (cream...)" was being labelled "caramel", which reads to a customer
    # as though caramel were an animal product.
    m = re.search(ANIMAL, t)
    if m:
        return re.sub(r'[^a-z ]', '', m.group(0)).strip() or 'animal'
    return 'animal'

def _canon_additive(ing):
    t = re.sub(r'\([^)]*\)', '', ing.lower())               # drop parenthetical detail
    t = re.sub(r'[^a-z\- ]', ' ', t)                         # strip %, digits, punctuation
    t = re.sub(r'\bemulsifiers?\b', 'emulsifier', t)
    return re.sub(r'\s+', ' ', t).strip()[:32] or 'additive'


def score_sku(name, ingredients, url='', processing='processed', price='', currency=''):
    """Deterministic per-SKU Throne Standard record in the canonical schema apply_cocoa_comp.php eats:
    {name, ingredients, animal[], additives[], processing, plant_based, natural_pct, score, url}.
    natural_pct = whole-plant ingredients / total (count-based, reproducible — not an LLM estimate).
    score bands match htt-cocoa-score (100->5, 90-99->4, 70-89->3, 40-69->2, <40->1); animal docks
    (dairy/honey chocolate is off-standard for a plant-based platform)."""
    # descriptors ("organic", "cold-pressed", provenance names) are not ingredients and must not sit
    # in the denominator: counting them was scoring whole foods as mostly synthetic
    if looks_like_junk(ingredients):
        # the capture failed for this SKU; refuse a verdict rather than invent one
        rec = {'name': name, 'ingredients': ingredients, 'animal': [], 'additives': [],
               'processing': processing, 'plant_based': None, 'natural_pct': None, 'score': None,
               'unreadable': True, 'reason': 'stored text is not an ingredient list', 'url': url}
        if price: rec['price'] = price
        if currency: rec['currency'] = currency
        return rec
    cleaned, _absent = strip_negations(ingredients)
    ings = [i for i in split_ingredients(cleaned) if not is_descriptor(i)]
    if not ings:
        rec = {'name': name, 'ingredients': ingredients, 'animal': [],
               'additives': [], 'processing': processing, 'plant_based': None,
               'natural_pct': None, 'score': None, 'unreadable': True,
               'reason': 'no ingredients after filtering', 'url': url}
        if price: rec['price'] = price
        if currency: rec['currency'] = currency
        return rec
    animal, additives, nat, unknown = set(), set(), 0, 0
    for ing in ings:
        c = classify_ingredient(ing)
        if c == 'animal':      animal.add(_canon_animal(ing))
        elif c == 'chemical':  additives.add(_canon_additive(ing))
        elif c == 'natural':   nat += 1
        else:                  unknown += 1
    animal = sorted(animal); additives = sorted(x for x in additives if x)
    total = len(ings) or 1
    known = total - unknown
    if known / total < MIN_KNOWN:
        # too little of this list is recognised to put a number on it
        rec = {'name': name, 'ingredients': ingredients, 'animal': sorted(animal),
               'additives': sorted(x for x in additives if x), 'processing': processing,
               'plant_based': not animal, 'natural_pct': None, 'score': None,
               'unreadable': True, 'known_ratio': round(known / total, 2), 'url': url}
        if price: rec['price'] = price
        if currency: rec['currency'] = currency
        return rec
    natural_pct = round(100 * nat / known)
    if   natural_pct >= 100: score = 5
    elif natural_pct >= 90:  score = 4
    elif natural_pct >= 70:  score = 3
    elif natural_pct >= 40:  score = 2
    else:                    score = 1
    if animal:                                  # dairy/honey present -> off-standard
        score = 2                               # THRONE STANDARD: animal is 2/5, never 1/5
    rec = {'name': name, 'ingredients': ingredients, 'animal': animal, 'additives': additives,
           'processing': processing, 'plant_based': not animal, 'natural_pct': natural_pct,
           'score': score, 'url': url}
    if price: rec['price'] = price
    if currency: rec['currency'] = currency
    return rec


def analyse(ingredients_text, additives_tags=None, nova=None):
    """Return the Throne Standard purity read for one product's ingredient list."""
    # what a label says it does NOT contain must never be counted as present
    cleaned, absent = strip_negations(ingredients_text)
    ings = [i for i in split_ingredients(cleaned) if not is_descriptor(i)]
    rows = [{'ingredient': i, 'class': classify_ingredient(i)} for i in ings]
    n = len(rows) or 1
    nat = sum(1 for r in rows if r['class'] == 'natural')
    chem = sum(1 for r in rows if r['class'] == 'chemical')
    ani = sum(1 for r in rows if r['class'] == 'animal')
    # additives_tags from OFF are authoritative chemical markers
    has_additive = bool(additives_tags) or chem > 0
    natural_pct = round(nat / n * 100, 1)
    # verdict: pure only if every ingredient is a whole natural food, no additives, not ultra-processed
    if chem == 0 and (nova is None or nova <= 2) and nat == n - ani:
        verdict = 'GENUINE'
    elif chem == 0 and nat >= n * 0.6:
        verdict = 'MOSTLY NATURAL'
    elif nat >= n * 0.5:
        verdict = 'PARTIAL'
    else:
        verdict = 'MOSTLY SYNTHETIC'
    if not rows:
        # nothing stated once negations are removed: unreadable, never a verdict
        return {'rows': [], 'natural_pct': None, 'natural': 0, 'chemical': 0, 'animal': 0,
                'nova': nova, 'has_additive': bool(additives_tags), 'absent': absent,
                'verdict': 'UNREADABLE'}
    return {'rows': rows, 'natural_pct': natural_pct, 'natural': nat, 'chemical': chem, 'animal': ani,
            'nova': nova, 'has_additive': has_additive, 'absent': absent, 'verdict': verdict}
