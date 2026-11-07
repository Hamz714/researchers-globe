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
