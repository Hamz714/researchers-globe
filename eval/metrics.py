"""Evaluation metrics (plan: Evaluation). Owner: Person B.

    python -m eval.metrics            # or: make eval

1. Judge accuracy: decision model vs keyword baseline, precision and recall on
   eval/labels.csv (positive class = relevant), plus Cohen's kappa on pairs
   labelled by two people. Gold "unsure" pairs are excluded; a predicted
   "unsure" counts as not relevant.
2. Ranking backtests: precision at 10 per method, from eval/backtests/*.json:
       {"event": "...", "affected": [asset ids],
        "rankings": {"system": [...], "keyword_count": [...], "exposure_only": [...], "random": [...]}}
3. Negative controls: queries that should leave almost everything dark.

Writes eval/results.json in the EvalReport shape served by GET /eval.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = HERE / "labels.csv"
CANDIDATES = HERE / "candidates.csv"
BACKTESTS = HERE / "backtests"
RESULTS = HERE / "results.json"
NEGATIVE_CONTROL_MAX_LIT_FRACTION = 0.1  # HAND-SET pass threshold


# ------------------------------------------------------------------ pure metrics (unit-tested)
def precision_recall(gold: list[bool], pred: list[bool]) -> tuple[float | None, float | None]:
    tp = sum(g and p for g, p in zip(gold, pred))
    fp = sum((not g) and p for g, p in zip(gold, pred))
    fn = sum(g and (not p) for g, p in zip(gold, pred))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return precision, recall


def precision_at_k(ranked: list[str], relevant: set[str], k: int = 10) -> float | None:
    top = ranked[:k]
    return sum(a in relevant for a in top) / len(top) if top else None


def cohens_kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    labels = sorted(set(a) | set(b))
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(lbl) / n) * (b.count(lbl) / n) for lbl in labels)
    return None if pe == 1 else (po - pe) / (1 - pe)


# ------------------------------------------------------------------ 1. judge accuracy
def judge_accuracy() -> tuple[list[dict], float | None]:
    if not LABELS.exists() or not CANDIDATES.exists():
        return [], None
    with LABELS.open(newline="") as f:
        labels = list(csv.DictReader(f))
    with CANDIDATES.open(newline="") as f:
        cands = {r["pair_id"]: r for r in csv.DictReader(f)}

    by_pair: dict[str, list[str]] = defaultdict(list)
    for r in labels:
        by_pair[r["pair_id"]].append(r["label"])
    doubles = [v[:2] for v in by_pair.values() if len(v) >= 2]
    kappa = cohens_kappa([d[0] for d in doubles], [d[1] for d in doubles])

    from reasoning.judge import judge_article, keyword_judge
    from reasoning.parser import load_channels

    library = load_channels()
    gold, model_pred, kw_pred = [], [], []
    for pid, labs in by_pair.items():
        g = labs[0]  # first labeller is gold; the overlap set is only for agreement
        if g == "unsure" or pid not in cands:
            continue
        c = cands[pid]
        article = {"title": c["article_title"], "url": c["article_url"], "date": c.get("article_date", ""),
                   "snippet": c.get("snippet", ""), "domain": ""}
        asset = {"name": c["asset_name"], "owner": c.get("asset_owner") or None,
                 "country": c.get("asset_country") or None, "terms": c["asset_terms"].split("|")}
        channel = library[c["channel_id"]]
        event = {"name": c.get("event_name", ""), "terms": [t for t in c.get("event_terms", "").split("|") if t]}
        gold.append(g == "relevant")
        model_pred.append(judge_article(article, asset, channel, event)["choice"] == "relevant")
        kw = keyword_judge(article, asset["terms"] + [t for t in [asset["owner"]] if t],
                           channel["search_terms"] + event["terms"])
        kw_pred.append(kw["choice"] == "relevant")

    rows = []
    for method, pred in (("decision model", model_pred), ("keyword baseline", kw_pred)):
        p, r = precision_recall(gold, pred)
        rows.append({"method": method, "precision": p, "recall": r, "n": len(gold)})
    return rows, kappa


# ------------------------------------------------------------------ 2. backtests
def backtests() -> list[dict]:
    rows = []
    for path in sorted(BACKTESTS.glob("*.json")) if BACKTESTS.exists() else []:
        bt = json.loads(path.read_text())
        for method, ranked in bt["rankings"].items():
            rows.append({"event": bt["event"], "method": method,
                         "precision_at_10": precision_at_k(ranked, set(bt["affected"]), 10)})
    return rows


# ------------------------------------------------------------------ 3. negative controls
NEGATIVE_CONTROL_QUERIES = [
    "Which iron ore mines are exposed to the Premier League title race?",
    # TODO(Person B): same hero query with the event moved to a random earlier date.
]


def negative_controls() -> list[dict]:
    from reasoning.run_query import run_query

    rows = []
    for q in NEGATIVE_CONTROL_QUERIES:
        out = run_query(q)
        lit = sum(r["tier"] in ("high", "medium") for r in out["results"])
        total = len(out["results"]) + out["dark_count"]
        rows.append({"query": q, "lit": lit, "dark": total - lit,
                     "passed": total == 0 or lit / total <= NEGATIVE_CONTROL_MAX_LIT_FRACTION})
    return rows


def main() -> None:
    judge_rows, kappa = judge_accuracy()
    report = {
        "judge": judge_rows,
        "agreement_kappa": kappa,
        "backtests": backtests(),
        "negative_controls": negative_controls(),
        "note": "" if judge_rows else "No labels yet: label pairs with `python -m eval.label_tool`.",
    }
    RESULTS.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nwrote {RESULTS}")


if __name__ == "__main__":
    main()
