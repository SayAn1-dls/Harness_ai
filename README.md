<div align="center">

<img src="docs/assets/hero.svg" alt="LCC AI Harness" width="100%"/>

# LCC

### We don't trust AI-written fixes. So we built something that makes the AI prove them.

[![CI](https://img.shields.io/github/actions/workflow/status/SayAn1-dls/Harness_ai/ci.yml?branch=agent/core-loop&style=flat-square&label=CI&labelColor=141413)](https://github.com/SayAn1-dls/Harness_ai/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20·%203.12%20·%203.13-D97757?style=flat-square&labelColor=141413)
![Models](https://img.shields.io/badge/runs%20on-DeepSeek%20·%20Qwen%20·%20your%20own%20LLM-D97757?style=flat-square&labelColor=141413)
![Merge](https://img.shields.io/badge/auto--merge-never.%20ever.-B5473A?style=flat-square&labelColor=141413)

</div>

<br/>

<img src="docs/assets/vs.svg" alt="A chatbot says fixed it and tests fail; LCC proves the fix before anyone sees it" width="100%"/>

## The problem

Every AI coding tool says "Fixed it!". Half the time it's right. The other half, a test you never looked at breaks, and you find out at 2am.

The model isn't the problem. The problem is that nobody makes it **show its work**.

## What LCC does

LCC is a **harness**: the workbench around an AI model. You hand it a bug, or just a link to a GitHub repo, and it goes through the same steps a careful engineer would:

**Read the repo → find the code that matters → fix it → write a test that fails without the fix → run everything → try again if something's off → hand a human the result.**

The rule that holds it all together:

> **A fix doesn't count until a test fails before the change and passes after it.**
> No test flip, no fix. We don't care how confident the model sounds.

And it never merges anything. You get a branch or a *draft* pull request. A human always has the last word.

---

## The scoreboard

<img src="docs/assets/scoreboard.svg" alt="Scoreboard" width="100%"/>

We didn't want to grade ourselves on toy examples we wrote, so we went digging through the git history of six real open-source Python libraries: **more-itertools, toolz, boltons, tabulate, sqlparse and parse**.

- We pulled out **101** real bug-fix commits. **63** survived our checks: the maintainers' own test has to fail on the code before the fix and pass after it. We picked **20** clear bugs from those.
- To test the harness itself, we fed it the real fix: it went through every stage and scored **20/20**.
- To test the grader, we plugged in a model that does nothing: it scored **0/20**. You can't pass by sitting still.
- **The live score is missing.** Our DeepSeek account ran out of credit (HTTP 402) and our Gemini project was blocked (HTTP 403) before we got a real run in. With a funded DeepSeek or Qwen key it's one command: `make eval-real`. Until then, we are not going to invent a number.

Behind all of that: **100 automated tests**, run on every push on Python 3.11, 3.12 and 3.13 (green ✅), plus the same checks inside a clean Linux container.

---

## Try it

```bash
git clone https://github.com/SayAn1-dls/Harness_ai.git && cd Harness_ai
export AI_API_KEY="your DeepSeek or Qwen key"    # the only secret. never written to disk.
make setup
make doctor     # sends ONE tiny request: is the key real? any balance left? does the model exist?
make run        # paste a bug report, a GitHub issue link, or just a repo link
```

<img src="docs/assets/terminal.svg" alt="What a run looks like" width="100%"/>
<sub>That's an illustration of the output, not a recording.</sub>

---

## What happens inside

<img src="docs/assets/loop.svg" alt="Implement, verify, diagnose, replan" width="100%"/>

**It reads the bug report first**, and turns "add is broken" into something testable, like "`add(2, 3)` must return 5". If a report makes no sense at all, it asks a human instead of guessing.

**Then it learns the repo.** It indexes every file, works out which file imports which, and ranks them by how much they have to do with the bug. The model starts with the right code in front of it, not the whole repo.

**Then the model goes to work** with 10 tools: search, find where something is defined, list files, read specific lines, edit, create a file, run tests, run a shell command, look at git history, and "I'm done".

**Then we check it**, and this part is where most tools stop and we don't. We run the tests from *before* the change and compare. Tests that were already broken don't count against the fix. Anything newly broken does. The new test has to fail on the old code, or it proves nothing.

**If it fails, it tries again, knowing why.** The next attempt sees the diff, the error and a diagnosis. And it knows when to quit: the same failure twice, the budget spent, or every attempt breaking more tests. Burning tokens on a lost cause is not a strategy.

---

## Three things no other harness we know of does

### 1. Every fix carries its own proof
When LCC verifies a fix, it writes a small proof file with the commit before the fix, the commit with it, and the exact tests that have to flip. Anyone can replay it:

```bash
make verify PROOF=outputs/GH-12.proof.json REPO=path/to/repo
#  ✓ on the original code the tests FAIL: the bug is real
#  ✓ with the fix the tests PASS
#  PROOF HOLDS
```

Every pull request LCC opens ends with a **"Verify it yourself"** section: four plain `git` and `pytest` commands. You don't have to install anything, and you don't have to trust the AI.

### 2. It tests the test
A test that fails before a fix and passes after it can still be lazy, like `assert result != 5`. So after verifying a fix, LCC **breaks the fixed lines on purpose**, one small change at a time: it flips `<` to `<=`, turns `and` into `or`, returns `None`, deletes a `raise`. Then it checks whether the new test notices. It never touches text inside strings or comments, because nobody could catch those changes.

The PR states the result: *"the new test catches 4/6 deliberate breaks of this fix."* With `LCC_MUTATION=gate`, a test that catches none of them gets sent back to be strengthened.

We ran it on the maintainers' own tests for our 20 real bugs: they catch **24 of 29** deliberate breaks. One that slips through: more-itertools checks that `sliced()` rejects `-1`, but never checks `0`.

### 3. It keeps score of how often the AI was right
In auto mode the model gets to claim bugs, and LCC counts what happens to every claim:

```
The model claimed 7 bug(s).
  3 dropped before any work: 2 had no way to trigger them, 1 low confidence, 0 in files it never read.
  4 attempted: 2 proven real with a failing test, 2 could not be proven.
Proven rate: 2/4 attempted claims (2/7 of everything it claimed).
```

The numbers above are an example of the format. The live numbers come from your run. We think "how often was the AI's bug claim real?" is a number every AI code tool should show, so ours does.

---

## Just a repo link? Auto mode

```bash
make auto REPO=https://github.com/someone/their-project
```

No bug report at all. One command, and it does the whole job:

1. **Reads the whole repo.** It runs the full test suite and a defect checker over every file. Then the AI reads *every* source file, most important first, within a budget you set (about 30k tokens by default; big files are split, never skipped). The report tells you exactly how much was read: *"The AI read 12 of 14 source files (2,300 of 2,710 lines)."*
2. **Tells you everything it found.** You get `outputs/ISSUES-<repo>.md` with every bug, security hole and slow spot: where it is, who found it (tests, the checker, or the AI), and what happened to it. Each one is fixed with proof and a PR link, attempted but not provable, found but not attempted, or dropped with the reason.
3. **Fixes the bugs,** but only with a test that fails before the fix and passes after it. **If a bug can't be proven, it's dropped**, so the AI can't make up bugs and send them to strangers.
4. **Optimizes slow code, and proves it's faster.** For a slow spot, the model writes a small benchmark. LCC times it on the old code and the new code (median of 7 runs) and accepts the change only if **every test still passes** and it's **at least 1.1× faster**. The PR says it straight: *"12.4 ms → 3.1 ms (4.0× faster)"*. We tested it on an O(n²) duplicate check, which came out more than 5× faster, and on a fake "optimization" that changed nothing, which was rejected.

What survives becomes a **draft** pull request on your fork. It asks before touching a repo you don't own, stops after 3 open PRs, and runs the stranger's code inside Docker when Docker is running. Maintainers decide what gets merged. Always.

---

## How we got here (including the embarrassing parts)

<img src="docs/assets/journey.svg" alt="Project journey" width="100%"/>

**Our first benchmark lied to us.** It said 4 bugs were fixed. The model had changed *nothing*: those tests were already passing. That's where the "test must flip from fail to pass" rule comes from, and it's still the most important line in the codebase.

**Then we attacked our own code** and found 12 real bugs. Two favourites:
- One unrelated old lint error in a repo made *every* correct fix fail. We wasted 22 model calls on something that now takes 5.
- A model answered `"none"`, and our code split it into `n`, `o`, `n`, `e`: four "blocking questions". A perfectly solvable task got escalated to a human. Every one of those 12 bugs now has a test, so it can't come back.

**We made it safe to run on other people's code.**
- The target project's code never sees your API keys.
- A git guard stops it from resetting or pushing your repo, even when the call comes from inside Python.
- Docker mode takes away the network and your files too.

We tested all of it: inside the container the key is gone, your home folder is invisible, and the internet is unreachable.

**We made it cheaper.** On the same 16-step session, prompts went from about 120.8k to about 96.5k tokens (**−20%**). Three changes did it:
- a file read that gets read again drops out of the conversation;
- a passing test run shows as one line instead of a wall of output;
- the tool descriptions got shorter.

**We made it fit the hackathon.** The final evaluation runs on DeepSeek and Qwen, so it detects either key without spending a token. It finds a model your account actually has, and adapts to local servers that can't do tool calls at all.

---

## What's still missing (we'd rather you hear it from us)

- **A live score from DeepSeek or Qwen.** Everything above proves the harness works. It doesn't yet prove how often the model fixes the bug.
- **Proof that every helper step pays off.** The planner, reviewer and intake calls might not be worth their tokens. `make ablation` switches each one off and measures it. It needs a key too.
- **Other languages get less help.** Python gets the most; JavaScript, Go and Rust tests run but get less help.
- **The token numbers are estimates.** They come from scripted runs. Live runs log what the provider actually bills.
- **20 bugs is a small benchmark.** And some of these fixes are public, so a model might have seen them during training.

---

<details>
<summary><b>Every command</b></summary>

| Command | What it does |
|---|---|
| `make run ISSUE=<link or text> REPO=<path or url> [BASE=<commit>]` | Fix one bug without the interactive prompt |
| `make auto REPO=<url> [PR=0\|1]` | Hunt → fix → prove → draft PRs (`PR=0` keeps everything local) |
| `make eval-real` | Live score on the 20 real bugs |
| `make ablation` | The same, with planner, reviewer and intake switched off one at a time |
| `make bench-check` | Re-prove the benchmark: 20/20 valid, 20/20 with the real fix, 0/20 doing nothing |
| `make verify PROOF=outputs/<task>.proof.json` | Replay a fix's proof: its test fails on the original code, passes with the fix |
| `make test` | 100 tests + a small offline benchmark, no key needed |
| `make doctor` · `make clean` | Health check · tidy up |
</details>

<details>
<summary><b>Models and settings</b></summary>

- **DeepSeek:** `deepseek-chat`, falling back to `deepseek-reasoner`.
- **Qwen (Alibaba DashScope, international or China):** `qwen3-coder-plus`, then `qwen-plus`, then `qwen-max`.
- **Your own model:** `LCC_BASE_URL=http://localhost:8000/v1 LCC_MODEL=<name>`. It works even without tool-call support.
- A plain `sk-` key is only ever shown to DeepSeek and DashScope.
- Everything is in `lcc.config.toml`, and can be overridden with `LCC_MODEL`, `LCC_PROVIDER`, `LCC_SANDBOX=docker` or `LCC_ABLATE=planner,reviewer`.
- Per-bug budget: 5 attempts, 300k tokens and 30 tool steps.
</details>

<details>
<summary><b>Rules it never breaks</b></summary>

- It only works on `agent/*` branches, and it never merges.
- Your unsaved changes are put aside before a run and put back after it.
- The target's code runs without your keys, and can't reset, commit to or push your repo.
- In Docker mode, the target's code gets no network and sees only its own folder.
- Your key comes from `AI_API_KEY` only, and it's never written anywhere.
</details>

<details>
<summary><b>Where things live</b></summary>

`src/lcc/`:
- `session.py`: `make run` / `make auto`
- `orchestrator.py`: the brain
- `agents.py`: the prompts
- `agent_loop.py` + `tools.py`: the hands
- `sandbox.py`: keys, git guard, Docker
- `model.py`: DeepSeek and Qwen
- `bench.py`: the benchmark
- `discover.py`: bug hunting

Elsewhere:
- `benchmarks/real/`: the 20 real bugs
- `benchmarks/tasks/`: 7 practice bugs
- `harness/docs/DECISIONS.md`: every decision we made, and why
</details>

<br/>
<div align="center">

**We didn't build a smarter model. We built a stricter boss for it.**

<sub>AI Harness Hackathon 2026</sub>

</div>
