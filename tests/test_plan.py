"""The shared question plan: every kind of question gets its filters (reasoning/plan.py).

A table of question shapes (filter x measure x negation x combination), so the whole family of
"part of the question was ignored" bugs is covered, not one example. Runs without a model.
"""
import os

import pytest

os.environ["GLOBE_OFFLINE"] = "1"

from reasoning.listing import matching_mine_ids  # noqa: E402
from reasoning.plan import execute, make_plan, rule_plan  # noqa: E402

CN = matching_mine_ids({"countries": ["CN"]})
AU = matching_mine_ids({"countries": ["AU"]})
US = matching_mine_ids({"countries": ["US"]})
BHP = matching_mine_ids({"owners": ["company_bhp"]})
RAIN = {"time": "2026-11-08T05:00", "temperature_2m": 12.0, "precipitation": 1.0, "rain": 1.0,
        "snowfall": 0.0, "wind_speed_10m": 5.0, "weather_code": 61}
WINDY_DRY = {**RAIN, "precipitation": 0.0, "rain": 0.0, "wind_speed_10m": 60.0}


@pytest.fixture(autouse=True)
def no_models_and_fake_weather(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    from pipeline import fetch_weather
    from reasoning import decision_model as dm
    monkeypatch.setattr(dm, "default_backends", lambda: [])
    # Chinese mines: rain; Australian mines: dry but windy; everything else: rain and wind.
    def fake(points):
        return {pid: (RAIN if pid in CN else WINDY_DRY if pid in AU else {**RAIN, "wind_speed_10m": 60.0})
                for pid, _, _ in points}
    monkeypatch.setattr(fetch_weather, "current", fake)


def ids(text):
    return {r["id"] for r in execute(make_plan(text), text)["results"]}


@pytest.mark.parametrize("q,inc,exc", [
    ("mines in China", ["CN"], []),
    ("Chinese mines", ["CN"], []),
    ("mines outside China", [], ["CN"]),
    ("mines not in China or Brazil", [], ["CN", "BR"]),
    ("mines in South America except Brazil", ["AR", "BO", "CL", "CO", "PE", "VE"], ["BR"]),
])
def test_country_include_exclude(q, inc, exc):
    p = rule_plan(q)
    assert set(p["include"]["countries"]) >= set(inc) and set(p["exclude"]["countries"]) == set(exc)
    assert not set(p["include"]["countries"]) & set(p["exclude"]["countries"])


def test_owner_negation():
    p = rule_plan("mines not owned by BHP")
    assert p["exclude"]["owners"] == ["company_bhp"] and not p["include"]["owners"]
    assert ids("mines not owned by BHP").isdisjoint(BHP)


@pytest.mark.parametrize("q,expected", [
    ("show all mines in china which are in rainy conditions", CN),
    ("mines outside China in the rain", None),            # everything rainy except China (and dry AU)
    ("mines that are raining and windy", None),           # everything except CN (no wind) and AU (no rain)
    ("mines that are raining or windy", None),
])
def test_conditions_with_filters_and_combinations(q, expected):
    got = ids(q)
    every = matching_mine_ids({})
    if q.startswith("show all mines in china"):
        assert got == CN
    elif "outside China" in q:
        assert got == every - CN - AU
    elif " and windy" in q:
        assert got == every - CN - AU
    else:
        assert got == every


def test_multiple_conditions_are_all_used():
    p = rule_plan("mines that are raining and windy")
    assert [m["weather"] for m in p["measures"]] == ["rain", "wind"] and p["combine"] == "and"
    assert rule_plan("snow or freezing at mines")["combine"] == "or"


def test_word_boundaries():
    assert not rule_plan("mines in Bahrain")["measures"]          # 'rain' inside Bahrain
    assert not rule_plan("mines near the train line")["measures"]


def test_capacity_numbers_are_not_temperatures():
    p = rule_plan("mines over 10 Mtpa in Australia where it is hot")
    assert p["min_capacity_mtpa"] == 10 and p["measures"][0]["threshold"] is None
    p = rule_plan("US states below 0 degrees")
    assert p["thing"] == "state" and p["measures"] == [{"kind": "weather", "weather": "freezing", "threshold": 0.0}]


def test_unrecognised_places_are_reported_not_dropped():
    out = execute(make_plan("mines in the Midwest"), "mines in the Midwest")
    assert "Midwest" in out["channels"][0]["reason"]


def test_event_places_are_not_filters():
    p = rule_plan("Which mines in Brazil are exposed to the US midterms and sanctions on Russia?")
    assert p["include"]["countries"] == ["BR"] and not p["exclude"]["countries"]
    assert p["measures"] == [{"kind": "exposure"}]


def test_exposure_respects_filters():
    q = "Which iron ore mines in Canada are exposed to the 2026 US midterms?"
    out = execute(make_plan(q), q)
    ca = matching_mine_ids({"countries": ["CA"]})
    assert out["results"] and {r["id"] for r in out["results"]} <= ca
    q2 = "Which iron ore mines outside the US are exposed to the 2026 US midterms?"
    assert {r["id"] for r in execute(make_plan(q2), q2)["results"]}.isdisjoint(US)


def test_top_n_after_ranking():
    out = execute(make_plan("top 3 BHP mines where it is windy"), "top 3 BHP mines where it is windy")
    assert len(out["results"]) == 3 and {r["id"] for r in out["results"]} <= BHP


def test_states():
    p = rule_plan("is it raining in Texas or Minnesota (US states)")
    assert p["thing"] == "state" and set(p["include"]["states"]) == {"state_TX", "state_MN"}
    assert not rule_plan("show all US states")["include"]["countries"]


@pytest.mark.parametrize("q,kinds", [
    ("show all mines", []),
    ("Show me every iron ore mine", []),
    ("which mines does BHP own?", []),
    ("show all mines in the rain", ["weather"]),
    ("what's the weather at the mines", ["weather"]),
    ("US states with a spike in Wikipedia views", ["views"]),
    ("which mines are exposed to sanctions on Russia?", ["exposure"]),
])
def test_question_kinds_without_a_model(q, kinds):
    assert [m["kind"] for m in make_plan(q, backends=[])["measures"]] == kinds


def test_lists():
    out = execute(make_plan("show all mines"), "show all mines")
    assert len(out["results"]) == 701 and out["dark_count"] == 0 and out["channels"][0]["id"] == "filter"
    top = execute(make_plan("3 largest mines in Australia"), "3 largest mines in Australia")
    assert len(top["results"]) == 3 and top["results"][0]["score"] == 1.0 and {r["id"] for r in top["results"]} <= AU


def test_api_answers_plain_questions():
    from fastapi.testclient import TestClient

    from backend.app import app

    c = TestClient(app)
    for q in ["show all mines", "mines", "show all US states", "largest mines in Canada", "hello",
              "mines outside China that are raining or windy", "mines in the Midwest"]:
        r = c.post("/query", json={"text": q})
        assert r.status_code == 200, (q, r.text[:200])


def test_model_plan_is_guarded_and_merged():
    from reasoning.plan import model_plan

    q = "Which mines in Brazil are exposed to the US midterms?"
    obj = {"kind": "exposure", "include_countries": ["Brazil", "United States"], "conditions": []}
    p = model_plan(obj, rule_plan(q), q)
    assert p["include"]["countries"] == ["BR"] and p["measures"] == [{"kind": "exposure"}]
    q2 = "mines outside Australia that are hot or windy"
    p2 = model_plan({"kind": "condition", "exclude_countries": ["AU"], "conditions": ["hot", "wind"], "combine": "or"},
                    rule_plan(q2), q2)
    assert p2["exclude"]["countries"] == ["AU"] and [m["weather"] for m in p2["measures"]] == ["hot", "wind"]
    p3 = model_plan({"kind": "list", "include_countries": ["Atlantis"]}, rule_plan("mines in Atlantis"), "mines in Atlantis")
    assert "Atlantis" in p3["unrecognised"]
