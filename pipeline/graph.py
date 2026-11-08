"""Entity graph: one table of nodes and one of edges (plan: Entity graph).

Owner: Person A. Publishes `graph.nodes` and `graph.edges` for Person B.

    nodes: id, type, name, lat, lon, country, wiki_title, search_terms
    edges: src, dst, type, weight, source_url

`search_terms` is stored as a "|"-separated string so the tables round-trip
through CSV and Parquet unchanged.

Run `python -m pipeline.graph --mock` to build the graph from data/mock and write
it to data/graph/. Once the real GEM file is downloaded, fill GEM_COLUMN_MAP and
run `python -m pipeline.graph --gem data/raw/<file>.xlsx`.
"""
from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = ROOT / "data" / "mock"
CURATED_DIR = Path(__file__).resolve().parent / "curated"
GRAPH_DIR = ROOT / "data" / "graph"

NODE_COLUMNS = ["id", "type", "name", "lat", "lon", "country", "wiki_title", "search_terms", "capacity_mtpa"]
# capacity_mtpa: mines only, GEM "Design capacity (ttpa)" / 1000; nullable (NaN when GEM has none).
# Used to break ties between equally exposed mines. Older graph files without it load as NaN.
EDGE_COLUMNS = ["src", "dst", "type", "weight", "source_url"]

NODE_TYPES = {"mine", "company", "country", "port", "commodity", "event", "channel"}
EDGE_TYPES = {
    "OWNED_BY",       # mine -> company      (weight = ownership share)
    "LOCATED_IN",     # mine -> country; also port -> country (needed for the plan's example path)
    "EXPORTS_VIA",    # mine -> port         (nearest curated export port in the same country)
    "EXPORTS_TO",     # country -> country   (weight = share of exports)
    "LISTED_IN",      # company -> country
    "ACTS_THROUGH",   # event -> channel     (chosen per query)
    "TOUCHES",        # channel -> country | company | commodity (| port, see README)
}

# Column names in the GEM Global Iron Ore Mines Tracker, September 2026 (V1) release,
# sheet "Main Data" (checked against the downloaded file). Keys are our names.
GEM_SHEET = "Main Data"
GEM_COLUMN_MAP: dict[str, str] = {
    "gem_id": "GEM asset ID",
    "name": "Asset name (English)",
    "coords": "Coordinates",              # "lat, lon" in one string
    "coord_accuracy": "Coordinate accuracy",  # exact | approximate
    "municipality": "Municipality",
    "country": "Country/area",            # country NAME -> ISO2 via curated/countries.csv
    "status": "Operating status",         # operating | proposed | mothballed | retired | shelved | cancelled | unknown
    "capacity_ttpa": "Design capacity (ttpa)",
    "production_2025_ttpa": "Production 2025 (ttpa)",
    "owner": "Owner (English)",           # immediate owner(s), "Name [85.0%]; Name [15.0%]"
    "parent": "Parent (English)",         # ultimate parent(s), same format, % sometimes missing
    "wiki": "GEM wiki page",              # GEM wiki, NOT Wikipedia: never used as wiki_title
}
GEM_SOURCE_URL = "https://globalenergymonitor.org/projects/global-iron-ore-mines-tracker"
COUNTRY_ALIASES = {"Türkiye": "TR", "Republic of the Congo": "CG", "North Korea": "KP", "South Korea": "KR",
                   "Russia": "RU", "Iran": "IR", "Laos": "LA", "Vietnam": "VN", "Bolivia": "BO",
                   "Venezuela": "VE", "United States": "US", "United Kingdom": "GB"}
BRAND_PREFIXES = ["US Steel", "Rio Tinto", "Anglo American", "Cleveland-Cliffs", "Quebec Iron", "Sishen Iron",
                  "Kumba", "ArcelorMittal", "Fortescue", "MinRes", "Vale", "BHP", "IOC", "EVRAZ", "NLMK", "Ferrexpo",
                  "SSGPO", "Metinvest", "Baffinland", "Champion", "Tacora Resources", "NMDC", "SAIL", "Tata Steel",
                  "JSW", "Ansteel", "HBIS", "Baogang", "Shougang", "Severstal", "Metalloinvest", "ERG"]
SKIP_OWNERS = {"unknown", "natural person(s)", "small shareholder(s)", ""}
_SUFFIX = r"\b(Ltd|Limited|PLC|plc|SA|S\.A\.|Corp|Corporation|Inc|Co|Company|Group|Holdings|PJSC|JSC|LLC|Pty|AG|AB|SAB de CV|SpA|BV|NV|Sarl|SARL)\b\.?"


@dataclass
class Graph:
    nodes: pd.DataFrame
    edges: pd.DataFrame

    # ------------------------------------------------------------------ checks
    def validate(self) -> None:
        missing = [c for c in NODE_COLUMNS if c not in self.nodes.columns]
        if missing:
            raise ValueError(f"nodes missing columns {missing}")
        missing = [c for c in EDGE_COLUMNS if c not in self.edges.columns]
        if missing:
            raise ValueError(f"edges missing columns {missing}")
        bad = set(self.nodes["type"]) - NODE_TYPES
        if bad:
            raise ValueError(f"unknown node types {bad}")
        bad = set(self.edges["type"]) - EDGE_TYPES
        if bad:
            raise ValueError(f"unknown edge types {bad}")
        if self.nodes["id"].duplicated().any():
            dups = self.nodes.loc[self.nodes["id"].duplicated(), "id"].tolist()
            raise ValueError(f"duplicate node ids {dups[:5]}")
        ids = set(self.nodes["id"])
        dangling = self.edges[~self.edges["src"].isin(ids) | ~self.edges["dst"].isin(ids)]
        if len(dangling):
            raise ValueError(f"{len(dangling)} edges point at unknown nodes, e.g. {dangling.iloc[0].to_dict()}")
        w = self.edges["weight"]
        if ((w < 0) | (w > 1) | w.isna()).any():
            raise ValueError("edge weights must be in [0, 1]")

    # ------------------------------------------------------------------ io
    def save(self, out_dir: Path = GRAPH_DIR) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self.nodes.to_parquet(out_dir / "nodes.parquet", index=False)
        self.edges.to_parquet(out_dir / "edges.parquet", index=False)

    @classmethod
    def load(cls, in_dir: Path = GRAPH_DIR) -> "Graph":
        nodes = pd.read_parquet(in_dir / "nodes.parquet")
        if "capacity_mtpa" not in nodes.columns:  # graph built before the column existed
            nodes["capacity_mtpa"] = float("nan")
        g = cls(nodes, pd.read_parquet(in_dir / "edges.parquet"))
        g.validate()
        return g

    # ------------------------------------------------------------------ helpers
    def node(self, node_id: str) -> dict:
        row = self.nodes.loc[self.nodes["id"] == node_id]
        if row.empty:
            raise KeyError(node_id)
        return row.iloc[0].to_dict()

    def with_edges(self, extra: list[dict]) -> "Graph":
        """Return a copy with extra edges (e.g. per-query TOUCHES) and any missing channel nodes."""
        if not extra:
            return self
        extra_df = pd.DataFrame(extra, columns=EDGE_COLUMNS)
        nodes = self.nodes
        new_channels = sorted(
            {e["src"] for e in extra if e["src"].startswith("channel_")} - set(nodes["id"])
        )
        if new_channels:
            nodes = pd.concat(
                [nodes, pd.DataFrame([_node(c, "channel", c.removeprefix("channel_")) for c in new_channels])],
                ignore_index=True,
            )
        return Graph(nodes, pd.concat([self.edges, extra_df], ignore_index=True))


def _node(id_: str, type_: str, name: str, lat=None, lon=None, country=None, wiki_title="", search_terms="",
          capacity_mtpa=None) -> dict:
    cap = pd.to_numeric(capacity_mtpa, errors="coerce") if capacity_mtpa is not None else float("nan")
    return {
        "id": id_, "type": type_, "name": name, "lat": lat, "lon": lon,
        "country": country, "wiki_title": wiki_title or "", "search_terms": search_terms or "",
        "capacity_mtpa": float(cap) if pd.notna(cap) else float("nan"),
    }


def _edge(src: str, dst: str, type_: str, weight: float = 1.0, source_url: str = "") -> dict:
    return {"src": src, "dst": dst, "type": type_, "weight": float(weight), "source_url": source_url}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_port(lat: float, lon: float, country_iso2: str, ports: pd.DataFrame) -> str | None:
    """Nearest port in the same country (plan: 'nearest major export port'). None if the country has none."""
    same = ports[ports["country_iso2"] == country_iso2]
    if same.empty:
        return None
    d = same.apply(lambda p: haversine_km(lat, lon, p["lat"], p["lon"]), axis=1)
    return str(same.loc[d.idxmin(), "port_id"])


def build_graph(
    mines: pd.DataFrame,
    ports: pd.DataFrame,
    trade: pd.DataFrame,
    countries: pd.DataFrame,
    mines_source_url: str = "",
) -> Graph:
    """Build nodes/edges from tables in the mock schema (see data/mock/*.csv)."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for c in countries.itertuples():
        nodes[f"country_{c.iso2}"] = _node(f"country_{c.iso2}", "country", c.name, c.lat, c.lon, c.iso2,
                                           search_terms=c.name)

    def ensure_country(iso2: str) -> str:
        cid = f"country_{iso2}"
        if cid not in nodes:
            nodes[cid] = _node(cid, "country", iso2, country=iso2, search_terms=iso2)
        return cid

    for p in ports.itertuples():
        nodes[p.port_id] = _node(p.port_id, "port", p.name, p.lat, p.lon, p.country_iso2, search_terms=p.name)
        edges.append(_edge(p.port_id, ensure_country(p.country_iso2), "LOCATED_IN", 1.0, p.source_url))

    for m in mines.itertuples():
        extra = [t for t in str(m.search_terms).split("|") if t] if isinstance(m.search_terms, str) else []
        terms = "|".join(extra or [m.name])
        nodes[m.id] = _node(m.id, "mine", m.name, m.lat, m.lon, m.country_iso2,
                            m.wiki_title if isinstance(m.wiki_title, str) else "", terms,
                            capacity_mtpa=getattr(m, "capacity_mtpa", None))
        edges.append(_edge(m.id, ensure_country(m.country_iso2), "LOCATED_IN", 1.0, mines_source_url))
        if isinstance(m.owner_id, str) and m.owner_id:
            if m.owner_id not in nodes:
                nodes[m.owner_id] = _node(m.owner_id, "company", m.owner_name, search_terms=m.owner_name)
                if isinstance(m.owner_listed_iso2, str) and m.owner_listed_iso2:
                    edges.append(_edge(m.owner_id, ensure_country(m.owner_listed_iso2), "LISTED_IN", 1.0))
            share = float(m.owner_share) if pd.notna(m.owner_share) else 1.0
            edges.append(_edge(m.id, m.owner_id, "OWNED_BY", share, mines_source_url))
        port_id = m.export_port_id if isinstance(m.export_port_id, str) and m.export_port_id else None
        if port_id is None:
            port_id = nearest_port(m.lat, m.lon, m.country_iso2, ports)
        if port_id:
            edges.append(_edge(m.id, port_id, "EXPORTS_VIA", 1.0))

    for t in trade.itertuples():
        edges.append(_edge(ensure_country(t.exporter_iso2), ensure_country(t.importer_iso2),
                           "EXPORTS_TO", t.share, t.source_url))

    g = Graph(pd.DataFrame(list(nodes.values()), columns=NODE_COLUMNS),
              pd.DataFrame(edges, columns=EDGE_COLUMNS))
    g.validate()
    return g


def build_mock_graph() -> Graph:
    return build_graph(
        pd.read_csv(MOCK_DIR / "mines_mock.csv"),
        pd.read_csv(MOCK_DIR / "ports_mock.csv"),
        pd.read_csv(MOCK_DIR / "trade_mock.csv"),
        pd.read_csv(MOCK_DIR / "countries_mock.csv"),
        mines_source_url="MOCK",
    )


def parse_owners(s: str) -> list[tuple[str, float | None]]:
    """'BHP Group Ltd [85.0%]; ITOCHU Corp; Mitsui & Co Ltd [7.0%]' -> [(name, share or None), ...]."""
    out = []
    for part in str(s).split(";"):
        m = re.match(r"\s*(.*?)\s*\[(\d+(?:\.\d+)?)%\]\s*$", part)
        name, share = (m.group(1), float(m.group(2)) / 100) if m else (part.strip(), None)
        if name.lower() not in SKIP_OWNERS:
            out.append((name, share))
    # Spread any unassigned share over owners listed without a percentage.
    known = sum(s for _, s in out if s is not None)
    missing = [i for i, (_, s) in enumerate(out) if s is None]
    if missing:
        each = max(1.0 - known, 0.0) / len(missing) or 0.05
        out = [(n, s if s is not None else max(each, 0.05)) for n, s in out]
    merged: dict[str, float] = {}
    for n, s in out:
        merged[n] = min(1.0, merged.get(n, 0.0) + s)
    return list(merged.items())


def short_name(company: str) -> str:
    s = re.sub(_SUFFIX, "", company)
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"\s+", " ", s).strip(" ,&")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def mine_terms(name: str, owner_terms: list[str]) -> list[str]:
    """Search terms for a GEM mine name like 'BHP South Flank Mine' -> ['South Flank']."""
    core = re.sub(r"\(.*?\)", "", name)
    core = re.sub(r"\b(Complex\s+)?Mine\b", "", core, flags=re.I)
    core = re.sub(r"\s+", " ", core).strip()
    terms = [core]
    for o in owner_terms:  # GEM names are usually "<Parent> <Location> Mine": drop the parent prefix
        if o and core.lower().startswith(o.lower() + " "):
            terms.insert(0, core[len(o) + 1:].strip())
    for prefix in BRAND_PREFIXES:  # operator brands GEM puts in front of the mine name
        if core.startswith(prefix + " ") and len(core) > len(prefix) + 4:
            terms.insert(0, core[len(prefix) + 1:].strip())
            break
    seen, out = set(), []
    for t in terms:
        if len(t) >= 4 and t.lower() not in seen:  # GDELT rejects very short keywords
            seen.add(t.lower())
            out.append(t)
    return out


def load_gem(path: Path, statuses: tuple[str, ...] = ("operating",)) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the GEM workbook. Returns (mines in the mock schema, ownership rows).

    Keeps mines with the given statuses and parseable coordinates. Owners are the
    GEM *parent* companies (ultimate owners); unknown/natural persons are dropped.
    """
    c = GEM_COLUMN_MAP
    df = pd.read_excel(path, sheet_name=GEM_SHEET)
    missing = [v for v in c.values() if v not in df.columns]
    if missing:
        raise ValueError(f"GEM columns not found: {missing}. Columns: {list(df.columns)}")
    df = df[df[c["status"]].isin(statuses)].copy()

    countries = pd.read_csv(CURATED_DIR / "countries.csv")
    name_to_iso = {**dict(zip(countries["name"], countries["iso2"])), **COUNTRY_ALIASES}
    companies = pd.read_csv(CURATED_DIR / "companies.csv")
    curated = {r.name_in_gem: r for r in companies.itertuples()}
    # exact Wikipedia titles for big mines, each checked against the live article (see the csv's `check`)
    mine_wiki = pd.read_csv(CURATED_DIR / "mine_wiki.csv").set_index("mine_id")["wiki_title"].to_dict()

    mines, owners = [], []
    for r in df.itertuples(index=False):
        row = dict(zip(df.columns, r))
        try:
            lat, lon = (float(x) for x in str(row[c["coords"]]).split(","))
        except ValueError:
            continue
        iso = name_to_iso.get(row[c["country"]])
        if iso is None:
            continue
        parents = parse_owners(row[c["parent"]])
        owner_short = []
        for pname, share in parents:
            cur = curated.get(pname)
            cid = cur.company_id if cur else "company_" + slug(short_name(pname))
            terms = cur.search_terms if cur else short_name(pname)
            owners.append({"mine_id": f"mine_{row[c['gem_id']]}", "company_id": cid, "company_name": pname,
                           "share": share, "listed_iso2": cur.listed_iso2 if cur else "",
                           "wiki_title": (cur.wiki_title if cur and isinstance(cur.wiki_title, str) else ""),
                           "search_terms": terms})
            owner_short += [t for t in str(terms).split("|") if t]
        cap = pd.to_numeric(row[c["capacity_ttpa"]], errors="coerce")
        mines.append({
            "id": f"mine_{row[c['gem_id']]}", "name": row[c["name"]], "lat": lat, "lon": lon,
            "country_iso2": iso, "status": row[c["status"]],
            "capacity_mtpa": round(cap / 1000, 2) if pd.notna(cap) else None,
            "owner_id": None, "owner_name": None, "owner_share": None, "owner_listed_iso2": None,
            "export_port_id": None, "wiki_title": mine_wiki.get(f"mine_{row[c['gem_id']]}", ""),
            "search_terms": "|".join(mine_terms(row[c["name"]], owner_short + [short_name(p) for p, _ in parents])),
            "coord_accuracy": row[c["coord_accuracy"]],
        })
    return pd.DataFrame(mines), pd.DataFrame(owners)


def build_real_graph(gem_path: Path) -> Graph:
    """GEM mines + curated ports/trade/countries/companies -> Graph (multi-owner aware)."""
    mines, owners = load_gem(gem_path)
    ports = pd.read_csv(CURATED_DIR / "ports.csv")
    g = build_graph(mines, ports, pd.read_csv(CURATED_DIR / "trade.csv"),
                    pd.read_csv(CURATED_DIR / "countries.csv"), mines_source_url=GEM_SOURCE_URL)
    nodes = {r["id"]: r for r in g.nodes.to_dict("records")}
    edges = g.edges.to_dict("records")
    for o in owners.itertuples():
        if o.company_id not in nodes:
            nodes[o.company_id] = _node(o.company_id, "company", o.company_name, wiki_title=o.wiki_title,
                                        search_terms=o.search_terms)
            if isinstance(o.listed_iso2, str) and o.listed_iso2 and f"country_{o.listed_iso2}" in nodes:
                edges.append(_edge(o.company_id, f"country_{o.listed_iso2}", "LISTED_IN", 1.0,
                                   "pipeline/curated/companies.csv"))
        edges.append(_edge(o.mine_id, o.company_id, "OWNED_BY", o.share, GEM_SOURCE_URL))
    out = Graph(pd.DataFrame(list(nodes.values()), columns=NODE_COLUMNS),
                pd.DataFrame(edges, columns=EDGE_COLUMNS).drop_duplicates(["src", "dst", "type"]))
    out.validate()
    return out


def default_graph() -> Graph:
    """The real graph if it has been built, else the mock graph. Used by the pipeline."""
    if (GRAPH_DIR / "nodes.parquet").exists():
        return Graph.load()
    return build_mock_graph()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the entity graph")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--mock", action="store_true", help="build from data/mock")
    src.add_argument("--gem", type=Path, help="path to the downloaded GEM file")
    args = ap.parse_args()
    if args.mock:
        g = build_mock_graph()
    else:
        g = build_real_graph(args.gem)
    g.save()
    print(f"wrote {len(g.nodes)} nodes, {len(g.edges)} edges to {GRAPH_DIR}")
    print(g.nodes["type"].value_counts().to_string())
