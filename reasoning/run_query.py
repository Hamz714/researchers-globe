"""run_query(text) -> result JSON (plan: Person B publishes this).

Wires the six steps together. Steps 1, 2 and 5 call the decision model; 3, 4 and 6
are plain code. Output is validated against backend.schemas.QueryResult before it
is returned, so a contract break fails here, not in the browser.

Offline (GLOBE_OFFLINE=1, the default here) step 4 only reads the cache, so with an
empty cache every asset gets zero signals and no articles, i.e. exposure-only scores.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

from backend.schemas import QueryResult
from pipeline.graph import Graph, default_graph
from reasoning import exposure as exp
from reasoning import scoring
from reasoning.judge import judge_article
from reasoning.parser import choose_channels, choose_touches, load_channels, parse_question

TOP_K = 50          # candidates returned (plan)
EVIDENCE_K = 10     # candidates we pull GDELT evidence for (rate limit: ~6 s per request, 3-4 requests each)
PLACEBO_K = 10      # candidates that also get the placebo control query
MAX_ARTICLES = 20
CONTROL_TERMS = ["copper"]  # last-resort placebo term; the real one is chosen per query (pipeline/controls.py)


def _terms(node: dict) -> list[str]:
    return [t for t in str(node.get("search_terms") or node["name"]).split("|") if t]


def evidence_terms(criteria: dict, channel: dict) -> list[str]:
    """Terms ANDed with an asset's names in its GDELT queries: the event terms plus the terms of the
    channel on the asset's strongest path. Not every chosen channel: 15 OR'd terms made queries of
    ~300 chars, which GDELT rejects ("Your query was too short or too long"); this keeps them short."""
    return list(dict.fromkeys(list(criteria["event"]["terms"]) + list(channel["search_terms"])))


def asset_evidence_terms(doc: dict, asset_id: str, library: dict) -> list[str] | None:
    """evidence_terms() for an asset in a finished result doc (its strongest path's channel), or None."""
    for r in doc.get("results", []):
        if r["id"] == asset_id and r.get("paths"):
            cid = r["paths"][0][-1].removeprefix("channel_")
            return evidence_terms(doc["criteria"], library[cid]) if cid in library else None
    return None


def _path_words(graph: Graph, path: list[str]) -> str:
    names = []
    for nid in path:
        try:
            names.append(graph.node(nid)["name"])
        except KeyError:
            names.append(nid)
    return " → ".join(names)


def _asset_context(graph: Graph, asset_id: str) -> dict:
    node = graph.node(asset_id)
    e = graph.edges
    owned = e[(e["src"] == asset_id) & (e["type"] == "OWNED_BY")].sort_values("weight", ascending=False)
    owner_node = graph.node(owned.iloc[0]["dst"]) if len(owned) else None
    country = graph.node(f"country_{node['country']}")["name"] if node.get("country") else None
    wiki = [t for t in [node.get("wiki_title"), owner_node.get("wiki_title") if owner_node else None]
            if isinstance(t, str) and t]
    return {"name": node["name"], "owner": owner_node["name"] if owner_node else None,
            "owner_terms": _terms(owner_node) if owner_node else [],
            "country": country, "terms": _terms(node), "wiki_titles": wiki}


def _gather(asset: dict, channel_terms: list[str], criteria: dict, end: date,
            with_control: bool = True, control: list[str] | None = None) -> tuple[dict, list[dict]]:
    """Step 4: signals + recent articles for one asset. Imported lazily so tests stay offline."""
    from pipeline import fetch_gdelt
    from pipeline.signals import signals

    asset_terms = asset["terms"] + asset["owner_terms"]
    win = criteria["window"]
    sig = signals(asset["name"], {
        "asset_terms": asset_terms, "channel_terms": channel_terms,
        "control_terms": (control or CONTROL_TERMS) if with_control else [],
        "wiki_titles": asset["wiki_titles"],
        "baseline_days": win["baseline_days"], "recent_days": win["recent_days"], "end": end,
    })
    arts = fetch_gdelt.articles(asset_terms, channel_terms, end - timedelta(days=win["recent_days"]), end,
                                max_records=MAX_ARTICLES)
    return sig.to_dict(), arts


def run_query(text: str, graph: Graph | None = None, end: date | None = None,
              backends: list | None = None) -> dict:
    os.environ.setdefault("GLOBE_OFFLINE", "1")
    end = end or date.today()
    library = load_channels()

    # 1-2. parse and choose channels (decision model, validated)
    criteria, preset = parse_question(text, backends=backends)
    channels = choose_channels(text, criteria, preset, backends=backends)
    chosen = [c["id"] for c in channels]

    # 3. exposure (deterministic)
    base = graph or default_graph()
    touches, touch_prov = choose_touches(text, criteria, chosen, base, end, preset, backends=backends)
    g = base.with_edges(exp.touches_edges(chosen, touches))
    top, dark = exp.propagate(g, {c: library[c]["weight"] for c in chosen}, top_k=TOP_K)

    from pipeline.controls import control_terms

    control = control_terms(criteria, chosen, library, end)  # per-query placebo (Person A's rule)
    results = []
    zero = {"news_z": 0.0, "tone_shift": 0.0, "attention_z": 0.0, "placebo_flag": False}
    for rank, ex in enumerate(top):
        node = g.node(ex.asset_id)
        asset = _asset_context(g, ex.asset_id)

        best_channel = library[ex.paths[0][-1].removeprefix("channel_")]

        # 4. evidence (cached fetches); beyond EVIDENCE_K the asset is scored on exposure alone
        if rank < EVIDENCE_K:
            sig, articles = _gather(asset, evidence_terms(criteria, best_channel), criteria, end,
                                    with_control=rank < PLACEBO_K, control=control["terms"])
        else:
            sig, articles = dict(zero), []

        # 5. judge each article against the channel on its strongest path
        evidence = []
        for a in articles[:MAX_ARTICLES]:
            art = {"title": a.get("title", ""), "url": a.get("url", ""), "date": str(a.get("seendate", ""))[:10],
                   "domain": a.get("domain", ""), "snippet": a.get("snippet", "")}
            j = judge_article(art, asset, best_channel, criteria["event"], backends=backends)
            evidence.append({"title": art["title"], "url": art["url"], "date": art["date"],
                             "judge": j["choice"], "confidence": j["confidence"]})
        relevant = [e for e in evidence if e["judge"] == "relevant"]

        # 6. score, tier, explain
        ev = scoring.evidence(sig["news_z"], sig["attention_z"], sig["tone_shift"], len(relevant))
        sc = scoring.score(ex.exposure, ev)
        t = scoring.tier(sc, len(relevant), ex.exposure)
        if t == "dark":
            dark += 1
            continue
        touched, ch_id = ex.paths[0][-2], ex.paths[0][-1].removeprefix("channel_")
        prov = touch_prov.get(ch_id, {})
        why_touch = ""
        if prov.get("source") == "gdelt_titles" and touched in prov.get("named", {}):
            why_touch = (f" ({g.node(touched)['name']} is named in {prov['named'][touched]} of "
                         f"{prov['articles']} {criteria['event']['name']} articles on {ch_id})")
        reason = (f"Exposed via {_path_words(g, ex.paths[0])}{why_touch}"
                  + (f" (+{len(ex.paths) - 1} more paths)" if len(ex.paths) > 1 else "")
                  + f". News volume z={sig['news_z']:.1f}, attention z={sig['attention_z']:.1f}, "
                  f"{len(relevant)} relevant article(s)."
                  + (f" The placebo control ({control['channel'] or 'copper'} news) spiked too, so the news spike was discounted."
                     if sig["placebo_flag"] else ""))
        # TODO(Person B): one-sentence grounded summary via the decision model, citing only `relevant`.
        results.append({
            "id": ex.asset_id, "name": node["name"], "lat": float(node["lat"]), "lon": float(node["lon"]),
            "tier": t, "score": round(sc, 4),
            "components": {"exposure": round(ex.exposure, 4), "news_z": round(sig["news_z"], 3),
                           "tone_shift": round(sig["tone_shift"], 3), "attention_z": round(sig["attention_z"], 3),
                           "relevant_articles": min(len(relevant), scoring.RELEVANT_CAP)},
            "paths": ex.paths[:5],
            "reason": reason,
            "evidence": relevant,  # plan: only relevant articles count as evidence
        })

    results.sort(key=lambda r: -r["score"])
    out = {"criteria": criteria, "channels": channels, "results": results, "dark_count": dark}
    return QueryResult.model_validate(out).model_dump()


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(run_query(" ".join(sys.argv[1:]) or "Which iron ore mines are exposed to the 2026 US midterms?"),
                     indent=2))
