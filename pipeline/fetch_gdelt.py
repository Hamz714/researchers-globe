"""GDELT DOC 2.0 fetcher with disk caching (plan: Step 4, Caching).

Owner: Person A. Uses the `gdeltdoc` client (checked against gdeltdoc 1.12.0).

Every call is cached under cache/gdelt/ keyed by (mode, query, start, end, n), so
the demo runs offline and no query is sent twice. Set GLOBE_OFFLINE=1 to forbid
network calls entirely (a cache miss then returns an empty frame).

Query shape: (asset terms OR ...) AND (channel/event terms OR ...). gdeltdoc's
Filters only takes ONE keyword group, so `_filters()` appends the second group
to Filters.query_params, which is an internal detail of gdeltdoc 1.12.0.

TODO(Person A), unverified facts to check before the demo:
  * GDELT DOC covers a rolling ~3 months (from the plan); requests outside it return nothing.
  * Rate limit: we've seen reports of "one request every 5 seconds"; MIN_INTERVAL_S is set
    to that. Check the error text you actually get and adjust.
  * Timeline resolution for a 60-day span: we resample to daily (sum for counts,
    mean for tone) so it is correct whatever resolution comes back.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "gdelt"
MIN_INTERVAL_S = float(os.environ.get("GDELT_MIN_INTERVAL_S", "5"))
_last_call = 0.0


def _offline() -> bool:
    return os.environ.get("GLOBE_OFFLINE", "0") == "1"


def cache_key(**parts) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _cache_path(key: str, suffix: str) -> Path:
    return CACHE_DIR / f"{key}.{suffix}"


def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _filters(terms_a: list[str], terms_b: list[str] | None, start: date, end: date, num_records: int = 250):
    from gdeltdoc import Filters

    f = Filters(keyword=terms_a if len(terms_a) > 1 else terms_a[0],
                start_date=start.isoformat(), end_date=end.isoformat(), num_records=num_records)
    if terms_b:
        f.query_params.insert(1, Filters._keyword_to_string(terms_b if len(terms_b) > 1 else terms_b[0]))
    return f


def timeline(terms_a: list[str], terms_b: list[str] | None, start: date, end: date,
             mode: str = "timelinevolraw") -> pd.Series:
    """Daily series for (terms_a AND terms_b). mode: 'timelinevolraw' (counts) or 'timelinetone'."""
    if mode not in {"timelinevolraw", "timelinetone"}:
        raise ValueError(mode)
    key = cache_key(kind="timeline", mode=mode, a=sorted(terms_a), b=sorted(terms_b or []), start=start, end=end)
    path = _cache_path(key, "json")
    if path.exists():
        raw = json.loads(path.read_text())
        return pd.Series(raw["values"], index=pd.to_datetime(raw["dates"]), name=mode, dtype=float)
    if _offline():
        return pd.Series(dtype=float, name=mode)

    from gdeltdoc import GdeltDoc

    _throttle()
    df = GdeltDoc().timeline_search(mode, _filters(terms_a, terms_b, start, end))
    if df.empty:
        s = pd.Series(dtype=float, name=mode)
    else:
        value_cols = [c for c in df.columns if c not in ("datetime", "All Articles")]
        s = df.set_index("datetime")[value_cols[0]].astype(float)
        s = s.resample("D").sum() if mode == "timelinevolraw" else s.resample("D").mean()
        s = s.rename(mode)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dates": [d.strftime("%Y-%m-%d") for d in s.index],
                                "values": [None if pd.isna(v) else float(v) for v in s.values]}))
    return s


def articles(terms_a: list[str], terms_b: list[str] | None, start: date, end: date,
             max_records: int = 20) -> list[dict]:
    """Recent articles: dicts with url, title, seendate, domain, language, sourcecountry."""
    key = cache_key(kind="artlist", a=sorted(terms_a), b=sorted(terms_b or []), start=start, end=end, n=max_records)
    path = _cache_path(key, "json")
    if path.exists():
        return json.loads(path.read_text())
    if _offline():
        return []

    from gdeltdoc import GdeltDoc

    _throttle()
    df = GdeltDoc().article_search(_filters(terms_a, terms_b, start, end, num_records=max_records))
    rows = [] if df.empty else df.to_dict(orient="records")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, default=str))
    return rows


if __name__ == "__main__":
    # make fetch-sample: one volume query, cached to disk.
    from datetime import timedelta

    end = date.today()
    s = timeline(["iron ore"], ["tariff", "midterms"], end - timedelta(days=60), end)
    print(s.tail(10).to_string() if len(s) else "no data (offline, blocked, or empty result)")
    print(f"cached under {CACHE_DIR}")
