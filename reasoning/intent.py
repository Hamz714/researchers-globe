"""Route any question to the right kind of answer. Owner: Person B.

Three question kinds, all answered in the frozen QueryResult shape:
  * list      "show all mines", "mines in Brazil owned by Vale", "10 largest mines"  -> reasoning/listing.py
  * condition "mines in the rain", "US states with a spike in Wikipedia views"      -> reasoning/screen.py
  * exposure  "mines exposed to the 2026 US midterms", "... affected by sanctions"  -> reasoning/run_query.py

route(text) -> (kind, payload). Order:
  1. a prepared preset question               -> exposure (cached result)
  2. explicit exposure wording ("exposed to", "affected by", "impact of", ...) -> exposure
  3. a recognised live condition (rule-based)  -> condition
  4. the model classifies the question (fallback LLM via decision_model.parse, schema-checked, cached)
  5. no model: list, with filters read by rules (countries, regions, owners, "largest N", capacity)
An exposure question whose event the parser can't find also falls back to a list (see backend/app.py).
"""
from __future__ import annotations

import re

from reasoning import decision_model
from reasoning.listing import _country_names, _owner_terms, parse_filters
from reasoning.screen import WEATHER, parse_screen

EXPOSURE_CUES = re.compile(
    r"\b(expos\w*|affect\w*|impact\w*|hit by|because of|due to|consequences?|vulnerab\w*|at risk|"
    r"risk from|threat\w*|disrupt\w*|linked to|connected to|tied to|related to|means? for|effects? of|"
    r"influenc\w*|suffer\w*|benefit\w*|react\w*)\b", re.I)

INTENT_SCHEMA = {
    "type": "object",
    "required": ["kind"],
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": ["list", "condition", "exposure"],
                 "description": "list = show/filter assets with no event; condition = assets where a live "
                                "condition holds now (weather, Wikipedia views/edits spike); exposure = assets "
                                "exposed to a named event or policy"},
        "thing": {"enum": ["mine", "state"]},
        "countries": {"type": "array", "items": {"type": "string"},
                      "description": "country names or ISO-2 codes the user restricts to (expand regions)"},
        "owners": {"type": "array", "items": {"type": "string"}, "description": "company names"},
        "min_capacity_mtpa": {"type": ["number", "null"]},
        "top_n": {"type": ["integer", "null"]},
        "condition": {"enum": [*WEATHER.keys(), "any_weather", "views", "edits", None]},
    },
}


def _to_filters(obj: dict, text: str) -> dict:
    """Model output -> listing filters, keeping only values that exist in our data."""
    names = _country_names()
    countries = []
    for c in obj.get("countries") or []:
        iso = c.upper() if len(c) == 2 and c.isalpha() else names.get(c.lower())
        if iso and iso not in countries:
            countries.append(iso)
    terms = _owner_terms()
    owners = []
    for o in obj.get("owners") or []:
        cid = terms.get(o.lower()) or next((cid for t, cid in terms.items() if o.lower() in t or t in o.lower()), None)
        if cid and cid not in owners:
            owners.append(cid)
    rules = parse_filters(text)  # the rules catch what the model leaves out (and vice versa)
    return {"thing": obj.get("thing") or rules["thing"],
            "countries": countries or rules["countries"], "owners": owners or rules["owners"],
            "min_capacity_mtpa": obj.get("min_capacity_mtpa", rules["min_capacity_mtpa"]),
            "top_n": obj.get("top_n") or rules["top_n"]}


def _condition_spec(obj: dict, text: str) -> dict | None:
    c = obj.get("condition")
    thing = obj.get("thing") or parse_filters(text)["thing"]
    if c in ("views", "edits"):
        return {"thing": thing, "condition": c}
    if c in WEATHER or c == "any_weather":
        return {"thing": thing, "condition": "weather", "weather": "any" if c == "any_weather" else c}
    return None


def route(text: str, backends: list | None = None) -> tuple[str, dict | None]:
    from reasoning.parser import load_presets

    norm = " ".join(text.lower().split())
    if any(" ".join(p["query"].lower().split()) == norm for p in load_presets()):
        return "exposure", None
    if EXPOSURE_CUES.search(text):
        return "exposure", None
    spec = parse_screen(text)
    if spec is not None:
        return "condition", spec

    def rules() -> dict:
        return {"kind": "list"}

    obj = decision_model.parse(
        "Classify this question about iron ore mines or US states and extract its filters. "
        f"Question: {text}", INTENT_SCHEMA, fallback=rules, backends=backends)
    if obj.get("kind") == "exposure":
        return "exposure", None
    if obj.get("kind") == "condition":
        spec = _condition_spec(obj, text)
        if spec:
            return "condition", spec
    return "list", _to_filters(obj, text)
