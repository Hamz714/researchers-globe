"""Exposure propagation on a hand-built 10-node toy graph."""
import pandas as pd
import pytest

from pipeline.graph import EDGE_COLUMNS, NODE_COLUMNS, Graph
from reasoning.exposure import noisy_or, propagate, touches_edges


def n(id_, type_):
    return {"id": id_, "type": type_, "name": id_, "lat": 0.0, "lon": 0.0, "country": None,
            "wiki_title": "", "search_terms": id_}


def e(src, dst, type_, w=1.0):
    return {"src": src, "dst": dst, "type": type_, "weight": w, "source_url": ""}


@pytest.fixture
def toy():
    # 10 nodes: 3 mines, 2 companies, 3 countries, 1 port, 1 channel
    nodes = [n("mine_a", "mine"), n("mine_b", "mine"), n("mine_c", "mine"),
             n("co_x", "company"), n("co_y", "company"),
             n("country_BR", "country"), n("country_CN", "country"), n("country_SE", "country"),
             n("port_p", "port"), n("channel_tariffs", "channel")]
    edges = [
        e("mine_a", "country_BR", "LOCATED_IN"), e("mine_a", "port_p", "EXPORTS_VIA"),
        e("port_p", "country_BR", "LOCATED_IN"), e("country_BR", "country_CN", "EXPORTS_TO", 0.5),
        e("mine_a", "co_x", "OWNED_BY", 0.8), e("co_x", "country_CN", "LISTED_IN"),
        e("mine_b", "co_x", "OWNED_BY", 1.0),
        e("mine_c", "country_SE", "LOCATED_IN"), e("mine_c", "co_y", "OWNED_BY"),
    ]
    g = Graph(pd.DataFrame(nodes, columns=NODE_COLUMNS), pd.DataFrame(edges, columns=EDGE_COLUMNS))
    g.validate()
    return g.with_edges(touches_edges(["tariffs"], {"tariffs": [["country_CN", 1.0]]}))


def test_noisy_or():
    assert noisy_or([]) == 0.0
    assert noisy_or([0.5, 0.5]) == pytest.approx(0.75)
    assert noisy_or([1.0, 0.2]) == pytest.approx(1.0)


def test_exposure_values(toy):
    top, dark = propagate(toy, {"tariffs": 0.9})
    got = {x.asset_id: x for x in top}
    # mine_a paths: BR->CN (0.5), port->BR->CN (0.5), co_x->CN (0.8); each * 0.9.
    # All three arrive via the same touched node (CN), so only the strongest counts.
    assert got["mine_a"].exposure == pytest.approx(0.72)
    assert len(got["mine_a"].paths) == 3
    # mine_b: co_x -> CN only
    assert got["mine_b"].exposure == pytest.approx(0.9)
    assert "mine_c" not in got and dark == 1


def test_paths_start_at_asset_end_at_channel(toy):
    top, _ = propagate(toy, {"tariffs": 0.9})
    for x in top:
        for p in x.paths:
            assert p[0] == x.asset_id and p[-1] == "channel_tariffs"
            assert len(p) == len(set(p))


def test_independent_touches_combine_with_noisy_or(toy):
    g = toy.with_edges(touches_edges(["tariffs"], {"tariffs": [["country_BR", 0.5]]}))
    top, _ = propagate(g, {"tariffs": 0.9})
    got = {x.asset_id: x for x in top}
    # strongest via CN = 0.72, via BR directly = 0.45 -> 1 - 0.28 * 0.55
    assert got["mine_a"].exposure == pytest.approx(1 - 0.28 * 0.55)


def test_no_path_through_another_mine(toy):
    top, _ = propagate(toy, {"tariffs": 0.9})
    for x in top:
        for p in x.paths:
            assert not any(node.startswith("mine_") for node in p[1:])


def test_top_k_and_dark_count(toy):
    top, dark = propagate(toy, {"tariffs": 0.9}, top_k=1)
    assert len(top) == 1 and top[0].asset_id == "mine_b" and dark == 2


def test_no_channels_means_everything_dark(toy):
    top, dark = propagate(toy, {})
    assert top == [] and dark == 3


def test_max_hops(toy):
    top, _ = propagate(toy, {"tariffs": 0.9}, max_hops=1)
    assert {x.asset_id for x in top} == set()  # every path needs >= 2 hops to reach CN
