# LCC — autonomous coding-agent harness. Standard evaluation interface:
#   export AI_API_KEY="..."   # never written to any file
#   make setup && make run
#
# make run                          interactive: paste a GitHub issue URL, an issue file path, or issue text
# make run ISSUE=<url|file|text> REPO=<path|git-url|owner/name>   one issue, non-interactive
# make run < issue.md               issue piped on stdin
# make test                         unit tests + offline end-to-end benchmark (no API key needed)
# make eval                         live benchmark on the 7 fixture tasks (uses AI_API_KEY)
# make doctor                       check python, git, config and credential presence
# make clean                        remove generated artefacts

SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV   ?= .venv
PY     := $(VENV)/bin/python
# Run from source so the harness never depends on editable-install .pth files.
RUN    := PYTHONPATH=$(CURDIR)/src $(PY) -m lcc
PYTHON ?= $(shell for p in python3.13 python3.12 python3.11 python3; do \
            command -v $$p >/dev/null 2>&1 && $$p -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { echo $$p; break; }; \
          done)

ISSUE    ?=
REPO     ?=
PROVIDER ?=
TASK     ?=

.PHONY: help setup run test eval doctor clean distclean

help:
	@sed -n '1,13p' Makefile | sed 's/^# \{0,1\}//'

setup:
	@echo "Setting up LCC harness..."
	@test -n "$(PYTHON)" || { echo "error: Python >= 3.11 is required (python3.11+ not found on PATH)"; exit 1; }
	@command -v git >/dev/null 2>&1 || { echo "error: git is required"; exit 1; }
	@echo "using $$($(PYTHON) --version) at $$(command -v $(PYTHON))"
	@test -x $(PY) || $(PYTHON) -m venv $(VENV)
	@$(PY) -m pip install --quiet --disable-pip-version-check --upgrade pip
	@$(PY) -m pip install --quiet --disable-pip-version-check -e ".[dev]"
	@# macOS may flag venv .pth files hidden, which Python >= 3.13 skips; keep the `lcc` script importable.
	@if [ "$$(uname)" = "Darwin" ]; then chflags nohidden $(VENV)/lib/python*/site-packages/*.pth 2>/dev/null || true; fi
	@$(RUN) doctor || true
	@echo "Setup complete. Next: make run"

run:
	@echo "Starting AI Harness..."
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@AI_API_KEY="$$AI_API_KEY" $(RUN) start \
		$(if $(ISSUE),--issue "$(ISSUE)" --once) \
		$(if $(REPO),--repo "$(REPO)") \
		$(if $(PROVIDER),--provider "$(PROVIDER)")

test:
	@echo "Running tests..."
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@$(PY) -m pytest -q
	@echo "Offline end-to-end benchmark (scripted model: no API key, deterministic)..."
	@$(RUN) bench -p scripted -t simple_bug -t failure_heavy -t ambiguous --min-resolved 3 --results-dir benchmarks/results

eval:
	@echo "Live benchmark (hidden checks grade every task)..."
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@AI_API_KEY="$$AI_API_KEY" $(RUN) bench $(if $(PROVIDER),-p "$(PROVIDER)") $(foreach t,$(TASK),-t $(t))

doctor:
	@$(RUN) doctor

clean:
	@echo "Removing generated artefacts..."
	rm -rf benchmarks/results outputs workspaces .pytest_cache build dist src/*.egg-info
	rm -rf harness/artifacts harness/cache
	rm -f harness/state/task_state.json harness/state/context_state.json harness/state/agent_state.json \
		harness/state/verification_state.json harness/state/events.jsonl
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

distclean: clean
	rm -rf $(VENV)
