from pipeline.controls import candidate_channels, pick
from reasoning.parser import load_channels


def test_candidates_exclude_chosen_and_overlapping():
    lib = load_channels()
    c = candidate_channels(["midterm", "midterms", "Congress"], ["tariffs", "infrastructure", "permits"], lib)
    assert not {"tariffs", "infrastructure", "permits"} & set(c)
    assert {"currency", "weather", "sanctions"} <= set(c)
    # labour preset: 'strike' is both an event term and a labour term
    assert "labour" not in candidate_channels(["Port Hedland", "strike"], [], lib)


def test_pick_prefers_calm_and_unpulled():
    cands = ["currency", "weather", "sanctions"]
    scores = {"currency": (0.4, 3.0), "weather": (5.0, 0.0), "sanctions": (0.2, 0.1)}
    assert pick(scores, cands)[0] == "sanctions"
    assert pick({"currency": (-2.0, -1.0), "weather": (0.0, 0.0)}, ["currency", "weather"])[0] == "currency"


def test_pick_falls_back_without_data():
    assert pick({"weather": None}, ["weather", "currency"])[0] == "currency"  # FALLBACK_ORDER
    assert pick({}, [])[0] is None
