"""Step 5: judge article relevance (plan: Judge relevance; evaluated in eval/metrics.py).

Owner: Person B. One closed question per (article, asset, channel). Only articles
judged "relevant" count as evidence. `keyword_judge` is both the last-resort
fallback and the baseline the model is measured against.
"""
from __future__ import annotations

import re

from reasoning import decision_model

OPTIONS = ["relevant", "not_relevant", "unsure"]


def _contains_any(text: str, terms: list[str]) -> bool:
    text = text.lower()
    return any(re.search(r"\b" + re.escape(t.lower()) + r"\b", text) for t in terms if t)


def keyword_judge(article: dict, asset_terms: list[str], channel_terms: list[str]) -> dict:
    """Baseline: relevant iff the title/snippet mentions an asset term AND a channel or event term."""
    text = f"{article.get('title', '')} {article.get('snippet', '')}"
    hit = _contains_any(text, asset_terms) and _contains_any(text, channel_terms)
    return {"choice": "relevant" if hit else "not_relevant", "confidence": 0.5}


def judge_article(article: dict, asset: dict, channel: dict, event: dict,
                  backends: list | None = None) -> dict:
    """article: title, url, date, domain, snippet (GDELT gives no snippet; may be empty).
    asset: name, owner, country, terms. channel: id, name, search_terms. event: name, terms."""
    question = (
        f"Is this article about {asset['name']} (or its owner {asset.get('owner') or 'unknown'}, "
        f"or its country {asset.get('country') or 'unknown'}) in connection with "
        f"{channel['name']} related to {event['name']}?"
    )
    context = {
        "article": {k: article.get(k, "") for k in ("title", "domain", "date", "snippet")},
        "asset": asset, "channel": {"id": channel["id"], "name": channel["name"]}, "event": event,
    }
    asset_terms = asset.get("terms", []) + asset.get("owner_terms", []) + [t for t in [asset.get("owner")] if t]
    channel_terms = channel.get("search_terms", []) + event.get("terms", [])
    return decision_model.judge(
        question, OPTIONS, context,
        fallback=lambda: keyword_judge(article, asset_terms, channel_terms),
        backends=backends,
    )
