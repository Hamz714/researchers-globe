import pytest

from reasoning import decision_model as dm
from reasoning.judge import keyword_judge
from reasoning.parser import choose_channels, parse_question


class Fake:
    def __init__(self, name, reply, ok=True):
        self.name, self.model, self.reply, self.ok, self.calls = name, "m", reply, ok, 0

    def configured(self):
        return True

    def complete(self, system, user):
        self.calls += 1
        if not self.ok:
            raise RuntimeError("down")
        return self.reply


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))


def test_judge_validates_and_caches():
    b = Fake("x", '```json\n{"choice": "relevant", "confidence": 0.8}\n```')
    out = dm.judge("q?", ["relevant", "not_relevant"], {"a": 1}, backends=[b])
    assert out == {"choice": "relevant", "confidence": 0.8}
    dm.judge("q?", ["relevant", "not_relevant"], {"a": 1}, backends=[b])
    assert b.calls == 1  # second call served from cache


def test_falls_through_bad_backend_to_next():
    bad = Fake("bad", '{"choice": "maybe", "confidence": 2}')
    down = Fake("down", "", ok=False)
    good = Fake("good", '{"choice": "not_relevant", "confidence": 0.6}')
    out = dm.judge("q?", ["relevant", "not_relevant"], {}, backends=[bad, down, good])
    assert out["choice"] == "not_relevant"


def test_code_fallback_when_all_fail():
    out = dm.judge("q?", ["relevant"], {}, fallback=lambda: {"choice": "relevant", "confidence": 0.5},
                   backends=[Fake("down", "", ok=False)])
    assert out["choice"] == "relevant"


def test_raises_without_fallback():
    with pytest.raises(dm.DecisionModelUnavailable):
        dm.judge("q?", ["relevant"], {}, backends=[])


def test_parser_falls_back_to_nearest_preset():
    criteria, preset = parse_question("iron ore mines exposed to the US midterms", backends=[])
    assert preset["id"] == "hero_midterms" and criteria["event"]["date"] == "2026-11-03"


def test_channel_choice_rejects_ids_outside_library():
    b = Fake("x", '{"aliens": 0.99, "tariffs": 0.9, "weather": 0.1}')
    crit, _ = parse_question("iron ore mines exposed to the US midterms", backends=[])
    out = choose_channels("anything", crit, None, backends=[b])
    assert [c["id"] for c in out] == ["tariffs"]


def test_channel_choice_falls_back_to_preset():
    crit, _ = parse_question("iron ore mines exposed to the US midterms", backends=[])
    out = choose_channels("iron ore mines exposed to the US midterms", crit, None, backends=[Fake("x", "nonsense")])
    assert {c["id"] for c in out} == {"tariffs", "infrastructure", "permits"}


class FakeJev(Fake):
    def judge_native(self, question, options, context):
        self.calls += 1
        return {"choice": "relevant", "confidence": 0.77}

    def select_native(self, question, options, context):
        self.calls += 1
        return [(k, 0.9 if k == "labour" else 0.1) for k in options]


def test_jev_native_paths_are_used():
    j = FakeJev("jev", "")
    assert dm.judge("q?", ["relevant", "not_relevant"], {}, backends=[j]) == {"choice": "relevant", "confidence": 0.77}
    assert dm.select("q?", {"labour": "x", "weather": "y"}, {}, backends=[j]) == [("labour", 0.9)]


def test_jev_cannot_parse_so_parse_falls_through():
    j = FakeJev("jev", "")
    j.complete = lambda s, u: (_ for _ in ()).throw(dm.NotSupported("no text"))
    g = Fake("fallback", '{"a": 1}')
    assert dm.parse("t", {"type": "object"}, backends=[j, g]) == {"a": 1}


def test_keyword_judge_needs_asset_and_channel_terms():
    art = {"title": "Steel tariff fight hits Hibbing"}
    assert keyword_judge(art, ["Hibbing"], ["tariff"])["choice"] == "relevant"
    assert keyword_judge(art, ["Hibbing"], ["strike"])["choice"] == "not_relevant"


def test_preset_question_skips_the_model():
    from reasoning.parser import load_presets

    hero = load_presets()[0]
    b = Fake("x", '{"never": "called"}')
    crit, preset = parse_question("  " + hero["query"].upper() + " ", backends=[b])
    assert preset["id"] == hero["id"] and b.calls == 0


def test_blank_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("JEV_MODEL", "")
    monkeypatch.setenv("JEV_BASE_URL", "")
    j = dm.JevBackend()
    assert j.model == "typesafe/jev-1.14" and j.base_url.endswith("/decisions")


# ---------------------------------------------------------------- step 6: one-sentence summary
from reasoning.decision_model import check_summary, summarise  # noqa: E402

TITLES = ["Cleveland-Cliffs urges new Congress to keep steel tariffs", "Iron Range braces for tariff fight"]


def test_check_summary_accepts_grounded_sentence():
    s = "Hibbing is exposed because its owner urges Congress to keep steel tariffs [1] and the Iron Range braces for a fight [2]."
    assert check_summary(s, TITLES) == s


@pytest.mark.parametrize("bad", [
    "Hibbing is exposed to tariffs.",                                        # cites nothing
    "Hibbing is exposed [3].",                                               # article not given
    "Hibbing is exposed [1]. Also the Iron Range [2].",                      # two sentences
    "Hibbing is exposed, see reuters.com [1].",                              # names a source
    'Hibbing is exposed as "Steel giant files for bankruptcy protection" says [1].',  # quotes a title not given
    " ".join(["word"] * 36) + " [1].",                                       # too long
])
def test_check_summary_rejects(bad):
    with pytest.raises(ValueError):
        check_summary(bad, TITLES)


class FakeWriter:
    model = "m"

    def __init__(self, reply, name="fake-writer"):
        self.reply, self.name = reply, name  # answers are cached per backend name

    def configured(self):
        return True

    def complete(self, system, user):
        return self.reply


def test_summarise_uses_generative_backend_and_drops_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBE_CACHE_DIR", str(tmp_path))
    args = ({"name": "Hibbing"}, {"id": "tariffs"}, {"name": "midterms"}, [{"title": t} for t in TITLES])
    good = "Hibbing is exposed through tariffs as its owner lobbies Congress [1]."
    assert summarise(*args, backends=[FakeWriter(good)]) == good
    assert summarise(*args, backends=[FakeWriter("Hibbing is exposed [7].", name="liar")]) is None
    assert summarise(args[0], args[1], args[2], [], backends=[FakeWriter(good)]) is None  # nothing to cite
