"""FastAPI backend (plan: Backend). Owner: Person C.

Run:  uvicorn backend.app:app --reload --port 8000   (or `make backend`)

Mode is chosen by USE_MOCK in .env (default: real pipeline):
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


@app.on_event("startup")
def _start_live_streams() -> None:
    """Live Wikipedia edits (pipeline/live_wiki.py): one streaming connection, started with the API.
    Off with LIVE_WIKI=0 or GLOBE_OFFLINE=1 (then the last snapshot is used)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    from pipeline.live_wiki import start_if_enabled

    try:
        start_if_enabled()
    except Exception:  # the API must come up even if the stream can't
        log.exception("live wiki stream failed to start")


@app.get("/live/status")
def live_status() -> dict:
    """Is the live Wikipedia edit stream connected, how far behind is it, how much has it matched."""
    from pipeline.live_wiki import LIVE

    return LIVE.status()


@app.get("/live/feed")
def live_feed(limit: int = 20) -> list[dict]:
    """The newest edits to watched pages (any kind of thing), newest first, for the globe's live ticker."""
    from pipeline.live_wiki import LIVE
    from reasoning.things import things

    names = {}
    for kind in ("mine", "state", "port", "country", "company"):
        names.update({t["id"]: t for t in things(kind)})
    out = []
    for e in LIVE.recent(max(1, min(limit, 100))):
        ids = [tid for _, tid in e["things"]]
        first = names.get(ids[0]) if ids else None
        out.append({**e, "thing_ids": ids, "name": first["name"] if first else e["title"],
                    "lat": first["lat"] if first else None, "lon": first["lon"] if first else None})
    return out
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_recent: OrderedDict[str, dict] = OrderedDict()


def use_pipeline() -> bool:
    # Real pipeline unless USE_MOCK=1. (The old USE_PIPELINE=0 from early .env files is ignored on purpose.)
    return os.environ.get("USE_MOCK", "0") != "1"


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


# Condition screens ("which <mines | US states> where <condition>?", reasoning/screen.py), shown
# first in the preset chips. Live data (Wikipedia, Open-Meteo), cached; not precomputed.
SCREEN_PRESETS = [  # suggestion chips; the first three update live (Wikimedia edit stream)
    {"id": "screen_live_countries", "label": "LIVE: countries being edited on Wikipedia right now",
     "query": "Which countries are being edited on Wikipedia right now?", "kind": "other"},
    {"id": "screen_live_companies", "label": "LIVE: mining companies edited in the last hour",
     "query": "Which mining companies have been edited on Wikipedia in the last hour?", "kind": "other"},
    {"id": "screen_live_states", "label": "LIVE: US states edited in the last 30 minutes",
     "query": "Which US states have been edited on Wikipedia in the last 30 minutes?", "kind": "other"},
    {"id": "screen_ports_weather", "label": "Export ports in the rain or wind right now",
     "query": "Which export ports are in the rain or wind right now?", "kind": "other"},
    {"id": "screen_states_views", "label": "US states: spike in Wikipedia views",
     "query": "Which US states have a spike in Wikipedia views?", "kind": "other"},
]


@app.post("/query", response_model=QueryResult)
def query(req: QueryRequest) -> dict:
    """Any question -> one plan (filters, conditions, event) -> one executor (reasoning/plan.py)."""
    preset = _preset_for(req.text)
    if preset is not None:
        cached = PRESET_RESULTS / f"{preset['id']}.json"
        if cached.exists():
            return _remember(req.text, json.loads(cached.read_text()))
    if not use_pipeline():
        return _mock_result()
    from reasoning.plan import answer

    try:
        return _remember(req.text, answer(req.text))
    except Exception as e:  # never show a stack trace on stage
        log.exception("query failed")
        raise HTTPException(status_code=503, detail=f"Could not answer that question: {e}") from e


@app.get("/presets", response_model=list[Preset])
def presets() -> list[dict]:
    return SCREEN_PRESETS + [{k: p[k] for k in ("id", "label", "query", "kind")} for p in _presets()]


@lru_cache(maxsize=1)
def _asset_points() -> list[dict]:
    from pipeline.graph import default_graph

    n = default_graph().nodes
    mines = n[n["type"] == "mine"]
    return [{"id": r.id, "name": r.name, "lat": float(r.lat), "lon": float(r.lon),
             "country": r.country if isinstance(r.country, str) else None} for r in mines.itertuples()]


@app.get("/assets", response_model=list[AssetPoint])
def assets(kind: str = "mine") -> list[dict]:
    """All things of a kind (mine | state | port | country | company, or the answer's asset_type), so the
    globe can draw the unlit ones as grey dots. Not in the plan's contract."""
    from reasoning.things import ASSET_TYPE_TO_KIND, KINDS, things

    kind = ASSET_TYPE_TO_KIND.get(kind, kind)
    if kind == "mine" or kind not in KINDS:
        return _asset_points()
    return [{"id": t["id"], "name": t["name"], "lat": t["lat"], "lon": t["lon"], "country": t["country"]}
            for t in things(kind)]


@app.get("/series/{kind}/{thing_id}")
def series(kind: str, thing_id: str) -> dict:
    """Daily Wikipedia views or edits behind a condition-screen result (detail chart)."""
    from reasoning.screen import series as screen_series

    if kind not in ("views", "edits"):
        raise HTTPException(status_code=404, detail="kind must be views or edits")
    out = screen_series(kind, thing_id)
    if out is None:
        raise HTTPException(status_code=404, detail="no Wikipedia article for this thing")
    return out


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
    """The asset's daily series from cache only, on one date axis (start..end of the query window):

    * news_volume / tone: GDELT for (asset terms AND the asset's evidence terms), as in
      reasoning.run_query step 4 (empty if the asset got no evidence pull);
    * pageviews: Wikipedia views of the mine's and its owner's articles, summed. It starts at
      dates[0] and stops at the last published day (Wikipedia lags 1-2 days), so it can be
      shorter than `dates`.

    The cache key contains the end date, so the last few days are tried (presets are
    precomputed on an earlier day)."""
    try:
        from pipeline import fetch_gdelt, fetch_wiki
        from pipeline.graph import default_graph
        from reasoning.run_query import _asset_context, asset_evidence_terms

        crit = doc["criteria"]
        channel_terms = asset_evidence_terms(doc, asset_id, _channel_library())
        ctx = _asset_context(default_graph(), asset_id)
        terms = ctx["terms"] + ctx["owner_terms"]
        win = crit["window"]
        days = win["baseline_days"] + win["recent_days"]
        with _offline():
            for back in range(0, 8):
                end = date.today() - timedelta(days=back)
                start = end - timedelta(days=days)
                vol = fetch_gdelt.timeline(terms, channel_terms, start, end, "timelinevolraw") if channel_terms else []
                views = None
                for title in ctx["wiki_titles"]:
                    pv = fetch_wiki.pageviews(title, start, end)
                    if len(pv):
                        views = pv if views is None else views.add(pv, fill_value=0)
                if not len(vol) and views is None:
                    continue
                idx = [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]
                notes = []
                news, tone_list = [], []
                if len(vol):
                    tone = fetch_gdelt.timeline(terms, channel_terms, start, end, "timelinetone")
                    v = {d.date().isoformat(): float(x) for d, x in vol.items()}
                    t = {d.date().isoformat(): float(x) for d, x in tone.items()} if len(tone) else {}
                    news = [v.get(d, 0.0) for d in idx]
                    tone_list = [t.get(d, 0.0) for d in idx] if t else []
                    notes.append("News: GDELT articles per day mentioning this mine or its owner AND the "
                                 "event/channel terms.")
                pageviews = []
                if views is not None:
                    pv = {d.date().isoformat(): float(x) for d, x in views.items()}
                    last = max(pv)
                    pageviews = [pv.get(d, 0.0) for d in idx if d <= last]
                    notes.append(f"Attention: Wikipedia views of {' + '.join(ctx['wiki_titles'])}, "
                                 f"published up to {last}.")
                notes.append(f"The last {win['recent_days']} days are compared with the "
                             f"{win['baseline_days']} before.")
                return SignalSeries(dates=idx, news_volume=news, tone=tone_list, pageviews=pageviews,
                                    recent_days=win["recent_days"]), " ".join(notes)
        return None, "No cached news or Wikipedia series for this mine yet."
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
