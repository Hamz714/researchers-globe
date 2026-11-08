"""Condition screens: "which <things> where <condition>?" (the demo's second question type).

    screen("Which US states have a spike in Wikipedia views?")       # attention
    screen("Which iron ore mines have a spike in Wikipedia edits?")  # edits
    screen("Which iron ore mines are in the rain right now?")        # weather
    parse_screen(text) -> spec or None   (None: not a screen; run_query handles it)

Things: iron ore mines (GEM, data/graph) and US states (pipeline/curated/us_states.csv).
Conditions:
  * views: Wikipedia pageviews of the thing's article (a mine's own article, else its
    owner's), weekday-adjusted robust z of the last 7 days vs the 60 before (signals.attention_burst);
  * edits: live edit counts per day (fetch_wiki.edits), robust z, same windows;
  * weather: current conditions from Open-Meteo (rain, snow, wind, storm, freezing, cold, hot).
The answer has the frozen QueryResult shape: criteria.event names the condition, one
channel (attention | edits | weather) says how it was measured, every match is a Result
whose `reason` gives the measured value and source. Things without data, or that do not
meet the condition, are counted as dark. A score is a ranking, not a probability.
"""
from __future__ import annotations

import csv
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
STATES_CSV = ROOT / "pipeline" / "curated" / "us_states.csv"
BASELINE_DAYS, RECENT_DAYS = 60, 7

# HAND-SET tiers. Wikipedia z (views, edits): low >= 1.5, medium >= 2.5, high >= 4.
Z_TIERS = (1.5, 2.5, 4.0)
# Weather: (field, comparison, default threshold, unit, label). Tiers by how far past the
# threshold: ratio >= 1 low, >= 2 medium, >= 4 high (temperature: 0, 5, 10 degrees past it).
WEATHER = {
    "rain":     ("precipitation", ">", 0.1, "mm", "rain"),
    "snow":     ("snowfall", ">", 0.05, "cm", "snow"),
    "wind":     ("wind_speed_10m", ">=", 30.0, "km/h", "wind"),
    "storm":    ("weather_code", ">=", 95, "", "thunderstorm"),
    "freezing": ("temperature_2m", "<=", 0.0, "°C", "freezing"),
    "cold":     ("temperature_2m", "<=", 5.0, "°C", "cold"),
    "hot":      ("temperature_2m", ">=", 30.0, "°C", "hot"),
}
WEATHER_WORDS = [  # first match wins
    (r"thunder|storm", "storm"), (r"snow", "snow"), (r"rain|raining|wet|precipitation", "rain"),
    (r"wind|windy|gust", "wind"), (r"freez|frost|below zero|sub-?zero", "freezing"),
    (r"\bcold\b|chilly|colder|(?:below|under) -?\d", "cold"), (r"\bhot\b|heat|warmer|(?:above|over) -?\d", "hot"),
    # No specific condition named ("bad weather", "extreme weather", "weather at mines"): check all of them.
    (r"\bweather\b", "any"),
]
ANY_KINDS = ["storm", "snow", "rain", "wind", "freezing", "hot"]  # "cold" (<= 5 °C) is too common to count as notable


# ---------------------------------------------------------------- parsing (rule-based, deterministic)
def parse_screen(text: str) -> dict | None:
    t = " ".join(text.lower().split())
    thing = "state" if re.search(r"\bstates?\b", t) else "mine" if re.search(r"\bmines?\b", t) else None
    if thing is None:
        return None
    wiki = re.search(r"wikipedia|\bwiki\b", t)
    if wiki and re.search(r"\bedit", t):
        return {"thing": thing, "condition": "edits"}
    if wiki or re.search(r"page ?views|attention", t):
        return {"thing": thing, "condition": "views"}
    for pat, kind in WEATHER_WORDS:
        if re.search(pat, t):
            spec = {"thing": thing, "condition": "weather", "weather": kind}
            m = re.search(r"(?:below|under|above|over|than)\s+(-?\d+(?:\.\d+)?)", t)
            if m:
                spec["threshold"] = float(m.group(1))
            return spec
    return None


# ---------------------------------------------------------------- things
def _states() -> list[dict]:
    with STATES_CSV.open(newline="") as f:
        return [{"id": r["state_id"], "name": r["name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                 "wiki_title": r["wiki_title"], "wiki_of": r["name"]} for r in csv.DictReader(f)]


def _mines() -> list[dict]:
    from pipeline.graph import default_graph

    g = default_graph()
    n, e = g.nodes, g.edges
    names = dict(zip(n["id"], n["name"]))
    wiki = dict(zip(n["id"], n["wiki_title"]))
    owners = e[e["type"] == "OWNED_BY"].sort_values("weight", ascending=False).groupby("src")["dst"].first()
    out = []
    for r in n[n["type"] == "mine"].itertuples():
        title, of = (r.wiki_title, r.name) if isinstance(r.wiki_title, str) and r.wiki_title else (None, None)
        owner = owners.get(r.id)
        if not title and owner and isinstance(wiki.get(owner), str) and wiki.get(owner):
            title, of = wiki[owner], names[owner]
        out.append({"id": r.id, "name": r.name, "lat": float(r.lat), "lon": float(r.lon),
                    "wiki_title": title, "wiki_of": of})
    return out


def things(kind: str) -> list[dict]:
    return _states() if kind == "state" else _mines()


# ---------------------------------------------------------------- measures
def _wiki_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))


def _tier_z(z: float) -> str | None:
    lo, mid, hi = Z_TIERS
    return "high" if z >= hi else "medium" if z >= mid else "low" if z >= lo else None


def _squash(x: float, scale: float) -> float:
    """0..1, monotone: a ranking score, not a probability."""
    return round(1.0 - math.exp(-max(0.0, x) / scale), 4)


def _wiki_measure(kind: str, items: list[dict], end: date) -> dict[str, dict]:
    """title -> {"z", "recent", "baseline_median", "last_day"} for every unique title."""
    from pipeline import fetch_wiki
    from pipeline.signals import attention_burst, robust_z, split_windows

    start = end - timedelta(days=BASELINE_DAYS + RECENT_DAYS)
    titles = sorted({i["wiki_title"] for i in items if i["wiki_title"]})

    def one(title: str):
        try:
            s = fetch_wiki.pageviews(title, start, end) if kind == "views" else fetch_wiki.edits(title, start, end)
        except Exception:
            return title, None
        if not len(s):
            return title, None
        z = attention_burst(s, BASELINE_DAYS, RECENT_DAYS, end) if kind == "views" else robust_z(s, BASELINE_DAYS, RECENT_DAYS, end)
        b, r = split_windows(s, BASELINE_DAYS, RECENT_DAYS, end)
        return title, {"z": float(z), "recent": float(r.sum()), "baseline_median": float(b.median()) if len(b) else 0.0,
                       "last_day": s.index.max().date().isoformat(), "series": s}

    # pageviews: REST API, a few in parallel; edits: the Action API asks for serial requests
    workers = 2 if kind == "views" else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return {t: m for t, m in ex.map(one, titles) if m is not None}


def _weather_match(cur: dict, kind: str, threshold: float | None) -> tuple[float, str] | None:
    if kind == "any":  # strongest of all notable conditions (each at its default threshold)
        hits = [h for k in ANY_KINDS if (h := _weather_match(cur, k, None))]
        return max(hits, key=lambda h: h[0]) if hits else None
    field, op, default, unit, label = WEATHER[kind]
    th = default if threshold is None else threshold
    v = cur.get(field)
    if v is None:
        return None
    v = float(v)
    if op == ">" and v > th or op == ">=" and v >= th:
        ratio = v / th if th > 0 else 4.0
        return ratio, f"{label}: {v:g} {unit} now (threshold {th:g} {unit})".replace("  ", " ")
    if op == "<=" and v <= th:
        return 1.0 + (th - v) / 5.0, f"{label}: {v:g} {unit} now (threshold {th:g} {unit})"
    return None


# ---------------------------------------------------------------- screen
def screen(text: str, spec: dict | None = None, end: date | None = None) -> dict:
    from backend.schemas import QueryResult

    spec = spec or parse_screen(text)
    if spec is None:
        raise ValueError("not a condition screen")
    end = end or date.today()
    from reasoning.listing import describe_filters, has_filters, matching_mine_ids, parse_filters

    all_items = things(spec["thing"])
    # Filters in the same question ("mines in China in the rain", "BHP mines with a Wikipedia spike",
    # "rain in Texas") restrict which things are checked; the rest stay dark.
    filters = spec.get("filters") or parse_filters(text)
    items, where = all_items, ""
    if spec["thing"] == "mine" and has_filters(filters):
        keep = matching_mine_ids(filters)
        items, where = [i for i in all_items if i["id"] in keep], describe_filters({**filters, "top_n": None})
    elif spec["thing"] == "state":
        named = [i for i in all_items if re.search(r"\b" + re.escape(i["name"].lower()) + r"\b", text.lower())]
        if named:
            items, where = named, "in " + ", ".join(i["name"] for i in named)
    thing_label = "US states" if spec["thing"] == "state" else "iron ore mines"
    results = []

    if spec["condition"] in ("views", "edits"):
        what = "views" if spec["condition"] == "views" else "edits"
        measured = _wiki_measure(spec["condition"], items, end)
        event = {"name": f"Spike in Wikipedia {what}", "date": end.isoformat(), "terms": [f"wikipedia {what}"]}
        channel = {"id": "attention" if what == "views" else "edits",
                   "reason": (f"Wikipedia {what} of each thing's article ({'the mine, else its owner' if spec['thing'] == 'mine' else 'the state'}): "
                              f"last {RECENT_DAYS} days vs the {BASELINE_DAYS} before, robust z"
                              + (", weekday-adjusted" if what == "views" else "") + ".")}
        for it in items:
            m = measured.get(it["wiki_title"]) if it["wiki_title"] else None
            tier = _tier_z(m["z"]) if m else None
            if not tier:
                continue
            via = "" if it["wiki_of"] == it["name"] else f" (via its owner {it['wiki_of']})"
            reason = (f"Wikipedia {what} of '{it['wiki_title']}'{via}: {m['recent']:,.0f} in the last {RECENT_DAYS} days "
                      f"vs a baseline median of {m['baseline_median']:,.0f}/day, z={m['z']:.1f} "
                      f"(data to {m['last_day']}).")
            results.append({
                "id": it["id"], "name": it["name"], "lat": it["lat"], "lon": it["lon"], "tier": tier,
                "score": _squash(m["z"], 4.0),
                "components": {"exposure": 0.0, "news_z": 0.0, "tone_shift": 0.0,
                               "attention_z": round(m["z"], 3), "relevant_articles": 0},
                "paths": [], "reason": reason,
                "evidence": [{"title": f"Wikipedia: {it['wiki_title']}", "url": _wiki_url(it["wiki_title"]),
                              "date": m["last_day"], "judge": "relevant", "confidence": 1.0}],
            })
    else:
        from pipeline import fetch_weather

        kind, th = spec["weather"], spec.get("threshold")
        # Always ask for ALL things: the weather cache is keyed by the full point set, so a filtered
        # subset would miss it (and cost extra API calls). Filtering happens below.
        cur = fetch_weather.current([(it["id"], it["lat"], it["lon"]) for it in all_items])
        if kind == "any":
            label, measured_fields = "notable weather", ", ".join(f"{WEATHER[k][4]}" for k in ANY_KINDS)
        else:
            label, measured_fields = WEATHER[kind][4], WEATHER[kind][0]
        event = {"name": f"{label[:1].upper()}{label[1:]} right now", "date": end.isoformat(), "terms": [kind]}
        channel = {"id": "weather", "reason": f"Current weather at each {spec['thing']}'s coordinates from Open-Meteo "
                                              f"({measured_fields}). Display only: weather now, not a forecast."}
        for it in items:
            c = cur.get(it["id"])
            hit = _weather_match(c, kind, th) if c else None
            if not hit:
                continue
            ratio, desc = hit
            tier = "high" if ratio >= 4 else "medium" if ratio >= 2 else "low"
            results.append({
                "id": it["id"], "name": it["name"], "lat": it["lat"], "lon": it["lon"], "tier": tier,
                "score": _squash(ratio, 2.0),
                "components": {"exposure": 0.0, "news_z": 0.0, "tone_shift": 0.0, "attention_z": 0.0,
                               "relevant_articles": 0},
                "paths": [], "reason": f"{desc[:1].upper()}{desc[1:]} at {c.get('time', '')} UTC (Open-Meteo).",
                "evidence": [],
            })

    results.sort(key=lambda r: (-r["score"], r["name"]))
    if filters.get("top_n"):
        results = results[: int(filters["top_n"])]
    if where:
        event = {**event, "name": f"{event['name']} — {thing_label} {where}"}
        channel = {**channel, "reason": f"Checked only {len(items)} {thing_label} {where}. " + channel["reason"]}
    out = {
        "criteria": {"asset_type": "us_state" if spec["thing"] == "state" else "iron_ore_mine", "event": event,
                     "window": {"baseline_days": BASELINE_DAYS, "recent_days": RECENT_DAYS},
                     "status": ["operating"] if spec["thing"] == "mine" else [], "region": "US" if spec["thing"] == "state" else "global"},
        "channels": [channel],
        "results": results,
        "dark_count": len(all_items) - len(results),
    }
    return QueryResult.model_validate(out).model_dump()


def series(kind: str, thing_id: str, end: date | None = None) -> dict | None:
    """Daily series behind a screen result, for the detail chart: {"label", "unit", "dates", "values",
    "recent_days", "source"}. kind: views | edits. None if the thing has no Wikipedia article."""
    import pandas as pd

    from pipeline import fetch_wiki

    end = end or date.today()
    item = next((i for i in things("state" if thing_id.startswith("state_") else "mine") if i["id"] == thing_id), None)
    if not item or not item["wiki_title"]:
        return None
    start = end - timedelta(days=BASELINE_DAYS + RECENT_DAYS)
    s = fetch_wiki.pageviews(item["wiki_title"], start, end) if kind == "views" else fetch_wiki.edits(item["wiki_title"], start, end)
    if not len(s):
        return None
    # highlight the same calendar window the z uses: days after end - RECENT_DAYS (some may be unpublished)
    recent = int((s.index > pd.Timestamp(end - timedelta(days=RECENT_DAYS))).sum())
    return {"label": f"Wikipedia {kind} per day: {item['wiki_title']}", "unit": kind, "recent_days": recent,
            "dates": [d.date().isoformat() for d in s.index], "values": [float(v) for v in s.values],
            "source": _wiki_url(item["wiki_title"])}
