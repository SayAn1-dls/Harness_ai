# LCC — autonomous coding-agent harness. Standard evaluation interface:
#   export AI_API_KEY="..."   # never written to any file
#   make setup && make run
#
# make run                          interactive: paste a GitHub issue URL, an issue file path, or issue text
# make run ISSUE=<url|file|text> REPO=<path|git-url|owner/name>   one issue, non-interactive
#          BASE=<sha>  (or REPO=<url>@<sha>, or a "Base commit: <sha>" line in the issue) pins the base commit
# make run < issue.md               issue piped on stdin
# make auto REPO=<github-url|path>  no issue: the agent finds bugs/optimizations, fixes them, opens one PR each
#                                   PR=0: local branches only; PR=1: open PRs even on repos you can't push to
# make verify PROOF=outputs/<task>.proof.json REPO=<repo>   replay a fix's proof: tests fail before, pass after
# make test                         unit tests + offline end-to-end benchmark (no API key needed)
# make eval                         live benchmark on the 7 fixture tasks (uses AI_API_KEY)
# make eval-real                    live benchmark on 20 real bug fixes from real repositories (uses AI_API_KEY)
# make ablation                     real tasks with planner / reviewer / intake switched off in turn (uses AI_API_KEY)
# make bench-check                  prove the real benchmark is sound: hidden tests fail before/pass after,
#                                   the reference fix scores 20/20 and a do-nothing model scores 0/20 (no key)
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
BASE     ?=
PROVIDER ?=
TASK     ?=
PR       ?=

.PHONY: help setup run auto verify test eval eval-real ablation bench-check doctor clean distclean

help:
	@sed -n '1,19p' Makefile | sed 's/^# \{0,1\}//'

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
		$(if $(BASE),--base "$(BASE)") \
		$(if $(PROVIDER),--provider "$(PROVIDER)") \
		$(if $(filter 0 no false,$(PR)),--no-pr)

auto:
	@echo "Starting AI Harness (auto mode: find, fix, open pull requests)..."
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@test -n "$(REPO)" || { echo "usage: make auto REPO=<github-url|path> [PR=0]"; exit 2; }
	@AI_API_KEY="$$AI_API_KEY" $(RUN) start --auto --repo "$(REPO)" \
		$(if $(BASE),--base "$(BASE)") \
		$(if $(PROVIDER),--provider "$(PROVIDER)") \
		$(if $(filter 0 no false,$(PR)),--no-pr) \
		$(if $(filter 1 yes true,$(PR)),--pr)

test:
	@echo "Running tests..."
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@$(PY) -m pytest -q
	@echo "Offline end-to-end benchmark (scripted model: no API key, deterministic)..."
	@$(RUN) bench -p scripted --min-resolved 7 --results-dir benchmarks/results

eval:
	@echo "Live benchmark (hidden checks grade every task)..."
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@AI_API_KEY="$$AI_API_KEY" $(RUN) bench $(if $(PROVIDER),-p "$(PROVIDER)") $(foreach t,$(TASK),-t $(t))

verify:
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@$(RUN) verify $(if $(REPO),--repo "$(REPO)") $(if $(PROOF),--proof "$(PROOF)") $(if $(BASE),--base "$(BASE)") $(if $(HEAD),--head "$(HEAD)")

eval-real:
	@echo "Live benchmark on 20 real bug fixes (hidden tests from the real fix commits)..."
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@AI_API_KEY="$$AI_API_KEY" $(RUN) bench --tasks-dir benchmarks/real $(if $(PROVIDER),-p "$(PROVIDER)") $(foreach t,$(TASK),-t $(t))

ablation:
	@echo "Ablation on the real tasks: does each model call earn its tokens? (uses AI_API_KEY)"
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@for v in none planner reviewer intake planner,reviewer,intake; do \
		echo "=== ablate: $$v"; \
		LCC_ABLATE=$$([ $$v = none ] && echo "" || echo $$v) AI_API_KEY="$$AI_API_KEY" $(RUN) bench --tasks-dir benchmarks/real \
			--results-dir benchmarks/results/ablation-$$(echo $$v | tr , -) | grep -E '"(resolved|total_tokens|avg_tokens_per_task)"'; \
	done

bench-check:
	@test -x $(PY) || { echo "error: run 'make setup' first"; exit 1; }
	@$(RUN) bench --validate --tasks-dir benchmarks/real
	@echo "Reference fixes through the full harness (oracle, not a model): expect 20/20"
	@$(RUN) bench -p oracle --tasks-dir benchmarks/real --min-resolved 20 --results-dir benchmarks/results
	@echo "Do-nothing model: expect 0/20"
	@$(RUN) bench -p mock --tasks-dir benchmarks/real --max-iterations 1 --results-dir benchmarks/results | tee /dev/stderr | grep -q '"resolved": 0,'

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
	rm -rf $(VENV) benchmarks/.cache
	-@command -v docker >/dev/null 2>&1 && docker volume ls -q --filter name=lcc-venv- | xargs docker volume rm >/dev/null 2>&1 || true
