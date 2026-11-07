"""Steps 1 and 2: parse the question, then choose impact channels (plan: Query pipeline).

Owner: Person B. Both steps go through decision_model.parse() and are validated in
code. If the model can't answer, the nearest preset is used (plan: "fall back to
the nearest preset").
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.schemas import Channel, Criteria
from reasoning import decision_model

ROOT = Path(__file__).resolve().parents[1]
CHANNELS_PATH = Path(__file__).resolve().parent / "channels.json"
PRESETS_PATH = ROOT / "data" / "presets.json"


def load_channels() -> dict[str, dict]:
    return {c["id"]: c for c in json.loads(CHANNELS_PATH.read_text())["channels"]}


def load_presets() -> list[dict]:
    return json.loads(PRESETS_PATH.read_text())["presets"]


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower())) - {"which", "are", "the", "to", "of", "a", "an", "is", "by", "and"}


def nearest_preset(text: str) -> dict:
    """Preset whose query shares the most words with `text` (Jaccard similarity)."""
    t = _tokens(text)

    def sim(p: dict) -> float:
        q = _tokens(p["query"])
        return len(t & q) / max(1, len(t | q))

    return max(load_presets(), key=sim)


CRITERIA_SCHEMA = Criteria.model_json_schema()


def parse_question(text: str, backends: list | None = None) -> tuple[dict, dict | None]:
    """Step 1. Returns (criteria, preset_used_or_None)."""
    used: dict = {}

    def fallback() -> dict:
        used["preset"] = nearest_preset(text)
        return used["preset"]["criteria"]

    raw = decision_model.parse(text, CRITERIA_SCHEMA, fallback=fallback, backends=backends)
    criteria = Criteria.model_validate(raw).model_dump()  # code check, even for fallback output
    return criteria, used.get("preset")


def channel_schema(library: dict[str, dict]) -> dict:
    return {
        "type": "object",
        "required": ["channels"],
        "additionalProperties": False,
        "properties": {
            "channels": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "required": ["id", "reason"],
                    "additionalProperties": False,
                    "properties": {"id": {"enum": sorted(library)}, "reason": {"type": "string", "maxLength": 200}},
                },
            }
        },
    }


def choose_channels(text: str, criteria: dict, preset: dict | None = None,
                    backends: list | None = None) -> list[dict]:
    """Step 2. The model picks from the fixed library only; anything else is rejected by code."""
    library = load_channels()
    if preset is not None:  # parse already fell back to a preset: stay consistent with it
        return preset["channels"]
    prompt = (
        f"Question: {text}\nParsed criteria: {json.dumps(criteria)}\n"
        "Pick the impact channels through which this event could affect these assets. "
        "Use ONLY these channel ids:\n"
        + "\n".join(f"- {c['id']}: {c['description']}" for c in library.values())
        + "\nGive a short reason for each. Pick none if the event is unrelated."
    )
    raw = decision_model.parse(prompt, channel_schema(library),
                               fallback=lambda: {"channels": nearest_preset(text)["channels"]},
                               backends=backends)
    out, seen = [], set()
    for ch in raw["channels"]:
        if ch["id"] in library and ch["id"] not in seen:  # schema enum already enforces this; belt and braces
            out.append(Channel.model_validate(ch).model_dump())
            seen.add(ch["id"])
    return out
