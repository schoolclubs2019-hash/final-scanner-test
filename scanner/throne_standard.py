#!/usr/bin/env python3
"""Load the canonical, machine-readable Throne Standard without third-party dependencies."""
import json
from pathlib import Path


_REPOSITORY_STANDARD_PATH = Path(__file__).resolve().parents[2] / "_Shared" / "Throne-Standard" / "throne_standard.json"
_PACKAGED_STANDARD_PATH = Path(__file__).resolve().with_name("throne_standard.json")
STANDARD_PATH = _REPOSITORY_STANDARD_PATH if _REPOSITORY_STANDARD_PATH.exists() else _PACKAGED_STANDARD_PATH
STANDARD = json.loads(STANDARD_PATH.read_text(encoding="utf-8"))
VERSION = STANDARD["version"]


def material_taxonomy():
    """Return canonical material terms and aliases mapped to one Standard class."""
    terms = {}
    for material_class, materials in STANDARD["material_taxonomy"].items():
        for canonical, aliases in materials.items():
            for term in (canonical, *aliases):
                key = term.casefold()
                previous = terms.get(key)
                if previous and previous != (canonical, material_class):
                    raise ValueError(f"Ambiguous Standard material term: {term}")
                terms[key] = (canonical, material_class)
    return terms


MATERIAL_TERMS = material_taxonomy()


def public_methodology_payload():
    """Return the public API representation directly from the canonical Standard."""
    return {
        "version": VERSION,
        "effective_date": STANDARD["effective_date"],
        "score": STANDARD["scoring"],
        "catalogue": STANDARD["catalogue"],
        "composition": STANDARD["composition"],
        "disclosure": STANDARD["disclosure"],
        "technical_failures": STANDARD["technical_failures"],
        "material_taxonomy": STANDARD["material_taxonomy"],
        "evidence": STANDARD["evidence"],
        "sampling": STANDARD["sampling"],
        "department_modules": STANDARD["department_modules"],
        "changelog": STANDARD["changelog"],
    }


def render_public_methodology():
    """Render a public-facing methodology page from the canonical Standard."""
    payload = public_methodology_payload()
    scoring = payload["score"]
    disclosure = payload["disclosure"]
    sampling = payload["sampling"]
    lines = [
        "# Throne Standard",
        "",
        f"Version: {payload['version']}",
        f"Effective date: {payload['effective_date']}",
        "",
        "## What a Throne Score means",
        f"Textile score formula: {scoring['textile_formula']}.",
        "The result is based on verified composition evidence, not an assumption about undisclosed products.",
        "Score bands:",
    ]
    for band in scoring["score_bands"]:
        ceiling = band["maximum_percent_exclusive"]
        bound = "and above" if ceiling is None else f"to below {ceiling}%"
        lines.append(f"- {band['score']}/5 ({band['label']}): {band['minimum_percent']}% {bound}.")
    if scoring["animal_floor"]["enabled"]:
        lines.append(
            f"Animal floor: a {scoring['animal_floor']['minimum_share_percent']}% classified "
            f"animal share sets a minimum score of {scoring['animal_floor']['minimum_score']}/5."
        )
    lines.extend([
        "",
        "## Catalogue coverage",
        payload["catalogue"]["target"],
        f"Eligible product evidence: {', '.join(payload['catalogue']['eligible_product_evidence'])}.",
        f"Deduplication precedence: {' > '.join(payload['catalogue']['deduplication_precedence'])}.",
        "",
        "## Disclosure",
        disclosure["definition"],
        disclosure["score_treatment"],
        f"The {disclosure['transparency_threshold_percent']}% threshold is a transparency flag, not a statistical guarantee.",
        "",
        "## Reader failures",
        f"Technical states: {', '.join(payload['technical_failures']['states'])}.",
        payload["technical_failures"]["score_policy"],
        "",
        "## Composition and components",
        payload["composition"]["incomplete_percentages"],
        payload["composition"]["unquantified_materials"],
        payload["composition"]["components"]["equal_component_fallback"]["required_explanation"],
        "",
        "## Material classifications",
    ])
    for material_class, materials in payload["material_taxonomy"].items():
        lines.append(f"- {material_class.replace('_', ' ').title()}: {', '.join(materials)}")
    lines.extend(["", "## Evidence hierarchy"])
    lines.extend(f"{index}. {source.replace('_', ' ')}" for index, source in enumerate(payload["evidence"]["hierarchy"], 1))
    lines.extend(["", "Accepted evidence sources:"])
    lines.extend(f"- {source}" for source in payload["evidence"]["accepted_sources"])
    lines.extend(["", "Rejected evidence types:"])
    lines.extend(f"- {source}" for source in payload["evidence"]["rejected_types"])
    lines.extend(["", payload["evidence"]["conflicts"]["rule"]])
    lines.extend([
        "",
        "## Sampling fallback",
        f"Sampling is used only when a census cannot be achieved. The target is {sampling['confidence_level']:.0%} confidence, "
        f"+/-{sampling['target_margin_of_error']:.0%} precision, and p={sampling['conservative_proportion']:.1f} with finite population correction.",
        f"Select randomly or with appropriate stratification; never take the first {sampling['large_population_approximation']} products.",
        "",
        "## Category-specific rules",
    ])
    lines.extend(f"- {category}: {module.replace('_', ' ')}" for category, module in payload["department_modules"].items())
    lines.extend(["", "## Changelog"])
    for entry in payload["changelog"]:
        lines.append(f"### {entry['version']} - {entry['effective_date']}")
        lines.extend(f"- {change}" for change in entry["changes"])
    return "\n".join(lines) + "\n"
