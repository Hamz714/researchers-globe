"""Minimal CLI for hand-labelling (article, asset, channel) pairs (plan: Evaluation 1).

Owner: Person B; everyone labels ~35 pairs in phase 3.

    python -m eval.label_tool --labeller hamzah
    python -m eval.label_tool --labeller hamzah --candidates eval/candidates.csv --overlap

Reads candidate pairs from a CSV, shows one at a time, and appends your label to
eval/labels.csv. Resumable: pairs you've already labelled are skipped. Keys:
  r = relevant   n = not relevant   u = unsure   s = skip   q = quit

Splitting the work: by default each labeller gets a disjoint slice (pair_id hash
mod 3). `--overlap` labels the shared 20-pair overlap set used for agreement.

TODO(Person B): build eval/candidates.csv from cached GDELT articles for the
hero-query candidates (eval/candidates_example.csv shows the columns).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = HERE / "labels.csv"
LABEL_COLUMNS = ["pair_id", "labeller", "label", "asset_id", "channel_id", "article_url", "timestamp"]
KEYS = {"r": "relevant", "n": "not_relevant", "u": "unsure"}
OVERLAP_SIZE = 20
N_LABELLERS = 3


def _bucket(pair_id: str) -> int:
    return int(hashlib.sha1(pair_id.encode()).hexdigest(), 16)


def overlap_ids(pair_ids: list[str]) -> set[str]:
    return set(sorted(pair_ids, key=_bucket)[:OVERLAP_SIZE])


def already_labelled(labeller: str) -> set[str]:
    if not LABELS.exists():
        return set()
    with LABELS.open(newline="") as f:
        return {r["pair_id"] for r in csv.DictReader(f) if r["labeller"] == labeller}


def append_label(row: dict) -> None:
    new = not LABELS.exists()
    with LABELS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labeller", required=True, help="your name, e.g. hamzah")
    ap.add_argument("--candidates", type=Path, default=HERE / "candidates.csv")
    ap.add_argument("--slot", type=int, choices=range(N_LABELLERS), help="0, 1 or 2 (default: from name)")
    ap.add_argument("--overlap", action="store_true", help="label the shared agreement set instead")
    args = ap.parse_args(argv)

    if not args.candidates.exists():
        print(f"{args.candidates} not found. Try --candidates eval/candidates_example.csv")
        return 1
    with args.candidates.open(newline="") as f:
        pairs = list(csv.DictReader(f))
    ids = [p["pair_id"] for p in pairs]
    shared = overlap_ids(ids)
    slot = args.slot if args.slot is not None else _bucket(args.labeller) % N_LABELLERS
    if args.overlap:
        todo = [p for p in pairs if p["pair_id"] in shared]
    else:
        todo = [p for p in pairs if p["pair_id"] not in shared and _bucket(p["pair_id"]) % N_LABELLERS == slot]
    done = already_labelled(args.labeller)
    todo = [p for p in todo if p["pair_id"] not in done]
    print(f"{len(todo)} pairs to label for {args.labeller} ({'overlap set' if args.overlap else f'slot {slot}'}).")

    for i, p in enumerate(todo, 1):
        print("\n" + "=" * 72)
        print(f"[{i}/{len(todo)}] pair {p['pair_id']}")
        print(f"ARTICLE : {p['article_title']}")
        print(f"          {p.get('article_date', '')}  {p['article_url']}")
        if p.get("snippet"):
            print(f"SNIPPET : {p['snippet']}")
        print(f"ASSET   : {p['asset_name']} (owner: {p.get('asset_owner', '?')}, country: {p.get('asset_country', '?')})")
        print(f"CHANNEL : {p['channel_id']}    EVENT: {p.get('event_name', '')}")
        print("Is this article about this asset (or its owner or country) in connection with this channel?")
        while True:
            k = input("[r]elevant / [n]ot / [u]nsure / [s]kip / [q]uit > ").strip().lower()
            if k in KEYS or k in {"s", "q"}:
                break
        if k == "q":
            break
        if k == "s":
            continue
        append_label({"pair_id": p["pair_id"], "labeller": args.labeller, "label": KEYS[k],
                      "asset_id": p["asset_id"], "channel_id": p["channel_id"], "article_url": p["article_url"],
                      "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    print(f"\nSaved to {LABELS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
