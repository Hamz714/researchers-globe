"""Any question gets an answer: list, live condition or exposure (reasoning/intent.py)."""
import os

import pytest
from fastapi.testclient import TestClient

os.environ["GLOBE_OFFLINE"] = "1"

from backend.app import app  # noqa: E402
from reasoning.intent import route  # noqa: E402
from reasoning.listing import list_assets, parse_filters  # noqa: E402


@pytest.fixture(autouse=True)
def no_models(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    from reasoning import decision_model as dm
    monkeypatch.setattr(dm, "default_backends", lambda: [])


@pytest.mark.parametrize("q,kind", [
    ("show all mines", "list"),
    ("Show me every iron ore mine", "list"),
    ("mines in Brazil", "list"),
    ("which mines does BHP own?", "list"),
    ("show all mines in the rain", "condition"),
    ("what's the weather at the mines", "condition"),
    ("US states with a spike in Wikipedia views", "condition"),
    ("which mines are exposed to sanctions on Russia?", "exposure"),
    ("Which iron ore mines are exposed to the 2026 US midterms?", "exposure"),
])
def test_routing_without_a_model(q, kind):
    assert route(q, backends=[])[0] == kind


def test_filters_by_rules():
    f = parse_filters("the 5 largest iron ore mines in Australia owned by BHP")
    assert f["countries"] == ["AU"] and f["owners"] == ["company_bhp"] and f["top_n"] == 5
    assert parse_filters("mines in South America")["countries"][:2] == ["AR", "BO"]
    assert parse_filters("show all US states")["thing"] == "state"
    assert "US" in parse_filters("mines in the US")["countries"]


def test_list_results():
    allm = list_assets(parse_filters("show all mines"))
    assert len(allm["results"]) == 701 and allm["dark_count"] == 0 and allm["channels"][0]["id"] == "filter"
    br = list_assets(parse_filters("mines in Brazil"))
    assert br["results"] and all(r["paths"][0][1] == "country_BR" for r in br["results"])
    top = list_assets(parse_filters("3 largest mines"))
    assert len(top["results"]) == 3 and top["results"][0]["score"] == 1.0


def test_api_never_503s_on_plain_questions():
    c = TestClient(app)
    for q in ["show all mines", "mines", "show all US states", "largest mines in Canada", "hello"]:
        r = c.post("/query", json={"text": q})
        assert r.status_code == 200, (q, r.text[:200])
    assert len(c.post("/query", json={"text": "show all mines"}).json()["results"]) == 701


def test_condition_respects_filters(monkeypatch):
    """'mines in China in the rain' checks only Chinese mines (bug: it returned rainy mines everywhere)."""
    from pipeline import fetch_weather
    from reasoning.intent import route
    from reasoning.listing import matching_mine_ids
    from reasoning.screen import screen

    rain = {"time": "2026-11-08T05:00", "temperature_2m": 12.0, "precipitation": 1.0, "rain": 1.0,
            "snowfall": 0.0, "wind_speed_10m": 5.0, "weather_code": 61}
    monkeypatch.setattr(fetch_weather, "current", lambda pts: {pid: rain for pid, _, _ in pts})
    q = "show all mines in china which are in rainy conditions"
    kind, spec = route(q, backends=[])
    assert kind == "condition"
    out = screen(q, spec)
    cn = matching_mine_ids({"countries": ["CN"]})
    assert out["results"] and {r["id"] for r in out["results"]} == cn
    assert out["dark_count"] == 701 - len(cn)
    assert "China" in out["criteria"]["event"]["name"]
    # no filter: every mine is checked
    assert len(screen("show all mines in the rain")["results"]) == 701
    # owner filter and top N
    bhp = screen("top 3 BHP mines in the rain")
    assert len(bhp["results"]) == 3 and set(r["id"] for r in bhp["results"]) <= matching_mine_ids({"owners": ["company_bhp"]})
