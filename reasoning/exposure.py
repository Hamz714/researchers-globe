"""Step 3: propagate exposure through the graph (plan: deterministic, noisy-OR).

Owner: Person B.

    path strength  = w_channel * product of edge weights on the path
    exposure(a)    = 1 - prod over paths (1 - path strength)

Paths that reach the channel through the SAME touched node (e.g. mine -> AU -> CN and
mine -> port -> AU -> CN both arrive via China) are not independent chances, so we
keep only the strongest path per touched node before combining (prevents saturation).
All paths are still returned, strongest first, for the UI.

A path runs from an asset to a chosen channel node, following edges in their own
direction and then the channel's TOUCHES edge backwards (the plan's example path
"mine -> port -> country -> country <- channel"). Rules that keep paths meaningful:
  * paths are simple (no node twice) and at most `max_hops` edges long;
  * they never pass through another asset, an event or another channel;
  * they end at the first channel node reached.
Paths through DIFFERENT touched nodes are still treated as independent (plan).
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

    Edges are walked FORWARD only (mine -> port -> country -> importing country,
    mine -> owner -> listing country), so a Chinese mine is not exposed to Brazil just
    because China imports Brazilian ore. A path ends where a chosen channel TOUCHES a node.
    Returns (top_k exposed assets sorted by exposure desc, dark_count).
    """
    types = dict(zip(graph.nodes["id"], graph.nodes["type"]))
    w_channel = {f"channel_{c}": w for c, w in channel_weights.items()}

    out_edges: dict[str, dict[str, float]] = defaultdict(dict)
    touched_by: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in graph.edges.itertuples():
        if e.src not in types or e.dst not in types:
            continue
        if e.type == "TOUCHES":
            if e.src in w_channel:
                touched_by[e.dst].append((e.src, float(e.weight)))
        else:
            out_edges[e.src][e.dst] = max(out_edges[e.src].get(e.dst, 0.0), float(e.weight))

    assets = [n for n, t in types.items() if t == asset_type]
    exposed: list[Exposed] = []
    for asset in assets:
        found: list[tuple[list[str], float]] = []

        def walk(node: str, path: list[str], strength: float) -> None:
            for nxt, w in out_edges[node].items():
                if nxt in path or types.get(nxt) not in PASS_THROUGH:
                    continue
                s = strength * w
                for ch, w_touch in touched_by.get(nxt, []):
                    found.append((path + [nxt, ch], s * w_touch * w_channel[ch]))
                if len(path) < max_hops:
                    walk(nxt, path + [nxt], s)

        walk(asset, [asset], 1.0)
        found = [(p, s) for p, s in found if s > 0]
        if found:
            found.sort(key=lambda ps: -ps[1])
            best_per_touch: dict[str, float] = {}
            for p, s in found:  # p[-2] is the node the channel touches
                key = p[-2] + "|" + p[-1]
                best_per_touch[key] = max(best_per_touch.get(key, 0.0), s)
            exposed.append(Exposed(asset, noisy_or(list(best_per_touch.values())),
                                   [p for p, _ in found], [s for _, s in found]))

    exposed.sort(key=lambda x: (-x.exposure, x.asset_id))
    top = exposed[:top_k]
    return top, len(assets) - len(top)
