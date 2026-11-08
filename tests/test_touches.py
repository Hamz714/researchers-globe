import pandas as pd

from pipeline.graph import EDGE_COLUMNS, NODE_COLUMNS, Graph
from reasoning.touches import _patterns, clean_title, count_mentions, weights_from_counts


def graph():
    n = lambda i, t, name, c=None, st="": {"id": i, "type": t, "name": name, "lat": 0.0, "lon": 0.0, "country": c,
                                          "wiki_title": "", "search_terms": st}
    nodes = [n("country_US", "country", "United States", "US"), n("country_CN", "country", "China", "CN"),
             n("country_CA", "country", "Canada", "CA"), n("company_bhp", "company", "BHP Group Ltd", st="BHP"),
             n("company_x", "company", "Mining Co", st="Mining"), n("port_hedland", "port", "Port Hedland", st="Port Hedland")]
    edges = [{"src": "company_bhp", "dst": "country_US", "type": "LISTED_IN", "weight": 1.0, "source_url": ""}]
    return Graph(pd.DataFrame(nodes, columns=NODE_COLUMNS), pd.DataFrame(edges, columns=EDGE_COLUMNS))


ARTS = [
    {"title": "US tariffs on Chinese steel back on the agenda after midterms", "domain": "a.com"},
    {"title": "What the midterms mean for American trade policy", "domain": "b.com"},
    {"title": "Canada watches US Congress on steel tariffs", "domain": "a.com"},
    {"title": "Beijing responds to US tariff threats", "domain": "c.com"},
    {"title": "Let us talk about the midterms", "domain": "d.com"},          # 'us' the pronoun must not count
    {"title": "BHP shares slip as Port Hedland talks stall", "domain": "e.com"},
    {"title": "Steel tariffs after the midterms - Thailand Business News", "domain": "thb.com"},
    {"title": "Steel trade outlook darkens | Indian Business of Tech", "domain": "ibt.com"},
]


def test_country_aliases_and_case():
    c = count_mentions(ARTS, _patterns(graph(), {"country"}))
    assert c["country_US"] == (4, 3) and c["country_CN"] == (2, 2) and c["country_CA"] == (1, 1)


def test_site_names_are_stripped():
    assert clean_title("Steel tariffs after the midterms - Thailand Business News") == "Steel tariffs after the midterms"
    assert clean_title("Steel trade outlook darkens | Indian Business of Tech") == "Steel trade outlook darkens"
    assert clean_title("Rio Tinto - BHP merger talk") == "Rio Tinto - BHP merger talk"  # too short to be a site suffix


def test_needs_two_different_sites():
    assert weights_from_counts({"country_IN": (5, 1), "country_US": (4, 3)}, 100) == [["country_US", 1.0, 4]]


def test_only_curated_companies_are_candidates():
    pats = _patterns(graph(), {"company", "port"})
    assert "company_bhp" in pats and "company_x" not in pats and "port_hedland" in pats


def test_weights_relative_to_most_named_with_thresholds():
    w = weights_from_counts({"country_US": (40, 9), "country_CN": (10, 4), "country_CA": (1, 1)}, 100)
    assert w == [["country_US", 1.0, 40], ["country_CN", 0.25, 10]]  # CA dropped: < 2 titles


def test_no_coverage_gives_nothing():
    assert weights_from_counts({}, 0) == []
