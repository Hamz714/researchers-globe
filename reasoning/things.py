"""Registry of the kinds of thing a question can be about. Owner: Person B.

Every kind provides the same item shape, so filters and measures work on all of them:

    {"id", "name", "lat", "lon", "country": ISO2 | None, "wiki_title": str | None,
     "wiki_note": str (e.g. " (via its owner Vale S.A.)"), "owners": [company ids], "capacity": float | None}

KINDS[kind] says how to recognise the kind in a question, how to label it, what `asset_type` the
answer carries, and which measures make sense for it (weather needs a real place; exposure needs
the mines graph). Adding a kind = one loader + one KINDS entry.
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
CURATED = ROOT / "pipeline" / "curated"

KINDS = {
    "mine": {"label": "iron ore mines", "asset_type": "iron_ore_mine",
             "noun": r"\bmines?\b",
             "measures": {"weather", "views", "edits", "live_edits", "exposure"}},
    "state": {"label": "US states", "asset_type": "us_state",
              "noun": r"\bstates?\b",
              "measures": {"weather", "views", "edits", "live_edits"}},
    "port": {"label": "export ports", "asset_type": "port",
             "noun": r"\b(?:sea)?ports?\b|\bharbou?rs?\b|\bterminals?\b",
             "measures": {"weather", "views", "edits", "live_edits"}},
    "country": {"label": "countries", "asset_type": "country",
                "noun": r"\bcountries\b|\bcountry\b|\bnations?\b",
                "measures": {"views", "edits", "live_edits"}},          # weather at a centroid is meaningless
    "company": {"label": "mining companies", "asset_type": "company",
                "noun": r"\bcompan(?:y|ies)\b|\bfirms?\b|\bminers\b|\bproducers?\b|\bcorporations?\b",
                "measures": {"views", "edits", "live_edits"}},
}
ASSET_TYPE_TO_KIND = {v["asset_type"]: k for k, v in KINDS.items()}
WIKI_COUNTRY_TITLES = {"Türkiye": "Turkey", "Czech Republic": "Czech Republic"}  # display name -> article


def detect_kind(text: str) -> str:
    """The kind whose noun appears FIRST ("which ports do mines in Brazil use" -> port). Default: mine."""
    low = re.sub(r"\bunited states\b", " ", text.lower())  # "United States" is a country, not "states"
    first = [(m.start(), k) for k, v in KINDS.items() if (m := re.search(v["noun"], low))]
    return min(first)[1] if first else "mine"


def _graph():
    from pipeline.graph import default_graph

    return default_graph()


def _wiki_from_url(url: str) -> str | None:
    m = re.match(r"https?://en\.wikipedia\.org/wiki/(.+)$", str(url or ""))
    return unquote(m.group(1)).replace("_", " ") if m else None


@lru_cache(maxsize=None)
def _load(kind: str) -> tuple:
    g = _graph()
    n, e = g.nodes, g.edges
    names = dict(zip(n["id"], n["name"]))
    wiki = dict(zip(n["id"], n["wiki_title"]))
    owned = e[e["type"] == "OWNED_BY"].sort_values("weight", ascending=False)
    owners_of = owned.groupby("src")["dst"].apply(list).to_dict()
    items: list[dict] = []

    if kind == "mine":
        for r in n[n["type"] == "mine"].itertuples():
            title, note = (r.wiki_title, "") if isinstance(r.wiki_title, str) and r.wiki_title else (None, "")
            own = owners_of.get(r.id, [])
            if not title and own and isinstance(wiki.get(own[0]), str) and wiki.get(own[0]):
                title, note = wiki[own[0]], f" (via its owner {names[own[0]]})"
            cap = float(r.capacity_mtpa) if "capacity_mtpa" in n.columns and r.capacity_mtpa == r.capacity_mtpa else None
            items.append({"id": r.id, "name": r.name, "lat": float(r.lat), "lon": float(r.lon), "country": r.country,
                          "wiki_title": title, "wiki_note": note, "owners": own, "capacity": cap})

    elif kind == "state":
        with (CURATED / "us_states.csv").open(newline="") as f:
            for r in csv.DictReader(f):
                items.append({"id": r["state_id"], "name": r["name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                              "country": "US", "wiki_title": r["wiki_title"] or None, "wiki_note": "",
                              "owners": [], "capacity": None})

    elif kind == "port":
        with (CURATED / "ports.csv").open(newline="") as f:
            for r in csv.DictReader(f):
                title = _wiki_from_url(r["source_url"])
                note = "" if not title or title.lower().startswith(r["name"].split(" (")[0].lower()[:6]) \
                    else f" (Wikipedia page '{title}')"
                items.append({"id": r["port_id"], "name": r["name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                              "country": r["country_iso2"], "wiki_title": title, "wiki_note": note,
                              "owners": [], "capacity": None})

    elif kind == "country":
        for r in n[n["type"] == "country"].itertuples():
            if not isinstance(r.name, str) or len(r.name) <= 3 or r.lat != r.lat:  # placeholder nodes
                continue
            items.append({"id": r.id, "name": r.name, "lat": float(r.lat), "lon": float(r.lon), "country": r.country,
                          "wiki_title": WIKI_COUNTRY_TITLES.get(r.name, r.name), "wiki_note": "",
                          "owners": [], "capacity": None})

    elif kind == "company":
        listed = e[e["type"] == "LISTED_IN"].set_index("src")["dst"].to_dict()   # curated companies only
        mines = {m["id"]: m for m in _load("mine")}
        by_owner: dict[str, list[str]] = {}
        for mine_id, own in owners_of.items():
            for o in own:
                by_owner.setdefault(o, []).append(mine_id)
        for cid, country_node in listed.items():
            held = [mines[m] for m in by_owner.get(cid, []) if m in mines]
            if not held:
                continue
            biggest = max(held, key=lambda m: m["capacity"] or -1)   # drawn at its largest mine
            items.append({"id": cid, "name": names.get(cid, cid), "lat": biggest["lat"], "lon": biggest["lon"],
                          "country": country_node.removeprefix("country_"),
                          "wiki_title": wiki.get(cid) if isinstance(wiki.get(cid), str) and wiki.get(cid) else None,
                          "wiki_note": "", "owners": [cid], "capacity": None})
    else:
        raise KeyError(kind)
    for it in items:  # graph cells can be NaN; the API schema wants str | None
        if not isinstance(it["country"], str) or not it["country"]:
            it["country"] = None
    return tuple(items)


def things(kind: str) -> list[dict]:
    return [dict(i) for i in _load(kind)]


def all_wiki_titles() -> dict[str, list[tuple[str, str]]]:
    """Wikipedia title -> [(kind, thing id)], for every kind: what the live edits stream watches."""
    out: dict[str, list[tuple[str, str, bool]]] = {}
    for kind in KINDS:
        for it in _load(kind):
            if it["wiki_title"]:
                out.setdefault(it["wiki_title"], []).append((kind, it["id"], bool(it["wiki_note"])))
    # the thing whose OWN page it is comes first ("Vale S.A." -> the company, then mines via their owner)
    return {t: [(k, i) for k, i, _ in sorted(v, key=lambda x: x[2])] for t, v in out.items()}


# ------------------------------------------------------------------ list answers per kind
def _tiers(scores: list[float]) -> tuple[float, float]:
    s = sorted((x for x in scores if x > 0), reverse=True)
    if len(s) < 3:
        return (s[-1] if s else 1.0), 0.0
    return s[len(s) // 3], s[2 * len(s) // 3]


def list_rows(kind: str, items: list[dict]) -> list[dict]:
    """Rows for a list answer over already-filtered things, ranked by a size measure that makes
    sense for the kind: GEM design capacity of the mines involved (mines: their own; ports: mines
    shipping through; countries: mines located there; companies: mines they hold stakes in). The score is size / largest: a ranking, not a probability."""
    from reasoning.listing import list_rows as mine_rows

    if kind == "mine":
        return mine_rows("mine", items)
    blank = {"exposure": 0.0, "news_z": 0.0, "tone_shift": 0.0, "attention_z": 0.0, "relevant_articles": 0}
    if kind == "state":
        return [{"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"], "tier": "medium", "score": 0.5,
                 "components": dict(blank), "paths": [], "reason": f"US state: {s['name']}.", "evidence": []}
                for s in sorted(items, key=lambda s: s["name"])]
    g = _graph()
    e, n = g.edges, g.nodes
    names = dict(zip(n["id"], n["name"]))
    mines = {m["id"]: m for m in _load("mine")}
    cap = lambda ids: sum(mines[i]["capacity"] or 0 for i in ids if i in mines)  # noqa: E731
    sizes: dict[str, tuple[float, str, list]] = {}
    for it in items:
        if kind == "port":
            via = list(e.loc[(e["type"] == "EXPORTS_VIA") & (e["dst"] == it["id"]), "src"])
            sizes[it["id"]] = (cap(via) or len(via) / 100, f"Export port in {names.get('country_' + str(it['country']), it['country'])}; "
                                         f"{len(via)} operating mines in our data ship through it "
                                         f"({cap(via):,.0f} Mtpa design capacity).", [[m, it["id"]] for m in via[:3]])
        elif kind == "country":
            in_c = [m for m, x in mines.items() if x["country"] == it["country"]]
            dest = e[(e["type"] == "EXPORTS_TO") & (e["src"] == it["id"])].sort_values("weight", ascending=False)
            exp = ", ".join(f"{names.get(r.dst, r.dst)} {r.weight:.0%}" for r in dest.head(3).itertuples())
            sizes[it["id"]] = (cap(in_c) or len(in_c) / 100, f"{len(in_c)} operating iron ore mines ({cap(in_c):,.0f} Mtpa design capacity)"
                                          + (f"; iron ore exports to {exp} (UN Comtrade 2023)." if exp else "."), [])
        elif kind == "company":
            held = [m for m, x in mines.items() if it["id"] in x["owners"]]
            stake = e[(e["type"] == "OWNED_BY") & (e["dst"] == it["id"])].set_index("src")["weight"].to_dict()
            attrib = sum((mines[m]["capacity"] or 0) * float(stake.get(m, 1.0)) for m in held)
            sizes[it["id"]] = (attrib or len(held) / 100, f"Holds stakes in {len(held)} operating iron ore mines "
                                          f"({attrib:,.0f} Mtpa of design capacity attributable to its shares); listed in "
                                          f"{names.get('country_' + str(it['country']), it['country'])}.",
                               [[it["id"], m] for m in held[:3]])
    top = max((s[0] for s in sizes.values()), default=0) or 1
    hi, mid = _tiers([s[0] for s in sizes.values()])
    rows = []
    for it in sorted(items, key=lambda i: (-sizes[i["id"]][0], i["name"])):
        size, reason, paths = sizes[it["id"]]
        rows.append({"id": it["id"], "name": it["name"], "lat": it["lat"], "lon": it["lon"],
                     "tier": "high" if size and size >= hi else "medium" if size and size >= mid else "low",
                     "score": round(size / top, 4), "components": dict(blank), "paths": paths,
                     "reason": reason, "evidence": []})
    return rows
