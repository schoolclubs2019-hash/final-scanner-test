#!/usr/bin/env python3
"""Canonical Throne Textile Standard for Clothing, Bedding and every textile vertical."""
from collections import Counter
from row_stream import RowStream, DiskRows, selected
from datetime import datetime, timezone
import html
import math
import random
import re
from statistics import NormalDist
from urllib.parse import urlsplit

from throne_standard import STANDARD, VERSION as STANDARD_VERSION
from throne_read import MATERIAL_TERMS, READER_VERSION, composition_shares, component_declarations, _clause_shares, _seg_pairs

POLICY_VERSION = STANDARD_VERSION
SCANNER_VERSION = "1.4.2"
MIN_SCOREABLE = int(STANDARD["scoring"]["subset_minimum_skus"])
FULL_DISCLOSURE = float(STANDARD["disclosure"]["transparency_threshold_percent"])
ANIMAL_DOMINANCE = float(STANDARD["scoring"]["animal_floor"]["minimum_share_percent"]) / 100
SCOREABLE_BANDS = ("plant", "animal", "synthetic", "semi", "blended")
TECHNICAL_HOW = {
    "invalid-classification", "missing-source-url", "evidence-conflict", "invalid-composition-total",
    "unread", "unread-no-url", "unread-capped", "unread-structured-only",
}
UNDISCLOSED_HOW = {"", "none", "undisclosed", "incomplete"}
PUBLIC_LABELS = {
    "verified": "Full result",
    "partial_disclosure": "Partial disclosure",
    "partial_scan": "Partial technical scan",
    "insufficient_evidence": "Not enough evidence to score",
    "not_assessed": "Not assessed",
}
_PARTIAL_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
def _partial_composition_band(composition):
    """Classify explicit material percentages without inventing the missing remainder."""
    hits = []
    for percentage, term in _seg_pairs(str(composition or "")):
        material_class = MATERIAL_TERMS.get(term, (None, None))[1]
        if material_class:
            hits.append((percentage, "semi" if material_class == "semi_synthetic" else material_class))
    classes = {material_class for _, material_class in hits}
    if not classes:
        return None, [], None
    band = "blended" if len(classes) > 1 else next(iter(classes))
    return band, hits, sum(percentage for percentage, _ in hits)


def score_for(pct):
    if pct is None: return None
    for band in STANDARD["scoring"]["score_bands"]:
        if pct >= band["minimum_percent"]:
            return int(band["score"])
    return None


def sample_size(population, confidence=None, margin_of_error=None, proportion=None):
    """Return the conservative simple-random sample size with finite population correction."""
    population = int(population)
    if population <= 0:
        return 0
    sampling = STANDARD["sampling"]
    confidence = float(confidence if confidence is not None else sampling["confidence_level"])
    margin_of_error = float(margin_of_error if margin_of_error is not None else sampling["target_margin_of_error"])
    proportion = float(proportion if proportion is not None else sampling["conservative_proportion"])
    if not 0 < confidence < 1 or not 0 < margin_of_error < 1 or not 0 < proportion < 1:
        raise ValueError("confidence, margin_of_error, and proportion must be between 0 and 1")
    z_score = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    infinite_population = math.ceil(z_score ** 2 * proportion * (1 - proportion) / margin_of_error ** 2)
    corrected = math.ceil(population * infinite_population / (population + infinite_population - 1))
    minimum = int(sampling["minimum_subset_size"])
    return min(population, max(minimum, corrected))


def select_random_sample(population, count, seed=None):
    """Select unique population records randomly; a full census preserves its input order."""
    records = list(population)
    count = max(0, min(int(count), len(records)))
    if count == len(records):
        return records
    rng = random.SystemRandom() if seed is None else random.Random(seed)
    return rng.sample(records, count)


def proportion_interval(successes, sample_count, population=None, confidence=None):
    """Wilson interval with finite-population correction, returned as percentage bounds."""
    sample_count = int(sample_count)
    if sample_count <= 0 or not 0 <= successes <= sample_count:
        raise ValueError("successes must be between zero and a positive sample_count")
    confidence = float(confidence if confidence is not None else STANDARD["sampling"]["confidence_level"])
    z_score = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    observed = successes / sample_count
    z2 = z_score * z_score
    denominator = 1 + z2 / sample_count
    center = (observed + z2 / (2 * sample_count)) / denominator
    half_width = z_score * math.sqrt(
        observed * (1 - observed) / sample_count + z2 / (4 * sample_count * sample_count)
    ) / denominator
    correction = 1.0
    if population is not None and int(population) > 1 and sample_count < int(population):
        correction = math.sqrt((int(population) - sample_count) / (int(population) - 1))
    return (
        round(max(0.0, center - half_width * correction) * 100, 2),
        round(min(1.0, center + half_width * correction) * 100, 2),
    )


def interval_crosses_score_boundary(lower_percent, upper_percent):
    """Return whether an uncertainty interval intersects multiple Standard score bands."""
    if lower_percent > upper_percent:
        raise ValueError("lower_percent cannot exceed upper_percent")
    boundaries = [band["minimum_percent"] for band in STANDARD["scoring"]["score_bands"]
                  if band["minimum_percent"] > 0]
    return any(lower_percent < boundary <= upper_percent for boundary in boundaries)


def sampled_summary(rows, population):
    """A bounded-mean interval is valid only for an explicitly random, complete selected sample."""
    chosen = [row for row in (rows or []) if row.get("sampling_selected")]
    eligible = [row for row in chosen if row_state(row) not in {"excluded_nontextile", "excluded_accessory"}]
    values = [row["composition_plant_share_pct"] for row in eligible
              if row.get("composition_plant_share_pct") is not None]
    complete = bool(eligible and len(values) == len(eligible))
    population = max(int(population or 0), len(eligible))
    mean = sum(values) / len(values) if values else None
    confidence = float(STANDARD["sampling"]["confidence_level"])
    # Hoeffding is conservative for sampling without replacement; a full census has no sampling error.
    radius = (100 * math.sqrt(math.log(2 / (1 - confidence)) / (2 * len(values)))
              if complete and len(values) < population else 0 if complete else 100)
    lower, upper = (max(0, mean - radius), min(100, mean + radius)) if complete else (0, 100)
    crosses = interval_crosses_score_boundary(lower, upper)
    score = score_for(mean) if complete and not crosses and len(values) >= MIN_SCOREABLE else None
    status = "sampled_estimate" if score is not None else "sampled_boundary_uncertain" if complete else "sampled_incomplete"
    return {
        "publication_status": status, "score_status": status, "score": score, "throne_score": score,
        "throne_score_label": STANDARD["scoring"]["textile_score_labels"].get(str(score)),
        "score_basis": "random_sample", "sampling_method": "random_simple_sample",
        "sampling_population": population, "sampling_sample_size": len(chosen),
        "sampling_readable_count": len(values),
        "sampling_plant_count": sum(v == 100 for v in values),
        "sampling_observed_plant_rate": round(mean, 2) if mean is not None else None,
        "sampling_confidence_level": confidence,
        "sampling_interval_lower": round(lower, 2), "sampling_interval_upper": round(upper, 2),
        "sampling_interval_method": "hoeffding_bounded_mean" if complete else None,
        "sampling_interval_crosses_score_boundary": crosses,
        "sampling_continuation_required": not complete or crosses,
        "qualification_note": (f"Random sample: {len(values)} fully quantified of {len(eligible)} eligible selected products. "
            + (f"Conservative {confidence:.0%} bounded-mean interval: {lower:.2f}% to {upper:.2f}%."
               if complete else "Missing selected evidence withholds catalogue inference.")),
    }


def row_state(row):
    band = str(row.get("band") or "").lower()
    how = str(row.get("how") or "").lower()
    if band == "accessory" or how == "accessory": return "excluded_accessory"
    if band == "nontextile": return "excluded_nontextile"
    if isinstance(row.get("independent_review"), dict) and row["independent_review"].get("pass") is False: return "technical_failure"
    if how == "components" and band == "unclear": return "partial_composition"
    if how.startswith("unread") or how in TECHNICAL_HOW: return "technical_failure"
    if band in SCOREABLE_BANDS: return "scoreable"
    if band == "undisclosed" or (band == "unclear" and how in UNDISCLOSED_HOW): return "undisclosed"
    if band == "unclear": return "technical_failure"
    return "technical_failure"


def evidence_source(row, raw=None):
    how = str(row.get("how") or "").lower()
    comp = re.sub(r"\s+", " ", html.unescape(str(row.get("composition") or row.get("fabric") or ""))).strip().lower()
    raw = raw or {}
    body = re.sub(r"<[^>]+>", " ", str(raw.get("body_html") or raw.get("body") or ""))
    body = re.sub(r"\s+", " ", html.unescape(body)).strip().lower()
    title = re.sub(r"\s+", " ", html.unescape(str(raw.get("title") or row.get("product") or ""))).strip().lower()
    tags = re.sub(r"\s+", " ", html.unescape(str(raw.get("tags") or ""))).strip().lower()
    if how == "override": return "Manual verification"
    if how == "prose-materials": return "Product description"
    if how == "product-material": return "Product-specific material field"
    if how == "page": return "Product page"
    if how == "evidence-conflict": return "Conflicting strong product evidence"
    if how == "component": return "Product description / component note"
    if how == "components-equal-split":
        return "Product description / component list" if comp and comp in body else "Published component list"
    if how == "own":
        if comp and comp in body: return "Product description"
        if comp and comp in title: return "Product title"
        if comp and comp in tags: return "Product metadata tag with explicit percentages"
        return "Product description / structured product data"
    if how == "incomplete": return "Incomplete published composition"
    if how == "accessory": return "Product identity / accessory classification"
    if how == "nontextile": return "Product type/material"
    if how in UNDISCLOSED_HOW: return "No scoreable composition published"
    if how.startswith("unread") or how in TECHNICAL_HOW: return "Technical read failure"
    return how.replace("-", " ").replace("_", " ").strip().title() or "Not stated"


def evidence_note(row, raw=None, manual_evidence=""):
    state = row_state(row); source = evidence_source(row, raw)
    comp = str(row.get("composition") or row.get("fabric") or "").strip(); notes = []
    if manual_evidence: notes.append(str(manual_evidence).strip())
    if state == "scoreable":
        notes.append(f"Material evidence found in: {source}.")
        if comp: notes.append(f"Published material/composition used for this result: {comp}.")
    elif state == "partial_composition":
        notes.append("Published component declarations are incomplete; no whole-product share was inferred.")
    elif state == "undisclosed":
        notes.append("The product was successfully inspected, but the brand did not publish usable composition evidence. It has no numerical composition contribution and is not assigned a material class.")
    elif str(row.get("how") or "").lower() == "evidence-conflict":
        notes.append("Strong product-specific sources disagree. No source was silently preferred; review is required before scoring.")
    elif state == "technical_failure":
        notes.append("Throne could not reliably read enough evidence for this product. This is a scanner limitation, not brand non-disclosure, and it does not count for or against the composition score.")
    elif state == "excluded_accessory":
        notes.append("This item is an accessory, so it is kept in catalogue reconciliation but excluded from the textile report and score.")
    else:
        notes.append("This item is not an eligible textile product for this textile score, so it is excluded rather than counted as a pass or a fail.")
    return notes


def annotate_row(row, raw=None, manual_evidence="", scanned_at=None, category=None):
    row = dict(row or {})
    if str(row.get("how") or "").lower() == "incomplete" and row.get("composition") and len(component_declarations(row["composition"])) < 2:
        derived_band, known_parts, known_total = _partial_composition_band(row.get("composition"))
        if derived_band:
            row["band"] = derived_band
            row["composition_partial"] = True
            row["composition_known_parts"] = known_parts
            row["composition_percent_total"] = round(known_total, 2)
            row["composition_unknown_remainder"] = round(max(0.0, 100.0 - known_total), 2)
            if known_total > 100.0:
                row["composition_over_100"] = True
                row["band"] = "unclear"
                row["how"] = "invalid-composition-total"
    if row.get("how") in {"components-equal-split", "prose-materials", "component-materials", "component"} or (
            row.get("how") == "product-material" and not composition_shares(row.get("composition"))):
        row["composition_partial"] = True
    if row.get("how") == "invalid-composition-total":
        row["composition_over_100"] = True
    components = component_declarations(str(row.get("composition") or ""))
    if len(components) > 1:
        # Totals belong to individual components, never a flattened garment.
        row["component_declarations"] = [{"label": label, "raw_declaration": clause,
                                            "shares": _clause_shares(clause)} for label, clause in components]
        row["composition_over_100"] = any(sum(float(m.group(1)) for m in _PARTIAL_PERCENT.finditer(clause)) > 100.05
                                            for _, clause in components)
        if row["composition_over_100"]:
            row["band"], row["how"] = "unclear", "invalid-composition-total"
    shares = composition_shares(str(row.get("composition") or ""), row.get("component_weights"))
    quantified = bool(shares and not row.get("composition_partial") and row_state(row) == "scoreable")
    if len(components) > 1:
        quantified = bool(shares and row_state(row) == "scoreable")
        row["component_incomplete"] = not quantified
    row["composition_partial"] = bool(row.get("composition_partial") or
                                        ((row_state(row) == "scoreable" or len(components) > 1) and not quantified))
    row["composition_plant_share_pct"] = shares["plant"] if quantified else None
    row["shares"] = shares if quantified else None
    state = row_state(row)
    row["evidence_conflict"] = bool(row.get("evidence_conflict") or row.get("how") == "evidence-conflict")
    row["evidence_state"] = state; row["scoreable"] = quantified
    row["evidence_source"] = evidence_source(row, raw)
    row["evidence_notes"] = evidence_note(row, raw, manual_evidence)
    evidence_record = row.get("evidence_record")
    if state == "scoreable" and row.get("legacy_cached") and isinstance(evidence_record, dict):
        composition = str(row.get("composition") or row.get("fabric") or "").strip()
        evidence_record.setdefault("source_type", row.get("source_type") or "legacy_cached_result")
        evidence_record.setdefault("source_url", row.get("url") or None)
        evidence_record.setdefault("source_field", row.get("source_field") or "composition")
        evidence_record.setdefault("accepted_text", composition)
        row["evidence_record"] = evidence_record
    elif state == "scoreable" and row.get("legacy_cached") and row.get("url") and (row.get("composition") or row.get("fabric")):
        composition = str(row.get("composition") or row.get("fabric") or "").strip()
        row["evidence_record"] = {
            "source_type": row.get("source_type") or "legacy_cached_result",
            "source_url": row.get("url"),
            "source_field": row.get("source_field") or "composition",
            "accepted_text": composition,
            "parsed_materials": [],
            "parsed_percentages": {},
        }
    if state == "scoreable":
        row["score_reason"] = ("Not numerically scored: partial or component evidence does not establish a complete whole-product composition."
                                if row.get("composition_partial") else "Included because enough published material information was found to classify this textile product.")
    elif state == "partial_composition": row["score_reason"] = "Not scored: incomplete component declarations."
    elif state == "undisclosed": row["score_reason"] = "Not scored because the brand did not publish enough material information."
    elif state == "technical_failure": row["score_reason"] = "Not scored because Throne could not reliably read this product."
    else: row["score_reason"] = "Excluded because this is not an eligible textile product for this textile score."
    row["scoring_model"] = "textile-composition-plant-share-v1"
    row["swimwear_score"] = None
    row["swimwear_natural_fibre_pct"] = None
    if scanned_at:
        row.setdefault("scanned_at", scanned_at)
        if (row.get("price_status") or row.get("price") is not None or row.get("price_min") is not None):
            row.setdefault("price_checked_at", scanned_at)
    return row


def _animal_rule(counts, raw_score):
    animal=counts.get("animal",0); syn=counts.get("synthetic",0); semi=counts.get("semi",0); denom=animal+syn+semi
    share=round(animal/denom*100,1) if denom else None
    qualifies=bool(denom and animal and animal/denom >= ANIMAL_DOMINANCE)
    final=max(raw_score,2) if raw_score is not None and qualifies else raw_score
    return final, share, qualifies, bool(qualifies and raw_score is not None and final != raw_score)


def animal_floor(score, counts):
    """Apply the Standard's animal-material floor to an already-derived score."""
    return _animal_rule(counts or {}, score)[0]


def _canonical(rows, total_products=None, scanned_at=None, category=None):
    scanned_at = scanned_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = (DiskRows(annotate_row(r, scanned_at=scanned_at, category=category) for r in rows)
            if isinstance(rows, RowStream) else [annotate_row(r, scanned_at=scanned_at, category=category) for r in (rows or [])])
    states = Counter(row_state(r) for r in rows)
    counts = Counter(str(r.get("band") or "") for r in rows)
    assessed_rows = selected(rows, lambda r: r.get("composition_plant_share_pct") is not None)
    scoreable = len(assessed_rows)
    undisclosed = states["undisclosed"]
    technical = states["technical_failure"]
    excluded_nontextile = states["excluded_nontextile"]
    excluded_accessory = states["excluded_accessory"]
    excluded = excluded_nontextile + excluded_accessory
    total_products = int(total_products if total_products is not None else len(rows))
    missing_rows = max(0, total_products - len(rows))
    technical += missing_rows
    eligible = len(rows) - excluded + missing_rows
    readable = eligible - technical
    plant = sum(1 for r in assessed_rows if r.get("band") == "plant")
    plant_points = sum(r["composition_plant_share_pct"] for r in assessed_rows)
    composition_missing = eligible - scoreable
    coverage_rate = round(scoreable / eligible * 100, 1) if eligible else 0.0
    composition_coverage = round(scoreable / eligible * 100, 1) if eligible else 0.0
    disclosure_rate = round(scoreable / readable * 100, 1) if readable else 0.0
    # Thresholds use the unrounded mean; display precision cannot turn 99.99 into a 5.
    plant_mean = plant_points / scoreable if scoreable else None
    plant_pct = round(plant_mean, 2) if plant_mean is not None else None
    disclosed_plant_rate = plant_pct
    census_complete = bool(total_products and eligible and not composition_missing and not technical
                           and not missing_rows and len(rows) == total_products)
    observed_score = score_for(plant_mean)
    assessed_counts = Counter(str(r.get("band") or "") for r in assessed_rows)
    final_score, animal_share, animal_qualifies, animal_applied = _animal_rule(assessed_counts, observed_score)

    if total_products <= 0 or eligible <= 0:
        status, score = "not_assessed", None
    elif census_complete:
        status, score = "verified", final_score
    elif scoreable >= MIN_SCOREABLE and readable:
        status, score = "partial_scan", final_score
    else:
        status, score = "partial_scan", None
    score_label = STANDARD["scoring"]["textile_score_labels"].get(str(score))

    priceable = selected(rows, lambda r: row_state(r) not in {"excluded_nontextile", "excluded_accessory"})
    priced = selected(priceable, lambda r: r.get("price") not in (None, "") or r.get("price_min") not in (None, ""))
    price_coverage = round(len(priced) / len(priceable) * 100, 1) if priceable else 0.0
    currencies = {str(r.get("currency")).upper() for r in priced if r.get("currency")}
    markets = {str(r.get("market")).upper() for r in rows if r.get("market")}
    locales = {str(r.get("locale")).lower() for r in rows if r.get("locale")}
    hosts = {str(r.get("source_host")).lower() for r in rows if r.get("source_host")}
    price_low = price_high = None
    for row in priced:
        for key in ("price_min", "price", "price_max"):
            value = row.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                price_low = float(value) if price_low is None else min(price_low, float(value))
                price_high = float(value) if price_high is None else max(price_high, float(value))
    quality_flags = []
    if disclosure_rate < FULL_DISCLOSURE:
        quality_flags.append("low_disclosure")
    if technical:
        quality_flags.append("technical_gap")
    if missing_rows:
        quality_flags.append("catalogue_partial")
    if any(r.get("component_incomplete") for r in rows):
        quality_flags.append("component_incomplete")
    if any(r.get("composition_partial") for r in rows):
        quality_flags.append("composition_partial")
    if any(r.get("composition_over_100") for r in rows):
        quality_flags.append("composition_over_100")
    if any(r.get("evidence_conflict") for r in rows):
        quality_flags.append("evidence_conflict")
    if priceable and price_coverage < 100:
        quality_flags.append("price_partial")
    if len(currencies) > 1 or len(markets) > 1 or len(locales) > 1 or len(hosts) > 1:
        quality_flags.append("locale_mixed")
    sampled = bool(rows and any(r.get("sampling_method") for r in rows))
    if sampled:
        quality_flags.append("sampled")
        if len(rows) < MIN_SCOREABLE:
            quality_flags.append("small_subset")

    publication_label = PUBLIC_LABELS.get(status, status.replace("_", " ").title())
    catalogue_explanation = (
        f"Throne identified {total_products:,} catalogue products. {eligible:,} are eligible textile "
        f"products; {excluded_accessory:,} accessories and {excluded_nontextile:,} non-textile items "
        f"are accounted for but out of scope for this score."
    )
    coverage_explanation = (
        f"Composition coverage is {coverage_rate:.1f}%: {scoreable:,} of {eligible:,} eligible textile "
        f"products have complete quantified composition; {technical:,} remain technical failures."
    )
    disclosure_explanation = (
        f"Disclosure is {disclosure_rate:.1f}%: {scoreable:,} of {readable:,} readable eligible "
        f"products had usable material evidence; {undisclosed:,} were successfully read but undisclosed."
    )
    score_basis = ("full_census" if census_complete else
                   "readable_subset" if score is not None else
                   "incomplete_census")
    score_explanation = (
        f"Composition mean across {scoreable:,} fully quantified products: "
        + (f"{plant_pct:.2f}% plant fibre. " if plant_pct is not None else "not available. ")
        + f"Composition coverage: {composition_coverage:.1f}%; {composition_missing:,} eligible products lack complete evidence. "
        + ("Full-catalogue composition evidence. " if census_complete else
           "Assessed products only; no inference about the unassessed catalogue. ")
        + "Missing, undisclosed and failed products have no numerical contribution."
    )
    animal_explanation = ""
    if animal_qualifies:
        animal_explanation = (
            f"Animal-material floor: animal products are {animal_share:.1f}% of classified animal, "
            "synthetic, and semi-synthetic products; the Standard floor is 2/5 at 95%."
        )
    status_explanation = (
        "Verified census: every eligible product has complete quantified composition and no technical failures."
        if census_complete else
        "Partial scan: one or more eligible products lack complete quantified composition; technical failures remain separate."
    )
    result = {
        "throne_standard_version": STANDARD_VERSION,
        "policy_version": STANDARD_VERSION,
        "methodology_version": STANDARD_VERSION,
        "scanner_version": SCANNER_VERSION,
        "reader_version": READER_VERSION,
        "scanned_at": scanned_at,
        "publication_status": status,
        "score_status": status,
        "publication_label": publication_label,
        "publication_status_explanation": status_explanation,
        "score": score,
        "throne_score": score,
        "throne_score_label": score_label,
        "observed_score": observed_score,
        "scoring_model": "textile-composition-plant-share-v1",
        "swimwear_score": None,
        "swimwear_natural_fibre_pct": None,
        "swimwear_eligible_count": 0,
        "swimwear_scoreable_count": 0,
        "swimwear_undisclosed_count": 0,
        "swimwear_technical_count": 0,
        "composition_plant_share_pct": plant_pct,
        "composition_coverage_pct": composition_coverage,
        "composition_assessed_count": scoreable,
        "composition_missing_count": composition_missing,
        "composition_score_scope": "full_catalogue" if census_complete else "assessed_products_only" if scoreable else "not_assessed",
        "catalogue_plant_share_lower_bound": round(plant_points / eligible, 2) if eligible else None,
        "catalogue_plant_share_upper_bound": round((plant_points + 100 * composition_missing) / eligible, 2) if eligible else None,
        "plant_pct": plant_pct,
        "full_catalogue_verified_plant_evidence_rate": plant_pct if census_complete else None,
        "plant_rate_among_disclosed_products": disclosed_plant_rate,
        "scoreable_skus": scoreable,
        "scored_skus": scoreable,
        "scored_evidence_count": scoreable,
        "eligible_textile_skus": eligible,
        "eligible_total": eligible,
        "readable_textile_skus": readable,
        "coverage_count": scoreable,
        "coverage_rate": coverage_rate,
        "catalogue_products_identified": total_products,
        "catalogue_total": total_products,
        "excluded_nontextile": excluded_nontextile,
        "excluded_accessory": excluded_accessory,
        "out_of_scope_count": excluded,
        "undisclosed": undisclosed,
        "undisclosed_skus": undisclosed,
        "undisclosed_count": undisclosed,
        "unread_count": technical,
        "reader_faults": technical,
        "technical_failures": technical,
        "scan_coverage": coverage_rate,
        "disclosure_rate": disclosure_rate,
        "disclosure_count": scoreable,
        "disclosed": scoreable,
        "coverage_read": scoreable,
        "coverage_total": eligible,
        "pure_plant": plant,
        "animal": counts.get("animal", 0),
        "synthetic": counts.get("synthetic", 0),
        "semi": counts.get("semi", 0),
        "blended": counts.get("blended", 0),
        "nontextile": excluded_nontextile,
        "accessory": excluded_accessory,
        "unclear": technical,
        "animal_share_of_classified_nonplant": animal_share,
        "animal_dominance_qualifies": animal_qualifies,
        "animal_floor_applied": animal_applied,
        "price_min": price_low if price_low is not None and len(currencies) == 1 and all(r.get("currency") for r in priced) else None,
        "price_max": price_high if price_high is not None and len(currencies) == 1 and all(r.get("currency") for r in priced) else None,
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "products_on_sale_count": sum(r.get("sale_price") not in (None, "") for r in rows),
        "price_coverage_count": len(priced),
        "price_coverage_total": len(priceable),
        "price_coverage_pct": price_coverage,
        "score_basis": score_basis,
        "catalogue_explanation": catalogue_explanation,
        "coverage_explanation": coverage_explanation,
        "disclosure_explanation": disclosure_explanation,
        "score_explanation": score_explanation,
        "animal_floor_explanation": animal_explanation,
        "quality_flags": quality_flags,
        "qualification_note": " ".join((status_explanation, score_explanation, coverage_explanation, disclosure_explanation, animal_explanation)),
        "clarity": {"status": publication_label, "score_basis": score_basis, "catalogue": catalogue_explanation,
                    "scan_coverage": coverage_explanation, "disclosure": disclosure_explanation,
                    "score": score_explanation, "animal_floor": animal_explanation},
    }
    if isinstance(rows, DiskRows):
        rows.close()
    return result


def canonicalize_result(result):
    result=dict(result or {}); rows=result.get("rows") or []
    scanned_at=result.get("scanned_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    category = result.get("category") or result.get("subcategory") or result.get("primary_category")
    result["rows"]=[annotate_row(r,scanned_at=scanned_at,category=category) for r in rows]
    result.update(_canonical(result["rows"],result.get("total_products",len(rows)),scanned_at,category=category))
    if result.get("sampling_enabled"):
        result.update(sampled_summary(result["rows"], result.get("sampling_population")))
        result["quality_flags"] = sorted(set(result.get("quality_flags", [])) | {"sampled"})
        result["publication_status_explanation"] = "A census was not completed; this result uses a random sample and is not a full-census score."
    result["domain"] = result.get("domain") or urlsplit(str(result.get("url") or "")).hostname or ""
    result["source_host"] = result.get("source_host") or result["domain"]
    result["duplicate_count"] = int(result.get("duplicate_products_removed", result.get("duplicate_count", 0)) or 0)
    result["duplicate_reasons"] = result.get("duplicate_reasons") or {}
    result["department"] = result.get("department") or "clothing"
    result["category"] = result.get("category") or "textile"
    result["market"] = result.get("market") or "unspecified"
    result["locale"] = result.get("locale") or "undetermined"
    result["min_possible_plant_pct"] = result.get("catalogue_plant_share_lower_bound")
    result["max_possible_plant_pct"] = result.get("catalogue_plant_share_upper_bound")
    result["textile"]=result.get("scoreable_skus",0); result["classified"]=result.get("scoreable_skus",0)
    return result


def api_brand_figure(rows, category=None):
    """Public textile projection. Swimwear follows ordinary textile scoring."""
    payload = {
        "rows": rows if isinstance(rows, RowStream) else list(rows or []),
        "total_products": len(rows or []),
    }
    if category:
        payload["category"] = category

    f = (_canonical(rows, len(rows), category=category)
         if isinstance(rows, RowStream)
         else canonicalize_result(payload))

    return {
        "plantPct": f["composition_plant_share_pct"],
        "composition_plant_share_pct": f["composition_plant_share_pct"],
        "composition_coverage_pct": f["composition_coverage_pct"],
        "composition_assessed_count": f["composition_assessed_count"],
        "composition_missing_count": f["composition_missing_count"],
        "composition_score_scope": f["composition_score_scope"],
        "catalogue_plant_share_lower_bound": f["catalogue_plant_share_lower_bound"],
        "catalogue_plant_share_upper_bound": f["catalogue_plant_share_upper_bound"],
        "naturalPct": None,
        "metricPct": f.get("plant_pct"),
        "metricLabel": "Plant fibre",
        "scoringModel": f.get("scoring_model"),
        "score": f["score"],
        "scoreLabel": f["throne_score_label"],
        "observedScore": f["observed_score"],
        "counts": {
            "plant": f["pure_plant"],
            "animal": f["animal"],
            "synthetic": f["synthetic"],
            "semi": f["semi"],
            "blended": f["blended"],
            "undisclosed": f["undisclosed"],
            "technical": f["technical_failures"],
            "excluded": f["out_of_scope_count"],
            "excluded_nontextile": f["excluded_nontextile"],
            "excluded_accessory": f["excluded_accessory"],
        },
        "assessed": f["scoreable_skus"],
        "scoreable": f["scoreable_skus"],
        "eligibleTextile": f["eligible_textile_skus"],
        "catalogue_total": f["catalogue_total"],
        "excluded_nontextile": f["excluded_nontextile"],
        "excluded_accessory": f["excluded_accessory"],
        "out_of_scope_count": f["out_of_scope_count"],
        "readableTextile": f["readable_textile_skus"],
        "unresolved": f["technical_failures"],
        "undisclosed": f["undisclosed"],
        "coverage": f["scan_coverage"],
        "disclosureRate": f["disclosure_rate"],
        "status": f["publication_status"],
        "statusLabel": f["publication_label"],
        "qualification": f["qualification_note"],
        "policyVersion": f["policy_version"],
        "throneStandardVersion": f["throne_standard_version"],
        "scannerVersion": f["scanner_version"],
        "readerVersion": f["reader_version"],
        "scoreBasis": f["score_basis"],
        "scoreExplanation": f["score_explanation"],
        "coverageExplanation": f["coverage_explanation"],
        "disclosureExplanation": f["disclosure_explanation"],
        "animalFloorApplied": f["animal_floor_applied"],
        "animalShareNonPlant": f["animal_share_of_classified_nonplant"],
        "animalFloorExplanation": f["animal_floor_explanation"],
        "price_min": f["price_min"],
        "price_max": f["price_max"],
        "currency": f["currency"],
        "products_on_sale_count": f["products_on_sale_count"],
        "quality_flags": f["quality_flags"],
        "price_coverage_count": f["price_coverage_count"],
        "price_coverage_total": f["price_coverage_total"],
        "price_coverage_pct": f["price_coverage_pct"],
        "priceCoveragePct": f["price_coverage_pct"],
        "minPct": f["catalogue_plant_share_lower_bound"],
        "maxPct": f["catalogue_plant_share_upper_bound"],
    }
