"""Fill the GDELT/Wikipedia cache from a machine that can reach them (Person A).

Some environments can't reach GDELT (sandboxes, blocked networks), and a venue
shares one IP between hundreds of hackers, so GDELT's "one request every 5 s"
limit bites. This splits fetching from computing:

    python -m pipeline.browser_fetch plan  [--preset ID ...]   # -> data/jobs/jobs.json (cache misses only)
    #   ... run the jobs anywhere: `python -m pipeline.browser_fetch run` on a laptop with
    #       internet, or the in-browser runner (BROWSER_RUNNER below) ...
    python -m pipeline.browser_fetch ingest results.json      # -> cache/gdelt, cache/wiki

results.json is {"<url>": "<raw response body>", ...}. Bodies are stored byte for byte
under the URL's cache key, so later runs are fully offline and reproducible.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "data" / "jobs" / "jobs.json"


def plan(preset_ids: list[str] | None, end: date) -> list[dict]:
    os.environ["GLOBE_OFFLINE"] = "1"
    from pipeline import fetch_gdelt, fetch_wiki
    from reasoning.parser import load_presets
    from reasoning.run_query import run_query

    fetch_gdelt.MISSES.clear()
    fetch_wiki.MISSES.clear()
    for p in load_presets():
        if preset_ids and p["id"] not in preset_ids:
            continue
        run_query(p["query"], end=end, backends=[])  # backends=[]: presets answer parse, no model calls
    jobs = [{"url": u, "kind": "gdelt"} for u in fetch_gdelt.MISSES] + \
           [{"url": u, "kind": "wiki"} for u in fetch_wiki.MISSES]
    JOBS.parent.mkdir(parents=True, exist_ok=True)
    JOBS.write_text(json.dumps({"created": time.strftime("%Y-%m-%dT%H:%M:%S"), "end": end.isoformat(),
                                "jobs": jobs}, indent=0))
    return jobs


def ingest(path: Path) -> tuple[int, int]:
    from pipeline import fetch_gdelt, fetch_wiki

    results = json.loads(Path(path).read_text())
    ok = bad = 0
    for url, body in results.items():
        if not isinstance(body, str) or "limit requests" in body[:200]:
            bad += 1
            continue
        (fetch_wiki if url.startswith(fetch_wiki.API) else fetch_gdelt).store(url, body)
        ok += 1
    return ok, bad


def run(jobs_path: Path, out: Path) -> None:
    """Plain-Python runner for a laptop with internet (same throttling as the browser runner)."""
    import requests

    jobs = json.loads(jobs_path.read_text())["jobs"]
    results = json.loads(out.read_text()) if out.exists() else {}
    for i, j in enumerate(jobs):
        if j["url"] in results:
            continue
        for attempt in range(5):
            r = requests.get(j["url"], timeout=30, headers={"User-Agent": "ResearchersGlobe/0.1 (HackTheBurgh 2026)"})
            if r.status_code == 429 or "limit requests" in r.text[:200]:
                time.sleep(10 * (attempt + 1))
                continue
            results[j["url"]] = "{}" if r.status_code == 404 else r.text
            break
        out.write_text(json.dumps(results))
        print(f"{i + 1}/{len(jobs)}", file=sys.stderr)
        if j["kind"] == "gdelt":
            time.sleep(6)


# Paste into a browser console on https://api.gdeltproject.org/ (same origin, no CORS issues),
# after setting window.JOBS = [...urls...]. Results land in window.RESULTS; call saveResults().
BROWSER_RUNNER = r"""
window.RESULTS = window.RESULTS || {}; window.FETCH_LOG = [];
window.saveResults = (name='results.json') => { const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([JSON.stringify(window.RESULTS)],{type:'application/json'}));
  a.download=name; document.body.appendChild(a); a.click(); a.remove(); };
(async () => { for (const u of window.JOBS) { if (window.RESULTS[u]) continue;
  for (let k=0;k<6;k++) { try { const r=await fetch(u); const t=await r.text();
      if (r.status===429 || /limit requests/i.test(t.slice(0,200))) { window.FETCH_LOG.push('429'); await new Promise(z=>setTimeout(z,10000*(k+1))); continue; }
      window.RESULTS[u] = r.status===404 ? '{}' : t; break; } catch(e) { window.FETCH_LOG.push(String(e)); await new Promise(z=>setTimeout(z,5000)); } }
  if (u.includes('gdeltproject')) await new Promise(z=>setTimeout(z,6000)); }
  window.FETCH_DONE = true; })();
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--preset", action="append")
    p.add_argument("--end", default=date.today().isoformat())
    i = sub.add_parser("ingest")
    i.add_argument("path", type=Path)
    r = sub.add_parser("run")
    r.add_argument("--jobs", type=Path, default=JOBS)
    r.add_argument("--out", type=Path, default=ROOT / "data" / "jobs" / "results.json")
    args = ap.parse_args()
    if args.cmd == "plan":
        jobs = plan(args.preset, date.fromisoformat(args.end))
        print(f"{len(jobs)} jobs ({sum(j['kind'] == 'gdelt' for j in jobs)} GDELT) -> {JOBS}")
    elif args.cmd == "ingest":
        ok, bad = ingest(args.path)
        print(f"stored {ok}, skipped {bad} (rate-limited or bad)")
    else:
        run(args.jobs, args.out)


if __name__ == "__main__":
    main()
