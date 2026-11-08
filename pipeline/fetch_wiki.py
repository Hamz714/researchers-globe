"""Wikimedia pageviews fetcher with disk caching (plan: Attention burst).

Owner: Person A. No key needed. Wikimedia asks API clients to send a descriptive
User-Agent; set WIKI_USER_AGENT in .env to include a contact for your team.

Raw responses are cached under cache/wiki/ keyed by URL, exactly like fetch_gdelt,
so pipeline/browser_fetch.py can fill the cache too. GLOBE_OFFLINE=1 forbids
network calls (a miss returns an empty series and is recorded in MISSES).

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

ROOT = Path(__file__).resolve().parents[1]
API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
USER_AGENT = os.environ.get("WIKI_USER_AGENT", "ResearchersGlobe/0.1 (HackTheBurgh 2026 student project)")
MISSES: dict[str, str] = {}


def cache_dir() -> Path:
    return Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "wiki"


def build_url(title: str, start: date, end: date, project: str = "en.wikipedia") -> str:
    article = quote(title.replace(" ", "_"), safe="")
    return f"{API}/{project}/all-access/user/{article}/daily/{start:%Y%m%d}/{end:%Y%m%d}"


def cache_path(url: str) -> Path:
    return cache_dir() / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.json"


def store(url: str, body: str) -> Path:
    p = cache_path(url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"url": url, "body": body}))
    return p


def get_raw(url: str) -> str | None:
    p = cache_path(url)
    if p.exists():
        return json.loads(p.read_text())["body"]
    if os.environ.get("GLOBE_OFFLINE", "0") == "1":
        MISSES[url] = str(p)
        return None
    import requests

    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    body = "{}" if r.status_code == 404 else (r.raise_for_status() or r.text)
    store(url, body)
    return body


def pageviews(title: str, start: date, end: date, project: str = "en.wikipedia") -> pd.Series:
    """Daily user pageviews for one article, indexed by date (missing days filled with 0)."""
    body = get_raw(build_url(title, start, end, project))
    try:
        items = json.loads(body).get("items", []) if body else []
    except json.JSONDecodeError:
        items = []
    s = pd.Series([float(i["views"]) for i in items],
                  index=pd.to_datetime([i["timestamp"][:8] for i in items], format="%Y%m%d"), name=title, dtype=float)
    if len(s):  # the API omits zero-view days: fill them so weekday logic stays aligned
        s = s.reindex(pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D"), fill_value=0.0)
    return s


if __name__ == "__main__":
    from datetime import timedelta

    end = date.today() - timedelta(days=1)
    s = pageviews("Iron ore", end - timedelta(days=60), end)
    print(s.tail(10).to_string() if len(s) else "no data (offline or blocked)")
    print(f"cache: {cache_dir()}")
