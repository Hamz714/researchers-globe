"""Decision-model adapter (plan: The decision model (Jev) and its fallback).

Owner: Person B. Everything that calls a language model goes through the two
functions below, so the model can be swapped without touching anything else:

    parse(text, schema) -> dict                                   # steps 1 and 2
    judge(question, options, context) -> {"choice", "confidence"} # step 5

Both run at temperature 0, validate the model's output in code, and cache every
validated response on disk (cache/llm/). Backends are tried in order:

    1. Jev            -- TODO: we do not know Jev's API yet (see JevBackend)
    2. Fallback LLM   -- any OpenAI-compatible /chat/completions endpoint
    3. `fallback=`    -- a plain-code function the caller passes in
                         (presets for parse, keyword matching for judge)

If every backend fails and no `fallback` was given, DecisionModelUnavailable is raised.
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
CACHE_DIR = Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "llm"
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "30"))


class DecisionModelUnavailable(RuntimeError):
    pass


class BackendNotConfigured(RuntimeError):
    pass


# ------------------------------------------------------------------ backends
class OpenAICompatibleBackend:
    """POST {base_url}/chat/completions with a Bearer key; reads choices[0].message.content."""

    def __init__(self, name: str, base_url: str | None, api_key: str | None, model: str | None):
        self.name, self.base_url, self.api_key, self.model = name, base_url, api_key, model

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def complete(self, system: str, user: str) -> str:
        if not self.configured():
            raise BackendNotConfigured(f"{self.name}: set base URL, API key and model in .env")
        r = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class JevBackend:
    """TODO(Person B): we don't know what Jev is or how to call it.

    Find out: endpoint URL, auth (header name, key format), request body, how to set
    temperature, response format, rate limits. Then either:
      * if Jev is OpenAI-compatible: set JEV_FORMAT=openai, JEV_BASE_URL, JEV_API_KEY, JEV_MODEL; or
      * implement `complete()` below for its real format.
    Until then this backend reports "not configured" and the chain moves on.
    """

    name = "jev"

    def __init__(self) -> None:
        self._inner = None
        if os.environ.get("JEV_FORMAT", "").lower() == "openai":
            self._inner = OpenAICompatibleBackend(
                "jev", os.environ.get("JEV_BASE_URL"), os.environ.get("JEV_API_KEY"), os.environ.get("JEV_MODEL")
            )

    def configured(self) -> bool:
        return self._inner is not None and self._inner.configured()

    def complete(self, system: str, user: str) -> str:
        if self._inner is None:
            raise BackendNotConfigured("jev: API unknown (see JevBackend docstring)")
        return self._inner.complete(system, user)

    @property
    def model(self) -> str:
        return os.environ.get("JEV_MODEL", "jev")


def default_backends() -> list:
    return [
        JevBackend(),
        OpenAICompatibleBackend(
            "fallback",
            os.environ.get("FALLBACK_LLM_BASE_URL"),
            os.environ.get("FALLBACK_LLM_API_KEY"),
            os.environ.get("FALLBACK_LLM_MODEL"),
        ),
    ]


# ------------------------------------------------------------------ cache
def _cache_get(key: str) -> Any | None:
    p = CACHE_DIR / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _cache_put(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(value))


def _key(backend: str, model: str, kind: str, payload: dict) -> str:
    blob = json.dumps({"b": backend, "m": model, "k": kind, "p": payload}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates ```json fences and prose)."""
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
        return json.loads(text[start : end + 1])


def _run(kind: str, payload: dict, system: str, user: str, validate: Callable[[Any], Any],
         fallback: Callable[[], Any] | None, backends: list | None) -> Any:
    for b in backends if backends is not None else default_backends():
        key = _key(b.name, getattr(b, "model", "") or "", kind, payload)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        if not b.configured():
            continue
        try:
            out = validate(extract_json(b.complete(system, user)))
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
    """Free text -> JSON object valid against `schema` (a JSON Schema dict)."""
    system = (
        "You convert a user's question into JSON. Reply with ONE JSON object only, no prose. "
        "It must validate against this JSON Schema:\n" + json.dumps(schema)
    )

    def validate(obj: Any) -> dict:
        jsonschema.validate(obj, schema)
        return obj

    return _run("parse", {"text": text, "schema": schema}, system, text, validate, fallback, backends)


def judge(question: str, options: list[str], context: dict,
          fallback: Callable[[], dict] | None = None, backends: list | None = None) -> dict:
    """Closed question -> {"choice": one of options, "confidence": 0..1}."""
    system = (
        "Answer the closed question using only the context. Reply with ONE JSON object only: "
        '{"choice": <one of ' + json.dumps(options) + '>, "confidence": <number from 0 to 1>}'
    )
    user = f"Question: {question}\n\nContext:\n{json.dumps(context, indent=1, default=str)}"

    def validate(obj: Any) -> dict:
        if not isinstance(obj, dict) or obj.get("choice") not in options:
            raise ValueError(f"choice not in {options}: {obj!r}")
        conf = float(obj.get("confidence"))
        if not 0.0 <= conf <= 1.0:
            raise ValueError(f"confidence out of range: {conf}")
        return {"choice": obj["choice"], "confidence": conf}

    payload = {"q": question, "o": options, "c": context}
    return _run("judge", payload, system, user, validate, fallback, backends)
