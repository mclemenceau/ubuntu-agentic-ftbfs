# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

# Common development and operations tasks. Run `make help` for a list.

UV ?= uv
RUN := $(UV) run
HOST ?= 127.0.0.1
PORT ?= 8047
# Extra arguments for pytest, run and report, e.g.
#   make test ARGS='-k web -x'
#   make run ARGS='--source xfaces --until diagnose'
ARGS ?=
# Vendored Vanilla Framework (see THIRD_PARTY.md) and the Sass compiler
# that builds it. `make vanilla` needs npm and the network.
VANILLA ?= 4.59.0
SASS ?= 1.93.2
VANILLA_OUT := ftbfs/web/static/vanilla
FONTS_URL := https://assets.ubuntu.com/v1/

.DEFAULT_GOAL := help
.PHONY: help sync lint test check serve ingest run status report clean \
	vanilla

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

vanilla: ## Rebuild the vendored Vanilla CSS and Ubuntu fonts (npm)
	set -eu; tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	npm pack --silent --pack-destination "$$tmp" \
		vanilla-framework@$(VANILLA) >/dev/null; \
	mkdir "$$tmp/lib"; tar xzf "$$tmp"/vanilla-framework-*.tgz -C "$$tmp"; \
	mv "$$tmp/package" "$$tmp/lib/vanilla-framework"; \
	rm -rf $(VANILLA_OUT); mkdir -p $(VANILLA_OUT)/fonts; \
	cp "$$tmp/lib/vanilla-framework/LICENSE" $(VANILLA_OUT)/LICENSE; \
	npx --yes sass@$(SASS) --no-source-map --style=compressed \
		--quiet-deps --silence-deprecation=import,global-builtin \
		--load-path="$$tmp/lib" ftbfs/web/vanilla.scss \
		$(VANILLA_OUT)/vanilla.css; \
	grep -o '/static/vanilla/fonts/[^")]*' $(VANILLA_OUT)/vanilla.css \
	| sort -u | while read -r url; do \
		name=$${url##*/}; \
		file=$$(printf '%s' "$$name" | sed 's/%5B/[/g; s/%5D/]/g'); \
		curl -sSfL -o "$(VANILLA_OUT)/fonts/$$file" "$(FONTS_URL)$$name"; \
	done
