# Researcher's Globe. Run `make help`. Windows: use Git Bash or WSL, or the raw commands in README.
PYTHON ?= python3.11
VENV   := .venv
PY     := $(VENV)/bin/python
ifeq ($(OS),Windows_NT)
PY     := $(VENV)/Scripts/python.exe
endif

.PHONY: gev gev-apply help setup backend frontend test fetch-sample eval graph pipeline-query presets fetch-plan fetch-run fetch-ingest fetch-all

help:
	@echo "setup         create .venv, install Python + frontend deps, copy .env.example"
	@echo "backend       FastAPI on http://localhost:8000 (mock data unless USE_PIPELINE=1)"
	@echo "frontend      Vite dev server on http://localhost:5173"
	@echo "test          pytest + frontend type check"
	@echo "fetch-sample  one cached GDELT call and one Wikipedia call"
	@echo "eval          compute evaluation tables into eval/results.json"
	@echo "graph         rebuild the real entity graph from the GEM file into data/graph/"
	@echo "presets       precompute all preset results into data/preset_results/ (offline)"
	@echo "fetch-plan    list GDELT/Wikipedia URLs missing from cache -> data/jobs/jobs.json"
	@echo "fetch-run     fetch those URLs (throttled; use a phone hotspot if the venue IP is blocked)"
	@echo "fetch-ingest  load data/jobs/results.json into cache/"
	@echo "fetch-all     plan+run+ingest in rounds until nothing is missing, then presets (~30-40 min)"
	@echo "gev           God's Eye View demo globe on http://localhost:4173 (needs \`make backend\` too)"
	@echo "gev-apply     (re)build .gev/: upstream gods-eye-view @$(GEV_COMMIT) + our overlay + patch"

setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	cd frontend && npm ci
	@test -f .env || cp .env.example .env

backend:
	$(PY) -m uvicorn backend.app:app --reload --port 8000

frontend:
	cd frontend && npm run dev

test:
	$(PY) -m pytest
	cd frontend && npm run typecheck

fetch-sample:
	GLOBE_OFFLINE=0 $(PY) -m pipeline.fetch_gdelt
	GLOBE_OFFLINE=0 $(PY) -m pipeline.fetch_wiki

eval:
	GLOBE_OFFLINE=1 $(PY) -m eval.metrics

graph:
	$(PY) -m pipeline.graph --gem data/gem/gem_iron_ore_2026-09.xlsx

presets:
	GLOBE_OFFLINE=1 $(PY) -m reasoning.precompute

fetch-plan:
	$(PY) -m pipeline.browser_fetch plan

fetch-run:
	$(PY) -m pipeline.browser_fetch run

fetch-all:
	$(PY) -m pipeline.browser_fetch all
	GLOBE_OFFLINE=1 $(PY) -m reasoning.precompute

fetch-ingest:
	$(PY) -m pipeline.browser_fetch ingest data/jobs/results.json

pipeline-query:
	GLOBE_OFFLINE=1 $(PY) -m reasoning.run_query

# God's Eye View demo globe. .gev/ is a throwaway upstream checkout (gitignored): only our overlay
# (gods-eye-view/) and the patch are committed. gev-apply resets .gev/ to the pinned commit every
# time, so local edits inside .gev/ are discarded: edit gods-eye-view/ instead.
GEV_REPO   := https://github.com/bilawalsidhu/gods-eye-view.git
GEV_COMMIT := 52187c9
GEV_DIRS   := server src tools public output

gev-apply:
	@test -d .gev/.git || git clone $(GEV_REPO) .gev
	cd .gev && git checkout -q $(GEV_COMMIT) && git reset -q --hard $(GEV_COMMIT) \
	  && git clean -fdq -e node_modules -e .env -e .gev-cache
	for d in $(GEV_DIRS); do [ -d gods-eye-view/$$d ] && cp -R gods-eye-view/$$d .gev/; done; true
	cd .gev && git apply ../gods-eye-view/gods-eye-view-integration.patch
	@# GEV reads provider keys from its own .env (Vite loadEnv): pass on the AIS key only.
	@grep -E '^AISSTREAM_API_KEY=' .env > .gev/.env 2>/dev/null || touch .gev/.env
	cd .gev && (test -d node_modules || npm ci)

gev: gev-apply
	cd .gev && npm run dev
