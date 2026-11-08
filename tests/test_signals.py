import numpy as np
import pandas as pd
import pytest

from pipeline import signals as sg


def series(values, start="2026-09-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D"), dtype=float)


def test_flat_series_has_zero_z():
    assert sg.robust_z(series([5.0] * 67)) == pytest.approx(0.0)


def test_spike_is_detected_with_mad_plus_one():
    s = series([5.0] * 60 + [15.0] * 7)
    # MAD of a constant baseline is 0, so the denominator is exactly 1 (the +1 pad)
    assert sg.robust_z(s) == pytest.approx(10.0)


def test_near_silent_series_does_not_explode():
    s = series([0.0] * 60 + [1.0] * 7)
    assert sg.robust_z(s) == pytest.approx(1.0)


def test_robust_to_one_outlier_in_baseline():
    rng = np.random.default_rng(0)
    base = rng.normal(20, 2, 60)
    with_outlier = base.copy()
    with_outlier[10] = 500
    recent = [20.0] * 7
    assert abs(sg.robust_z(series(list(base) + recent)) - sg.robust_z(series(list(with_outlier) + recent))) < 0.3


def test_baseline_excludes_recent_window():
    b, r = sg.split_windows(series(list(range(67))), baseline_days=60, recent_days=7)
    assert len(r) == 7 and len(b) == 60 and b.index.max() < r.index.min()


def test_tone_shift_negative_when_coverage_turns_negative():
    rng = np.random.default_rng(1)
    tone = series(list(rng.normal(0, 1, 60)) + [-3.0] * 7)
    assert sg.tone_shift(tone) < -2


def test_tone_shift_uses_std_floor():
    tone = series([1.0] * 60 + [0.5] * 7)
    assert sg.tone_shift(tone) == pytest.approx(-0.5 / sg.TONE_MIN_STD)


def test_attention_ignores_weekly_pattern_but_catches_burst():
    weekly = [100.0 if d % 7 == 0 else 10.0 for d in range(67)]
    assert abs(sg.attention_burst(series(weekly))) < 0.5
    burst = weekly[:60] + [v + 200 for v in weekly[60:]]
    assert sg.attention_burst(series(burst)) > 5


def test_placebo_discounts_shared_spike():
    adjusted, flag = sg.placebo_adjust(6.0, 4.0)
    assert flag and adjusted == pytest.approx(2.0)
    adjusted, flag = sg.placebo_adjust(6.0, 0.5)
    assert not flag and adjusted == 6.0


def test_asset_signals_applies_placebo():
    target = series([5.0] * 60 + [15.0] * 7)
    control = series([5.0] * 60 + [12.0] * 7)
    out = sg.asset_signals(target, control_volume=control)
    assert out.placebo_flag and out.news_z == pytest.approx(3.0)


def test_empty_series_give_zero():
    out = sg.asset_signals(pd.Series(dtype=float))
    assert out.news_z == 0 and out.tone_shift == 0 and out.attention_z == 0


# ---------------------------------------------------------------- GDELT outage gaps (NaN days)
def with_gap(values, gap):
    """values with positions in `gap` (a range) set to NaN, as parse_timeline does for outage days."""
    v = list(values)
    for i in gap:
        v[i] = np.nan
    return series(v)


def test_gap_does_not_create_negative_z():
    # steady 20/day; GDELT outage covers 5 of the 7 recent days: must not read as a drop
    s = with_gap([20.0] * 67, range(60, 65))
    assert sg.robust_z(s) == pytest.approx(0.0)
    zeros = series([20.0] * 60 + [0.0] * 5 + [20.0] * 2)  # the old behaviour: outage counted as zeros
    assert sg.robust_z(zeros) < -5


def test_post_gap_spike_still_detected():
    s = with_gap([5.0] * 60 + [0.0] * 5 + [15.0] * 2, range(60, 65))
    assert sg.robust_z(s) == pytest.approx(10.0)


def test_gap_in_baseline_is_dropped():
    s = with_gap([5.0] * 60 + [15.0] * 7, range(20, 32))
    b, r = sg.split_windows(s)
    assert len(b) == 48 and len(r) == 7 and sg.robust_z(s) == pytest.approx(10.0)


def test_insufficient_recent_days_gives_zero_and_flag():
    s = with_gap([5.0] * 60 + [50.0] * 7, range(60, 66))  # only 1 valid recent day
    assert sg.robust_z(s) == 0.0 and not sg.sufficient(s)
    out = sg.asset_signals(s, tone=s, views=series([10.0] * 67))
    assert out.news_z == 0.0 and out.insufficient == ["news", "tone"] and out.attention_z == pytest.approx(0.0)


def test_windows_are_calendar_based():
    # series stops 2 days before `end` (unpublished days): recent = (end-7, end] has only 5 valid days
    s = series([5.0] * 60 + [15.0] * 5)  # 2026-09-01 .. 2026-11-04
    end = s.index.max() + pd.Timedelta(days=2)
    b, r = sg.split_windows(s, end=end)
    assert len(r) == 5 and r.index.max() == s.index.max()
    assert len(b) == 60 and b.index.max() < r.index.min()


def test_parse_timeline_marks_doc_gaps_missing(monkeypatch):
    import json

    from pipeline import fetch_gdelt

    monkeypatch.setenv("GDELT_DOC_GAPS", "2026-10-03:2026-10-05")
    data = [{"date": f"202610{d:02d}T000000Z", "value": 0 if 3 <= d <= 5 else 9} for d in range(1, 8)]
    s = fetch_gdelt.parse_timeline(json.dumps({"timeline": [{"data": data}]}), "timelinevolraw")
    assert s.isna().sum() == 3 and s.loc["2026-10-02"] == 9 and s.loc["2026-10-06"] == 9
    assert fetch_gdelt.gap_days_in(pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-10-31").date()) == 3
    monkeypatch.setenv("GDELT_DOC_GAPS", "")
    assert fetch_gdelt.parse_timeline(json.dumps({"timeline": [{"data": data}]}), "timelinevolraw").isna().sum() == 0
