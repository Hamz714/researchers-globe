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
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = ROOT / "data" / "mock"
CURATED_DIR = Path(__file__).resolve().parent / "curated"
GRAPH_DIR = ROOT / "data" / "graph"

NODE_COLUMNS = ["id", "type", "name", "lat", "lon", "country", "wiki_title", "search_terms"]
EDGE_COLUMNS = ["src", "dst", "type", "weight", "source_url"]

NODE_TYPES = {"mine", "company", "country", "port", "commodity", "event", "channel"}
EDGE_TYPES = {
    "OWNED_BY",       # mine -> company      (weight = ownership share)
    "LOCATED_IN",     # mine -> country; also port -> country (needed for the plan's example path)
    "EXPORTS_VIA",    # mine -> port         (nearest major export port)
    "EXPORTS_TO",     # country -> country   (weight = share of exports)
    "LISTED_IN",      # company -> country
    "ACTS_THROUGH",   # event -> channel     (chosen per query)
    "TOUCHES",        # channel -> country | company | commodity (| port, see README)
}

# TODO(Person A): fill in once the GEM Global Iron Ore Mines Tracker file is downloaded.
# We have NOT seen the file; do not trust any column name until you've printed df.columns.
# Keys are our names, values are the GEM column names.
GEM_COLUMN_MAP: dict[str, str | None] = {
    "gem_id": None,
    "name": None,
    "lat": None,
    "lon": None,
    "country": None,          # likely a country NAME; we need ISO2 -> map via curated/countries.csv
    "status": None,
    "capacity_mtpa": None,
    "owner": None,            # may be a single string with several owners and percentages
}


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
        g = cls(pd.read_parquet(in_dir / "nodes.parquet"), pd.read_parquet(in_dir / "edges.parquet"))
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


def _node(id_: str, type_: str, name: str, lat=None, lon=None, country=None, wiki_title="", search_terms="") -> dict:
    return {
        "id": id_, "type": type_, "name": name, "lat": lat, "lon": lon,
        "country": country, "wiki_title": wiki_title or "", "search_terms": search_terms or "",
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
        terms = "|".join(t for t in [m.name, *(str(m.search_terms).split("|") if isinstance(m.search_terms, str) else [])] if t)
        nodes[m.id] = _node(m.id, "mine", m.name, m.lat, m.lon, m.country_iso2,
                            m.wiki_title if isinstance(m.wiki_title, str) else "", terms)
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


def load_gem(path: Path) -> pd.DataFrame:
    """TODO(Person A): read the GEM file and return it in the mock mines schema.

    Steps: print df.columns, fill GEM_COLUMN_MAP, keep status == operating with
    coordinates and owners, map country names to ISO2, split multi-owner strings.
    """
    df = pd.read_excel(path) if path.suffix in {".xlsx", ".xls"} else pd.read_csv(path)
    if any(v is None for v in GEM_COLUMN_MAP.values()):
        raise NotImplementedError(
            "Fill GEM_COLUMN_MAP in pipeline/graph.py first. Columns in this file:\n  " + "\n  ".join(map(str, df.columns))
        )
    raise NotImplementedError("TODO(Person A): convert GEM rows to the mock mines schema")


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
        mines = load_gem(args.gem)
        g = build_graph(mines, pd.read_csv(CURATED_DIR / "ports.csv"), pd.read_csv(CURATED_DIR / "trade.csv"),
                        pd.read_csv(CURATED_DIR / "countries.csv"),
                        mines_source_url="https://globalenergymonitor.org/projects/global-iron-ore-mines-tracker")
    g.save()
    print(f"wrote {len(g.nodes)} nodes, {len(g.edges)} edges to {GRAPH_DIR}")
    print(g.nodes["type"].value_counts().to_string())
