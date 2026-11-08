"""Pydantic models for the result JSON contract.

THE CONTRACT IS FROZEN. These models mirror the "What the API returns to the globe"
block in the build plan field for field. Do not add, rename or remove a field in
Criteria / Channel / Components / Evidence / Result / QueryResult without telling
all three people: the frontend types in frontend/src/types.ts mirror this file.

The models at the bottom (Preset, AssetDetail, EvalReport) are NOT in the plan's
contract; they are our proposed shapes for GET /presets, GET /asset/{id} and
GET /eval, which the plan only describes in words. Change them freely, but tell C.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Shared config: reject unknown fields so contract drift fails loudly in tests.
_STRICT = ConfigDict(extra="forbid")

Tier = Literal["high", "medium", "low"]
JudgeChoice = Literal["relevant", "not_relevant", "unsure"]


# ---------------------------------------------------------------- step 1: criteria
class Event(BaseModel):
    model_config = _STRICT
    name: str
    date: str = Field(description="ISO date, e.g. 2026-11-03")
    terms: list[str]


class Window(BaseModel):
    model_config = _STRICT
    baseline_days: int = Field(gt=0)
    recent_days: int = Field(gt=0)


class Criteria(BaseModel):
    model_config = _STRICT
    asset_type: str
    event: Event
    window: Window
    status: list[str]
    region: str


# ---------------------------------------------------------------- step 2: channels
class Channel(BaseModel):
    model_config = _STRICT
    id: str
    reason: str


# ---------------------------------------------------------------- step 6: results
class Components(BaseModel):
    model_config = _STRICT
    exposure: float = Field(ge=0.0, le=1.0)
    news_z: float
    tone_shift: float
    attention_z: float
    relevant_articles: int = Field(ge=0)


class Evidence(BaseModel):
    model_config = _STRICT
    title: str
    url: str
    date: str
    judge: JudgeChoice
    confidence: float = Field(ge=0.0, le=1.0)


class Result(BaseModel):
    model_config = _STRICT
    id: str
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    tier: Tier
    score: float = Field(ge=0.0, le=1.0)
    components: Components
    paths: list[list[str]] = Field(description="Each path is a list of node ids, asset first, channel last")
    reason: str
    evidence: list[Evidence]


class QueryResult(BaseModel):
    model_config = _STRICT
    criteria: Criteria
    channels: list[Channel]
    results: list[Result]
    dark_count: int = Field(ge=0)


class QueryRequest(BaseModel):
    model_config = _STRICT
    text: str = Field(min_length=1, max_length=500)


# ======================================================= proposed (not in the plan's contract)
class Preset(BaseModel):
    id: str
    label: str
    query: str
    kind: Literal["hero", "weather", "labour", "negative_control", "other"] = "other"


class AssetPoint(BaseModel):
    """GET /assets: every asset on the globe, so dark ones can be drawn as grey dots."""
    id: str
    name: str
    lat: float
    lon: float
    country: str | None = None


class PathNode(BaseModel):
    id: str
    type: str
    name: str
    lat: float | None = None
    lon: float | None = None


class SignalSeries(BaseModel):
    dates: list[str]
    news_volume: list[float] = []
    tone: list[float] = []
    pageviews: list[float] = []
    recent_days: int = 7


class AssetDetail(BaseModel):
    id: str
    name: str
    path_nodes: list[PathNode] = Field(
        default_factory=list,
        description="Coordinates for every node on the stored paths, so the globe can draw arcs",
    )
    paths: list[list[str]] = []
    series: SignalSeries | None = None
    evidence: list[Evidence] = []


class JudgeRow(BaseModel):
    method: str
    precision: float | None
    recall: float | None
    n: int


class BacktestRow(BaseModel):
    event: str
    method: str
    precision_at_10: float | None


class NegativeControlRow(BaseModel):
    query: str
    lit: int
    dark: int
    passed: bool


class EvalReport(BaseModel):
    judge: list[JudgeRow] = []
    agreement_kappa: float | None = None
    backtests: list[BacktestRow] = []
    negative_controls: list[NegativeControlRow] = []
    note: str = ""
