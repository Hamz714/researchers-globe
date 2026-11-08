"""Live Wikipedia edits and the new kinds of thing (ports, countries, companies)."""
import json
import os
import time

import pytest

os.environ["GLOBE_OFFLINE"] = "1"

from pipeline.live_wiki import LIVE, LiveWiki  # noqa: E402
from reasoning.plan import execute, make_plan, rule_plan  # noqa: E402
from reasoning.things import all_wiki_titles, detect_kind, things  # noqa: E402


def ev(title, ago=60, bot=False, wiki="enwiki", ns=0, typ="edit", user="Alice"):
    return {"wiki": wiki, "namespace": ns, "type": typ, "title": title, "timestamp": time.time() - ago,
            "user": user, "bot": bot, "comment": "copyedit", "revision": {"old": 1, "new": 2},
            "server_url": "https://en.wikipedia.org"}


@pytest.fixture(autouse=True)
def fresh_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    from reasoning import decision_model as dm
    monkeypatch.setattr(dm, "default_backends", lambda: [])
    LIVE.edits.clear()
    LIVE.set_watch(all_wiki_titles())
    yield
    LIVE.edits.clear()


@pytest.mark.parametrize("q,kind", [
    ("which countries are being edited on Wikipedia right now", "country"),
    ("which mining companies were edited in the last 30 minutes", "company"),
    ("which export ports are in the rain", "port"),
    ("which ports do mines in Brazil ship through", "port"),
    ("mines in the United States", "mine"),
    ("show all US states", "state"),
])
def test_detect_kind(q, kind):
    assert detect_kind(q) == kind


def test_registry_shapes():
    for kind, n_min in (("port", 30), ("country", 50), ("company", 20), ("state", 50), ("mine", 700)):
        items = things(kind)
        assert len(items) >= n_min
        assert {"id", "name", "lat", "lon", "country", "wiki_title", "owners", "capacity"} <= set(items[0])
    assert "China" in all_wiki_titles() and "BHP" in all_wiki_titles()


def test_handle_filters_to_watched_english_articles():
    w = LiveWiki()
    w.set_watch({"China": [("country", "country_CN")]})
    assert w.handle(ev("China"))
    assert not w.handle(ev("China", wiki="dewiki")) and not w.handle(ev("China", ns=1))
    assert not w.handle(ev("Somewhere else")) and not w.handle(ev("China", typ="log"))
    assert len(w.edits) == 1 and w.events_seen == 5


def test_live_question_counts_window_and_tiers():
    for _ in range(5):
        LIVE.handle(ev("China", ago=120))
    LIVE.handle(ev("Brazil", ago=300, bot=True))
    LIVE.handle(ev("Australia", ago=7200))          # outside "right now" (60 min)
    q = "which countries are being edited on Wikipedia right now"
    p = make_plan(q, backends=[])
    assert p["thing"] == "country" and p["measures"] == [{"kind": "live_edits"}] and p["window_minutes"] == 60
    out = execute(p, q)
    got = {r["name"]: r for r in out["results"]}
    assert set(got) == {"China", "Brazil"} and got["China"]["tier"] == "high" and got["Brazil"]["tier"] == "low"
    assert "bot" in got["Brazil"]["reason"] and out["channels"][0]["id"] == "live_edits"
    assert out["criteria"]["asset_type"] == "country" and got["China"]["evidence"][0]["url"].startswith("https://")
    assert "Australia" in {r["name"] for r in execute(make_plan("countries edited in the last 3 hours"), "x")["results"]}


def test_live_filters_apply():
    LIVE.handle(ev("China"))
    LIVE.handle(ev("Brazil"))
    q = "countries in South America being edited right now"
    assert {r["name"] for r in execute(make_plan(q), q)["results"]} == {"Brazil"}


def test_new_kinds_list_and_weather(monkeypatch):
    out = execute(make_plan("show all export ports in Australia"), "show all export ports in Australia")
    assert out["criteria"]["asset_type"] == "port" and {r["name"] for r in out["results"]} >= {"Port Hedland", "Dampier"}
    assert out["results"][0]["name"] == "Port Hedland"            # most mines ship through it
    top = execute(make_plan("top 5 countries"), "top 5 countries")
    assert len(top["results"]) == 5 and top["results"][0]["name"] in {"Australia", "Brazil", "China"}
    comp = execute(make_plan("mining companies listed in Australia"), "mining companies listed in Australia")
    assert "BHP Group Ltd" in {r["name"] for r in comp["results"]}
    # weather is not available for companies: said so, not silently ignored
    c = execute(make_plan("mining companies in the rain"), "mining companies in the rain")
    assert "Not available for mining companies" in c["channels"][0]["reason"]


def test_snapshot_round_trip():
    LIVE.handle(ev("China"))
    LIVE.save_snapshot()
    w = LiveWiki()
    w.load_snapshot()
    assert [e["title"] for e in w.edits] == ["China"]


def test_stream_parser(monkeypatch):
    """The SSE loop: data lines joined, blank line dispatches, id remembered for resume."""
    import requests

    w = LiveWiki()
    w.set_watch({"China": [("country", "country_CN")]})
    lines = ["event: message", 'id: [{"offset":1}]', "data: " + json.dumps(ev("China")), "",
             ":ok", "data: " + json.dumps(ev("Elsewhere")), ""]

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            yield from lines
            w._stop.set()

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    w._run()
    assert [e["title"] for e in w.edits] == ["China"] and w._last_id == '[{"offset":1}]' and w.events_seen == 2
