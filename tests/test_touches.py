import pandas as pd

from pipeline.graph import EDGE_COLUMNS, NODE_COLUMNS, Graph
from reasoning.touches import _patterns, count_mentions, weights_from_counts


def graph():
    n = lambda i, t, name, c=None, st="": {"id": i, "type": t, "name": name, "lat": 0.0, "lon": 0.0, "country": c,
                                          "wiki_title": "", "search_terms": st}
    nodes = [n("country_US", "country", "United States", "US"), n("country_CN", "country", "China", "CN"),
             n("country_CA", "country", "Canada", "CA"), n("company_bhp", "company", "BHP Group Ltd", st="BHP"),
             n("company_x", "company", "Mining Co", st="Mining"), n("port_hedland", "port", "Port Hedland", st="Port Hedland")]
    edges = [{"src": "company_bhp", "dst": "country_US", "type": "LISTED_IN", "weight": 1.0, "source_url": ""}]
    return Graph(pd.DataFrame(nodes, columns=NODE_COLUMNS), pd.DataFrame(edges, columns=EDGE_COLUMNS))


TITLES = [
    "US tariffs on Chinese steel back on the agenda after midterms",
    "What the midterms mean for American trade policy",
    "Canada watches US Congress on steel tariffs",
    "Beijing responds to US tariff threats",
    "Let us talk about the midterms",          # 'us' the pronoun must not count
    "BHP shares slip as Port Hedland talks stall",
]


def test_country_aliases_and_case():
    c = count_mentions(TITLES, _patterns(graph(), {"country"}))
    assert c["country_US"] == 4 and c["country_CN"] == 2 and c["country_CA"] == 1


def test_only_curated_companies_are_candidates():
    pats = _patterns(graph(), {"company", "port"})
    assert "company_bhp" in pats and "company_x" not in pats and "port_hedland" in pats


def test_weights_relative_to_most_named_with_thresholds():
    w = weights_from_counts({"country_US": 40, "country_CN": 10, "country_CA": 1}, 100)
    assert w == [["country_US", 1.0, 40], ["country_CN", 0.25, 10]]  # CA dropped: < 2 titles


def test_no_coverage_gives_nothing():
    assert weights_from_counts({}, 0) == []
