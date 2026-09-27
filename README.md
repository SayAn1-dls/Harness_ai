<div align="center">

<img src="docs/assets/hero.svg" alt="LCC AI Harness" width="100%"/>

# LCC: an AI teammate that fixes bugs *and proves it*

[![CI](https://github.com/SayAn1-dls/Harness_ai/actions/workflows/ci.yml/badge.svg?branch=agent/core-loop)](https://github.com/SayAn1-dls/Harness_ai/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)
![Models](https://img.shields.io/badge/works%20with-DeepSeek%20%C2%B7%20Qwen%20%C2%B7%20any%20OpenAI--compatible-6f42c1)
![Merge](https://img.shields.io/badge/merges%20by%20itself-never-critical)

</div>

---

## So what is this?

Ask a chatbot to fix a bug and it hands you code that *looks* right. Maybe it works, maybe it doesn't, and you're the one who has to find out.

**We built the part that finds out.**

LCC is a **harness**: the workbench around an AI model. You give it a bug report (or just a GitHub link), and it:

1. 🔍 **reads the repository** the way a new teammate would, and finds the files that matter;
2. 🛠️ **lets the model fix the bug** with real tools: search, read, edit, run the tests;
3. 🧪 **refuses to believe the model.** The fix only counts if a test **fails before the change and passes after it**;
4. 🔁 **tries again, smarter,** when it fails, carrying forward what went wrong;
5. 📬 **hands you a branch or a draft pull request.** A human always decides whether it gets merged.

> The model is the brain. The harness is the discipline.

---

## Where we are, honestly

<img src="docs/assets/scoreboard.svg" alt="Scoreboard" width="100%"/>

| | Result | Why it matters |
|---|---|---|
| 🧾 **Real benchmark** | 20 real bugs, taken from the actual history of 6 open-source libraries (more-itertools, toolz, boltons, tabulate, sqlparse, parse) | Not toy examples we wrote ourselves. Each one was a real bug that real maintainers fixed |
| ✅ **Benchmark is valid** | 20/20: each hidden test fails before the real fix and passes after it | The grader measures the right thing |
| ⚙️ **Pipeline works on real repos** | 20/20 when the known fix is fed through every stage | Setup, testing, proof, review and grading all work on real codebases |
| 🚫 **Can't be cheated** | A model that changes nothing scores **0/20** | You can't pass by doing nothing |
| 🤖 **Live DeepSeek or Qwen score** | **Not run yet.** We need a funded API key. One command: `make eval-real` | The number that really matters, and we say so openly |
| 🧪 **Tests** | 91 passing on macOS and Linux; CI runs every push on Python 3.11 to 3.13 | Nothing breaks silently |

---

## Try it in 60 seconds

```bash
git clone https://github.com/SayAn1-dls/Harness_ai.git && cd Harness_ai
export AI_API_KEY="your DeepSeek or Qwen key"   # the only secret, never saved to disk
make setup        # installs everything
make doctor       # one tiny test call: is the key valid? is there balance? does the model exist?
make run          # paste a bug report, a GitHub issue link, or just a repo link
```

<img src="docs/assets/terminal.svg" alt="What a run looks like" width="100%"/>
<sub>An illustration of the output, not a recording.</sub>

---

## How it thinks

<img src="docs/assets/loop.svg" alt="The fix, test, diagnose, retry loop" width="100%"/>

| Step | In plain words |
|---|---|
| **Understand** | Turns the bug report into checkable goals ("`add(2, 3)` must return 5"). If the report is truly unanswerable, it asks a human instead of guessing. |
| **Find the code** | Indexes the repo, maps which file imports which, and ranks files by how well they match the bug. |
| **Fix** | The model works in a loop with 10 tools: search, find a definition, list files, read lines, edit, create a file, run tests, run a shell command, git history, finish. |
| **Prove** | Compares the test results with a run from *before* the change. Old failures don't block the fix; new failures do. A new test must fail without the fix. |
| **Retry or stop** | On failure it diagnoses why and tries again with that knowledge. It gives up early when retrying is pointless: the same error twice, out of budget, or more tests failing each time. |

---

## Just a repo link? Auto mode

```bash
make auto REPO=https://github.com/someone/their-project
```

No bug report needed. It hunts for problems on its own: tests that already fail, real defects found by a static analyser (never style nitpicks), and a short AI code review that must show *how to trigger* each bug.

Every candidate goes through the same proof. **If it can't prove the bug with a failing test, it drops it**, so imagined bugs never become pull requests. What survives becomes a **draft PR** on your fork, for the maintainer to judge. It asks before touching a repo you don't own, stops at 3 PRs, and runs the stranger's code inside Docker when Docker is running.

---

## What we built along the way

<img src="docs/assets/journey.svg" alt="Project journey" width="100%"/>

<details>
<summary><b>The full story: every stage and the problem it solved</b></summary>

1. **Core loop.** A model that can use tools, not just type code. Every decision is written to a durable log (`events.jsonl`), so a run can be audited afterwards.
2. **Proof, not promises.** Our first benchmark said four bugs were fixed when the model had changed *nothing*: the tests already passed. That led to the rule that defines the project: a fix counts only if a test flips from failing to passing.
3. **Hackathon interface.** `make setup / run / test / clean`, one key (`AI_API_KEY`), one config file.
4. **DeepSeek and Qwen.** The key is recognised automatically without spending tokens. A tiny test call catches a bad key, zero balance or a missing model in seconds. It falls back to a model the account actually has, and adapts to local servers that can't do tool calls.
5. **Baseline-aware testing.** Real projects often have tests that are already broken. Those no longer block a correct fix, while any *new* breakage still fails it.
6. **Auto mode.** A repo link in, draft PRs out, and never an automatic merge.
7. **QA pass.** We attacked our own code and found 12 real bugs. For example, one unrelated old lint error made *every* correct fix fail: 22 model calls wasted, versus 5 now. Another: a model answering `"none"` got split into four "blocking questions". Each bug now has a test.
8. **A real benchmark.** We mined 101 real bug-fix commits: 63 passed our checks (the test fails before the fix and passes after it), and we picked 20 clear bug fixes from those.
9. **Security.** The target project's code never sees your API keys. A git guard stops it resetting your repo, even from inside Python code. Docker mode takes away the network and your files too.
10. **Leaner prompts.** On the same 16-step session, the prompt shrank from about 120.8k to about 96.5k tokens (−20%): no re-sent stale file reads, one-line output for passing tests, smaller tool descriptions.
</details>

---

## What we haven't done yet

- ❌ **No live-model score.** Everything above tests the harness, not how well DeepSeek or Qwen fixes bugs. That's the next command we run.
- ❌ We haven't yet proven that each helper step (planner, reviewer, intake) is worth its tokens. `make ablation` measures that once we have a key.
- ⚠️ Python is the main target. JavaScript, Go and Rust tests run, but get less help.
- ⚠️ Token figures are estimates from scripted runs; live runs record the provider's billed numbers.
- ⚠️ 20 tasks is a small benchmark, and some of these public fixes may be in the models' training data.

---

<details>
<summary><b>📖 All commands</b></summary>

| Command | What it does |
|---|---|
| `make run ISSUE=<link or text> REPO=<path or url> [BASE=<commit>]` | Fix one bug, non-interactive |
| `make auto REPO=<url> [PR=0\|1]` | Find → fix → prove → draft PRs (`PR=0`: keep local branches only) |
| `make eval-real` | Live score on the 20 real bugs |
| `make ablation` | Same, with the planner, reviewer and intake switched off in turn |
| `make bench-check` | Re-prove the benchmark itself (valid 20/20, reference fix 20/20, do-nothing 0/20) |
| `make test` | 91 tests + an offline mini-benchmark, no key needed |
| `make doctor` / `make clean` | Health check / clean up |
</details>

<details>
<summary><b>🤖 Models and settings</b></summary>

- **DeepSeek:** `deepseek-chat`, falling back to `deepseek-reasoner`.
- **Qwen (Alibaba DashScope, international or China):** `qwen3-coder-plus`, then `qwen-plus`, then `qwen-max`.
- **Your own model:** `export LCC_BASE_URL=http://localhost:8000/v1 LCC_MODEL=<name>`. It works even if the server can't do tool calls.
- A plain `sk-` key is only ever shown to DeepSeek and DashScope, never to other companies.
- Everything lives in `lcc.config.toml`: `LCC_MODEL`, `LCC_PROVIDER`, `LCC_SANDBOX=docker`, `LCC_ABLATE=planner,reviewer`. Budgets: 5 attempts, 300k tokens and 30 tool steps per bug.
</details>

<details>
<summary><b>🛡️ Safety rules it never breaks</b></summary>

- It works only on `agent/*` branches and **never merges**.
- Your unsaved changes are stashed before a run and put back afterwards.
- The target's code runs without your keys, and it can't reset, commit to or push your repo.
- In Docker mode the target's code has no network, and only the project folder is visible.
- Your key is read from `AI_API_KEY` only, and is never written to any file.
</details>

<details>
<summary><b>🗂️ Where things live</b></summary>

`src/lcc/`:
- `session.py`: what `make run` and `make auto` do
- `orchestrator.py`: the step-by-step brain
- `agents.py`: the prompts
- `agent_loop.py` + `tools.py`: the model's hands
- `sandbox.py`: key stripping, git guard, Docker
- `model.py`: DeepSeek and Qwen
- `bench.py`: the benchmark
- `discover.py`: bug hunting for auto mode

Elsewhere:
- `benchmarks/real/`: the 20 real bugs
- `benchmarks/tasks/`: 7 small practice bugs
- `harness/docs/DECISIONS.md`: every design decision, with the reason for it
</details>

<div align="center">
<br/>
<b>We didn't try to build a smarter model. We built a stricter workbench, so any model has to prove its fixes.</b>
<br/><sub>AI Harness Hackathon 2026</sub>
</div>
