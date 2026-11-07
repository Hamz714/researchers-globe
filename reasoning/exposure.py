"""Step 3: propagate exposure through the graph (plan: deterministic, noisy-OR).

Owner: Person B.

    path strength  = w_channel * product of edge weights on the path
    exposure(a)    = 1 - prod over paths (1 - path strength)

A path runs from an asset to a chosen channel node. Edges are walked in either
direction (the plan's example path "mine -> port -> country -> country <- channel"
uses both). Rules that keep paths meaningful:
  * paths are simple (no node twice) and at most `max_hops` edges long;
  * they never pass through another asset, an event or another channel;
  * they end at the first channel node reached.
Known simplification: paths that share edges are treated as independent (plan).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from pipeline.graph import Graph

PASS_THROUGH = {"company", "country", "port", "commodity"}


@dataclass
class Exposed:
    asset_id: str
    exposure: float
    paths: list[list[str]] = field(default_factory=list)
    strengths: list[float] = field(default_factory=list)


def touches_edges(channel_ids: list[str], touches: dict[str, list]) -> list[dict]:
    """Per-query TOUCHES edges: {"tariffs": [["country_US", 1.0], ...]} -> edge dicts."""
    return [
        {"src": f"channel_{cid}", "dst": node, "type": "TOUCHES", "weight": float(w), "source_url": ""}
        for cid in channel_ids for node, w in touches.get(cid, [])
    ]


def noisy_or(strengths: list[float]) -> float:
    p = 1.0
    for s in strengths:
        p *= 1.0 - s
    return 1.0 - p


def propagate(graph: Graph, channel_weights: dict[str, float], asset_type: str = "mine",
              max_hops: int = 4, top_k: int = 50) -> tuple[list[Exposed], int]:
    """channel_weights maps channel ids (without the 'channel_' prefix) to w_channel.

    Returns (top_k exposed assets sorted by exposure desc, dark_count).
    """
    types = dict(zip(graph.nodes["id"], graph.nodes["type"]))
    targets = {f"channel_{c}": w for c, w in channel_weights.items()}

    adj: dict[str, dict[str, float]] = defaultdict(dict)  # undirected, keep the max weight per pair
    for e in graph.edges.itertuples():
        if e.src not in types or e.dst not in types:
            continue
        for a, b in ((e.src, e.dst), (e.dst, e.src)):
            adj[a][b] = max(adj[a].get(b, 0.0), float(e.weight))

    assets = [n for n, t in types.items() if t == asset_type]
    exposed: list[Exposed] = []
    for asset in assets:
        found: list[tuple[list[str], float]] = []

        def walk(node: str, path: list[str], strength: float) -> None:
            for nxt, w in adj[node].items():
                if nxt in path:
                    continue
                if nxt in targets:
                    found.append((path + [nxt], strength * w * targets[nxt]))
                elif len(path) < max_hops and types.get(nxt) in PASS_THROUGH:
                    walk(nxt, path + [nxt], strength * w)

        walk(asset, [asset], 1.0)
        found = [(p, s) for p, s in found if s > 0]
        if found:
            found.sort(key=lambda ps: -ps[1])
            exposed.append(Exposed(asset, noisy_or([s for _, s in found]),
                                   [p for p, _ in found], [s for _, s in found]))

    exposed.sort(key=lambda x: (-x.exposure, x.asset_id))
    top = exposed[:top_k]
    return top, len(assets) - len(top)
