"""One plan for every question, one executor. Owner: Person B.

Every question becomes the same structure, so no kind of question can forget a filter:

    plan = {
      "thing":    "mine" | "state" | "port" | "country" | "company"   (reasoning/things.py KINDS),
      "include":  {"countries": [ISO2], "owners": [company ids], "states": [state ids]},
      "exclude":  {"countries": [...],  "owners": [...],          "states": [...]},
      "min_capacity_mtpa": float | None, "max_capacity_mtpa": float | None,
      "measures": [ {"kind": "weather", "weather": "rain"|..., "threshold": x|None}
                  | {"kind": "views"} | {"kind": "edits"}          (daily Wikipedia z, last 7 days vs 60)
                  | {"kind": "live_edits"}                          (live stream: edited in the last window_minutes)
                  | {"kind": "exposure"} ],
      "window_minutes": int,             # for live_edits ("right now" = 60, "last 10 minutes", "today")
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
            "top_n": None, "unrecognised": [], "source": "rules", "window_minutes": 60}


# ------------------------------------------------------------------ rules
def _vocab() -> list[tuple[str, str, str]]:
    """(lowercase phrase, field, value) for every place/company we know, longest first."""
    from reasoning.listing import REGIONS, _country_names, _owner_terms
    from reasoning.things import things

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
    from reasoning.things import detect_kind

    plan["thing"] = detect_kind(t)

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
    live = re.search(r"\b(?:right now|now|currently|live|at the moment|this hour|past hour|last hour|today|"
                     r"(?:last|past)\s+\d+\s*(?:min\w*|hours?|hrs?))\b", low)
    if re.search(r"\bedit", low) and (live or re.search(r"\bbeing edited\b", low)) \
            and not re.search(r"\b(?:spike|burst|surge|unusual)\b", low):
        plan["measures"].append({"kind": "live_edits"})
        m = re.search(r"(?:last|past)\s+(\d+)\s*(min\w*|hours?|hrs?)", low)
        if m:
            plan["window_minutes"] = int(m.group(1)) * (1 if m.group(2).startswith("min") else 60)
        elif re.search(r"\btoday\b|24 hours", low):
            plan["window_minutes"] = 1440
    elif wiki and re.search(r"\bedit", low):
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
        "thing": {"enum": ["mine", "state", "port", "country", "company"]},
        "include_countries": {"type": "array", "items": {"type": "string"}},
        "exclude_countries": {"type": "array", "items": {"type": "string"}},
        "include_owners": {"type": "array", "items": {"type": "string"}},
        "exclude_owners": {"type": "array", "items": {"type": "string"}},
        "include_states": {"type": "array", "items": {"type": "string"}},
        "exclude_states": {"type": "array", "items": {"type": "string"}},
        "conditions": {"type": "array", "items": {"enum": ["rain", "snow", "wind", "storm", "freezing", "cold",
                                                          "hot", "any_weather", "views", "edits", "live_edits"]}},
        "window_minutes": {"type": ["integer", "null"]},
        "combine": {"enum": ["and", "or"]},
        "min_capacity_mtpa": {"type": ["number", "null"]},
        "max_capacity_mtpa": {"type": ["number", "null"]},
        "top_n": {"type": ["integer", "null"]},
    },
}
MODEL_PROMPT = (
    "Turn this question into a plan. thing: iron ore mines, US states, export ports, countries or mining "
    "companies. kind: 'list' (show/filter things, no event), 'condition' (things where a condition holds: "
    "weather now, a spike in Wikipedia views or edits over the last week ('views'/'edits'), or Wikipedia pages "
    "being edited right now / in the last N minutes ('live_edits', with window_minutes)), or 'exposure' (things "
    "exposed to a named event or policy). Put places and companies the user "
    "wants in include_*, and places/companies they want left out ('outside', 'not', 'except') in exclude_*. "
    "Countries as English names or ISO-2 codes; expand regions into countries. Places or companies that are "
    "part of the EVENT itself (e.g. 'US' in 'exposed to the US midterms') are NOT filters. Question: ")


def _resolve(names: list[str], field: str, plan: dict) -> list[str]:
    from reasoning.listing import _country_names, _owner_terms
    from reasoning.things import things

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
    if obj.get("window_minutes"):
        plan["window_minutes"] = int(obj["window_minutes"])
    else:
        plan["window_minutes"] = rules["window_minutes"]
    for c in obj.get("conditions") or []:
        if c in ("views", "edits", "live_edits"):
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
    """(all things of the plan's kind, those passing every filter). Filters are applied here, once,
    for every kind: countries (where the thing is / is listed), owners (mines owned by / the company
    itself), named US states, capacity (mines)."""
    from reasoning.things import things

    all_items = things(plan["thing"])
    inc, exc = plan["include"], plan["exclude"]
    kind = plan["thing"]

    def ok(it: dict) -> bool:
        if inc["countries"] and it["country"] not in inc["countries"]:
            return False
        if it["country"] in exc["countries"]:
            return False
        if kind in ("mine", "company"):
            if inc["owners"] and not set(it["owners"]) & set(inc["owners"]):
                return False
            if set(it["owners"]) & set(exc["owners"]):
                return False
        if kind == "state":
            if inc["states"] and it["id"] not in inc["states"]:
                return False
            if it["id"] in exc["states"]:
                return False
        if kind == "mine":
            cap = it["capacity"]
            if plan["min_capacity_mtpa"] is not None and (cap is None or cap < float(plan["min_capacity_mtpa"])):
                return False
            if plan["max_capacity_mtpa"] is not None and (cap is None or cap > float(plan["max_capacity_mtpa"])):
                return False
        return True

    return all_items, [it for it in all_items if ok(it)]


def describe(plan: dict) -> str:
    """Human description of the filters ('in China; not owned by BHP; ≥ 10 Mtpa')."""
    from reasoning.listing import _graph

    names = dict(zip(_graph().nodes["id"], _graph().nodes["name"]))
    from reasoning.things import things

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
    if m["kind"] == "live_edits":
        return "Wikipedia page edited in the last " + _window_words(m.get("window_minutes", 60))
    if m["weather"] == "any":
        return "notable weather"
    label = WEATHER[m["weather"]][4]
    if m.get("threshold") is not None:
        op = "≥" if m["weather"] == "hot" else "≤"
        label += f" ({op} {m['threshold']:g} °C)"
    return label


def _window_words(minutes: int) -> str:
    if minutes % 1440 == 0:
        return "24 hours" if minutes == 1440 else f"{minutes // 1440} days"
    if minutes % 60 == 0:
        return "hour" if minutes == 60 else f"{minutes // 60} hours"
    return f"{minutes} minutes"


def _ago(ts: float, now: float) -> str:
    s = max(0, int(now - ts))
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago"


def _live_edits(m: dict, items: list[dict]) -> dict[str, dict]:
    """Things whose Wikipedia page was edited in the window, from the live stream (pipeline/live_wiki.py)."""
    import time

    from pipeline.live_wiki import LIVE

    now = time.time()
    since = now - 60 * m.get("window_minutes", 60)
    out = {}
    for it in items:
        if not it["wiki_title"]:
            continue
        edits = LIVE.edits_for(it["wiki_title"], since)
        if not edits:
            continue
        n = len(edits)
        humans = [e for e in edits if not e["bot"]]
        last = edits[-1]
        comment = f": \u201c{last['comment'][:80]}\u201d" if last["comment"] else ""
        out[it["id"]] = {
            "score": round(1.0 - 0.6 ** n, 4), "z": 0.0,
            "tier": "high" if n >= 5 else "medium" if n >= 2 else "low",
            "reason": (f"Wikipedia page '{it['wiki_title']}'{it.get('wiki_note', '')} edited {n} time{'s' if n > 1 else ''} "
                       f"in the last {_window_words(m.get('window_minutes', 60))} ({len(humans)} by people, "
                       f"{n - len(humans)} by bots); latest {_ago(last['ts'], now)} by {last['user']}"
                       f"{' (bot)' if last['bot'] else ''}{comment}."),
            "evidence": [{"title": f"Edit by {e['user']}{' (bot)' if e['bot'] else ''}, {_ago(e['ts'], now)}"
                                   + (f": {e['comment'][:70]}" if e['comment'] else ""),
                          "url": e["url"], "date": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(e["ts"])),
                          "judge": "relevant", "confidence": 1.0} for e in edits[::-1][:5]]}
    return out


def _evaluate(m: dict, all_items: list[dict], items: list[dict], end: date) -> dict[str, dict]:
    """thing id -> {"score", "tier", "reason", "evidence", "z"} for things meeting one condition."""
    from reasoning import screen as sc

    if m["kind"] == "live_edits":
        return _live_edits(m, items)
    out: dict[str, dict] = {}
    if m["kind"] in ("views", "edits"):
        what = "views" if m["kind"] == "views" else "edits"
        measured = sc._wiki_measure(m["kind"], items, end)
        for it in items:
            r = measured.get(it["wiki_title"]) if it["wiki_title"] else None
            tier = sc._tier_z(r["z"]) if r else None
            if not tier:
                continue
            via = it.get("wiki_note", "")
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


def _live_status_note() -> str:
    import time

    from pipeline.live_wiki import LIVE

    st = LIVE.status()
    if st["connected"]:
        lag = (st["now"] - st["last_event_ts"]) if st["last_event_ts"] else None
        catching = f" (catching up: stream is {int(lag // 60)} min behind)" if lag and lag > 120 else ""
        return (f"Live Wikimedia edit stream connected{catching}; {st['matched_24h']} edits to watched pages in "
                f"the last 24 h. ")
    if st["matched_24h"]:
        newest = max(e["ts"] for e in LIVE.recent(1)) if LIVE.recent(1) else None
        return (f"Live stream NOT connected: answering from the last snapshot"
                + (f" (newest edit {_ago(newest, time.time())})" if newest else "") + ". ")
    return "Live stream NOT connected and no snapshot yet, so nothing can match. "


def execute(plan: dict, text: str, end: date | None = None) -> dict:
    from backend.schemas import QueryResult
    from reasoning.screen import BASELINE_DAYS, RECENT_DAYS, WEATHER, ANY_KINDS
    from reasoning.things import KINDS

    end = end or date.today()
    kind_info = KINDS[plan["thing"]]
    all_items, items = select_things(plan)
    thing_label = kind_info["label"]
    where = describe(plan)
    filtered = len(items) != len(all_items)
    for m in plan["measures"]:
        if m["kind"] == "live_edits":
            m["window_minutes"] = plan.get("window_minutes", 60)
    # Measures that make no sense for this kind (weather at a company, exposure of a port) are dropped
    # and SAID so, never silently.
    unsupported = [m for m in plan["measures"] if m["kind"] not in kind_info["measures"]]
    measures = [m for m in plan["measures"] if m["kind"] in kind_info["measures"]]
    conditions = [m for m in measures if m["kind"] in ("weather", "views", "edits", "live_edits")]
    exposure = any(m["kind"] == "exposure" for m in measures)
    note = _notes(plan, len(items), thing_label, where if filtered else "")
    if unsupported:
        note += ("Not available for " + thing_label + ": "
                 + ", ".join(sorted({"event exposure" if m["kind"] == "exposure" else _measure_label(m) for m in unsupported}))
                 + " (ignored). ")
    if any(m["kind"] == "live_edits" for m in conditions):
        note += _live_status_note()

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
            elif m["kind"] == "live_edits":
                lab = _measure_label(m)
                ev_name = f"{lab[:1].upper()}{lab[1:]}"
                channel = {"id": "live_edits",
                           "reason": "Live English Wikipedia edits to each thing's page (Wikimedia EventStreams, one "
                                     "streaming connection, updated within seconds). 1 edit low, 2-4 medium, 5+ high. "
                                     "Edits show attention, not importance."}
            else:
                lab = _measure_label(m)
                fields = ", ".join(WEATHER[k][4] for k in ANY_KINDS) if m["weather"] == "any" else WEATHER[m["weather"]][0]
                ev_name = f"{lab[:1].upper()}{lab[1:]} right now"
                channel = {"id": "weather", "reason": f"Current weather at each thing's coordinates from Open-Meteo "
                                                      f"({fields}). Display only: weather now, not a forecast."}
        else:
            ev_name = f"{labels[:1].upper()}{labels[1:]}" + ("" if any(m["kind"] == "live_edits" for m in conditions) else " right now")
            channel = {"id": "conditions", "reason": f"Each thing must meet {'all' if plan['combine'] == 'and' else 'at least one'} "
                                                     f"of: {labels}. Weather from Open-Meteo, attention and edits from Wikipedia."}
        ev_name += f" — {thing_label}" + (f" {where}" if where else "")
        channel["reason"] = note + channel["reason"]
        event = {"name": ev_name, "date": end.isoformat(), "terms": [m.get("weather") or m["kind"] for m in conditions]}
    else:
        from reasoning.things import list_rows

        results = list_rows(plan["thing"], items)
        desc = ("Operating iron ore mines" if plan["thing"] == "mine" else thing_label[:1].upper() + thing_label[1:]) \
            + (f" {where}" if where else "")
        ranked = {"mine": "ranked by design capacity", "port": "ranked by the capacity of mines shipping through",
                  "country": "ranked by the capacity of mines there", "company": "ranked by the capacity of mines held",
                  "state": "listed"}[plan["thing"]]
        event = {"name": desc, "date": "", "terms": []}
        channel = {"id": "filter", "reason": note + f"No event or condition in the question, so this is a list: {desc}, "
                                                    f"{ranked} (top third high, middle medium, rest low). Ask about an "
                                                    f"event ('exposed to …') or a condition ('in the rain', 'being "
                                                    f"edited right now') for scores."}
    if plan["top_n"]:
        results = results[: plan["top_n"]]
    out = {"criteria": {"asset_type": kind_info["asset_type"], "event": event,
                        "window": {"baseline_days": BASELINE_DAYS, "recent_days": RECENT_DAYS},
                        "status": ["operating"] if plan["thing"] == "mine" else [],
                        "region": "US" if plan["thing"] == "state" else "global"},
           "channels": [channel], "results": results, "dark_count": len(all_items) - len(results)}
    return QueryResult.model_validate(out).model_dump()


def answer(text: str, backends: list | None = None, end: date | None = None) -> dict:
    """The one entry point for any question."""
    return execute(make_plan(text, backends=backends), text, end=end)
