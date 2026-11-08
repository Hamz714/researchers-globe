"""Condition screens (reasoning/screen.py): parsing, weather rules, the QueryResult shape. Offline."""
from datetime import date

import pandas as pd
import pytest

from reasoning import screen as sc


@pytest.mark.parametrize("text,expected", [
    ("Which US states have a spike in Wikipedia views?", {"thing": "state", "condition": "views"}),
    ("Which iron ore mines have a spike in Wikipedia edits?", {"thing": "mine", "condition": "edits"}),
    ("Which iron ore mines are windy right now?", {"thing": "mine", "condition": "weather", "weather": "wind"}),
    ("Which US states are below 0 degrees right now?",
     {"thing": "state", "condition": "weather", "weather": "cold", "threshold": 0.0}),
    ("Which states have snow?", {"thing": "state", "condition": "weather", "weather": "snow"}),
])
def test_parse_screen(text, expected):
    assert sc.parse_screen(text) == expected


@pytest.mark.parametrize("text", [
    "Which iron ore mines are exposed to the 2026 US midterms?",   # event exposure: run_query's job
    "Which iron ore mines are exposed to the Premier League title race?",
    "What is the weather like?",                                    # no thing
])
def test_not_a_screen(text):
    assert sc.parse_screen(text) is None


def test_weather_rules():
    assert sc._weather_match({"wind_speed_10m": 20.0}, "wind", None) is None
    ratio, desc = sc._weather_match({"wind_speed_10m": 61.0}, "wind", None)
    assert ratio > 2 and "61" in desc
    assert sc._weather_match({"temperature_2m": 1.0}, "cold", 0.0) is None
    assert sc._weather_match({"temperature_2m": -7.0}, "cold", 0.0)[0] > 1
    assert sc._weather_match({"precipitation": 0.0}, "rain", None) is None


def test_weather_screen_has_contract_shape(monkeypatch):
    from pipeline import fetch_weather

    items = [{"id": "state_AK", "name": "Alaska", "lat": 65.4, "lon": -151.6, "wiki_title": "Alaska", "wiki_of": "Alaska"},
             {"id": "state_HI", "name": "Hawaii", "lat": 21.5, "lon": -158.0, "wiki_title": "Hawaii", "wiki_of": "Hawaii"}]
    monkeypatch.setattr(sc, "things", lambda kind: items)
    monkeypatch.setattr(fetch_weather, "current", lambda pts: {
        "state_AK": {"time": "2026-11-08T04:00", "temperature_2m": -7.1},
        "state_HI": {"time": "2026-11-08T04:00", "temperature_2m": 24.0}})
    out = sc.screen("Which US states are below 0 degrees right now?", end=date(2026, 11, 8))
    assert [r["id"] for r in out["results"]] == ["state_AK"] and out["dark_count"] == 1
    assert out["channels"][0]["id"] == "weather" and "°C" in out["results"][0]["reason"]


def test_views_screen_lights_only_spikes(monkeypatch):
    from pipeline import fetch_wiki

    items = [{"id": "state_GA", "name": "Georgia", "lat": 32.6, "lon": -83.4, "wiki_title": "Georgia (U.S. state)",
              "wiki_of": "Georgia"},
             {"id": "state_OH", "name": "Ohio", "lat": 40.3, "lon": -82.8, "wiki_title": "Ohio", "wiki_of": "Ohio"}]
    monkeypatch.setattr(sc, "things", lambda kind: items)
    days = pd.date_range("2026-09-02", "2026-11-06", freq="D")

    def views(title, start, end):
        v = [100.0] * len(days)
        if title.startswith("Georgia"):
            v[-5:] = [2000.0] * 5
        return pd.Series(v, index=days)

    monkeypatch.setattr(fetch_wiki, "pageviews", views)
    out = sc.screen("Which US states have a spike in Wikipedia views?", end=date(2026, 11, 8))
    assert [r["name"] for r in out["results"]] == ["Georgia"] and out["results"][0]["tier"] == "high"
    assert out["results"][0]["evidence"][0]["url"].startswith("https://en.wikipedia.org/wiki/Georgia")
