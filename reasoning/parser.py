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


def choose_channels(text: str, criteria: dict, preset: dict | None = None,
                    backends: list | None = None) -> list[dict]:
    """Step 2. The model scores each channel in the fixed library (Jev: one yes/no per channel);
    code keeps those with p >= 0.5, at most 4. Nothing outside the library can come back."""
    library = load_channels()
    if preset is not None:  # parse already fell back to a preset: stay consistent with it
        return preset["channels"]
    picked = decision_model.select(
        f"Could the event in this question affect {criteria['asset_type'].replace('_', ' ')}s through this channel?",
        {cid: c["description"] for cid, c in library.items()},
        {"question": text, "event": criteria["event"]},
        max_n=4,
        fallback=lambda: [(c["id"], 1.0) for c in nearest_preset(text)["channels"]],
        backends=backends,
    )
    return [Channel(id=cid, reason=f"{library[cid]['description']} (model p={p:.2f})").model_dump()
            for cid, p in picked if cid in library]


def choose_touches(text: str, criteria: dict, channel_ids: list[str], node_names: dict[str, str],
                   preset: dict | None = None, backends: list | None = None) -> dict[str, list]:
    """Which nodes each chosen channel touches for THIS event (the TOUCHES edges).

    Presets carry hand-reviewed touches. For free text the model picks, per channel, from
    the countries in the graph (Jev: one yes/no each); p becomes the edge weight. If the
    model is unavailable, channels.json `default_touches` are used.
    """
    library = load_channels()
    if preset is not None and preset.get("touches") is not None:
        return preset["touches"]
    countries = {nid: name for nid, name in node_names.items() if nid.startswith("country_")}
    out: dict[str, list] = {}
    for cid in channel_ids:
        picked = decision_model.select(
            f"Through '{library[cid]['name']}', which country would this event act on most directly?",
            countries, {"question": text, "event": criteria["event"]}, max_n=5,
            fallback=lambda cid=cid: [tuple(t) for t in library[cid].get("default_touches", [])],
            backends=backends)
        out[cid] = [[nid, round(float(p), 3)] for nid, p in picked if nid in node_names]
    return out
