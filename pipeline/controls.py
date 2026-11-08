"""Per-query placebo control (plan: Placebo check). Owner: Person A. PROPOSAL, not yet wired in.

Why not a fixed "copper": the placebo asks "is this asset in the news more for every topic right
now?", so the control must be (a) unrelated to the event and the chosen channels, and (b) calm
overall in the window, so that a spike in (asset AND control) can only come from the asset.
"copper" fails both ways: it is causally linked for diversified miners (BHP, Vale, Anglo, Rio all
mine copper, so copper news moves with them) and near-silent for pure iron-ore miners
(Fortescue, Roy Hill, Ferrexpo), where the +1 MAD pad leaves the control z at ~0 whatever happens.

Rule, measured like TOUCHES (reasoning/touches.py), same control for every asset in one query:
  candidates = library channels NOT chosen for this query whose terms share no word with the
               event terms or the chosen channels' terms
  for each:   z_global = robust z of (channel terms) volume over the query window
              z_event  = robust z of (event terms AND channel terms) volume over the window
  pick        the candidate with the smallest max(z_global, 0) + max(z_event, 0)
              (calm overall AND not being pulled by the event); ties -> library order
That is 2 cached GDELT timeline calls per candidate per query (<= 14), shared by all assets.
Offline / no data: the first candidate in FALLBACK_ORDER, then ["copper"].

Wiring (Person B, reasoning/run_query.py): compute once per query, after `chosen` is known,
    control = control_terms(criteria, chosen, library, end)
and pass `control["terms"]` instead of CONTROL_TERMS in _gather(); `control["channel"]` and
`control["why"]` can go into the reason text.
"""
from __future__ import annotations

from datetime import date, timedelta

# Calm-by-default first; used only when GDELT has nothing for any candidate.
FALLBACK_ORDER = ["currency", "weather", "sanctions", "labour", "conflict", "permits", "infrastructure", "tariffs"]
DEFAULT_CONTROL = ["copper"]


def _words(terms: list[str]) -> set[str]:
    return {w.lower() for t in terms for w in t.split() if len(w) >= 3}


def candidate_channels(event_terms: list[str], chosen: list[str], library: dict) -> list[str]:
    """Non-chosen channels whose terms share no word with the event or the chosen channels (library order)."""
    taken = _words(event_terms) | _words([t for c in chosen for t in library[c]["search_terms"]])
    return [cid for cid, ch in library.items() if cid not in chosen and not (_words(ch["search_terms"]) & taken)]


def pick(scores: dict[str, tuple[float, float] | None], candidates: list[str]) -> tuple[str | None, str]:
    """scores[cid] = (z_global, z_event) or None (no data). Returns (channel id or None, reason)."""
    measured = {c: s for c, s in scores.items() if s is not None and c in candidates}
    if measured:
        best = min(candidates, key=lambda c: (max(measured[c][0], 0) + max(measured[c][1], 0), candidates.index(c))
                   if c in measured else (float("inf"), 0))
        zg, ze = measured[best]
        return best, f"calmest unrelated channel this window (z={zg:.1f} overall, z={ze:.1f} with the event)"
    for c in FALLBACK_ORDER:
        if c in candidates:
            return c, "no coverage cached; first unrelated channel in the fixed order"
    return None, "no unrelated channel left; default control term"


def control_terms(criteria: dict, chosen: list[str], library: dict, end: date) -> dict:
    """{"channel": id | None, "terms": [...], "why": str} for this query. Cached GDELT reads only."""
    from pipeline import fetch_gdelt
    from pipeline.signals import robust_z

    ev, win = criteria["event"], criteria["window"]
    start = end - timedelta(days=win["baseline_days"] + win["recent_days"])
    cands = candidate_channels(ev["terms"], chosen, library)
    scores: dict[str, tuple[float, float] | None] = {}
    for cid in cands:
        terms = library[cid]["search_terms"]
        glob = fetch_gdelt.timeline(terms, None, start, end, "timelinevolraw")
        with_ev = fetch_gdelt.timeline(ev["terms"], terms, start, end, "timelinevolraw")
        scores[cid] = None if not len(glob) else (
            robust_z(glob, win["baseline_days"], win["recent_days"], end),
            robust_z(with_ev, win["baseline_days"], win["recent_days"], end) if len(with_ev) else 0.0)
    cid, why = pick(scores, cands)
    return {"channel": cid, "terms": library[cid]["search_terms"] if cid else DEFAULT_CONTROL, "why": why}
