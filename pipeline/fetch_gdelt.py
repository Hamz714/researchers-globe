"""GDELT DOC 2.0 fetcher with disk caching (plan: Step 4, Caching).

Owner: Person A.

Every request is identified by its exact URL and the raw response body is cached
under cache/gdelt/<sha>.json, so the demo runs offline, no URL is fetched twice,
and the cache can be filled from anywhere that can reach GDELT (a laptop, a
browser; see pipeline/browser_fetch.py) and copied in.

Modes:
  GLOBE_OFFLINE=1  never touch the network; a cache miss returns an empty result
                   and is recorded in MISSES (browser_fetch.py turns those into a job list).
  GLOBE_OFFLINE=0  fetch misses live, at most one request every MIN_INTERVAL_S.

Facts checked at the venue on 8 Nov 2026:
  * Rate limit: GDELT answers HTTP 429 with "Please limit requests to one every 5 seconds".
  * GDELT DOC only covers a rolling ~3 months (from the plan).
Timelines are resampled to daily (sum for counts, mean for tone) whatever resolution comes back,
and days inside a known DOC outage (GDELT_DOC_GAPS) are NaN.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_INTERVAL_S = float(os.environ.get("GDELT_MIN_INTERVAL_S", "5.5"))

# GDELT DOC outages: days the API returns ~nothing for every query. Measured on 8 Nov 2026 from a
# hotspot: "iron ore" and four high-volume queries (e.g. attack/conflict ~15,000 articles/day)
# drop to 0 (a few hundred at most) on 3-14 Oct and 26 Oct-5 Nov 2026; 6-7 Nov recover to only
# ~25-37% of normal. parse_timeline() sets these days to NaN (missing), not 0, so signals skip
# them instead of reading an outage as a drop in coverage. Override with GDELT_DOC_GAPS in .env:
# comma-separated "YYYY-MM-DD:YYYY-MM-DD" ranges, inclusive ("" = none).
DEFAULT_DOC_GAPS = "2026-10-03:2026-10-14,2026-10-26:2026-11-05"


def doc_gaps() -> list[tuple[date, date]]:
    """Configured outage ranges (inclusive), read at call time so a .env loaded later applies."""
    raw = os.environ.get("GDELT_DOC_GAPS", DEFAULT_DOC_GAPS)
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split(":")
        out.append((date.fromisoformat(a.strip()), date.fromisoformat(b.strip())))
    return out


def in_gap(day: date) -> bool:
    return any(a <= day <= b for a, b in doc_gaps())


def gap_days_in(start: date, end: date) -> int:
    """Outage days within [start, end]."""
    return sum(in_gap(start + timedelta(days=i)) for i in range((end - start).days + 1))
MISSES: dict[str, str] = {}  # url -> cache path, filled when offline
_last_call = 0.0


def cache_dir() -> Path:
    return Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "gdelt"


def _offline() -> bool:
    """GDELT's DOC API is read from cache only unless GDELT_LIVE=1 (set by `make fetch-all` and
    `make fetch-sample`). At query time it is too slow and rate-limited (429s from shared IPs),
    and it had outages (GDELT_DOC_GAPS), so live questions never wait on it."""
    return os.environ.get("GLOBE_OFFLINE", "0") == "1" or os.environ.get("GDELT_LIVE", "0") != "1"


def _group(terms: list[str]) -> str:
    """('a', 'b c') -> '(a OR "b c")'; a single term gets no parentheses (GDELT rejects them)."""
    terms = [t.strip() for t in terms if t and t.strip()]
    quoted = [f'"{t}"' if " " in t or "-" in t else t for t in dict.fromkeys(terms)]
    if not quoted:
        raise ValueError("empty term group")
    return quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"


def build_url(mode: str, terms_a: list[str], terms_b: list[str] | None, start: date, end: date,
              max_records: int = 250) -> str:
    query = _group(terms_a) + (" " + _group(terms_b) if terms_b else "")
    params = {
        "query": query, "mode": mode, "format": "json",
        "startdatetime": datetime.combine(start, datetime.min.time()).strftime("%Y%m%d%H%M%S"),
        "enddatetime": datetime.combine(end + timedelta(days=1), datetime.min.time()).strftime("%Y%m%d%H%M%S"),
    }
    if mode == "artlist":
        params.update(maxrecords=str(max_records), sort="hybridrel")
    return f"{API}?{urlencode(params)}"


def cache_path(url: str) -> Path:
    return cache_dir() / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}.json"


def store(url: str, body: str) -> Path:
    """Write one raw response into the cache (used by the live fetch and by browser_fetch ingest)."""
    p = cache_path(url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"url": url, "body": body}))
    return p


def get_raw(url: str) -> str | None:
    """Raw body for a URL: from cache, else live (if allowed), else None and the URL is recorded as a miss."""
    global _last_call
    p = cache_path(url)
    if p.exists():
        return json.loads(p.read_text())["body"]
    if _offline():
        MISSES[url] = str(p)
        return None
    import requests

    for attempt in range(3):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        r = requests.get(url, timeout=30, headers={"User-Agent": "ResearchersGlobe/0.1 (HackTheBurgh 2026)"})
        if r.status_code == 429 or "limit requests" in r.text[:200]:
            time.sleep(MIN_INTERVAL_S * (attempt + 2))
            continue
        r.raise_for_status()
        store(url, r.text)
        return r.text
    return None


def _json(body: str | None) -> dict:
    if not body or not body.strip().startswith("{"):
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        from gdeltdoc.helpers import load_json  # tolerates GDELT's stray control characters

        try:
            return load_json(body, 100)
        except Exception:
            return {}


def parse_timeline(body: str | None, mode: str) -> pd.Series:
    j = _json(body)
    tl = j.get("timeline") or []
    if not tl or not tl[0].get("data"):
        return pd.Series(dtype=float, name=mode)
    data = tl[0]["data"]
    s = pd.Series([float(d["value"]) for d in data],
                  index=pd.to_datetime([d["date"] for d in data], format="%Y%m%dT%H%M%SZ"), name=mode)
    s = s.resample("D").sum() if mode == "timelinevolraw" else s.resample("D").mean()
    gaps = [ts for ts in s.index if in_gap(ts.date())]
    if gaps:
        s = s.astype(float)
        s.loc[gaps] = np.nan  # outage: missing, not zero
    return s.rename(mode)


def timeline(terms_a: list[str], terms_b: list[str] | None, start: date, end: date,
             mode: str = "timelinevolraw") -> pd.Series:
    """Daily series for (terms_a AND terms_b). mode: 'timelinevolraw' (counts) or 'timelinetone'."""
    if mode not in {"timelinevolraw", "timelinetone"}:
        raise ValueError(mode)
    return parse_timeline(get_raw(build_url(mode, terms_a, terms_b, start, end)), mode)


def articles(terms_a: list[str], terms_b: list[str] | None, start: date, end: date,
             max_records: int = 20) -> list[dict]:
    """Recent articles: dicts with url, title, seendate, domain, language, sourcecountry."""
    j = _json(get_raw(build_url("artlist", terms_a, terms_b, start, end, max_records)))
    return list(j.get("articles") or [])


if __name__ == "__main__":
    # make fetch-sample: one volume query, cached to disk.
    end = date.today()
    s = timeline(["iron ore"], ["tariff", "midterms"], end - timedelta(days=60), end)
    print(s.tail(10).to_string() if len(s) else "no data (offline, blocked, or empty result)")
    print(f"cache: {cache_dir()}  misses: {len(MISSES)}")
