"""Which nodes does a channel touch for THIS event, and how strongly? Measured, not hand-set.

Owner: Person B.

For each chosen channel we pull the event's coverage from GDELT:

    (event terms) AND (channel terms), from the day before the event to today, up to 250 articles

and count, for every node of a type the channel may touch (channels.json `touches`),
how many article titles name it (country names and demonyms, company search terms,
port names). Then

    touch weight(node) = titles naming node / titles naming the most-named node

keeping nodes named in at least MIN_TITLES titles from at least MIN_DOMAINS different
sites and at least MIN_SHARE of the coverage, top MAX_TOUCHES. Site names are cut off
the end of titles first ("... - Thailand Business News"), so an outlet's name never counts. Example: midterms + tariffs coverage names the US most,
so US = 1.0, and China, Canada... get weights in proportion to how often they appear.

This is one cached GDELT request per (query, channel), it is reproducible, and the UI
can show the counts ("named in 41 of 180 articles"). If GDELT has nothing (offline,
cache miss), the caller falls back to the decision model, then preset/default touches.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from pipeline import fetch_gdelt
from pipeline.graph import Graph

MAX_ARTICLES = 250
MIN_TITLES = 2
MIN_DOMAINS = 2
MIN_SHARE = 0.03
MAX_TOUCHES = 8

# Demonyms and short names, so "Chinese steel" counts for China. Keys are ISO2.
COUNTRY_ALIASES = {
    "US": ["United States", "U.S.", "US", "USA", "America", "American", "Americans", "Washington"],
    "CN": ["China", "Chinese", "Beijing"], "CA": ["Canada", "Canadian", "Ottawa"],
    "MX": ["Mexico", "Mexican"], "BR": ["Brazil", "Brazilian"], "AU": ["Australia", "Australian"],
    "IN": ["India", "Indian"], "JP": ["Japan", "Japanese", "Tokyo"], "KR": ["South Korea", "Korean", "Seoul"],
    "RU": ["Russia", "Russian", "Moscow", "Kremlin"], "UA": ["Ukraine", "Ukrainian", "Kyiv"],
    "ZA": ["South Africa", "South African"], "DE": ["Germany", "German", "Berlin"],
    "GB": ["Britain", "British", "UK", "United Kingdom"], "SE": ["Sweden", "Swedish"],
    "NO": ["Norway", "Norwegian"], "IR": ["Iran", "Iranian", "Tehran"], "TR": ["Turkey", "Turkish", "Türkiye"],
    "NL": ["Netherlands", "Dutch"], "FR": ["France", "French"], "IT": ["Italy", "Italian"],
    "VN": ["Vietnam", "Vietnamese"], "ID": ["Indonesia", "Indonesian"], "MY": ["Malaysia", "Malaysian"],
    "CL": ["Chile", "Chilean"], "PE": ["Peru", "Peruvian"], "MN": ["Mongolia", "Mongolian"],
    "KZ": ["Kazakhstan", "Kazakh"], "GN": ["Guinea", "Guinean"], "MR": ["Mauritania", "Mauritanian"],
    "SL": ["Sierra Leone"], "LR": ["Liberia", "Liberian"], "SA": ["Saudi Arabia", "Saudi"],
    "KP": ["North Korea", "North Korean", "Pyongyang"], "VE": ["Venezuela", "Venezuelan"],
}


def _patterns(graph: Graph, node_types: set[str]) -> dict[str, re.Pattern]:
    out = {}
    e = graph.edges
    curated_companies = set(e.loc[e["type"] == "LISTED_IN", "src"])  # skip obscure owners with generic names
    for n in graph.nodes.itertuples():
        if n.type not in node_types or (n.type == "company" and n.id not in curated_companies):
            continue
        if n.type == "country":
            terms = COUNTRY_ALIASES.get(n.country, [n.name])
        else:
            terms = [t for t in str(n.search_terms or n.name).split("|") if len(t) >= 3]
        if not terms:
            continue
        # Case-sensitive on short all-caps aliases (US, UK) so "us" the pronoun doesn't count.
        parts = [re.escape(t) if (t.isupper() and len(t) <= 4) else "(?i:" + re.escape(t) + ")" for t in terms]
        out[n.id] = re.compile(r"(?<![\w.])(?:" + "|".join(parts) + r")(?![\w])")
    return out


_SITE_SUFFIX = re.compile(r"\s+(?:-|\||–|—|::)\s+[^-|–—:]{2,60}$")


def clean_title(title: str) -> str:
    """Drop a trailing ' - Site Name' / ' | Site Name' (GDELT titles often carry the outlet's name)."""
    title = title.strip()
    cut = _SITE_SUFFIX.sub("", title)
    return cut if len(cut.split()) >= 3 else title  # keep short titles whole ("Rio Tinto - BHP merger talk")


def count_mentions(articles: list[dict], patterns: dict[str, re.Pattern]) -> dict[str, tuple[int, int]]:
    """node_id -> (titles naming it, distinct domains among those). Articles: dicts with title, domain."""
    out = {}
    for nid, p in patterns.items():
        hits = [a for a in articles if p.search(clean_title(a.get("title", "")))]
        if hits:
            out[nid] = (len(hits), len({a.get("domain", "") for a in hits}))
    return out


def weights_from_counts(counts: dict[str, tuple[int, int]], n_titles: int) -> list[list]:
    if not counts or n_titles == 0:
        return []
    counts = {nid: c for nid, (c, d) in counts.items() if d >= MIN_DOMAINS}
    if not counts:
        return []
    top = max(counts.values())
    keep = [(nid, c) for nid, c in counts.items() if c >= MIN_TITLES and c / n_titles >= MIN_SHARE]
    keep.sort(key=lambda kv: (-kv[1], kv[0]))
    return [[nid, round(c / top, 3), c] for nid, c in keep[:MAX_TOUCHES]]


def measured_touches(criteria: dict, channel: dict, graph: Graph, end: date) -> tuple[list[list], int]:
    """Returns ([[node_id, weight, n_titles_naming_it], ...], n_articles). Empty if no coverage cached."""
    ev = criteria["event"]
    try:
        start = date.fromisoformat(ev["date"]) - timedelta(days=1)
    except ValueError:
        start = end - timedelta(days=criteria["window"]["recent_days"])
    start = max(start, end - timedelta(days=85))  # GDELT DOC covers ~3 months
    arts = fetch_gdelt.articles(ev["terms"], channel["search_terms"], start, end, max_records=MAX_ARTICLES)
    counts = count_mentions(arts, _patterns(graph, set(channel["touches"])))
    return weights_from_counts(counts, len(arts)), len(arts)
