"""List / filter questions: "show all mines", "iron ore mines in Brazil", "mines owned by BHP",
"the 10 largest mines in Australia", "show all US states". Owner: Person B.

These have no event, so the exposure pipeline (which needs an event to search news for) does
not apply: we filter the asset table and rank by size. Deterministic, offline, instant.

    list_rows(thing, items)    -> result rows for a list answer (used by reasoning/plan.py)
    matching_mine_ids(filters) -> ids passing simple include filters (helper for tests and tools)

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


def list_rows(thing: str, items: list[dict]) -> list[dict]:
    """Result rows for a list answer over already-filtered things (dicts with id, name, lat, lon).
    Mines are ranked by GEM design capacity (top third high, middle medium, rest/unknown low)."""
    blank = {"exposure": 0.0, "news_z": 0.0, "tone_shift": 0.0, "attention_z": 0.0, "relevant_articles": 0}
    if thing == "state":
        return [{"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"], "tier": "medium", "score": 0.5,
                 "components": dict(blank), "paths": [], "reason": f"US state: {s['name']}.", "evidence": []}
                for s in sorted(items, key=lambda s: s["name"])]
    g = _graph()
    n, e = g.nodes, g.edges
    names = dict(zip(n["id"], n["name"]))
    ids = {i["id"] for i in items}
    mines = n[(n["type"] == "mine") & n["id"].isin(ids)]
    own = e[e["type"] == "OWNED_BY"].sort_values("weight", ascending=False)
    owners_of = own.groupby("src")["dst"].apply(list).to_dict()
    share = {(r.src, r.dst): r.weight for r in own.itertuples()}
    mines = mines.assign(cap_sort=mines["capacity_mtpa"].astype(float).fillna(-1.0)).sort_values(
        ["cap_sort", "name"], ascending=[False, True])
    caps = sorted((c for c in mines["cap_sort"] if c > 0), reverse=True)
    top = caps[0] if caps else 0.0
    hi = caps[len(caps) // 3] if len(caps) >= 3 else (caps[-1] if caps else 0)
    mid = caps[2 * len(caps) // 3] if len(caps) >= 3 else 0
    rows = []
    for r in mines.itertuples():
        c = r.cap_sort
        own_ids = owners_of.get(r.id, [])
        owner_txt = ", ".join(f"{names.get(o, o)} ({share[(r.id, o)]:.0%})" for o in own_ids[:3]) or "unknown"
        cap_txt = f"{c:g} Mtpa design capacity" if c > 0 else "capacity not reported"
        rows.append({
            "id": r.id, "name": r.name, "lat": float(r.lat), "lon": float(r.lon),
            "tier": "high" if c > 0 and c >= hi else "medium" if c > 0 and c >= mid else "low",
            "score": round(c / top, 4) if c > 0 and top > 0 else 0.0, "components": dict(blank),
            "paths": [[r.id, f"country_{r.country}"]] + [[r.id, o] for o in own_ids[:3]],
            "reason": f"Operating iron ore mine in {names.get(f'country_{r.country}', r.country)}; owner {owner_txt}; "
                      f"{cap_txt} ({GEM_CREDIT}).",
            "evidence": []})
    return rows


