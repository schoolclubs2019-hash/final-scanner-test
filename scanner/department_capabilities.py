"""Canonical validated scoring and evidence-capture capabilities by department."""


CAPABILITIES = {
    "clothing": {
        "state": "validated", "reader": "textile", "scoring": True,
        "publishing": True, "enumerator": True,
    },
    "beauty": {
        "state": "validated", "reader": "inci_read", "scoring": True,
        "publishing": True, "enumerator": False,
    },
    "food": {
        "state": "capture_only", "reader": "ingredient_read", "scoring": False,
        "publishing": False, "enumerator": False,
    },
    "homeware": {
        "state": "capture_only", "reader": "evidence_capture", "scoring": False,
        "publishing": False, "enumerator": False,
    },
    "hotels": {
        "state": "capture_only", "reader": "evidence_capture", "scoring": False,
        "publishing": False, "enumerator": False,
    },
}


# CPT routes without an approved rubric still return evidence and an explicit status.
for _department in ("bedding", "supplements", "pet", "jewellery", "candles", "electronics",
                    "household", "vehicle", "airlines", "femcare", "toys", "case", "nappies", "product"):
    CAPABILITIES[_department] = {
        "state": "capture_only", "reader": "evidence_capture", "scoring": False,
        "publishing": False, "enumerator": False,
    }
CAPABILITIES["bedding"] = dict(CAPABILITIES["clothing"])


def capability(department):
    return CAPABILITIES.get(str(department or "").strip().lower(), {
        "state": "locked", "reader": None, "scoring": False,
        "publishing": False, "enumerator": False,
    })