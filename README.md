# Researcher's Globe

Ask a question like *"which iron ore mines are exposed to the 2026 US midterms?"* and the
matching assets light up on a globe. Each one comes with a score, the path that connects
it to the event, and links to the evidence.

We rank assets by **exposure plus current evidence**: how an asset connects to the event,
and whether news and attention around it are unusually high right now. We do not predict
impact size, and we never assert impact without a linked source. Call it a **score**,
never a probability.

HackTheBurgh 2026 · HRT, G-Research and Metaview challenges · submission **12:00 Sunday**.
Data: [Global Energy Monitor](https://globalenergymonitor.org/projects/global-iron-ore-mines-tracker) (CC BY 4.0), GDELT, Wikimedia.

## Setup (everyone, once)

Needs Python 3.11, Node 22 and git.

```bash
git clone https://github.com/Hamz714/researchers-globe.git
cd researchers-globe
make setup            # .venv + pip install + npm ci + copies .env.example to .env
git checkout <your branch>   # data (A) | reasoning (B) | globe (C)
make test             # should be green before you change anything
```

Windows without `make` (PowerShell):

```powershell
py -3.11 -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt
cd frontend; npm ci; cd ..; copy .env.example .env
.venv\Scripts\python -m pytest
```

## Run it

```bash
make backend    # terminal 1: FastAPI on http://localhost:8000 (docs at /docs)
make frontend   # terminal 2: open http://localhost:5173
```

The backend serves **mock data** until you set `USE_PIPELINE=1` in `.env`; then `POST /query`
runs `reasoning.run_query.run_query`. Set `GLOBE_OFFLINE=1` to read only from the cache, and use it on stage.

Other targets: `make test`, `make fetch-sample` (one cached GDELT + Wikipedia call),
`make eval` (writes `eval/results.json`, served at `GET /eval`), `make graph` (rebuild the real graph), `make presets` (precompute preset results),
`make pipeline-query` (runs the hero query through the real pipeline offline).

## Who owns what

| Folder | Owner | What's there |
| --- | --- | --- |
| `pipeline/` | **A**, data and signals | `graph.py` (nodes/edges tables), `fetch_gdelt.py`, `fetch_wiki.py` (cached), `signals.py` (robust z, tone shift, attention burst, placebo), `curated/` tables |
| `reasoning/` | **B**, reasoning | `channels.json`, `decision_model.py` (Jev adapter + fallbacks), `parser.py`, `judge.py`, `exposure.py` (noisy-OR), `scoring.py`, `run_query.py` |
| `eval/` | **B**, evaluation | `label_tool.py` (labelling CLI), `metrics.py` (P/R vs keywords, P@10, negative controls, kappa) |
| `backend/` | **C** | `app.py` (FastAPI), `schemas.py` (**the contract**) |
| `frontend/` | **C** | Vite + TypeScript + MapLibre globe |
| `data/mock/` | all | mock result JSON and 20 mock mines, **not real data** |
| `data/presets.json` | C (B reads it) | the four presets, also the parser's fallback |

## The contract

`backend/schemas.py` is the result JSON from the plan, field for field; `frontend/src/types.ts`
mirrors it. `tests/test_schemas.py` fails if a field is added or renamed. **Nobody changes it
without telling the other two**, and any change updates both files in the same commit.

`GET /presets`, `GET /asset/{id}` and `GET /eval` have shapes we proposed (bottom of
`schemas.py`); the plan only describes them in words.

## Branches and merging

- `main` is always demo-able. Work on your own branch: `data` (A), `reasoning` (B), `globe` (C).
- Stay in your own folders. Touching someone else's folder means telling them first.
- **Merge at every gate**: pull `main` into your branch, `make test`, open a PR to `main`,
  another person merges it. CI (pytest + frontend type check) must be green.
- C builds against mock JSON from minute one. B builds against the mock graph until A's tables land.
- Blocked for 30 minutes: tell the team, then cut or use the fallback (plan: Risks).

## Gates (from the plan)

| Time | Gate |
| --- | --- |
| **00:00** | 5-minute check-in: mines loaded? decision model answering? globe rendering? Anything red gets its fallback now. |
| **03:00** | Hero query lights up **real** mines end to end. A: graph tables + signals for the hero candidates. B: `/query` pipeline working on the hero query. C: globe showing real `/query` results. |
| 03:00–06:00 | A sleeps (start the backtest-data pull first). C sleeps 03:30–06:30. |
| 06:30–09:00 | B sleeps (judge, labelled set and eval harness finished first, so A can run it from 06:00). |
| **08:30** | A: backtest data cached, placebo checks. B: judge accuracy, backtest results, negative controls. C: all panels, presets, live layers, slides draft. |
| **10:30** | **Scope freeze, no exceptions.** Backup video recorded. |
| **12:00** | Submission. Judging 12:30. |

After 03:00 every phase adds depth or polish, never new scope. A missed gate triggers its
fallback, not extra hours. Everyone labels ~35 article pairs in phase 3
(`python -m eval.label_tool --labeller <name>`).

## Data (real, committed)

- **Mines:** `data/gem/` holds the GEM Global Iron Ore Mines Tracker, Sep 2026 (V1), CC BY 4.0.
  There are 701 operating mines with parent owners and shares. `make graph` rebuilds `data/graph/`.
- **Curated tables:** `pipeline/curated/` has 37 export ports, trade shares (UN Comtrade via WITS, 2023)
  and company mappings (listing country, exact Wikipedia title, search terms). Every row has a `source_url`.
- **Cache:** `cache/` is committed, so everyone runs offline on identical data.
- **GDELT from the venue:** GDELT answers 429 *"Please limit requests to one every 5 seconds"* to
  every request, probably because the whole venue shares one IP. Fetch from a phone hotspot:
  `make fetch-all` (plan, fetch, ingest in rounds until nothing is missing, then precompute presets).

## Decision model (Jev)

Jev is TypeSafe's decision model. It answers typed questions (yes/no, choice, score) with
probabilities and generates no text. We call it through OpenRouter's Decisions API
(`POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.14`):

| Step | Function | Backend order |
| --- | --- | --- |
| 1. Parse the question | `parse()` | fallback LLM (Jev can't write JSON), then nearest preset |
| 2. Channels and touched countries | `select()` | Jev (one yes/no per option), then fallback LLM, then preset or `default_touches` |
| 5. Relevance judge | `judge()` | Jev (choice question), then fallback LLM, then keyword match |

The fallback LLM is `anthropic/claude-haiku-5.5` via OpenRouter, at temperature 0. One OpenRouter key
works for both; put it in `.env` as `JEV_API_KEY` and `FALLBACK_LLM_API_KEY`. Jev has no temperature,
and repeat calls vary by about 0.02, so every validated answer is cached in `cache/llm/` and replayed.

## Presets (`data/presets.json`)

1. **Hero:** iron ore mines exposed to the 2026 US midterms (3 Nov).
2. **Weather:** mines exporting through ports hit by **Cyclone Murasu**. It made landfall near Ongole,
   Andhra Pradesh, on 3 Nov 2026 and touches the Krishnapatnam, Visakhapatnam and Gangavaram ports.
3. **Labour:** the **BHP Port Hedland ports wage dispute**. There were strikes in Jul and Aug 2026, and arbitration
   was sought on 15 Sep with no deal reported since. No iron ore strike was reported in the week before the hackathon.
4. **Negative control:** the Premier League title race. It should stay dark.

Each preset lists its sources. TOUCHES weights are measured from coverage at query time.

## Decisions taken on 8 Nov (were open questions)

1. **Dark dots:** `GET /assets` returns every mine, and the globe draws the unlit ones grey.
   The `/query` contract is unchanged.
2. **TOUCHES edges are measured, not hand-set** (`reasoning/touches.py`). For each chosen channel we pull up to
   250 GDELT articles on (event terms AND channel terms) since the day before the event, and count how many
   titles name each node the channel may touch: country names and demonyms, curated companies, ports.
   weight = titles naming the node / titles naming the most-named node (≥2 titles, ≥3% of coverage, top 8).
   The reason text says so, e.g. "United States is named in 41 of 180 ... articles on tariffs".
   If there is no coverage, the decision model picks, then the preset's `fallback_touches`, then `default_touches`.
3. **Exposure no longer saturates:** edges are walked in their own direction, so a Chinese mine isn't
   exposed to Brazil just because China imports Brazilian ore. Only the strongest path per
   (touched node, channel) counts in the noisy-OR. All paths are still shown.
4. **Rate-limit budget:** all 50 candidates are returned. GDELT evidence is pulled for the top 10
   (`EVIDENCE_K`, was 25), and the placebo for the top 10 (`PLACEBO_K`).
5. **Short GDELT queries:** an asset's queries AND its names with the event terms plus the terms of the
   channel on its strongest path only (`evidence_terms()` in `run_query.py`, also used by `GET /asset/{id}`).
   ANDing all chosen channels' terms made ~300-character queries that GDELT rejects ("too short or too long").

## Hand-set numbers (record every change here)

| Where | Value | Why |
| --- | --- | --- |
| `reasoning/scoring.py` | a=0.5, b=0.3, c=0.4, d=1.0, **k=3.0** | k=3 keeps an asset with zero evidence below the medium tier |
| `reasoning/channels.json` | channel weights 0.5–0.9 | first guess |
| `reasoning/touches.py` | ≥2 titles, ≥3% share, top 8 touched nodes | keeps one-off mentions out |
| `data/presets.json` | `fallback_touches` | only used offline with an empty cache |
| `pipeline/signals.py` | tone std floor 0.25, placebo threshold z≥2 | the plan gives none |
| `reasoning/run_query.py` | EVIDENCE_K=10 (was 25), PLACEBO_K=10, control term "copper" | GDELT rate limit: ~120 requests for all presets instead of ~250 |
| `eval/metrics.py` | negative control passes if ≤10% lit | first guess |

## Still open

- GDELT articles have **no snippet**, so the judge sees title, domain and date only.
- Small additions to the plan's graph schema: `port -LOCATED_IN-> country`, and channels may touch `port`.
- Pipeline mode in `GET /asset/{id}` still returns a mock series. It needs the cached series (A+C).
