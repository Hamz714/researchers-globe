import math

import pytest

from reasoning import scoring


def test_evidence_is_sigmoid_of_weighted_sum():
    w = scoring.WEIGHTS
    x = w["a"] * 4.0 + w["b"] * 2.0 + w["c"] * 1.0 + w["d"] * math.log1p(3) - w["k"]
    assert scoring.evidence(4.0, 2.0, -1.0, 3) == pytest.approx(1 / (1 + math.exp(-x)))


def test_only_positive_parts_count():
    # negative news/attention z and a positive tone shift add nothing
    assert scoring.evidence(-3, -3, 2.0, 0) == pytest.approx(scoring.evidence(0, 0, 0, 0))


def test_relevant_count_capped_at_10():
    assert scoring.evidence(0, 0, 0, 50) == pytest.approx(scoring.evidence(0, 0, 0, 10))


def test_exposure_alone_gives_modest_score():
    assert scoring.score(1.0, 0.0) == pytest.approx(0.3)
    assert scoring.score(0.5, 1.0) == pytest.approx(0.5)


@pytest.mark.parametrize("score,n,exposure,expected", [
    (0.7, 2, 0.9, "high"),
    (0.7, 1, 0.9, "medium"),   # high needs >= 2 relevant articles
    (0.4, 5, 0.9, "medium"),
    (0.2, 0, 0.5, "low"),
    (0.0, 0, 0.0, "dark"),
])
def test_tiers(score, n, exposure, expected):
    assert scoring.tier(score, n, exposure) == expected
