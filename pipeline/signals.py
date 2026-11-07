"""Signals we compute ourselves (plan: Signals we compute ourselves).

Owner: Person A. Publishes `signals(asset_id, query)`; see `asset_signals()` below.

Every signal compares "now" with the asset's own normal:
  * recent window   = the last `recent_days` days of the series
  * baseline window = up to `baseline_days` days immediately before that
    (plan: "the previous 60 days, excluding the last 7". If you pull exactly 60 days,
    the baseline is 53 days. Pull baseline_days + recent_days to get a full 60.)

All functions take a pandas Series indexed by date (daily) and return floats, so
they are trivially unit-testable on synthetic series (tests/test_signals.py).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

MAD_SCALE = 1.4826     # makes MAD comparable to a standard deviation (plan)
MAD_PAD = 1.0          # "+1 to the MAD to stop near-silent series exploding" (plan)
TONE_MIN_STD = 0.25    # HAND-SET floor for the tone-shift denominator (plan gives none); tone units
PLACEBO_THRESHOLD = 2.0  # HAND-SET: control z at or above this means "general news surge"


def split_windows(x: pd.Series, baseline_days: int = 60, recent_days: int = 7) -> tuple[pd.Series, pd.Series]:
    x = x.sort_index().dropna()
    recent = x.iloc[-recent_days:]
    baseline = x.iloc[:-recent_days].iloc[-baseline_days:]
    return baseline, recent


def robust_z(x: pd.Series, baseline_days: int = 60, recent_days: int = 7) -> float:
    """z = (mean(recent) - median(baseline)) / (1.4826 * MAD(baseline) + 1)."""
    baseline, recent = split_windows(x, baseline_days, recent_days)
    if len(baseline) < 2 or len(recent) == 0:
        return 0.0
    m = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - m)))
    return (float(recent.mean()) - m) / (MAD_SCALE * mad + MAD_PAD)


def tone_shift(tone: pd.Series, baseline_days: int = 60, recent_days: int = 7) -> float:
    """(mean recent tone - mean baseline tone) / baseline std. Negative = coverage turned more negative."""
    baseline, recent = split_windows(tone, baseline_days, recent_days)
    if len(baseline) < 2 or len(recent) == 0:
        return 0.0
    sd = max(float(baseline.std(ddof=1)), TONE_MIN_STD)
    return (float(recent.mean()) - float(baseline.mean())) / sd


def attention_burst(views: pd.Series, baseline_days: int = 60, recent_days: int = 7) -> float:
    """Robust z of pageviews after removing the weekday pattern (plan: 'compared with the same weekday').

    Each day's value has its weekday's baseline median subtracted; the robust z is
    then computed on those residuals, so a normal Monday peak is not a burst.
    """
    x = views.sort_index().dropna()
    if len(x) <= recent_days + 1:
        return 0.0
    baseline, _ = split_windows(x, baseline_days, recent_days)
    weekday_median = baseline.groupby(baseline.index.day_of_week).median()
    expected = pd.Series(x.index.day_of_week, index=x.index).map(weekday_median).fillna(float(baseline.median()))
    return robust_z(x - expected, baseline_days, recent_days)


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

    def to_dict(self) -> dict:
        return asdict(self)


def asset_signals(
    volume: pd.Series,
    tone: pd.Series | None = None,
    views: pd.Series | None = None,
    control_volume: pd.Series | None = None,
    baseline_days: int = 60,
    recent_days: int = 7,
) -> AssetSignals:
    """All signals for one asset and one query, from already-fetched series."""
    news = robust_z(volume, baseline_days, recent_days) if len(volume) else 0.0
    flag = False
    if control_volume is not None and len(control_volume):
        news, flag = placebo_adjust(news, robust_z(control_volume, baseline_days, recent_days))
    return AssetSignals(
        news_z=news,
        tone_shift=tone_shift(tone, baseline_days, recent_days) if tone is not None and len(tone) else 0.0,
        attention_z=attention_burst(views, baseline_days, recent_days) if views is not None and len(views) else 0.0,
        placebo_flag=flag,
    )


def signals(asset_id: str, query: dict) -> AssetSignals:
    """Published interface for Person B: fetch (cached) series for one asset and compute its signals.

    `query` carries: asset_terms, channel_terms (incl. event terms), control_terms,
    wiki_titles, baseline_days, recent_days, end (date).
    """
    from datetime import timedelta

    from pipeline import fetch_gdelt, fetch_wiki

    end = query["end"]
    days = query.get("baseline_days", 60) + query.get("recent_days", 7)
    start = end - timedelta(days=days)
    vol = fetch_gdelt.timeline(query["asset_terms"], query["channel_terms"], start, end, "timelinevolraw")
    tone = fetch_gdelt.timeline(query["asset_terms"], query["channel_terms"], start, end, "timelinetone")
    control = None
    if query.get("control_terms"):
        control = fetch_gdelt.timeline(query["asset_terms"], query["control_terms"], start, end, "timelinevolraw")
    views = None
    for title in query.get("wiki_titles", []):
        v = fetch_wiki.pageviews(title, start, end)
        views = v if views is None else views.add(v, fill_value=0)
    return asset_signals(vol, tone, views, control, query.get("baseline_days", 60), query.get("recent_days", 7))
