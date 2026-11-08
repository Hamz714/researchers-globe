"""Sanity report on the cache and one preset's signals (Person A). Offline: reads cache/ only.

    python -m pipeline.sanity [preset_id]        # default hero_midterms

1. Every cached GDELT body: rejected by GDELT ("too short or too long"), empty, or a timeline
   (raw resolution, number of daily points after resampling, first/last day).
2. For the preset: measured TOUCHES per channel, then news_z, tone_shift, attention_z and the
   placebo flag for the candidates that get evidence, with anything odd flagged.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

os.environ["GLOBE_OFFLINE"] = "1"

from pipeline import fetch_gdelt  # noqa: E402

EXPECTED_DAYS = 67  # baseline 60 + recent 7; GDELT has no bucket yet for the current (end) day


def cache_report() -> None:
    kinds, odd = Counter(), []
    for p in sorted(fetch_gdelt.cache_dir().glob("*.json")):
        d = json.loads(p.read_text())
        q = parse_qs(urlparse(d["url"]).query)
        mode, query, body = q["mode"][0], q["query"][0], d["body"]
        if "too short or too long" in body[:200]:
            kinds["rejected (query too short/long)"] += 1
            odd.append(f"REJECTED len={len(query)}: {query[:110]}")
            continue
        j = fetch_gdelt._json(body)
        if mode == "artlist":
            kinds[f"artlist ({'empty' if not j.get('articles') else 'ok'})"] += 1
            continue
        tl = (j.get("timeline") or [{}])[0].get("data") or []
        if not tl:
            kinds[f"{mode} (empty)"] += 1
            continue
        s = fetch_gdelt.parse_timeline(body, mode)
        raw_step = (fetch_gdelt.pd.to_datetime(tl[1]["date"], format="%Y%m%dT%H%M%SZ")
                    - fetch_gdelt.pd.to_datetime(tl[0]["date"], format="%Y%m%dT%H%M%SZ")) if len(tl) > 1 else None
        kinds[f"{mode} raw step {raw_step}"] += 1
        if len(s) != EXPECTED_DAYS:
            odd.append(f"{mode}: {len(s)} daily points (expected {EXPECTED_DAYS}), "
                       f"{s.index[0].date()}..{s.index[-1].date()}: {query[:80]}")
    print("== cache/gdelt")
    for k, v in sorted(kinds.items()):
        print(f"  {v:4d}  {k}")
    for o in odd[:25]:
        print("  !", o)
    if len(odd) > 25:
        print(f"  ! ... {len(odd) - 25} more")


def preset_report(preset_id: str) -> None:
    from pipeline.graph import default_graph
    from reasoning import exposure as exp
    from reasoning.parser import choose_touches, load_channels, load_presets
    from reasoning.run_query import EVIDENCE_K, _asset_context, _gather, evidence_terms

    p = next(x for x in load_presets() if x["id"] == preset_id)
    crit, chosen, end = p["criteria"], [c["id"] for c in p["channels"]], date.today()
    lib, g0 = load_channels(), default_graph()
    print(f"\n== {preset_id}: channels {chosen}, end {end}")
    touches, prov = choose_touches(p["query"], crit, chosen, g0, end, p, backends=[])
    for cid in chosen:
        pv = prov.get(cid, {})
        print(f"  touches[{cid}] source={pv.get('source')} articles={pv.get('articles')}")
        for nid, w in touches.get(cid, []):
            print(f"      {w:5.2f}  {g0.node(nid)['name']}  (named in {pv.get('named', {}).get(nid, '-')})")
    g = g0.with_edges(exp.touches_edges(chosen, touches))
    top, dark = exp.propagate(g, {c: lib[c]["weight"] for c in chosen}, top_k=50)
    print(f"  {len(top)} candidates, {dark} dark; evidence for top {EVIDENCE_K}")
    print(f"  {'exposure':>8} {'news_z':>7} {'tone':>6} {'attn_z':>7} plc arts  name")
    flags = Counter()
    for rank, ex in enumerate(top[:EVIDENCE_K]):
        a = _asset_context(g, ex.asset_id)
        best = lib[ex.paths[0][-1].removeprefix("channel_")]
        sig, arts = _gather(a, evidence_terms(crit, best), crit, end, with_control=rank < 10)
        note = []
        if all(sig[k] == 0 for k in ("news_z", "tone_shift", "attention_z")):
            note.append("all-zero")
        if abs(sig["news_z"]) > 10 or abs(sig["tone_shift"]) > 10:
            note.append("extreme")
        if a["wiki_titles"] and sig["attention_z"] == 0:
            note.append("wiki title but attn=0")
        flags.update(note)
        print(f"  {ex.exposure:8.3f} {sig['news_z']:7.2f} {sig['tone_shift']:6.2f} {sig['attention_z']:7.2f} "
              f"{'Y' if sig['placebo_flag'] else '.':>3} {len(arts):4d}  {a['name'][:45]}"
              + (f"  <- {', '.join(note)}" if note else ""))
    if flags:
        print("  flags:", dict(flags))


if __name__ == "__main__":
    cache_report()
    preset_report(sys.argv[1] if len(sys.argv) > 1 else "hero_midterms")
