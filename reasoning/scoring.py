"""Step 6: score and tier (plan: Scoring, the decision model and evaluation).

Owner: Person B.

    evidence = sigmoid(a*z_news+ + b*z_attention+ + c*(-dtone)+ + d*ln(1 + n_relevant) - k)
    score    = exposure * (0.3 + 0.7 * evidence)

The weights below are HAND-SET tonight (plan). Record every change in the README.
If we tune them, tune on two backtest events and report on the others.
Call it a score or ranking everywhere, never a probability.
"""
from __future__ import annotations

import math

WEIGHTS = {"a": 0.5, "b": 0.3, "c": 0.4, "d": 1.0, "k": 3.0}  # k=3 keeps zero-evidence assets below the medium tier
RELEVANT_CAP = 10           # plan: relevant article count capped at 10
HIGH_SCORE, HIGH_MIN_ARTICLES = 0.6, 2
MEDIUM_SCORE = 0.35


def _pos(x: float) -> float:
    return max(0.0, x)


def evidence(news_z: float, attention_z: float, tone_shift: float, n_relevant: int,
             w: dict[str, float] = WEIGHTS) -> float:
    n = min(max(n_relevant, 0), RELEVANT_CAP)
    x = (w["a"] * _pos(news_z) + w["b"] * _pos(attention_z) + w["c"] * _pos(-tone_shift)
         + w["d"] * math.log1p(n) - w["k"])
    return 1.0 / (1.0 + math.exp(-x))


def score(exposure: float, ev: float) -> float:
    return exposure * (0.3 + 0.7 * ev)


def tier(score_: float, n_relevant: int, exposure: float) -> str:
    """'high' | 'medium' | 'low' | 'dark'. Dark assets are counted, not returned (contract)."""
    if exposure <= 0:
        return "dark"
    if score_ >= HIGH_SCORE and n_relevant >= HIGH_MIN_ARTICLES:
        return "high"
    if score_ >= MEDIUM_SCORE:
        return "medium"
    return "low"
