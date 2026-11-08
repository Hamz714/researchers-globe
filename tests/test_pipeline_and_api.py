import os

import pytest
from fastapi.testclient import TestClient

os.environ["GLOBE_OFFLINE"] = "1"

from backend.app import app  # noqa: E402
from backend.schemas import QueryResult  # noqa: E402
from pipeline.graph import build_mock_graph  # noqa: E402
from reasoning.run_query import run_query  # noqa: E402

client = TestClient(app)
HERO = "Which iron ore mines are exposed to the 2026 US midterms?"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    from reasoning import decision_model as dm
    monkeypatch.setattr(dm, "default_backends", lambda: [])


def test_mock_graph_is_valid():
    g = build_mock_graph()
    assert (g.nodes["type"] == "mine").sum() == 20


def test_run_query_end_to_end_offline():
    out = run_query(HERO, graph=build_mock_graph())
    QueryResult.model_validate(out)
    assert out["results"], "hero query should expose some mines"
    assert len(out["results"]) + out["dark_count"] == 20
    us = {r["id"] for r in out["results"]}
    assert {"mine_014", "mine_015"} <= us  # the two US mines


def test_negative_control_stays_dark():
    out = run_query("Which iron ore mines are exposed to the Premier League title race?", graph=build_mock_graph())
    assert out["results"] == [] and out["dark_count"] == 20


def test_api_endpoints():
    assert client.get("/health").json()["ok"]
    r = client.post("/query", json={"text": HERO})
    assert r.status_code == 200
    QueryResult.model_validate(r.json())
    presets = client.get("/presets").json()
    events = [p for p in presets if not p["id"].startswith("screen_")]
    assert len(events) == 4 and events[0]["kind"] == "hero"
    assert len(presets) > len(events)  # condition screens come first
    a = client.get("/asset/mine_014").json()
    assert a["path_nodes"] and a["series"]["news_volume"]
    assert client.get("/asset/nope").status_code == 404
    assert "judge" in client.get("/eval").json()
    pts = client.get("/assets").json()
    assert len(pts) >= 20 and {"id", "name", "lat", "lon"} <= set(pts[0])


def test_api_rejects_empty_query():
    assert client.post("/query", json={"text": ""}).status_code == 422
