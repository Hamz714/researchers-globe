"""Decision-model adapter (plan: The decision model (Jev) and its fallback).

Owner: Person B. Everything that calls a model goes through these functions:

    parse(text, schema) -> dict                                   # step 1 (free-form criteria)
    select(question, options, context) -> [(option_id, p), ...]   # step 2 (channels, touched nodes)
    judge(question, options, context) -> {"choice", "confidence"} # step 5

WHAT JEV IS (checked 8 Nov 2026). Jev is TypeSafe's *decision* model: it takes a
`state` and typed `questions` (noul = yes/no probability, choice = one of N options
with probabilities, score = ordinal rubric) and returns typed answers. It generates
no free text, so it is used for select() and judge(), and parse() goes to the
generative fallback. Access (all need a paid key; TypeSafe direct is waitlisted):
  * OpenRouter: POST https://openrouter.ai/api/alpha/decisions, model "typesafe/jev-1.14"
  * AI/ML API:  POST https://api.aimlapi.com/v1/decisions,      model "typesafe/jev"
  Body: {"model", "state", "questions": {key: {"type": "choice", "instructions", "criteria": {opt: desc}}}}
  Answer: answers[key] = {"type": "choice", "choice", "confidence", "probabilities"} | {"type": "noul", "noul"}
Jev has no temperature setting and repeat calls can differ by ~0.02, so every
validated answer is cached on disk (cache/llm/) and replayed: the demo is deterministic.

Backends are tried in order: Jev -> fallback LLM (any OpenAI-compatible
/chat/completions, temperature 0) -> the caller's code fallback (keywords / presets).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

import jsonschema
import requests

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "30"))
SELECT_THRESHOLD = 0.5


def cache_dir() -> Path:
    return Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "llm"


class DecisionModelUnavailable(RuntimeError):
    pass


class BackendNotConfigured(RuntimeError):
    pass


class NotSupported(RuntimeError):
    pass


# ------------------------------------------------------------------ backends
class JevBackend:
    """Jev via a Decisions endpoint (OpenRouter by default). Supports select() and judge()."""

    name = "jev"

    def __init__(self) -> None:
        # `or`: a blank line in .env (JEV_MODEL=) must fall back to the default, not to ""
        self.base_url = os.environ.get("JEV_BASE_URL") or "https://openrouter.ai/api/alpha/decisions"
        self.api_key = os.environ.get("JEV_API_KEY") or ""
        self.model = os.environ.get("JEV_MODEL") or "typesafe/jev-1.14"

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def decide(self, state: Any, questions: dict) -> dict:
        r = requests.post(self.base_url, timeout=TIMEOUT_S,
                          headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                          json={"model": self.model, "state": state, "questions": questions})
        r.raise_for_status()
        answers = r.json()["answers"]
        for k, a in answers.items():
            if a.get("type") == "refusal":
                raise ValueError(f"jev refused question {k}")
        return answers

    def judge_native(self, question: str, options: list[str], context: dict) -> dict:
        a = self.decide(context, {"q": {"type": "choice", "instructions": question,
                                        "criteria": {o: None for o in options}}})["q"]
        return {"choice": a["choice"], "confidence": float(a.get("confidence", max(a.get("probabilities", {0: 0.5}).values())))}

    def select_native(self, question: str, options: dict[str, str], context: dict) -> list[tuple[str, float]]:
        qs = {f"o{i}": {"type": "noul", "instructions": f"{question}\nOption: {oid}: {desc}"}
              for i, (oid, desc) in enumerate(options.items())}
        ans = self.decide(context, qs)
        return [(oid, float(ans[f"o{i}"]["noul"])) for i, oid in enumerate(options)]

    def complete(self, system: str, user: str) -> str:
        raise NotSupported("Jev does not generate text")


class OpenAICompatibleBackend:
    """POST {base_url}/chat/completions with a Bearer key at temperature 0."""

    def __init__(self, name: str, base_url: str | None, api_key: str | None, model: str | None):
        self.name, self.base_url, self.api_key, self.model = name, base_url, api_key, model

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def complete(self, system: str, user: str) -> str:
        r = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions", timeout=TIMEOUT_S,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "temperature": 0,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def default_backends() -> list:
    return [
        JevBackend(),
        OpenAICompatibleBackend(
            "fallback",
            os.environ.get("FALLBACK_LLM_BASE_URL") or "https://openrouter.ai/api/v1",
            os.environ.get("FALLBACK_LLM_API_KEY"),
            os.environ.get("FALLBACK_LLM_MODEL") or "anthropic/claude-haiku-5.5",
        ),
    ]


# ------------------------------------------------------------------ cache + helpers
def _key(backend: str, model: str, kind: str, payload: dict) -> str:
    blob = json.dumps({"b": backend, "m": model, "k": kind, "p": payload}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _cache_get(key: str) -> Any | None:
    p = cache_dir() / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _cache_put(key: str, value: Any) -> None:
    cache_dir().mkdir(parents=True, exist_ok=True)
    (cache_dir() / f"{key}.json").write_text(json.dumps(value))


def extract_json(text: str) -> Any:
    """Parse the first JSON value in a model reply (tolerates ```json fences and prose)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start: end + 1])


def _run(kind: str, payload: dict, attempt: Callable[[Any], Any], validate: Callable[[Any], Any],
         fallback: Callable[[], Any] | None, backends: list | None) -> Any:
    for b in backends if backends is not None else default_backends():
        key = _key(b.name, getattr(b, "model", "") or "", kind, payload)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        if not b.configured():
            continue
        try:
            out = validate(attempt(b))
        except NotSupported:
            continue
        except Exception as e:  # network, HTTP, JSON or validation failure: try the next backend
            log.warning("%s %s failed: %s", b.name, kind, e)
            continue
        _cache_put(key, out)
        log.info("%s answered by %s", kind, b.name)
        return out
    if fallback is not None:
        log.info("%s answered by code fallback", kind)
        return fallback()
    raise DecisionModelUnavailable(f"no backend could answer {kind}")


# ------------------------------------------------------------------ public API
def parse(text: str, schema: dict, fallback: Callable[[], dict] | None = None,
          backends: list | None = None) -> dict:
    """Free text -> JSON object valid against `schema` (a JSON Schema dict). Generative backends only."""
    system = ("You convert a user's question into JSON. Reply with ONE JSON object only, no prose. "
              "It must validate against this JSON Schema:\n" + json.dumps(schema))

    def attempt(b):
        return extract_json(b.complete(system, text))

    def validate(obj: Any) -> dict:
        jsonschema.validate(obj, schema)
        return obj

    return _run("parse", {"text": text, "schema": schema}, attempt, validate, fallback, backends)


def select(question: str, options: dict[str, str], context: dict, max_n: int = 4,
           threshold: float = SELECT_THRESHOLD, fallback: Callable[[], list] | None = None,
           backends: list | None = None) -> list[tuple[str, float]]:
    """Pick the options (id -> description) that answer `question`; returns [(id, p)] with p >= threshold."""
    system = ("For each option, give the probability (0 to 1) that it answers the question, using only the "
              "context. Reply with ONE JSON object mapping every option id to a number, no prose.")
    user = (f"Question: {question}\nContext: {json.dumps(context, default=str)}\nOptions:\n"
            + "\n".join(f"- {k}: {v}" for k, v in options.items()))

    def attempt(b):
        if hasattr(b, "select_native"):
            return b.select_native(question, options, context)
        obj = extract_json(b.complete(system, user))
        return [(k, float(obj[k])) for k in options if k in obj]

    def validate(pairs: Any) -> list:
        out = [[k, float(p)] for k, p in pairs if k in options and 0.0 <= float(p) <= 1.0]
        if not out:
            raise ValueError("no valid option probabilities")
        out.sort(key=lambda kp: -kp[1])
        return [kp for kp in out if kp[1] >= threshold][:max_n]

    payload = {"q": question, "o": options, "c": context, "n": max_n, "t": threshold}
    return [tuple(x) for x in _run("select", payload, attempt, validate, fallback, backends)]


def judge(question: str, options: list[str], context: dict,
          fallback: Callable[[], dict] | None = None, backends: list | None = None) -> dict:
    """Closed question -> {"choice": one of options, "confidence": 0..1}."""
    system = ("Answer the closed question using only the context. Reply with ONE JSON object only: "
              '{"choice": <one of ' + json.dumps(options) + '>, "confidence": <number from 0 to 1>}')
    user = f"Question: {question}\n\nContext:\n{json.dumps(context, indent=1, default=str)}"

    def attempt(b):
        if hasattr(b, "judge_native"):
            return b.judge_native(question, options, context)
        return extract_json(b.complete(system, user))

    def validate(obj: Any) -> dict:
        if not isinstance(obj, dict) or obj.get("choice") not in options:
            raise ValueError(f"choice not in {options}: {obj!r}")
        conf = float(obj.get("confidence"))
        if not 0.0 <= conf <= 1.0:
            raise ValueError(f"confidence out of range: {conf}")
        return {"choice": obj["choice"], "confidence": conf}

    return _run("judge", {"q": question, "o": options, "c": context}, attempt, validate, fallback, backends)
