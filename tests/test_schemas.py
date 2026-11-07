import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.schemas import QueryResult

MOCK = Path(__file__).resolve().parents[1] / "data" / "mock" / "mock_result.json"


def mock():
    return json.loads(MOCK.read_text())


def test_mock_result_matches_contract():
    QueryResult.model_validate(mock())


def test_contract_field_names_exactly():
    r = mock()
    assert set(r) == {"criteria", "channels", "results", "dark_count"}
    assert set(r["results"][0]) == {"id", "name", "lat", "lon", "tier", "score", "components",
                                    "paths", "reason", "evidence"}
    assert set(r["results"][0]["components"]) == {"exposure", "news_z", "tone_shift", "attention_z",
                                                  "relevant_articles"}
    assert set(r["results"][0]["evidence"][0]) == {"title", "url", "date", "judge", "confidence"}


def test_unknown_field_rejected():
    r = mock()
    r["results"][0]["probability"] = 0.9
    with pytest.raises(ValidationError):
        QueryResult.model_validate(r)


def test_bad_tier_rejected():
    r = mock()
    r["results"][0]["tier"] = "critical"
    with pytest.raises(ValidationError):
        QueryResult.model_validate(r)


def test_score_must_be_in_unit_interval():
    r = mock()
    r["results"][0]["score"] = 1.5
    with pytest.raises(ValidationError):
        QueryResult.model_validate(r)
