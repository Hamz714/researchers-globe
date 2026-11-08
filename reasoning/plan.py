"""One plan for every question, one executor. Owner: Person B.

Every question becomes the same structure, so no kind of question can forget a filter:

    plan = {
      "thing":    "mine" | "state",
      "include":  {"countries": [ISO2], "owners": [company ids], "states": [state ids]},
      "exclude":  {"countries": [...],  "owners": [...],          "states": [...]},
      "min_capacity_mtpa": float | None, "max_capacity_mtpa": float | None,
      "measures": [ {"kind": "weather", "weather": "rain"|..., "threshold": x|None}
                  | {"kind": "views"} | {"kind": "edits"} | {"kind": "exposure"} ],
      "combine":  "and" | "or",           # how several conditions combine
      "top_n":    int | None,
      "unrecognised": [str],              # place-like words we could not match: reported, never silently dropped
      "source":   "rules" | "model" | "preset",
    }

    make_plan(text)      -> plan   (rules always; the model, when reachable, is merged in; code validates)
    execute(plan, text)  -> QueryResult (frozen contract)

execute() ALWAYS applies the filters first (select_things), then evaluates the measures on what is
left: no measures = a list ranked by size; weather/views/edits = live conditions combined with
and/or; exposure = the event pipeline restricted to the filtered mines (plus any conditions, ANDed).
"""
from __future__ import annotations

import re
from datetime import date

from reasoning import decision_model

TIER_RANK = {"low": 0, "medium": 1, "high": 2}
RANK_TIER = {v: k for k, v in TIER_RANK.items()}

# ------------------------------------------------------------------ vocabulary
# Each weather word maps to a condition. Word boundaries everywhere ("Bahrain" is not rain).
WEATHER_PATTERNS = [
    (r"\bthunder\w*|\bstorm\w*", "storm"),
    (r"\bsnow\w*", "snow"),
    (r"\brain\w*|\bwet\b|\bprecipitation\b|\bshowers?\b", "rain"),
    (r"\bwind(?:y|s)?\b|\bgust\w*", "wind"),
    (r"\bfreez\w*|\bfrost\w*|\bbelow zero\b|\bsub-?zero\b", "freezing"),
    (r"\bcold\b|\bchilly\b|\bcolder\b", "cold"),
    (r"\bhot\b|\bheat\b|\bheatwave\b|\bwarm(?:er)?\b", "hot"),
]
NEGATION = re.compile(r"(?:\boutside(?: of)?|\bnot(?: in| owned by| from)?|\bexcept(?: in| for)?|\bexcluding|"
                      r"\bother than|\bapart from|\bbut not|\bwithout|\bnon-|\bex-)\s*(?:the\s+)?$", re.I)
EXPOSURE_CUES = re.compile(
    r"\b(expos\w*|affect\w*|impact\w*|hit by|because of|due to|consequences?|vulnerab\w*|at risk|"
    r"risk from|threat\w*|disrupt\w*|linked to|connected to|tied to|related to|means? for|effects? of|"
    r"influenc\w*|suffer\w*|benefit\w*|react\w*)\b", re.I)
CAPACITY_UNIT = r"\s*(?:mtpa|mt\b|million\s*(?:tonnes|tons|t)?)"
# Capitalised words after "in/from/near/..." that are not places in our data are reported.
PLACE_INTRO = re.compile(r"\b(?:in|from|near|across|around|outside(?: of)?|except(?: in)?|excluding)\s+(?:the\s+)?"
                         r"([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)")
NOT_PLACES = {"wikipedia", "the", "rain", "us", "usa", "u.s.", "uk"}


def empty_plan() -> dict:
    return {"thing": "mine", "include": {"countries": [], "owners": [], "states": []},
            "exclude": {"countries": [], "owners": [], "states": []},
            "min_capacity_mtpa": None, "max_capacity_mtpa": None, "measures": [], "combine": "and",
            "top_n": None, "unrecognised": [], "source": "rules"}


# ------------------------------------------------------------------ rules
def _vocab() -> list[tuple[str, str, str]]:
    """(lowercase phrase, field, value) for every place/company we know, longest first."""
    from reasoning.listing import REGIONS, _country_names, _owner_terms
    from reasoning.screen import things

    out = [(name, "countries", iso) for name, iso in _country_names().items()]
    out += [(region, "regions", region) for region in REGIONS]
    out += [(term, "owners", cid) for term, cid in _owner_terms().items()]
    out += [(s["name"].lower(), "states", s["id"]) for s in things("state")]
    return sorted(out, key=lambda x: -len(x[0]))


def _mentions(text: str) -> list[tuple[int, int, str, str]]:
    """Non-overlapping (start, end, field, value) for known places/companies in the text."""
    low = text.lower()
    taken: list[tuple[int, int]] = []
    found = []
    for phrase, field, value in _vocab():
        for m in re.finditer(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", low):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append((m.start(), m.end(), field, value))
    for short, iso in (("US", "US"), ("USA", "US"), ("U.S.", "US"), ("UK", "GB")):  # case-sensitive
        for m in re.finditer(r"(?<![\w.])" + re.escape(short) + r"(?![\w])", text):
            if not any(a < m.end() and m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                found.append((m.start(), m.end(), "countries", iso))
    return sorted(found)


def _numbers_with_units(low: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"-?\d+(?:\.\d+)?" + CAPACITY_UNIT, low)]


def rule_plan(text: str) -> dict:
    from reasoning.listing import REGIONS, _WORDS

    plan = empty_plan()
    t = " ".join(text.split())
    low = t.lower()
    mentions_mines = re.search(r"\bmines?\b|\bmining\b", low)
    plan["thing"] = "state" if re.search(r"\bstates?\b", low) and not mentions_mines else "mine"

    # Places named inside the event ("exposed to the US midterms", "... sanctions on Russia") describe the
    # event, not which mines to look at: only mentions BEFORE the exposure wording are filters.
    cue = EXPOSURE_CUES.search(t)
    cutoff = cue.start() if cue else len(t)

    # places and companies, each included or excluded by the words just before it
    prev_end, prev_negated = None, False
    for start, end, field, value in _mentions(t):
        if start >= cutoff:
            continue
        # A list continues the previous polarity: "not in China or Brazil", "excluding Vale, BHP and Rio Tinto"
        in_list = prev_end is not None and re.fullmatch(r"[\s,]*(?:or|and|nor|,)?[\s,]*", t[prev_end:start] or " ")
        negated = prev_negated if in_list else bool(NEGATION.search(t[:start][-30:]))
        prev_end, prev_negated = end, negated
        bucket = plan["exclude"] if negated else plan["include"]
        if field == "regions":
            for iso in REGIONS[value]:
                if iso not in bucket["countries"]:
                    bucket["countries"].append(iso)
        elif field == "states":
            if plan["thing"] == "state" and value not in bucket["states"]:
                bucket["states"].append(value)
        elif field == "countries" and plan["thing"] == "state" and value == "US":
            continue  # "US states": not a filter
        elif value not in bucket[field]:
            bucket[field].append(value)

    # Exclusions win: "South America except Brazil"
    for field in ("countries", "owners", "states"):
        plan["include"][field] = [v for v in plan["include"][field] if v not in plan["exclude"][field]]

    # unrecognised place-like phrases ("mines in the Midwest")
    known = {m[0] for m in _vocab()}
    for m in PLACE_INTRO.finditer(t[:cutoff]):
        phrase = m.group(1).strip()
        words = phrase.lower()
        if words in NOT_PLACES or any(words == k or words.startswith(k + " ") or k.startswith(words) for k in known):
            continue
        plan["unrecognised"].append(phrase)

    # capacity
    m = re.search(r"(?:over|above|more than|at least|>=?)\s*(\d+(?:\.\d+)?)" + CAPACITY_UNIT, low)
    if m:
        plan["min_capacity_mtpa"] = float(m.group(1))
    m = re.search(r"(?:under|below|less than|at most|<=?)\s*(\d+(?:\.\d+)?)" + CAPACITY_UNIT, low)
    if m:
        plan["max_capacity_mtpa"] = float(m.group(1))

    # top N
    m = re.search(r"\b(?:top|largest|biggest)\s+(\d+|" + "|".join(_WORDS) + r")\b", low) or \
        re.search(r"\b(\d+|" + "|".join(_WORDS) + r")\s+(?:largest|biggest)\b", low)
    if m:
        plan["top_n"] = int(_WORDS.get(m.group(1), m.group(1)))
    elif re.search(r"\b(?:largest|biggest)\b", low):
        plan["top_n"] = 10

    # measures
    if EXPOSURE_CUES.search(t):
        plan["measures"].append({"kind": "exposure"})
    wiki = re.search(r"\bwikipedia\b|\bwiki\b", low)
    if wiki and re.search(r"\bedit", low):
        plan["measures"].append({"kind": "edits"})
    elif wiki or re.search(r"\bpage ?views?\b|\battention\b", low):
        plan["measures"].append({"kind": "views"})
    unit_spans = _numbers_with_units(low)
    weather_hits = []
    for pat, kind in WEATHER_PATTERNS:
        for m in re.finditer(pat, low):
            weather_hits.append((m.start(), kind))
    for pos, kind in sorted(weather_hits):
        if any(c["kind"] == "weather" and c["weather"] == kind for c in plan["measures"]):
            continue
        if kind == "cold" and any(c.get("weather") == "freezing" for c in plan["measures"]):
            continue
        plan["measures"].append({"kind": "weather", "weather": kind, "threshold": None})
    # temperature thresholds: "below 0", "above 30 degrees" (not capacity numbers)
    for m in re.finditer(r"(below|under|above|over|than)\s+(-?\d+(?:\.\d+)?)", low):
        if any(a <= m.start(2) < b for a, b in unit_spans):
            continue
        val, above = float(m.group(2)), m.group(1) in ("above", "over")
        kind = "hot" if above else ("freezing" if val <= 0 else "cold")
        existing = [c for c in plan["measures"] if c["kind"] == "weather" and c["weather"] in ("hot", "cold", "freezing")]
        if existing:
            existing[0]["threshold"] = val
        elif re.search(r"degree|°|celsius|temperature|\bc\b", low):
            plan["measures"].append({"kind": "weather", "weather": kind, "threshold": val})
    if re.search(r"\bweather\b", low) and not any(c["kind"] == "weather" for c in plan["measures"]):
        plan["measures"].append({"kind": "weather", "weather": "any", "threshold": None})
    cond_pos = sorted(p for p, _ in weather_hits)
    if len(cond_pos) >= 2 and re.search(r"\bor\b", low[cond_pos[0]:cond_pos[-1]]):
        plan["combine"] = "or"
    return plan


# ------------------------------------------------------------------ model
MODEL_SCHEMA = {
    "type": "object",
    "required": ["kind"],
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": ["list", "condition", "exposure"]},
        "thing": {"enum": ["mine", "state"]},
        "include_countries": {"type": "array", "items": {"type": "string"}},
        "exclude_countries": {"type": "array", "items": {"type": "string"}},
        "include_owners": {"type": "array", "items": {"type": "string"}},
        "exclude_owners": {"type": "array", "items": {"type": "string"}},
        "include_states": {"type": "array", "items": {"type": "string"}},
        "exclude_states": {"type": "array", "items": {"type": "string"}},
        "conditions": {"type": "array", "items": {"enum": ["rain", "snow", "wind", "storm", "freezing", "cold",
                                                          "hot", "any_weather", "views", "edits"]}},
        "combine": {"enum": ["and", "or"]},
        "min_capacity_mtpa": {"type": ["number", "null"]},
        "max_capacity_mtpa": {"type": ["number", "null"]},
        "top_n": {"type": ["integer", "null"]},
    },
}
MODEL_PROMPT = (
    "Turn this question about iron ore mines or US states into a plan. kind: 'list' (show/filter things, no "
    "event), 'condition' (things where a live condition holds now: weather, or a spike in Wikipedia views or "
    "edits), or 'exposure' (things exposed to a named event or policy). Put places and companies the user "
    "wants in include_*, and places/companies they want left out ('outside', 'not', 'except') in exclude_*. "
    "Countries as English names or ISO-2 codes; expand regions into countries. Places or companies that are "
    "part of the EVENT itself (e.g. 'US' in 'exposed to the US midterms') are NOT filters. Question: ")


def _resolve(names: list[str], field: str, plan: dict) -> list[str]:
    from reasoning.listing import _country_names, _owner_terms
    from reasoning.screen import things

    out = []
    for raw in names or []:
        n = raw.strip()
        if field == "countries":
            v = n.upper() if len(n) == 2 and n.isalpha() else _country_names().get(n.lower())
        elif field == "owners":
            terms = _owner_terms()
            v = terms.get(n.lower()) or next((cid for t, cid in terms.items() if len(t) >= 4 and (t in n.lower() or n.lower() in t)), None)
        else:
            v = next((s["id"] for s in things("state") if s["name"].lower() == n.lower() or s["id"] == n), None)
        if v and v not in out:
            out.append(v)
        elif not v and n and n not in plan["unrecognised"]:
            plan["unrecognised"].append(n)
    return out


def model_plan(obj: dict, rules: dict, text: str = "") -> dict:
    """Validated model output -> plan. Rules fill what the model left empty; names must exist in our data.
    Code still guards the model: a place it lists as a filter but that the text only names inside the
    event ("... exposed to the US midterms") is dropped."""
    plan = empty_plan()
    plan["source"] = "model"
    plan["thing"] = obj.get("thing") or rules["thing"]
    plan["unrecognised"] = list(rules["unrecognised"])
    cue = EXPOSURE_CUES.search(text)
    cutoff = cue.start() if cue else len(text)
    for side in ("include", "exclude"):
        for field in ("countries", "owners", "states"):
            vals = _resolve(obj.get(f"{side}_{field}") or [], field, plan)
            if vals and cutoff < len(text):
                before = {v for st, _, f, v in _mentions(text) if st < cutoff}
                after = {v for st, _, f, v in _mentions(text) if st >= cutoff}
                vals = [v for v in vals if v in before or v not in after]
            plan[side][field] = vals or rules[side][field]
    for k in ("min_capacity_mtpa", "max_capacity_mtpa", "top_n"):
        plan[k] = obj.get(k) if obj.get(k) is not None else rules[k]
    plan["combine"] = obj.get("combine") or rules["combine"]
    measures = []
    if obj.get("kind") == "exposure":
        measures.append({"kind": "exposure"})
    for c in obj.get("conditions") or []:
        if c in ("views", "edits"):
            measures.append({"kind": c})
        else:
            th = next((m.get("threshold") for m in rules["measures"] if m.get("weather") == c), None)
            measures.append({"kind": "weather", "weather": "any" if c == "any_weather" else c, "threshold": th})
    if obj.get("kind") == "condition" and not measures:
        measures = [m for m in rules["measures"] if m["kind"] != "exposure"]
    plan["measures"] = measures if obj.get("kind") != "list" else []
    if not plan["measures"] and rules["measures"]:
        # The model said "list" but the words clearly name a condition or an event: keep them, never drop them.
        plan["measures"] = rules["measures"]
    return plan


def make_plan(text: str, backends: list | None = None) -> dict:
    from reasoning.parser import load_presets

    norm = " ".join(text.lower().split())
    if any(" ".join(p["query"].lower().split()) == norm for p in load_presets()):
        plan = empty_plan()
        plan.update(measures=[{"kind": "exposure"}], source="preset")
        return plan
    rules = rule_plan(text)
    obj = decision_model.parse(MODEL_PROMPT + text, MODEL_SCHEMA, fallback=lambda: None, backends=backends)
    return model_plan(obj, rules, text) if obj else rules


# ------------------------------------------------------------------ execution
def select_things(plan: dict) -> tuple[list[dict], list[dict]]:
    """(all things of the plan's kind, those passing every filter). Filters are applied here, once."""
    from reasoning.screen import things

    all_items = things(plan["thing"])
    inc, exc = plan["include"], plan["exclude"]
    if plan["thing"] == "state":
        keep = [s for s in all_items if (not inc["states"] or s["id"] in inc["states"]) and s["id"] not in exc["states"]]
        return all_items, keep
    from reasoning.listing import _graph

    g = _graph()
    n, e = g.nodes, g.edges
    mines = n[n["type"] == "mine"]
    if inc["countries"]:
        mines = mines[mines["country"].isin(inc["countries"])]
    if exc["countries"]:
        mines = mines[~mines["country"].isin(exc["countries"])]
    owned = e[e["type"] == "OWNED_BY"]
    if inc["owners"]:
        mines = mines[mines["id"].isin(set(owned.loc[owned["dst"].isin(inc["owners"]), "src"]))]
    if exc["owners"]:
        mines = mines[~mines["id"].isin(set(owned.loc[owned["dst"].isin(exc["owners"]), "src"]))]
    cap = mines["capacity_mtpa"].astype(float)
    if plan["min_capacity_mtpa"] is not None:
        mines = mines[cap >= float(plan["min_capacity_mtpa"])]
        cap = mines["capacity_mtpa"].astype(float)
    if plan["max_capacity_mtpa"] is not None:
        mines = mines[cap <= float(plan["max_capacity_mtpa"])]
    keep_ids = set(mines["id"])
    return all_items, [i for i in all_items if i["id"] in keep_ids]


def describe(plan: dict) -> str:
    """Human description of the filters ('in China; not owned by BHP; ≥ 10 Mtpa')."""
    from reasoning.listing import _graph

    names = dict(zip(_graph().nodes["id"], _graph().nodes["name"]))
    from reasoning.screen import things

    names.update({s["id"]: s["name"] for s in things("state")})
    nm = lambda ids, pre: names.get(f"{pre}{ids}", ids)  # noqa: E731
    parts = []
    for side, word in (("include", ""), ("exclude", "not ")):
        f = plan[side]
        if f["countries"]:
            cs = [nm(c, "country_") for c in f["countries"]]
            parts.append(f"{word}in " + ", ".join(cs[:8]) + ("…" if len(cs) > 8 else ""))
        if f["owners"]:
            parts.append(f"{word}owned by " + ", ".join(names.get(o, o) for o in f["owners"]))
        if f["states"]:
            parts.append(f"{word}in " + ", ".join(names.get(s, s) for s in f["states"]))
    if plan["min_capacity_mtpa"] is not None:
        parts.append(f"≥ {plan['min_capacity_mtpa']:g} Mtpa")
    if plan["max_capacity_mtpa"] is not None:
        parts.append(f"≤ {plan['max_capacity_mtpa']:g} Mtpa")
    return "; ".join(parts)


def _measure_label(m: dict) -> str:
    from reasoning.screen import WEATHER

    if m["kind"] == "views":
        return "spike in Wikipedia views"
    if m["kind"] == "edits":
        return "spike in Wikipedia edits"
    if m["weather"] == "any":
        return "notable weather"
    label = WEATHER[m["weather"]][4]
    if m.get("threshold") is not None:
        op = "≥" if m["weather"] == "hot" else "≤"
        label += f" ({op} {m['threshold']:g} °C)"
    return label


def _evaluate(m: dict, all_items: list[dict], items: list[dict], end: date) -> dict[str, dict]:
    """thing id -> {"score", "tier", "reason", "evidence", "z"} for things meeting one condition."""
    from reasoning import screen as sc

    out: dict[str, dict] = {}
    if m["kind"] in ("views", "edits"):
        what = "views" if m["kind"] == "views" else "edits"
        measured = sc._wiki_measure(m["kind"], items, end)
        for it in items:
            r = measured.get(it["wiki_title"]) if it["wiki_title"] else None
            tier = sc._tier_z(r["z"]) if r else None
            if not tier:
                continue
            via = "" if it["wiki_of"] == it["name"] else f" (via its owner {it['wiki_of']})"
            out[it["id"]] = {
                "score": sc._squash(r["z"], 4.0), "tier": tier, "z": r["z"],
                "reason": (f"Wikipedia {what} of '{it['wiki_title']}'{via}: {r['recent']:,.0f} in the last "
                           f"{sc.RECENT_DAYS} days vs a baseline median of {r['baseline_median']:,.0f}/day, "
                           f"z={r['z']:.1f} (data to {r['last_day']})."),
                "evidence": [{"title": f"Wikipedia: {it['wiki_title']}", "url": sc._wiki_url(it["wiki_title"]),
                              "date": r["last_day"], "judge": "relevant", "confidence": 1.0}]}
        return out
    from pipeline import fetch_weather

    # Ask for ALL things: the weather cache is keyed by the full point set (a subset would miss it).
    cur = fetch_weather.current([(it["id"], it["lat"], it["lon"]) for it in all_items])
    for it in items:
        c = cur.get(it["id"])
        hit = sc._weather_match(c, m["weather"], m.get("threshold")) if c else None
        if not hit:
            continue
        ratio, desc = hit
        out[it["id"]] = {"score": sc._squash(ratio, 2.0), "z": 0.0,
                         "tier": "high" if ratio >= 4 else "medium" if ratio >= 2 else "low",
                         "reason": f"{desc[:1].upper()}{desc[1:]} at {c.get('time', '')} UTC (Open-Meteo).",
                         "evidence": []}
    return out


def _combine(hits: list[dict[str, dict]], how: str) -> dict[str, dict]:
    ids = set.intersection(*[set(h) for h in hits]) if how == "and" else set.union(*[set(h) for h in hits])
    out = {}
    for i in ids:
        parts = [h[i] for h in hits if i in h]
        pick = min if how == "and" else max
        out[i] = {"score": round(pick(p["score"] for p in parts), 4),
                  "tier": RANK_TIER[pick(TIER_RANK[p["tier"]] for p in parts)],
                  "z": max(p["z"] for p in parts),
                  "reason": " ".join(p["reason"] for p in parts),
                  "evidence": [e for p in parts for e in p["evidence"]]}
    return out


def _result(it: dict, hit: dict) -> dict:
    return {"id": it["id"], "name": it["name"], "lat": it["lat"], "lon": it["lon"], "tier": hit["tier"],
            "score": hit["score"],
            "components": {"exposure": 0.0, "news_z": 0.0, "tone_shift": 0.0,
                           "attention_z": round(hit["z"], 3), "relevant_articles": 0},
            "paths": [], "reason": hit["reason"], "evidence": hit["evidence"]}


def _notes(plan: dict, checked: int, thing_label: str, where: str) -> str:
    notes = []
    if where:
        notes.append(f"Checked only the {checked} {thing_label} {where}.")
    if plan["unrecognised"]:
        notes.append("Not recognised, so not used as a filter: " + ", ".join(f"'{u}'" for u in plan["unrecognised"]) + ".")
    return (" ".join(notes) + " ") if notes else ""


def execute(plan: dict, text: str, end: date | None = None) -> dict:
    from backend.schemas import QueryResult
    from reasoning.screen import BASELINE_DAYS, RECENT_DAYS, WEATHER, ANY_KINDS

    end = end or date.today()
    all_items, items = select_things(plan)
    thing_label = "US states" if plan["thing"] == "state" else "iron ore mines"
    where = describe(plan)
    filtered = len(items) != len(all_items)
    conditions = [m for m in plan["measures"] if m["kind"] in ("weather", "views", "edits")]
    exposure = any(m["kind"] == "exposure" for m in plan["measures"])
    note = _notes(plan, len(items), thing_label, where if filtered else "")

    if exposure and plan["thing"] == "mine":
        from reasoning.run_query import NoEvent, run_query

        try:
            out = run_query(text, end=end, only={i["id"] for i in items} if filtered else None)
        except NoEvent:
            out = None
        if out is not None:
            if conditions:  # "mines exposed to X that are in the rain": keep exposed mines meeting the conditions
                hits = _combine([_evaluate(m, all_items, [i for i in items if i["id"] in {r["id"] for r in out["results"]}], end)
                                 for m in conditions], plan["combine"])
                kept = [r for r in out["results"] if r["id"] in hits]
                for r in kept:
                    r["reason"] = r["reason"] + " Also: " + hits[r["id"]]["reason"]
                out["dark_count"] += len(out["results"]) - len(kept)
                out["results"] = kept
                labels = f" {plan['combine']} ".join(_measure_label(m) for m in conditions)
                out["criteria"]["event"]["name"] += f" — and {labels} now"
            if plan["top_n"]:
                out["dark_count"] += max(0, len(out["results"]) - plan["top_n"])
                out["results"] = out["results"][: plan["top_n"]]
            if note or where:
                out["channels"] = [{**c, "reason": note + c["reason"]} for c in out["channels"]]
                if where:
                    out["criteria"]["event"]["name"] += f" ({where})"
            return QueryResult.model_validate(out).model_dump()
        conditions = conditions  # no event found: fall through to conditions or a list

    if conditions:
        hits = _combine([_evaluate(m, all_items, items, end) for m in conditions], plan["combine"])
        results = sorted((_result(i, hits[i["id"]]) for i in items if i["id"] in hits), key=lambda r: (-r["score"], r["name"]))
        labels = f" {plan['combine']} ".join(_measure_label(m) for m in conditions)
        if len(conditions) == 1:
            m = conditions[0]
            if m["kind"] in ("views", "edits"):
                what = "views" if m["kind"] == "views" else "edits"
                ev_name = f"Spike in Wikipedia {what}"
                channel = {"id": "attention" if what == "views" else "edits",
                           "reason": f"Wikipedia {what} of each thing's article: last {RECENT_DAYS} days vs the "
                                     f"{BASELINE_DAYS} before, robust z" + (", weekday-adjusted." if what == "views" else ".")}
            else:
                lab = _measure_label(m)
                fields = ", ".join(WEATHER[k][4] for k in ANY_KINDS) if m["weather"] == "any" else WEATHER[m["weather"]][0]
                ev_name = f"{lab[:1].upper()}{lab[1:]} right now"
                channel = {"id": "weather", "reason": f"Current weather at each thing's coordinates from Open-Meteo "
                                                      f"({fields}). Display only: weather now, not a forecast."}
        else:
            ev_name = f"{labels[:1].upper()}{labels[1:]} right now"
            channel = {"id": "conditions", "reason": f"Each thing must meet {'all' if plan['combine'] == 'and' else 'at least one'} "
                                                     f"of: {labels}. Weather from Open-Meteo, attention from Wikipedia."}
        if where:
            ev_name += f" — {thing_label} {where}"
        channel["reason"] = note + channel["reason"]
        event = {"name": ev_name, "date": end.isoformat(), "terms": [m.get("weather") or m["kind"] for m in conditions]}
    else:
        from reasoning.listing import list_rows

        results = list_rows(plan["thing"], items)
        desc = ("US states" if plan["thing"] == "state" else "Operating iron ore mines") + (f" {where}" if where else "")
        ranked = "ranked by design capacity (top third high, middle medium, rest or unknown low)" \
            if plan["thing"] == "mine" else "listed"
        event = {"name": desc, "date": "", "terms": []}
        channel = {"id": "filter", "reason": note + f"No event or condition in the question, so this is a list: {desc}, "
                                                    f"{ranked}. Ask about an event ('exposed to …') or a condition "
                                                    f"('in the rain') for scores."}
    if plan["top_n"]:
        results = results[: plan["top_n"]]
    out = {"criteria": {"asset_type": "us_state" if plan["thing"] == "state" else "iron_ore_mine", "event": event,
                        "window": {"baseline_days": BASELINE_DAYS, "recent_days": RECENT_DAYS},
                        "status": ["operating"] if plan["thing"] == "mine" else [],
                        "region": "US" if plan["thing"] == "state" else "global"},
           "channels": [channel], "results": results, "dark_count": len(all_items) - len(results)}
    return QueryResult.model_validate(out).model_dump()


def answer(text: str, backends: list | None = None, end: date | None = None) -> dict:
    """The one entry point for any question."""
    return execute(make_plan(text, backends=backends), text, end=end)
