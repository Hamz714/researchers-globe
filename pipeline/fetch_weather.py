"""Current weather at many points from Open-Meteo (keyless), cached. Owner: Person A.

    current(points) -> {point_id: {"time", "temperature_2m", "precipitation", "rain", "snowfall",
                                   "wind_speed_10m", "weather_code"}}

Checked 8 Nov 2026: GET https://api.open-meteo.com/v1/forecast with comma-separated
latitude/longitude lists returns a JSON list, one object per location, each with a
`current` block (precipitation in mm and snowfall in cm over the preceding interval,
wind in km/h, WMO weather code). Open-Meteo's free tier counts EVERY location as a call
(600/min, 5,000/hour; a 701-point query twice in a few minutes got HTTP 429), so points are
snapped to a GRID_DEG grid and deduplicated (mines cluster), sent 100 per request with a
pause between requests, and answers are cached per 60-minute slot under cache/weather/.
On an error, or with GLOBE_OFFLINE=1, the newest cached slot is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.open-meteo.com/v1/forecast"
FIELDS = "temperature_2m,precipitation,rain,snowfall,wind_speed_10m,weather_code"
CHUNK = 100
SLOT_S = 60 * 60
GRID_DEG = 0.5   # ~50 km: current weather does not differ much within a cell
PAUSE_S = 2.0
USER_AGENT = os.environ.get("WIKI_USER_AGENT", "ResearchersGlobe/0.1 (HackTheBurgh 2026 student project)")
SOURCE_URL = "https://open-meteo.com/"


def cache_dir() -> Path:
    return Path(os.environ.get("GLOBE_CACHE_DIR", ROOT / "cache")) / "weather"


def _key(points: list[tuple[str, float, float]]) -> str:
    return hashlib.sha256(json.dumps(points).encode()).hexdigest()[:24]


def _cell(lat: float, lon: float) -> tuple[float, float]:
    return round(round(lat / GRID_DEG) * GRID_DEG, 3), round(round(lon / GRID_DEG) * GRID_DEG, 3)


def _newest(d: Path):
    cached = sorted(d.glob("*.json")) if d.exists() else []
    return json.loads(cached[-1].read_text()) if cached else None


def current(points: list[tuple[str, float, float]]) -> dict[str, dict]:
    """points = [(id, lat, lon), ...] -> {id: current-weather dict of its grid cell}. Missing points are left out."""
    cells = sorted({_cell(a, o) for _, a, o in points})
    by_cell: dict[tuple[float, float], dict] = {}
    slot = int(time.time() // SLOT_S)
    offline = os.environ.get("GLOBE_OFFLINE", "0") == "1"
    for i in range(0, len(cells), CHUNK):
        chunk = cells[i:i + CHUNK]
        d = cache_dir() / _key([("cell", a, o) for a, o in chunk])
        path = d / f"{slot}.json"
        body = None
        if path.exists():
            body = json.loads(path.read_text())
        elif offline:
            body = _newest(d)
        else:
            import requests

            try:
                if i:
                    time.sleep(PAUSE_S)
                r = requests.get(API, timeout=30, headers={"User-Agent": USER_AGENT}, params={
                    "latitude": ",".join(f"{a:.3f}" for a, _ in chunk),
                    "longitude": ",".join(f"{o:.3f}" for _, o in chunk),
                    "current": FIELDS, "timezone": "UTC"})
                r.raise_for_status()
                body = r.json()
                body = body if isinstance(body, list) else [body]
                d.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(body))
            except Exception:  # rate limit or network: newest cached answer, if any
                body = _newest(d)
        if body:
            for cell, loc in zip(chunk, body):
                if isinstance(loc, dict) and loc.get("current"):
                    by_cell[cell] = loc["current"]
    return {pid: by_cell[_cell(a, o)] for pid, a, o in points if _cell(a, o) in by_cell}
