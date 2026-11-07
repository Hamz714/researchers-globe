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
`make eval` (writes `eval/results.json`, served at `GET /eval`), `make graph` (mock graph),
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

## Hand-set numbers (record every change here)

| Where | Value | Why |
| --- | --- | --- |
| `reasoning/scoring.py` | a=0.5, b=0.3, c=0.4, d=1.0, **k=3.0** | k=3 keeps an asset with zero evidence below the medium tier |
| `reasoning/channels.json` | channel weights 0.5–0.9 | first guess |
| `pipeline/signals.py` | tone std floor 0.25, placebo threshold z≥2 | the plan gives none |
| `eval/metrics.py` | negative control passes if ≤10% lit | first guess |

## Open questions found while scaffolding

1. **Dark dots.** The contract gives `dark_count`, not dark positions, so the globe can't draw
   grey dots yet. Option: add a `GET /assets` endpoint (no contract change). Decide before 03:00.
2. **Where channel→country `TOUCHES` edges come from.** They depend on the event (tariffs touch
   the US for the midterms). For now presets carry them, and `channels.json` has
   `default_touches` for free-text questions.
3. **Exposure saturates on the mock graph.** Noisy-OR over many overlapping paths puts most
   mines at 0.5–1.0 and leaves 3 of 20 dark. Consider de-duplicating paths or lowering weights.
4. GDELT article lists have **no snippet**, so the judge sees title, domain and date only.
5. Small additions to the plan's graph schema: `port -LOCATED_IN-> country` (needed for the
   plan's own example path) and channels touching `port` (for the storm preset).
