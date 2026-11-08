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
    import time

    import requests

    for attempt in range(5):  # Wikimedia answers bursts from a shared IP with 429
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code != 429:
            break
        try:
            delay = float(r.headers.get("Retry-After", ""))
        except ValueError:
            delay = 2.0 * (attempt + 1)
        time.sleep(min(delay, 30.0))
    body = "{}" if r.status_code == 404 else (r.raise_for_status() or r.text)
    store(url, body)
    return body


def pageviews(title: str, start: date, end: date, project: str = "en.wikipedia") -> pd.Series:
    """Daily user pageviews for one article, indexed by date, up to the last published day
    (days the API omits inside that range are filled with 0)."""
    body = get_raw(build_url(title, start, end, project))
    try:
        items = json.loads(body).get("items", []) if body else []
    except json.JSONDecodeError:
        items = []
    s = pd.Series([float(i["views"]) for i in items],
                  index=pd.to_datetime([i["timestamp"][:8] for i in items], format="%Y%m%d"), name=title, dtype=float)
    if len(s):
        # The API omits zero-view days: fill them so weekday logic stays aligned. Fill only up to
        # the last day the API returned: pageviews are published with a ~1-2 day lag, and padding
        # the newest (not yet published) days with zeros would fake a drop in the recent window.
        s = s.reindex(pd.date_range(pd.Timestamp(start), s.index.max(), freq="D"), fill_value=0.0)
    return s


if __name__ == "__main__":
    from datetime import timedelta

    end = date.today() - timedelta(days=1)
    s = pageviews("Iron ore", end - timedelta(days=60), end)
    print(s.tail(10).to_string() if len(s) else "no data (offline or blocked)")
    print(f"cache: {cache_dir()}")


# ---------------------------------------------------------------- edits (live, MediaWiki Action API)
ACTION_API = "https://en.wikipedia.org/w/api.php"
EDITS_MAX_PAGES = 10  # x 500 revisions; a page edited more than that in the window is capped (and noted)


ACTION_MIN_INTERVAL_S = 1.0  # one Action API request per second (it 429s bursts from a shared IP)
_last_action = 0.0


def _action_get(requests, params: dict, retries: int = 5) -> dict:
    """GET the Action API at most once per ACTION_MIN_INTERVAL_S, honouring Retry-After on 429."""
    global _last_action
    import time

    for attempt in range(retries):
        wait = ACTION_MIN_INTERVAL_S - (time.monotonic() - _last_action)
        if wait > 0:
            time.sleep(wait)
        _last_action = time.monotonic()
        r = requests.get(ACTION_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 429:
            try:
                delay = float(r.headers.get("Retry-After", ""))
            except ValueError:
                delay = 5.0 * (attempt + 1)
            time.sleep(min(delay, 60.0))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def edits_cache_path(title: str, start: date, end: date) -> Path:
    key = hashlib.sha256(f"edits|{title}|{start}|{end}".encode()).hexdigest()[:32]
    return Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "wiki_edits" / f"{key}.json"


def edits(title: str, start: date, end: date) -> pd.Series:
    """Daily edit counts for one English Wikipedia article, start..end inclusive (zero-edit days = 0).

    Uses the live MediaWiki revisions API (the Wikimedia REST edit metrics come from monthly
    dumps and stop at the end of the previous month, checked 8 Nov 2026). Revisions are paged
    500 at a time, newest first, at most EDITS_MAX_PAGES pages. Cached per (title, start, end);
    GLOBE_OFFLINE=1 reads the cache only (a miss gives an empty series)."""
    p = edits_cache_path(title, start, end)
    if p.exists():
        stamps = json.loads(p.read_text())["timestamps"]
    elif os.environ.get("GLOBE_OFFLINE", "0") == "1":
        return pd.Series(dtype=float, name=title)
    else:
        import requests

        params = {"action": "query", "prop": "revisions", "titles": title, "rvprop": "timestamp",
                  "rvlimit": "500", "rvstart": f"{end:%Y-%m-%d}T23:59:59Z", "rvend": f"{start:%Y-%m-%d}T00:00:00Z",
                  "format": "json", "formatversion": "2", "redirects": "1"}
        stamps, pages = [], 0
        while pages < EDITS_MAX_PAGES:
            d = _action_get(requests, params)
            for pg in d.get("query", {}).get("pages", []):
                stamps += [rv["timestamp"] for rv in pg.get("revisions", [])]
            pages += 1
            if "continue" not in d:
                break
            params.update(d["continue"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"title": title, "start": str(start), "end": str(end), "timestamps": stamps,
                                 "capped": pages >= EDITS_MAX_PAGES}))
    days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    if not stamps:
        return pd.Series(0.0, index=days, name=title)
    counts = pd.Series(1.0, index=pd.to_datetime([s[:10] for s in stamps])).groupby(level=0).sum()
    return counts.reindex(days, fill_value=0.0).rename(title)
