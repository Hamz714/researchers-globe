"""Signals we compute ourselves (plan: Signals we compute ourselves).

Owner: Person A. Publishes `signals(asset_id, query)`; see `asset_signals()` below.

Every signal compares "now" with the asset's own normal, on the CALENDAR:
  * recent window   = (end - recent_days, end]
  * baseline window = (end - recent_days - baseline_days, end - recent_days]
Missing days (NaN) are dropped from each window: GDELT DOC outage days are NaN (see
fetch_gdelt.GDELT_DOC_GAPS), and so are days a source has not published yet. A window
with fewer than MIN_RECENT_DAYS valid recent days (or fewer than 2 baseline days)
gives 0 and the signal is reported as "insufficient data", rather than measuring an
outage as a drop in coverage.

All functions take a pandas Series indexed by date (daily) and return floats, so they
are unit-testable on synthetic series (tests/test_signals.py). `end` defaults to the
series' last day.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

MAD_SCALE = 1.4826     # makes MAD comparable to a standard deviation (plan)
MAD_PAD = 1.0          # "+1 to the MAD to stop near-silent series exploding" (plan)
TONE_MIN_STD = 0.25    # HAND-SET floor for the tone-shift denominator (plan gives none); tone units
PLACEBO_THRESHOLD = 2.0  # HAND-SET: control z at or above this means "general news surge"
MIN_RECENT_DAYS = 2    # HAND-SET: fewer valid recent days than this = insufficient data
MIN_BASELINE_DAYS = 2


def _end(x: pd.Series, end) -> pd.Timestamp | None:
    if end is not None:
        return pd.Timestamp(end).normalize()
    return x.index.max().normalize() if len(x) else None


def split_windows(x: pd.Series, baseline_days: int = 60, recent_days: int = 7,
                  end=None) -> tuple[pd.Series, pd.Series]:
    """(baseline, recent) on the calendar, NaN days dropped from both."""
    x = x.sort_index()
    e = _end(x, end)
    if e is None:
        return x.iloc[:0], x.iloc[:0]
    cut = e - pd.Timedelta(days=recent_days)
    start = cut - pd.Timedelta(days=baseline_days)
    recent = x[(x.index > cut) & (x.index <= e)].dropna()
    baseline = x[(x.index > start) & (x.index <= cut)].dropna()
    return baseline, recent


def _enough(baseline: pd.Series, recent: pd.Series) -> bool:
    return len(baseline) >= MIN_BASELINE_DAYS and len(recent) >= MIN_RECENT_DAYS


def robust_z(x: pd.Series, baseline_days: int = 60, recent_days: int = 7, end=None) -> float:
    """z = (mean(recent) - median(baseline)) / (1.4826 * MAD(baseline) + 1); 0 if insufficient data."""
    baseline, recent = split_windows(x, baseline_days, recent_days, end)
    if not _enough(baseline, recent):
        return 0.0
    m = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - m)))
    return (float(recent.mean()) - m) / (MAD_SCALE * mad + MAD_PAD)


def tone_shift(tone: pd.Series, baseline_days: int = 60, recent_days: int = 7, end=None) -> float:
    """(mean recent tone - mean baseline tone) / baseline std. Negative = coverage turned more negative."""
    baseline, recent = split_windows(tone, baseline_days, recent_days, end)
    if not _enough(baseline, recent):
        return 0.0
    sd = max(float(baseline.std(ddof=1)), TONE_MIN_STD)
    return (float(recent.mean()) - float(baseline.mean())) / sd


def attention_burst(views: pd.Series, baseline_days: int = 60, recent_days: int = 7, end=None) -> float:
    """Robust z of pageviews after removing the weekday pattern (plan: 'compared with the same weekday').

    Each day's value has its weekday's baseline median subtracted; the robust z is
    then computed on those residuals, so a normal Monday peak is not a burst.
    """
    x = views.sort_index()
    baseline, recent = split_windows(x, baseline_days, recent_days, end)
    if not _enough(baseline, recent):
        return 0.0
    weekday_median = baseline.groupby(baseline.index.day_of_week).median()
    expected = pd.Series(x.index.day_of_week, index=x.index).map(weekday_median).fillna(float(baseline.median()))
    return robust_z(x - expected, baseline_days, recent_days, end)


def sufficient(x: pd.Series | None, baseline_days: int = 60, recent_days: int = 7, end=None) -> bool:
    """True if the series has enough valid days in both windows to compute a z."""
    if x is None or not len(x):
        return False
    return _enough(*split_windows(x, baseline_days, recent_days, end))


def placebo_adjust(z_target: float, z_control: float, threshold: float = PLACEBO_THRESHOLD) -> tuple[float, bool]:
    """Discount a spike that a control term shares (plan: Placebo check).

    Returns (adjusted_z, control_spiked). If the control term (e.g. a different
    commodity, same window) spikes too, subtract its positive z from the target's.
    """
    spiked = z_control >= threshold
    if not spiked:
        return z_target, False
    return max(0.0, z_target - max(0.0, z_control)), True


@dataclass
class AssetSignals:
    news_z: float = 0.0
    tone_shift: float = 0.0
    attention_z: float = 0.0
    placebo_flag: bool = False
    gap_days_dropped: int = 0  # GDELT outage days inside the query window (excluded, not zero)
    insufficient: list[str] = field(default_factory=list)  # signals with too few valid days

    def to_dict(self) -> dict:
        return asdict(self)


def asset_signals(
    volume: pd.Series,
    tone: pd.Series | None = None,
    views: pd.Series | None = None,
    control_volume: pd.Series | None = None,
    baseline_days: int = 60,
    recent_days: int = 7,
    end=None,
    gap_days_dropped: int = 0,
) -> AssetSignals:
    """All signals for one asset and one query, from already-fetched series."""
    w = (baseline_days, recent_days, end)
    insufficient = []
    news = 0.0
    if len(volume):
        if sufficient(volume, *w):
            news = robust_z(volume, *w)
        else:
            insufficient.append("news")
    flag = False
    if control_volume is not None and len(control_volume) and sufficient(control_volume, *w):
        news, flag = placebo_adjust(news, robust_z(control_volume, *w))
    tone_z = 0.0
    if tone is not None and len(tone):
        if sufficient(tone, *w):
            tone_z = tone_shift(tone, *w)
        else:
            insufficient.append("tone")
    attention = 0.0
    if views is not None and len(views):
        if sufficient(views, *w):
            attention = attention_burst(views, *w)
        else:
            insufficient.append("attention")
    return AssetSignals(news_z=news, tone_shift=tone_z, attention_z=attention, placebo_flag=flag,
                        gap_days_dropped=gap_days_dropped, insufficient=insufficient)


def signals(asset_id: str, query: dict) -> AssetSignals:
    """Published interface for Person B: fetch (cached) series for one asset and compute its signals.

    `query` carries: asset_terms, channel_terms (incl. event terms), control_terms,
    wiki_titles, baseline_days, recent_days, end (date).
    """
    from datetime import timedelta

    from pipeline import fetch_gdelt, fetch_wiki

    end = query["end"]
    bd, rd = query.get("baseline_days", 60), query.get("recent_days", 7)
    start = end - timedelta(days=bd + rd)
    vol = fetch_gdelt.timeline(query["asset_terms"], query["channel_terms"], start, end, "timelinevolraw")
    tone = fetch_gdelt.timeline(query["asset_terms"], query["channel_terms"], start, end, "timelinetone")
    if len(tone) and len(vol):  # GDELT reports tone 0 on days with no articles: that is no data, not neutral
        tone = tone.where(vol.reindex(tone.index).fillna(0) > 0)
    control = None
    if query.get("control_terms"):
        control = fetch_gdelt.timeline(query["asset_terms"], query["control_terms"], start, end, "timelinevolraw")
    views = None
    for title in query.get("wiki_titles", []):
        v = fetch_wiki.pageviews(title, start, end)
        views = v if views is None else views.add(v, fill_value=0)
    return asset_signals(vol, tone, views, control, bd, rd, end=end,
                         gap_days_dropped=fetch_gdelt.gap_days_in(start, end))
