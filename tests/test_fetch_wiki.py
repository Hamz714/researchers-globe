import json
from datetime import date

from pipeline import fetch_wiki


def _store(title, start, end, days_views):
    items = [{"timestamp": f"{d}00", "views": v} for d, v in days_views]
    fetch_wiki.store(fetch_wiki.build_url(title, start, end), json.dumps({"items": items}))


def test_pageviews_fill_gaps_but_not_unpublished_days(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GLOBE_OFFLINE", "1")
    start, end = date(2026, 11, 1), date(2026, 11, 8)
    # API returned 1-6 Nov except 3 Nov (zero views); 7-8 Nov are not published yet.
    _store("X", start, end, [("20261101", 5), ("20261102", 6), ("20261104", 7), ("20261105", 8), ("20261106", 9)])
    s = fetch_wiki.pageviews("X", start, end)
    assert s.index.max().date() == date(2026, 11, 6)  # no zero padding past the last published day
    assert s.loc["2026-11-03"] == 0.0 and len(s) == 6  # an omitted day inside the range is a zero


def test_pageviews_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GLOBE_OFFLINE", "1")
    assert len(fetch_wiki.pageviews("Nope", date(2026, 11, 1), date(2026, 11, 8))) == 0
