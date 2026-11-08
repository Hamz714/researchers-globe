"""GET /asset/{id} on real preset results: paths, edge types, cached news series (Person C)."""
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["GLOBE_OFFLINE"] = "1"

from backend import app as backend_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HERO = "Which iron ore mines are exposed to the 2026 US midterms?"
client = TestClient(backend_app.app)


@pytest.fixture(autouse=True)
def empty_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    backend_app._recent.clear()


def _hero_row(with_port: bool = True) -> dict:
    doc = json.loads((ROOT / "data" / "preset_results" / "hero_midterms.json").read_text())
    for r in doc["results"]:
        if not with_port or any(n.startswith("port_") for p in r["paths"] for n in p):
            return r
    pytest.skip("no hero result with a port on its paths")


def test_real_asset_opens_with_paths_and_edge_types():
    row = _hero_row()
    r = client.get(f"/asset/{row['id']}", params={"q": HERO})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["paths"] == row["paths"]
    types = {e["type"] for e in d["path_edges"]}
    assert "TOUCHES" in types and "UNKNOWN" not in types
    nodes = {n["id"]: n for n in d["path_nodes"]}
    port = next(n for n in nodes.values() if n["type"] == "port")
    assert port["lat"] is not None and port["lon"] is not None
    assert all(n["type"] == "channel" for nid, n in nodes.items() if nid.startswith("channel_"))
    # empty cache: no fake series for real data, and a note saying why
    assert d["series"] is None and d["series_note"]


def test_asset_found_without_question():
    row = _hero_row(with_port=False)
    assert client.get(f"/asset/{row['id']}").status_code == 200


def test_cached_series_is_used(tmp_path):
    from pipeline import fetch_gdelt
    from pipeline.graph import default_graph
    from reasoning.parser import load_channels
    from reasoning.run_query import _asset_context, asset_evidence_terms

    row = _hero_row()
    doc = json.loads((ROOT / "data" / "preset_results" / "hero_midterms.json").read_text())
    channel_terms = asset_evidence_terms(doc, row["id"], load_channels())
    ctx = _asset_context(default_graph(), row["id"])
    terms = ctx["terms"] + ctx["owner_terms"]
    end = date.today() - timedelta(days=1)  # precomputed yesterday
    start = end - timedelta(days=67)
    data = [{"date": (start + timedelta(days=i)).strftime("%Y%m%dT000000Z"), "value": 3 if i < 60 else 12}
            for i in range(68)]
    fetch_gdelt.store(fetch_gdelt.build_url("timelinevolraw", terms, channel_terms, start, end),
                      json.dumps({"timeline": [{"data": data}]}))
    d = client.get(f"/asset/{row['id']}", params={"q": HERO}).json()
    s = d["series"]
    assert s is not None and len(s["news_volume"]) == 68 and s["recent_days"] == 7
    assert s["news_volume"][-1] == 12 and s["news_volume"][0] == 3


def test_unknown_asset_is_404():
    assert client.get("/asset/mine_nope", params={"q": HERO}).status_code == 404
