"""Precompute every preset's /query result into data/preset_results/<id>.json (plan: cached mode).

    GLOBE_OFFLINE=1 python -m reasoning.precompute        # or: make presets

The backend serves these files first, in mock and pipeline mode, so the demo works with
no network. Re-run after new GDELT/Wikipedia data is ingested into cache/.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "preset_results"


def main() -> None:
    os.environ.setdefault("GLOBE_OFFLINE", "1")
    from reasoning.parser import load_presets
    from reasoning.run_query import run_query

    OUT.mkdir(parents=True, exist_ok=True)
    for p in load_presets():
        res = run_query(p["query"])
        (OUT / f"{p['id']}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
        tiers = {t: sum(r["tier"] == t for r in res["results"]) for t in ("high", "medium", "low")}
        print(f"{p['id']}: {tiers} dark={res['dark_count']}")


if __name__ == "__main__":
    main()
