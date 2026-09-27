# LCC — autonomous coding-agent harness.
#
#   export AI_API_KEY="..."     the only credential; never written to a file (or: make run AI_API_KEY=...)
#   make setup && make run
#
# Fixing code
#   make run                          interactive: paste a GitHub issue URL, an issue file, issue text, or a repo link
#   make run ISSUE=<url|file|text> REPO=<path|url|owner/name> [BASE=<sha>] [PR=0|1]
#                                     one issue; opens a draft PR when the fix is proven (PR=0: keep it local)
#   make run < issue.md               issue piped on stdin
#   make auto REPO=<url|path> [PR=0|1]
#                                     no issue: read the whole repo, list every issue, fix and speed up with proof,
#                                     one draft PR per fix (PR=0: local only; PR=1: also on repos you can't push to)
#   make verify PROOF=<file> [REPO=<repo>]   or   make verify BASE=<sha> HEAD=<sha|branch> [REPO=<repo>]
#                                     replay a fix's proof: its tests fail on the old code and pass with the fix
#
# Checking the harness
#   make test                         all tests + offline benchmark (no API key needed)
#   make lint                         ruff on src/ and tests/
#   make eval                         live score on the 7 practice tasks            (uses AI_API_KEY)
#   make eval-real                    live score on the 20 real bug fixes           (uses AI_API_KEY)
#   make ablation                     real tasks with planner / reviewer / intake off in turn (uses AI_API_KEY)
#   make bench-check                  prove the real benchmark: 20/20 valid, real fix 20/20, do-nothing 0/20
#
# Housekeeping
#   make doctor                       python, git, config, key, and one tiny model call
#   make clean                        remove generated files
#   make distclean                    also remove the venv, benchmark cache and sandbox volumes
#
# Options: PROVIDER=deepseek|qwen|qwen-cn|mock|...   TASK="id1 id2" (eval)   VENV=.venv   PYTHON=python3.12

SHELL       := /bin/bash
.SHELLFLAGS := -o pipefail -c
.DEFAULT_GOAL := help

VENV   ?= .venv
PY     := $(VENV)/bin/python
# Run from source, so the harness never depends on editable-install .pth files.
RUN    := PYTHONPATH="$(CURDIR)/src" "$(PY)" -m lcc
PYTHON ?= $(shell for p in python3.13 python3.12 python3.11 python3; do \
            command -v $$p >/dev/null 2>&1 && $$p -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { echo $$p; break; }; \
          done)

ISSUE    ?=
REPO     ?=
BASE     ?=
HEAD     ?=
PROOF    ?=
PROVIDER ?=
TASK     ?=
PR       ?=

# Credentials and model settings given on the command line (make run AI_API_KEY=...) reach the harness too.
export AI_API_KEY LCC_PROVIDER LCC_MODEL LCC_BASE_URL LCC_SANDBOX LCC_MUTATION LCC_ABLATE

PR_FLAG   = $(if $(filter 0 no false,$(PR)),--no-pr,$(if $(filter 1 yes true,$(PR)),--pr))
NEED_VENV = @test -x "$(PY)" || { echo "error: run 'make setup' first"; exit 1; }

.PHONY: help setup run auto verify test lint eval eval-real ablation bench-check doctor clean distclean

help:  ## this text: the comment block at the top of the file
	@awk '/^#/ { sub(/^# ?/, ""); print; next } { exit }' $(firstword $(MAKEFILE_LIST))

setup:
	@echo "Setting up LCC..."
	@test -n "$(PYTHON)" || { echo "error: Python >= 3.11 is required (none found on PATH; set PYTHON=...)"; exit 1; }
	@command -v git >/dev/null 2>&1 || { echo "error: git is required"; exit 1; }
	@if [ -e "$(VENV)" ] && ! "$(PY)" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then \
		echo "recreating $(VENV): its python is missing, broken or older than 3.11"; rm -rf "$(VENV)"; fi
	@test -x "$(PY)" || { echo "creating $(VENV) with $$($(PYTHON) --version)"; "$(PYTHON)" -m venv "$(VENV)"; }
	@echo "using $$("$(PY)" --version) in $(VENV)"
	@"$(PY)" -m pip install --quiet --disable-pip-version-check --upgrade pip
	@"$(PY)" -m pip install --quiet --disable-pip-version-check -e ".[dev]"
	@# macOS can mark venv .pth files hidden, which Python >= 3.13 skips; keep the `lcc` script importable.
	@if [ "$$(uname)" = "Darwin" ]; then chflags nohidden "$(VENV)"/lib/python*/site-packages/*.pth 2>/dev/null || true; fi
	@$(RUN) doctor || true
	@echo "Setup complete. Next: make run"

run:
	$(NEED_VENV)
	@echo "Starting LCC..."
	@$(RUN) start \
		$(if $(ISSUE),--issue "$(ISSUE)" --once) \
		$(if $(REPO),--repo "$(REPO)") \
		$(if $(BASE),--base "$(BASE)") \
		$(if $(PROVIDER),--provider "$(PROVIDER)") \
		$(PR_FLAG)

auto:
	$(NEED_VENV)
	@test -n "$(REPO)" || { echo "usage: make auto REPO=<github-url|path> [PR=0|1] [PROVIDER=...]"; exit 2; }
	@echo "Starting LCC in auto mode (read the repo, list issues, fix with proof, open draft PRs)..."
	@$(RUN) start --auto --repo "$(REPO)" \
		$(if $(BASE),--base "$(BASE)") \
		$(if $(PROVIDER),--provider "$(PROVIDER)") \
		$(PR_FLAG)

verify:
	$(NEED_VENV)
	@test -n "$(PROOF)$(BASE)" || { echo "usage: make verify PROOF=outputs/<task>.proof.json [REPO=<repo>]"; \
		echo "   or: make verify BASE=<sha> HEAD=<sha|branch> [REPO=<repo>]"; exit 2; }
	@$(RUN) verify $(if $(REPO),--repo "$(REPO)") $(if $(PROOF),--proof "$(PROOF)") \
		$(if $(BASE),--base "$(BASE)") $(if $(HEAD),--head "$(HEAD)")

test:
	$(NEED_VENV)
	@echo "Running tests..."
	@"$(PY)" -m pytest -q
	@echo "Offline end-to-end benchmark (scripted model: no API key, deterministic)..."
	@$(RUN) bench -p scripted --min-resolved 7 --results-dir benchmarks/results

lint:
	$(NEED_VENV)
	@"$(PY)" -m ruff check src tests --select F,E9,PLE,B --ignore B008

eval:
	$(NEED_VENV)
	@echo "Live benchmark on the 7 practice tasks (hidden checks grade every task)..."
	@$(RUN) bench $(if $(PROVIDER),-p "$(PROVIDER)") $(foreach t,$(TASK),-t $(t))

eval-real:
	$(NEED_VENV)
	@echo "Live benchmark on 20 real bug fixes (hidden tests from the real fix commits)..."
	@$(RUN) bench --tasks-dir benchmarks/real $(if $(PROVIDER),-p "$(PROVIDER)") $(foreach t,$(TASK),-t $(t))

ablation:
	$(NEED_VENV)
	@echo "Ablation on the real tasks: does each model call earn its tokens?"
	@for v in none planner reviewer intake planner,reviewer,intake; do \
		echo "=== switched off: $$v"; \
		LCC_ABLATE="$$([ "$$v" = none ] || echo "$$v")" $(RUN) bench --tasks-dir benchmarks/real \
			$(if $(PROVIDER),-p "$(PROVIDER)") --results-dir "benchmarks/results/ablation-$${v//,/-}" \
			| grep -E '"(resolved|total_tokens|avg_tokens_per_task)"' || echo "   (this variant failed; see benchmarks/results)"; \
	done

bench-check:
	$(NEED_VENV)
	@$(RUN) bench --validate --tasks-dir benchmarks/real
	@echo "Reference fixes through the whole harness (oracle, not a model): expect 20/20"
	@$(RUN) bench -p oracle --tasks-dir benchmarks/real --min-resolved 20 --results-dir benchmarks/results
	@echo "Do-nothing model: expect 0/20"
	@out="$$($(RUN) bench -p mock --tasks-dir benchmarks/real --max-iterations 1 --results-dir benchmarks/results)"; \
		echo "$$out" | tail -16; \
		echo "$$out" | grep -q '"resolved": 0,' || { echo "error: a do-nothing model resolved tasks; the grader is broken"; exit 1; }

doctor:
	$(NEED_VENV)
	@$(RUN) doctor

clean:
	@echo "Removing generated files..."
	@rm -rf benchmarks/results outputs workspaces .pytest_cache build dist src/*.egg-info
	@rm -rf harness/artifacts harness/cache
	@rm -f harness/state/task_state.json harness/state/context_state.json harness/state/agent_state.json \
		harness/state/verification_state.json harness/state/events.jsonl
	@find . -path "./$(VENV)" -prune -o -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

distclean: clean
	@echo "Removing the venv, the benchmark cache and sandbox volumes..."
	@rm -rf "$(VENV)" benchmarks/.cache
	@if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then \
		for v in $$(docker volume ls -q --filter name=lcc-venv-); do docker volume rm "$$v" >/dev/null; done; fi
