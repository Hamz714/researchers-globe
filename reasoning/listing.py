"""List / filter questions: "show all mines", "iron ore mines in Brazil", "mines owned by BHP",
"the 10 largest mines in Australia", "show all US states". Owner: Person B.

These have no event, so the exposure pipeline (which needs an event to search news for) does
not apply: we filter the asset table and rank by size. Deterministic, offline, instant.

    parse_filters(text)  -> filters dict (rule-based; reasoning/intent.py may fill it from the model)
    list_assets(filters) -> QueryResult (frozen contract)

filters = {"thing": "mine" | "state", "countries": [ISO2...], "owners": [company ids...],
           "min_capacity_mtpa": float | None, "top_n": int | None}

Tiers rank by GEM design capacity among the matches (top third high, middle medium, rest or
unknown capacity low). The score is capacity / largest match: a size ranking, not a probability.
"""
from __future__ import annotations

import re
from functools import lru_cache

from reasoning.touches import COUNTRY_ALIASES

GEM_CREDIT = "GEM Global Iron Ore Mines Tracker, Sep 2026"

# Regions -> ISO2 codes of producing countries in our data (rule-based fallback only).
REGIONS = {
    "africa": ["DZ", "EG", "GA", "GN", "KE", "LR", "MA", "MR", "MZ", "NA", "NG", "SL", "SD", "TN", "ZA", "ZM", "CM", "CG", "CD", "AO", "ML"],
    "south america": ["AR", "BO", "BR", "CL", "CO", "PE", "VE"],
    "latin america": ["AR", "BO", "BR", "CL", "CO", "PE", "VE", "MX"],
    "north america": ["CA", "MX", "US"],
    "europe": ["AT", "BA", "BG", "DE", "ES", "FI", "FR", "GB", "GR", "HU", "IT", "NO", "PL", "RO", "RS", "SE", "SI", "SK", "UA", "BY", "RU"],
    "asia": ["AF", "BD", "CN", "IN", "ID", "IR", "JP", "KH", "KP", "KR", "KZ", "LA", "MN", "MY", "NP", "PH", "PK", "SA", "TH", "TR", "UZ", "VN", "OM", "QA"],
    "middle east": ["IR", "SA", "OM", "QA", "TR", "EG"],
    "oceania": ["AU", "NZ"],
}
_WORDS = {"one": 1, "two": 2, "three": 3, "five": 5, "ten": 10, "twenty": 20}


@lru_cache(maxsize=1)
def _graph():
    from pipeline.graph import default_graph

    return default_graph()


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    """lowercase name/alias -> ISO2, for every country in the graph."""
    n = _graph().nodes
    out = {}
    for r in n[n["type"] == "country"].itertuples():
        out[str(r.name).lower()] = r.country
        for a in COUNTRY_ALIASES.get(r.country, []):
            if not (a.isupper() and len(a) <= 3):  # skip "US"/"UK" here: handled case-sensitively below
                out[a.lower()] = r.country
    return out


@lru_cache(maxsize=1)
def _owner_terms() -> dict[str, str]:
    """lowercase company term -> company id, for companies that own at least one mine."""
    g = _graph()
    e, n = g.edges, g.nodes
    owners = set(e.loc[e["type"] == "OWNED_BY", "dst"])
    out = {}
    for r in n[n["id"].isin(owners)].itertuples():
        for t in str(r.search_terms or r.name).split("|"):
            if len(t) >= 3:
                out[t.lower()] = r.id
    return out


def parse_filters(text: str) -> dict:
    t = " ".join(text.split())
    low = t.lower()
    thing = "state" if re.search(r"\bstates?\b", low) and not re.search(r"\bmines?\b", low) else "mine"
    countries: list[str] = []
    no_regions = low
    for region in REGIONS:  # "South America" must not read as "America" -> US
        no_regions = re.sub(r"\b" + region + r"\b", " ", no_regions)
    for name, iso in sorted(_country_names().items(), key=lambda kv: -len(kv[0])):
        if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", no_regions) and iso not in countries:
            countries.append(iso)
    if thing == "mine":
        for short, iso in (("US", "US"), ("USA", "US"), ("UK", "GB")):
            if re.search(r"(?<![\w.])" + short + r"(?![\w])", t) and iso not in countries:
                countries.append(iso)
    for region, isos in REGIONS.items():
        if re.search(r"\b" + region + r"\b", low):
            countries += [c for c in isos if c not in countries]
    owners: list[str] = []
    for term, cid in sorted(_owner_terms().items(), key=lambda kv: -len(kv[0])):
        if re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", low) and cid not in owners:
            owners.append(cid)
    top_n = None
    m = re.search(r"\b(?:top|largest|biggest)\s+(\d+|" + "|".join(_WORDS) + r")\b", low) or \
        re.search(r"\b(\d+|" + "|".join(_WORDS) + r")\s+(?:largest|biggest)\b", low)
    if m:
        top_n = int(_WORDS.get(m.group(1), m.group(1)))
    elif re.search(r"\b(?:largest|biggest)\b", low):
        top_n = 10
    min_cap = None
    m = re.search(r"(?:over|above|more than|at least|>=?)\s*(\d+(?:\.\d+)?)\s*(?:mtpa|mt|million)", low)
    if m:
        min_cap = float(m.group(1))
    if thing == "state":  # a state question may name states
        countries, owners = [], []
    return {"thing": thing, "countries": countries, "owners": owners, "min_capacity_mtpa": min_cap, "top_n": top_n}


def _states(names_in_text: str) -> list[dict]:
    from reasoning.screen import things

    states = things("state")
    named = [s for s in states if re.search(r"\b" + re.escape(s["name"].lower()) + r"\b", names_in_text.lower())]
    return named or states


def has_filters(filters: dict) -> bool:
    return bool(filters.get("countries") or filters.get("owners") or filters.get("min_capacity_mtpa") is not None)


def matching_mine_ids(filters: dict) -> set[str]:
    """Ids of mines passing the country / owner / capacity filters (top_n is applied by the caller,
    after its own ranking)."""
    g = _graph()
    n, e = g.nodes, g.edges
    mines = n[n["type"] == "mine"]
    if filters.get("countries"):
        mines = mines[mines["country"].isin(filters["countries"])]
    if filters.get("owners"):
        want = set(filters["owners"])
        owned = set(e.loc[(e["type"] == "OWNED_BY") & e["dst"].isin(want), "src"])
        mines = mines[mines["id"].isin(owned)]
    if filters.get("min_capacity_mtpa") is not None:
        mines = mines[mines["capacity_mtpa"].astype(float) >= float(filters["min_capacity_mtpa"])]
    return set(mines["id"])


def describe_filters(filters: dict, with_top: bool = True) -> str:
    """'in China; owned by Vale SA; ≥ 10 Mtpa' ('' when there are no filters)."""
    names = dict(zip(_graph().nodes["id"], _graph().nodes["name"]))
    parts = []
    if filters.get("countries"):
        parts.append("in " + ", ".join(names.get(f"country_{c}", c) for c in filters["countries"][:8])
                     + ("…" if len(filters["countries"]) > 8 else ""))
    if filters.get("owners"):
        parts.append("owned by " + ", ".join(names.get(o, o) for o in filters["owners"]))
    if filters.get("min_capacity_mtpa") is not None:
        parts.append(f"≥ {filters['min_capacity_mtpa']:g} Mtpa")
    if with_top and filters.get("top_n"):
        parts.append(f"top {filters['top_n']}")
    return "; ".join(parts)


def list_assets(filters: dict, text: str = "") -> dict:
    from backend.schemas import QueryResult

    g = _graph()
    n, e = g.nodes, g.edges
    names = dict(zip(n["id"], n["name"]))
    if filters.get("thing") == "state":
        items = _states(text)
        total = 50
        results = [{"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"], "tier": "medium", "score": 0.5,
                    "components": {"exposure": 0.0, "news_z": 0.0, "tone_shift": 0.0, "attention_z": 0.0,
                                   "relevant_articles": 0},
                    "paths": [], "reason": f"US state: {s['name']}.", "evidence": []} for s in items]
        desc, ranked = "US states" + ("" if len(items) == total else f" named in the question ({len(items)})"), "listed"
    else:
        mines = n[n["type"] == "mine"].copy()
        own = e[e["type"] == "OWNED_BY"].sort_values("weight", ascending=False)
        owners_of = own.groupby("src")["dst"].apply(list).to_dict()
        share = {(r.src, r.dst): r.weight for r in own.itertuples()}
        mines = mines[mines["id"].isin(matching_mine_ids(filters))]
        mines = mines.assign(cap_sort=mines["capacity_mtpa"].astype(float).fillna(-1.0)).sort_values(
            ["cap_sort", "name"], ascending=[False, True])
        if filters.get("top_n"):
            mines = mines.head(int(filters["top_n"]))
        caps = [c for c in mines["cap_sort"] if c > 0]
        top = max(caps) if caps else 0.0
        hi = sorted(caps, reverse=True)[len(caps) // 3] if len(caps) >= 3 else (min(caps) if caps else 0)
        mid = sorted(caps, reverse=True)[2 * len(caps) // 3] if len(caps) >= 3 else 0
        results = []
        for r in mines.itertuples():
            c = r.cap_sort
            tier = "high" if c > 0 and c >= hi else "medium" if c > 0 and c >= mid else "low"
            own_ids = owners_of.get(r.id, [])
            owner_txt = ", ".join(f"{names.get(o, o)} ({share[(r.id, o)]:.0%})" for o in own_ids[:3]) or "unknown"
            cap_txt = f"{c:g} Mtpa design capacity" if c > 0 else "capacity not reported"
            results.append({
                "id": r.id, "name": r.name, "lat": float(r.lat), "lon": float(r.lon), "tier": tier,
                "score": round(c / top, 4) if c > 0 and top > 0 else 0.0,
                "components": {"exposure": 0.0, "news_z": 0.0, "tone_shift": 0.0, "attention_z": 0.0,
                               "relevant_articles": 0},
                "paths": [[r.id, f"country_{r.country}"]] + [[r.id, o] for o in own_ids[:3]],
                "reason": f"Operating iron ore mine in {names.get(f'country_{r.country}', r.country)}; owner {owner_txt}; "
                          f"{cap_txt} ({GEM_CREDIT}).",
                "evidence": [],
            })
        total = int((n["type"] == "mine").sum())
        where = describe_filters({**filters, "top_n": None})
        desc = "Operating iron ore mines" + (" " + where if where else "") + (
            f"; top {filters['top_n']} by capacity" if filters.get("top_n") else "")
        ranked = "ranked by design capacity (top third high, middle medium, rest or unknown low)"
    out = {
        "criteria": {"asset_type": "us_state" if filters.get("thing") == "state" else "iron_ore_mine",
                     "event": {"name": desc, "date": "", "terms": []},
                     "window": {"baseline_days": 60, "recent_days": 7},
                     "status": ["operating"], "region": "US" if filters.get("thing") == "state" else "global"},
        "channels": [{"id": "filter", "reason": f"No event in the question, so this is a list: {desc}, {ranked}. "
                                                f"Ask about an event (e.g. 'exposed to …') to get exposure scores."}],
        "results": results,
        "dark_count": max(0, total - len(results)),
    }
    return QueryResult.model_validate(out).model_dump()
