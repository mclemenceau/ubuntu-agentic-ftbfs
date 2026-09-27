# Common development and operations tasks. Run `make help` for a list.

UV ?= uv
RUN := $(UV) run
HOST ?= 127.0.0.1
PORT ?= 8047
# Extra arguments for pytest, run and report, e.g.
#   make test ARGS='-k web -x'
#   make run ARGS='--source xfaces --until diagnose'
ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help sync lint test check serve ingest run status report clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / \
		{printf "  %-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## Install dependencies into .venv
	$(UV) sync

lint: ## Lint with ruff
	$(RUN) ruff check .

test: ## Run the test suite (ARGS passed to pytest)
	$(RUN) pytest -q $(ARGS)

check: lint test ## Lint and test

serve: ## Web UI (HOST/PORT, default 127.0.0.1:8047)
	$(RUN) ftbfs serve --host $(HOST) --port $(PORT)

ingest: ## Fetch, parse and diff the FTBFS page
	$(RUN) ftbfs ingest

run: ## Run the pipeline on the selection (ARGS passed to ftbfs run)
	$(RUN) ftbfs run $(ARGS)

status: ## Runs, per-stage counts, gates, cost
	$(RUN) ftbfs status

report: ## Write investigation.md per source (ARGS passed)
	$(RUN) ftbfs report $(ARGS)

clean: ## Remove Python and tool caches (keeps state/, cache/, work/)
	rm -rf .pytest_cache .ruff_cache
	find . -path ./.venv -prune -o -name __pycache__ -type d \
		-exec rm -rf {} +
