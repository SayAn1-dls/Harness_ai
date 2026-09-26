from __future__ import annotations

HARNESS_DIR = "harness"
STATE_DIR = "state"
ARTIFACTS_DIR = "artifacts"
DOCS_DIR = "docs"
RESEARCH_DIR = "research"
CACHE_DIR = "cache"

TASK_STATE_FILE = "task_state.json"
CONTEXT_STATE_FILE = "context_state.json"
AGENT_STATE_FILE = "agent_state.json"
VERIFICATION_STATE_FILE = "verification_state.json"
EVENTS_FILE = "events.jsonl"

MAX_AGENT_DEPTH = 1
DEFAULT_MAX_PARALLEL_AGENTS = 3
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_TOKEN_BUDGET = 100_000
DEFAULT_RUNTIME_SECONDS = 1800

DEFAULT_TOOL_BUDGET = {
    "max_search_calls": 20,
    "max_file_reads": 50,
    "max_shell_calls": 40,
    "max_full_test_runs": 5,
    "max_iterations": DEFAULT_MAX_ITERATIONS,
}

CONTEXT_SCORE_GATE = 75
INTAKE_PASS_SCORE = 80
INTAKE_HIGH_RISK_SCORE = 90

TOKEN_ALLOCATION = {
    "context": 20_000,
    "planning": 8_000,
    "coding": 35_000,
    "testing": 10_000,
    "review": 12_000,
    "recovery": 10_000,
    "reserve": 5_000,
}

STATIC_PROMPT_PARTS = (
    "system_prompt",
    "harness_rules",
    "agent_contract",
    "repository_rules",
    "context_snapshot",
)
