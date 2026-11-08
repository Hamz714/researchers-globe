"""Build eval/candidates.csv: (article, asset, channel) pairs to hand-label (plan: Evaluation 1).

Owner: Person A. Offline: reads cached GDELT article lists only.

    GLOBE_OFFLINE=1 python -m eval.build_candidates            # ~60 asset-level + ~50 event-level
    python -m eval.build_candidates --asset-n 60 --event-n 50 --seed 2026 --end 2026-11-08

For every preset except the negative control, the candidates are rebuilt exactly as
reasoning.run_query does (channels, measured touches, exposure, top EVIDENCE_K), then:

* likely-relevant pairs: (1) articles cached for (asset terms AND the asset's evidence terms),
  paired with that asset and the channel on its strongest path; (2) event coverage (event terms
  AND channel terms, the articles TOUCHES are measured from) whose title names an exposed mine
  or its owner, paired with that mine, then titles naming an exposed mine's country.
  (1) is nearly empty for the presets on 8 Nov 2026 because 5 of the 7 recent days fall in the
  GDELT DOC outage (fetch_gdelt.DEFAULT_DOC_GAPS), hence (2).
* likely-not-relevant pairs: event coverage paired with a random exposed mine whose names and
  country the title does not mention.

Labelling both kinds lets eval.metrics measure precision AND recall of the judge.
Columns follow eval/candidates_example.csv. A fixed seed makes the file reproducible.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "candidates.csv"
COLUMNS = ["pair_id", "article_title", "article_url", "article_date", "snippet", "asset_id", "asset_name",
           "asset_owner", "asset_country", "asset_terms", "channel_id", "event_name", "event_terms"]


def _date(seendate: str) -> str:
    s = str(seendate or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else ""


def _has(title: str, terms) -> bool:
    import re

    for t in terms:
        if not t or len(t) < 3:
            continue
        pat = re.escape(t) if (t.isupper() and len(t) <= 4) else "(?i:" + re.escape(t) + ")"
        if re.search(r"(?<![\w.])" + pat + r"(?![\w])", title):
            return True
    return False


def mentions(title: str, ctx: dict, level: str) -> bool:
    """level 'asset': the title names the mine or its owner; 'country': ... or the mine's country."""
    if _has(title, ctx["terms"] + ctx["owner_terms"]):
        return True
    if level == "country" and ctx.get("iso2"):
        from reasoning.touches import COUNTRY_ALIASES

        return _has(title, COUNTRY_ALIASES.get(ctx["iso2"], [ctx["country"] or ""]))
    return False


ASSET_DAYS = 30  # asset-level article window: wider than the 7-day signal window, to reach past the outage


def collect(end: date, asset_days: int = ASSET_DAYS) -> tuple[list[dict], list[dict]]:
    """(asset-level rows, event-level rows) for all presets, without pair ids.

    Offline, every article list that is not cached is recorded in fetch_gdelt.MISSES, so
    pipeline.browser_fetch plan can queue it (it calls this after the hero preset)."""
    from pipeline import fetch_gdelt
    from pipeline.graph import default_graph
    from reasoning import exposure as exp
    from reasoning.parser import choose_touches, load_channels, load_presets
    from reasoning.run_query import EVIDENCE_K, MAX_ARTICLES, TOP_K, _asset_context, evidence_terms
    from reasoning.touches import MAX_ARTICLES as TOUCH_ARTICLES

    library = load_channels()
    base = default_graph()
    asset_rows, event_rows = [], []
    for p in load_presets():
        chosen = [c["id"] for c in p.get("channels", []) if c["id"] in library]
        if p.get("kind") == "negative_control" or not chosen:
            continue
        crit = p["criteria"]
        ev = crit["event"]
        touches, _ = choose_touches(p["query"], crit, chosen, base, end, p, backends=[])
        g = base.with_edges(exp.touches_edges(chosen, touches))
        top, _ = exp.propagate(g, {c: library[c]["weight"] for c in chosen}, top_k=TOP_K)
        common = {"event_name": ev["name"], "event_terms": "|".join(ev["terms"]), "snippet": ""}

        def asset_fields(asset_id: str, ctx: dict) -> dict:
            return {"asset_id": asset_id, "asset_name": ctx["name"], "asset_owner": ctx["owner"] or "",
                    "asset_country": ctx["country"] or "", "asset_terms": "|".join(ctx["terms"])}

        # asset-level: step 4's query for the top EVIDENCE_K candidates, over a wider window
        window_start = end - timedelta(days=asset_days)
        for ex in top[:EVIDENCE_K]:
            ctx = _asset_context(g, ex.asset_id)
            channel = library[ex.paths[0][-1].removeprefix("channel_")]
            arts = fetch_gdelt.articles(ctx["terms"] + ctx["owner_terms"], evidence_terms(crit, channel),
                                        window_start, end, max_records=MAX_ARTICLES)
            for a in arts:
                asset_rows.append({"article_title": a.get("title", ""), "article_url": a.get("url", ""),
                                   "article_date": _date(a.get("seendate")), **asset_fields(ex.asset_id, ctx),
                                   "channel_id": channel["id"], "preset": p["id"], **common})

        # event-level: the coverage TOUCHES are measured from, paired with a random exposed mine
        try:
            ev_start = date.fromisoformat(ev["date"]) - timedelta(days=1)
        except ValueError:
            ev_start = end - timedelta(days=crit["window"]["recent_days"])
        ev_start = max(ev_start, end - timedelta(days=85))
        ctxs = {ex.asset_id: {**_asset_context(g, ex.asset_id), "iso2": g.node(ex.asset_id).get("country")}
                for ex in top}
        for cid in chosen:
            for a in fetch_gdelt.articles(ev["terms"], library[cid]["search_terms"], ev_start, end,
                                          max_records=TOUCH_ARTICLES):
                event_rows.append({"article_title": a.get("title", ""), "article_url": a.get("url", ""),
                                   "article_date": _date(a.get("seendate")), "channel_id": cid,
                                   "preset": p["id"], "_pool": list(ctxs), "_ctxs": ctxs, **common})
    return asset_rows, event_rows


def build(end: date, asset_n: int, event_n: int, seed: int, asset_days: int = ASSET_DAYS) -> list[dict]:
    rng = random.Random(seed)
    asset_rows, event_rows = collect(end, asset_days)
    seen: set[tuple[str, str]] = set()

    def dedupe(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            key = (r["article_url"], r.get("asset_id", ""))
            if r["article_url"] and r["article_title"] and key not in seen:
                seen.add(key)
                out.append(r)
        return out

    asset_rows = dedupe(asset_rows)
    rng.shuffle(asset_rows)
    picked = asset_rows[:asset_n]

    by_url = {}
    for r in event_rows:
        by_url.setdefault(r["article_url"], r)
    events = [r for u, r in sorted(by_url.items()) if r["article_title"]]
    rng.shuffle(events)

    def pair(r: dict, aid: str) -> dict | None:
        if (r["article_url"], aid) in seen:
            return None
        seen.add((r["article_url"], aid))
        ctx = r["_ctxs"][aid]
        row = {k: v for k, v in r.items() if not k.startswith("_")}
        row.update({"asset_id": aid, "asset_name": ctx["name"], "asset_owner": ctx["owner"] or "",
                    "asset_country": ctx["country"] or "", "asset_terms": "|".join(ctx["terms"])})
        return row

    # likely relevant: titles naming an exposed mine/owner first, then its country
    used_urls = set()
    for level in ("asset", "country"):
        for r in events:
            if len(picked) >= asset_n:
                break
            if r["article_url"] in used_urls:
                continue
            hits = sorted(a for a in r["_pool"] if mentions(r["article_title"], r["_ctxs"][a], level))
            if hits:
                row = pair(r, rng.choice(hits))
                if row:
                    picked.append(row)
                    used_urls.add(r["article_url"])

    # likely not relevant: a random exposed mine the title says nothing about
    for r in events:
        if len(picked) >= asset_n + event_n:
            break
        if r["article_url"] in used_urls:
            continue
        pool = sorted(a for a in r["_pool"] if not mentions(r["article_title"], r["_ctxs"][a], "country"))
        if pool:
            row = pair(r, rng.choice(pool))
            if row:
                picked.append(row)
                used_urls.add(r["article_url"])

    rng.shuffle(picked)  # labellers must not see the two kinds in blocks
    return [{**{k: r.get(k, "") for k in COLUMNS}, "pair_id": f"c{i:03d}"} for i, r in enumerate(picked, 1)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--asset-n", type=int, default=60)
    ap.add_argument("--event-n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--asset-days", type=int, default=ASSET_DAYS)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    os.environ.setdefault("GLOBE_OFFLINE", "1")
    rows = build(date.fromisoformat(args.end), args.asset_n, args.event_n, args.seed, args.asset_days)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} pairs to {args.out}")


if __name__ == "__main__":
    main()
