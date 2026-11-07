import pytest

from eval.metrics import cohens_kappa, precision_at_k, precision_recall


def test_precision_recall():
    p, r = precision_recall([True, True, False, False], [True, False, True, False])
    assert p == pytest.approx(0.5) and r == pytest.approx(0.5)
    assert precision_recall([False], [False]) == (None, None)


def test_precision_at_k():
    assert precision_at_k(["a", "b", "c"], {"a", "c"}, k=2) == pytest.approx(0.5)
    assert precision_at_k([], {"a"}) is None


def test_kappa():
    assert cohens_kappa(["r", "n", "r"], ["r", "n", "r"]) == pytest.approx(1.0)
    assert cohens_kappa([], []) is None
