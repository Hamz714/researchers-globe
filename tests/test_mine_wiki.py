"""pipeline/curated/mine_wiki.csv: every row maps a real GEM mine to an exact, checked Wikipedia title."""
import pandas as pd

from pipeline.graph import CURATED_DIR, GRAPH_DIR


def test_mine_wiki_rows_are_complete_and_unique():
    t = pd.read_csv(CURATED_DIR / "mine_wiki.csv")
    assert t["mine_id"].is_unique
    assert t["mine_id"].str.startswith("mine_P").all()
    for col in ("name_in_gem", "wiki_title", "check", "source_url"):
        assert t[col].notna().all() and (t[col].str.strip() != "").all(), col
    assert t["source_url"].str.startswith("https://en.wikipedia.org/wiki/").all()


def test_mine_wiki_matches_built_graph():
    t = pd.read_csv(CURATED_DIR / "mine_wiki.csv").set_index("mine_id")
    nodes = pd.read_parquet(GRAPH_DIR / "nodes.parquet").set_index("id")
    assert set(t.index) <= set(nodes.index), "mine_id not in the graph (not an operating GEM mine?)"
    assert (nodes.loc[t.index, "name"] == t["name_in_gem"]).all()
    assert (nodes.loc[t.index, "wiki_title"] == t["wiki_title"]).all(), "run `make graph`"
