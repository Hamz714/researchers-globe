"""Fill the GDELT/Wikipedia cache from a machine that can reach them (Person A).

Some environments can't reach GDELT (sandboxes, blocked networks), and a venue
shares one IP between hundreds of hackers, so GDELT's "one request every 5 s"
limit bites. This splits fetching from computing:

    python -m pipeline.browser_fetch plan  [--preset ID ...]   # -> data/jobs/jobs.json (cache misses only)
    #   (--preset eval: only the labelled set's article lists; with no --preset, those come right after the hero)
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
        if p.get("kind") == "hero" and (not preset_ids or "eval" in preset_ids):
            # right after the hero: article lists for the labelled set (eval/build_candidates.py)
            from eval.build_candidates import collect

            collect(end)
    if preset_ids and "eval" in preset_ids and not any(p["id"] in preset_ids for p in load_presets()):
        from eval.build_candidates import collect

        collect(end)
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


class RateLimited(RuntimeError):
    """GDELT keeps answering 429 to this IP: stop instead of grinding through every job."""


def _interval() -> float:
    # read at call time so GDELT_MIN_INTERVAL_S in .env (loaded in main) takes effect
    return max(5.5, float(os.environ.get("GDELT_MIN_INTERVAL_S", "6")))


def run(jobs_path: Path, out: Path, give_up_after: int = 2) -> int:
    """Plain-Python runner for a laptop with internet. Resumable: results are saved after every job.

    One request per GDELT_MIN_INTERVAL_S (>= 5.5 s, GDELT's stated limit is one per 5 s); Wikipedia
    jobs go first and are not throttled. Raises RateLimited after `give_up_after` consecutive jobs
    that were still rate-limited after all retries. Returns the number of new results.
    """
    import requests

    from pipeline.fetch_wiki import USER_AGENT as WIKI_UA

    jobs = sorted(json.loads(jobs_path.read_text())["jobs"], key=lambda j: j["kind"] != "wiki")
    results = json.loads(out.read_text()) if out.exists() else {}
    todo = [j for j in jobs if j["url"] not in results]
    interval, new, failed_in_row, last = _interval(), 0, 0, 0.0
    print(f"{len(todo)} to fetch, GDELT interval {interval:.1f}s (~{len(todo) * interval / 60:.0f} min)", file=sys.stderr)
    for i, j in enumerate(todo, 1):
        gdelt = j["kind"] == "gdelt"
        ua = "ResearchersGlobe/0.1 (HackTheBurgh 2026)" if gdelt else WIKI_UA
        status = "?"
        for attempt in range(4):
            if gdelt:
                time.sleep(max(0.0, interval - (time.monotonic() - last)))
                last = time.monotonic()
            try:
                r = requests.get(j["url"], timeout=40, headers={"User-Agent": ua})
            except requests.RequestException as e:  # hotspot drop-outs: back off and retry
                status = type(e).__name__
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429 or "limit requests" in r.text[:200]:
                status = "429"
                time.sleep(interval * 2 * (attempt + 1))
                continue
            if r.status_code >= 500:
                status = str(r.status_code)
                time.sleep(5 * (attempt + 1))
                continue
            results[j["url"]] = "{}" if r.status_code == 404 else r.text
            status, new = "ok", new + 1
            out.write_text(json.dumps(results))
            break
        failed_in_row = 0 if status == "ok" else failed_in_row + 1
        print(f"{i}/{len(todo)} {j['kind']} {status}", file=sys.stderr)
        if failed_in_row >= give_up_after:
            raise RateLimited(f"{failed_in_row} jobs in a row failed ({status}); {new} new results saved. "
                              "GDELT is limiting this IP: wait, or switch network, then re-run (resumable).")
    return new


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
    a = sub.add_parser("all", help="plan -> run -> ingest, repeated until nothing is missing (max 4 rounds)")
    a.add_argument("--end", default=date.today().isoformat())
    r = sub.add_parser("run")
    r.add_argument("--jobs", type=Path, default=JOBS)
    r.add_argument("--out", type=Path, default=ROOT / "data" / "jobs" / "results.json")
    args = ap.parse_args()
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")  # GDELT_MIN_INTERVAL_S, WIKI_USER_AGENT
    if args.cmd == "plan":
        jobs = plan(args.preset, date.fromisoformat(args.end))
        print(f"{len(jobs)} jobs ({sum(j['kind'] == 'gdelt' for j in jobs)} GDELT) -> {JOBS}")
    elif args.cmd == "ingest":
        ok, bad = ingest(args.path)
        print(f"stored {ok}, skipped {bad} (rate-limited or bad)")
    elif args.cmd == "all":
        # Rounds are needed because measured TOUCHES change which mines are candidates,
        # and so which evidence URLs are needed.
        out = ROOT / "data" / "jobs" / "results.json"
        for rnd in range(1, 5):
            jobs = plan(None, date.fromisoformat(args.end))
            print(f"round {rnd}: {len(jobs)} missing", file=sys.stderr)
            if not jobs:
                break
            try:
                new = run(JOBS, out)
            except RateLimited as e:
                ingest(out)  # keep what we got
                sys.exit(f"stopped: {e}")
            ok, bad = ingest(out)
            print(f"round {rnd}: {new} new, stored {ok}, skipped {bad}", file=sys.stderr)
            if not new:
                print("nothing new this round; stopping", file=sys.stderr)
                break
    else:
        try:
            run(args.jobs, args.out)
        except RateLimited as e:
            sys.exit(f"stopped: {e}")


if __name__ == "__main__":
    main()
