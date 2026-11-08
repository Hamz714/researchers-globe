"""FastAPI backend (plan: Backend). Owner: Person C.

Run:  uvicorn backend.app:app --reload --port 8000   (or `make backend`)

Mode is chosen by USE_PIPELINE in .env:
  0 (default) -> every endpoint returns mock data in the contract shape, so the
                 frontend works from minute one;
  1           -> POST /query calls reasoning.run_query.run_query (Person B).
Cached preset results in data/preset_results/<preset_id>.json are served first in
both modes (plan: cached mode for every preset, for when venue Wi-Fi fails).

GET /asset/{id}?q=<question> looks the asset up in that question's result (preset file
or a recent /query), then in every preset result, then in the mock result. Real assets
get their stored paths, graph edge types, cached 60+7-day GDELT news series (never a
live fetch) and evidence; only mock assets get the mock series.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.schemas import (AssetDetail, AssetPoint, EvalReport, PathEdge, PathNode, Preset, QueryRequest,
                             QueryResult, SignalSeries)

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
log = logging.getLogger("globe")

MOCK_RESULT = ROOT / "data" / "mock" / "mock_result.json"
PRESETS = ROOT / "data" / "presets.json"
PRESET_RESULTS = ROOT / "data" / "preset_results"
EVAL_RESULTS = ROOT / "eval" / "results.json"
RECENT_MAX = 16  # recent /query results kept in memory so /asset can find their assets

app = FastAPI(title="Researcher's Globe API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_recent: OrderedDict[str, dict] = OrderedDict()


def use_pipeline() -> bool:
    return os.environ.get("USE_PIPELINE", "0") == "1"


def _presets() -> list[dict]:
    return json.loads(PRESETS.read_text())["presets"]


def _mock_result() -> dict:
    return json.loads(MOCK_RESULT.read_text())


def _key(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _preset_for(text: str) -> dict | None:
    t = _key(text)
    return next((p for p in _presets() if _key(p["query"]) == t), None)


def _remember(text: str, result: dict) -> dict:
    _recent[_key(text)] = result
    _recent.move_to_end(_key(text))
    while len(_recent) > RECENT_MAX:
        _recent.popitem(last=False)
    return result


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": "pipeline" if use_pipeline() else "mock"}


@app.post("/query", response_model=QueryResult)
def query(req: QueryRequest) -> dict:
    preset = _preset_for(req.text)
    if preset is not None:
        cached = PRESET_RESULTS / f"{preset['id']}.json"
        if cached.exists():
            return _remember(req.text, json.loads(cached.read_text()))
    if use_pipeline():
        from reasoning.run_query import run_query

        try:
            return _remember(req.text, run_query(req.text))
        except Exception as e:  # never show a stack trace on stage
            log.exception("pipeline failed")
            raise HTTPException(status_code=503, detail=f"pipeline failed: {e}") from e
    if preset is not None and preset["kind"] == "negative_control":
        mock = _mock_result()
        return {"criteria": preset["criteria"], "channels": [], "results": [],
                "dark_count": mock["dark_count"] + len(mock["results"])}
    return _mock_result()


@app.get("/presets", response_model=list[Preset])
def presets() -> list[dict]:
    return [{k: p[k] for k in ("id", "label", "query", "kind")} for p in _presets()]


@lru_cache(maxsize=1)
def _asset_points() -> list[dict]:
    from pipeline.graph import default_graph

    n = default_graph().nodes
    mines = n[n["type"] == "mine"]
    return [{"id": r.id, "name": r.name, "lat": float(r.lat), "lon": float(r.lon),
             "country": r.country if isinstance(r.country, str) else None} for r in mines.itertuples()]


@app.get("/assets", response_model=list[AssetPoint])
def assets() -> list[dict]:
    """All assets (real graph if built, else the 20 mock mines). Not in the plan's contract:
    added so the globe can draw dark assets as grey dots (decision, 8 Nov)."""
    return _asset_points()


# ----------------------------------------------------------------- /asset helpers
def _num(v) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)


@lru_cache(maxsize=1)
def _edge_types() -> dict[tuple[str, str], str]:
    from pipeline.graph import default_graph

    out: dict[tuple[str, str], str] = {}
    for r in default_graph().edges.itertuples():
        out.setdefault((r.src, r.dst), r.type)
    return out


@lru_cache(maxsize=1)
def _channel_library() -> dict[str, dict]:
    from reasoning.parser import load_channels

    return load_channels()


def _find(asset_id: str, q: str | None) -> tuple[dict, dict, bool] | None:
    """(result document, result row, is_mock) for an asset, preferring the question asked."""
    docs: list[tuple[dict, bool]] = []
    if q:
        if _key(q) in _recent:
            docs.append((_recent[_key(q)], False))
        preset = _preset_for(q)
        if preset and (PRESET_RESULTS / f"{preset['id']}.json").exists():
            docs.append((json.loads((PRESET_RESULTS / f"{preset['id']}.json").read_text()), False))
    docs += [(d, False) for d in reversed(_recent.values())]
    docs += [(json.loads(f.read_text()), False) for f in sorted(PRESET_RESULTS.glob("*.json"))]
    docs.append((_mock_result(), True))
    for doc, is_mock in docs:
        row = next((r for r in doc.get("results", []) if r["id"] == asset_id), None)
        if row is not None:
            return doc, row, is_mock
    return None


def _path_nodes(paths: list[list[str]]) -> list[PathNode]:
    from pipeline.graph import default_graph

    g = default_graph()
    library = _channel_library()
    out = []
    for nid in dict.fromkeys(n for p in paths for n in p):
        if nid.startswith("channel_"):
            ch = library.get(nid.removeprefix("channel_"), {})
            out.append(PathNode(id=nid, type="channel", name=ch.get("name", nid.removeprefix("channel_"))))
            continue
        try:
            n = g.node(nid)
            out.append(PathNode(id=nid, type=n["type"], name=n["name"], lat=_num(n["lat"]), lon=_num(n["lon"])))
        except KeyError:
            out.append(PathNode(id=nid, type=nid.split("_")[0], name=nid))
    return out


def _path_edges(paths: list[list[str]]) -> list[PathEdge]:
    types = _edge_types()
    seen, out = set(), []
    for p in paths:
        for a, b in zip(p, p[1:]):
            if (a, b) in seen:
                continue
            seen.add((a, b))
            t = "TOUCHES" if b.startswith("channel_") else types.get((a, b)) or types.get((b, a)) or "UNKNOWN"
            out.append(PathEdge(src=a, dst=b, type=t))
    return out


@contextmanager
def _offline():
    old = os.environ.get("GLOBE_OFFLINE")
    os.environ["GLOBE_OFFLINE"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("GLOBE_OFFLINE", None)
        else:
            os.environ["GLOBE_OFFLINE"] = old


def _cached_series(doc: dict, asset_id: str) -> tuple[SignalSeries | None, str]:
    """The asset's daily GDELT volume/tone for (asset terms AND channel terms), from cache only.

    Uses the same terms and window as reasoning.run_query step 4. The cache key contains the
    end date, so the last few days are tried (presets are precomputed on an earlier day)."""
    try:
        from pipeline import fetch_gdelt
        from pipeline.graph import default_graph
        from reasoning.run_query import _asset_context

        crit = doc["criteria"]
        library = _channel_library()
        chosen = [c["id"] for c in doc.get("channels", []) if c["id"] in library]
        channel_terms = sorted({t for c in chosen for t in library[c]["search_terms"]} | set(crit["event"]["terms"]))
        ctx = _asset_context(default_graph(), asset_id)
        terms = ctx["terms"] + ctx["owner_terms"]
        win = crit["window"]
        days = win["baseline_days"] + win["recent_days"]
        with _offline():
            for back in range(0, 8):
                end = date.today() - timedelta(days=back)
                start = end - timedelta(days=days)
                vol = fetch_gdelt.timeline(terms, channel_terms, start, end, "timelinevolraw")
                if len(vol):
                    tone = fetch_gdelt.timeline(terms, channel_terms, start, end, "timelinetone")
                    idx = [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]
                    v = {d.date().isoformat(): float(x) for d, x in vol.items()}
                    t = {d.date().isoformat(): float(x) for d, x in tone.items()} if len(tone) else {}
                    return SignalSeries(dates=idx, news_volume=[v.get(d, 0.0) for d in idx],
                                        tone=[t.get(d, 0.0) for d in idx] if t else [],
                                        recent_days=win["recent_days"]), (
                        f"GDELT articles per day mentioning this mine or its owner AND the event/channel terms; "
                        f"the last {win['recent_days']} days are compared with the {win['baseline_days']} before.")
        return None, "No cached GDELT news series for this mine yet."
    except Exception as e:  # the panel must open even if the cache or a helper misbehaves
        log.warning("series for %s failed: %s", asset_id, e)
        return None, "News series unavailable."


@app.get("/asset/{asset_id}", response_model=AssetDetail)
def asset(asset_id: str, q: str | None = None) -> dict:
    found = _find(asset_id, q)
    if found is None:
        raise HTTPException(status_code=404, detail=f"unknown asset {asset_id}")
    doc, result, is_mock = found
    paths = result["paths"]
    if is_mock:
        # MOCK series: flat baseline with a spike in the last 7 days, scaled by the mock news_z.
        end = date.today()
        dates = [(end - timedelta(days=66 - i)).isoformat() for i in range(67)]
        z = result["components"]["news_z"]
        volume = [5.0 + (i % 3) for i in range(60)] + [5.0 + 2.5 * z + (i % 2) for i in range(7)]
        series, note = SignalSeries(dates=dates, news_volume=volume, recent_days=7), "MOCK series (not real data)."
    else:
        series, note = _cached_series(doc, asset_id)
    return AssetDetail(
        id=result["id"], name=result["name"], path_nodes=_path_nodes(paths), paths=paths,
        path_edges=_path_edges(paths), series=series, series_note=note, evidence=result["evidence"],
    ).model_dump()


@app.get("/eval", response_model=EvalReport)
def eval_report() -> dict:
    if EVAL_RESULTS.exists():
        return json.loads(EVAL_RESULTS.read_text())
    return EvalReport(
        judge=[{"method": "decision model", "precision": None, "recall": None, "n": 0},
               {"method": "keyword baseline", "precision": None, "recall": None, "n": 0}],
        note="MOCK: run `make eval` after labelling to fill this in.",
    ).model_dump()
