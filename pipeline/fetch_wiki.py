"""Wikimedia pageviews fetcher with disk caching (plan: Attention burst).

Owner: Person A. No key needed. Wikimedia asks API clients to send a descriptive
User-Agent; set WIKI_USER_AGENT in .env to include a contact for your team.

Cached under cache/wiki/ keyed by (title, start, end). GLOBE_OFFLINE=1 forbids
network calls (a cache miss returns an empty series).

Only call this for nodes whose `wiki_title` is filled in. Never guess a title.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "wiki"
API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
USER_AGENT = os.environ.get("WIKI_USER_AGENT", "ResearchersGlobe/0.1 (HackTheBurgh 2026 student project)")


def _key(title: str, start: date, end: date) -> str:
    return hashlib.sha256(f"{title}|{start}|{end}".encode()).hexdigest()[:24]


def pageviews(title: str, start: date, end: date, project: str = "en.wikipedia") -> pd.Series:
    """Daily user pageviews for one article, indexed by date."""
    path = CACHE_DIR / f"{_key(title, start, end)}.json"
    if path.exists():
        raw = json.loads(path.read_text())
        return _series(raw["dates"], raw["values"], title, start, end)
    if os.environ.get("GLOBE_OFFLINE", "0") == "1":
        return pd.Series(dtype=float, name=title)

    article = quote(title.replace(" ", "_"), safe="")
    url = f"{API}/{project}/all-access/user/{article}/daily/{start:%Y%m%d}/{end:%Y%m%d}"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if r.status_code == 404:  # no such article, or no views in range
        items = []
    else:
        r.raise_for_status()
        items = r.json().get("items", [])
    dates = [pd.to_datetime(i["timestamp"][:8], format="%Y%m%d").strftime("%Y-%m-%d") for i in items]
    values = [float(i["views"]) for i in items]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dates": dates, "values": values}))
    return _series(dates, values, title, start, end)


def _series(dates: list[str], values: list[float], title: str, start: date, end: date) -> pd.Series:
    s = pd.Series(values, index=pd.to_datetime(dates), name=title, dtype=float)
    # Days with zero views are omitted by the API: fill them so weekday logic stays aligned.
    if len(s):
        s = s.reindex(pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D"), fill_value=0.0)
    return s


if __name__ == "__main__":
    from datetime import timedelta

    end = date.today() - timedelta(days=1)
    s = pageviews("Iron ore", end - timedelta(days=60), end)
    print(s.tail(10).to_string() if len(s) else "no data (offline or blocked)")
    print(f"cached under {CACHE_DIR}")
