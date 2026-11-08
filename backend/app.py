"""FastAPI backend (plan: Backend). Owner: Person C.

Run:  uvicorn backend.app:app --reload --port 8000   (or `make backend`)

Mode is chosen by USE_PIPELINE in .env:
  0 (default) -> every endpoint returns mock data in the contract shape, so the
                 frontend works from minute one;
  1           -> POST /query calls reasoning.run_query.run_query (Person B).
Cached preset results in data/preset_results/<preset_id>.json are served first in
both modes (plan: cached mode for every preset, for when venue Wi-Fi fails).
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from functools import lru_cache

from backend.schemas import (AssetDetail, AssetPoint, EvalReport, PathNode, Preset, QueryRequest, QueryResult,
                             SignalSeries)

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
log = logging.getLogger("globe")

MOCK_RESULT = ROOT / "data" / "mock" / "mock_result.json"
PRESETS = ROOT / "data" / "presets.json"
PRESET_RESULTS = ROOT / "data" / "preset_results"
EVAL_RESULTS = ROOT / "eval" / "results.json"

app = FastAPI(title="Researcher's Globe API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def use_pipeline() -> bool:
    return os.environ.get("USE_PIPELINE", "0") == "1"


def _presets() -> list[dict]:
    return json.loads(PRESETS.read_text())["presets"]


def _mock_result() -> dict:
    return json.loads(MOCK_RESULT.read_text())


def _preset_for(text: str) -> dict | None:
    t = text.strip().lower()
    return next((p for p in _presets() if p["query"].strip().lower() == t), None)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": "pipeline" if use_pipeline() else "mock"}


@app.post("/query", response_model=QueryResult)
def query(req: QueryRequest) -> dict:
    preset = _preset_for(req.text)
    if preset is not None:
        cached = PRESET_RESULTS / f"{preset['id']}.json"
        if cached.exists():
            return json.loads(cached.read_text())
    if use_pipeline():
        from reasoning.run_query import run_query

        try:
            return run_query(req.text)
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


@app.get("/asset/{asset_id}", response_model=AssetDetail)
def asset(asset_id: str) -> dict:
    # TODO(Person C + A): in pipeline mode, read stored paths, cached series and evidence for this asset.
    result = next((r for r in _mock_result()["results"] if r["id"] == asset_id), None)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown asset {asset_id}")
    from pipeline.graph import default_graph

    g = default_graph()
    node_ids = list(dict.fromkeys(n for p in result["paths"] for n in p))
    path_nodes = []
    for nid in node_ids:
        try:
            n = g.node(nid)
            lat = None if n["lat"] is None or (isinstance(n["lat"], float) and math.isnan(n["lat"])) else float(n["lat"])
            lon = None if n["lon"] is None or (isinstance(n["lon"], float) and math.isnan(n["lon"])) else float(n["lon"])
            path_nodes.append(PathNode(id=nid, type=n["type"], name=n["name"], lat=lat, lon=lon))
        except KeyError:
            path_nodes.append(PathNode(id=nid, type=nid.split("_")[0], name=nid))
    # MOCK series: flat baseline with a spike in the last 7 days, scaled by the mock news_z.
    end = date.today()
    dates = [(end - timedelta(days=66 - i)).isoformat() for i in range(67)]
    z = result["components"]["news_z"]
    volume = [5.0 + (i % 3) for i in range(60)] + [5.0 + 2.5 * z + (i % 2) for i in range(7)]
    return AssetDetail(
        id=result["id"], name=result["name"], path_nodes=path_nodes, paths=result["paths"],
        series=SignalSeries(dates=dates, news_volume=volume, recent_days=7),
        evidence=result["evidence"],
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
