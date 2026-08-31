# Self-healing loop framework

A framework that installs a fully autonomous self-healing CI/CD loop into a project's own GitHub repo. It watches logs for failures and, for each one, files an issue, writes a fix, tests it, opens and reviews a PR, merges, deploys, and verifies — with no human in the happy path. A harness is config rather than framework code, and the model behind it is a repo variable. Two harnesses ship: Claude Code and OpenCode.

**Which part do you need?**

- **Installing it into a repo** — go to [Install](#install). You never read this file's internals; the installer is [`SKILL.md`](SKILL.md).
- **Deciding whether it fits your repo** — [What has run](#what-has-run) and [Risks to accept before installing](#risks-to-accept-before-installing). Read both before you install.
- **Working on the framework** — [`CLAUDE.md`](CLAUDE.md) is the development guide.

## What it does

Projects produce failures that someone has to triage and fix by hand. This framework makes that loop autonomous, and guards it so the agent doing the work cannot game its way to green or leak secrets.

Where the failure signal comes from is the target's own answer, settled at install: a log the project writes, a log the host keeps, an error tracker, or simply the test suite going red. The last of those needs no deployment at all, so a library works as well as a service.

## How it works

On a schedule, a GitHub Actions job reads the target's failure signal and, if there is a real failure, runs one cycle:

```text
cron
 │
 ▼
Watch ─── nothing real ───▶ IDLE
 │
 │ real failure
 ▼
Diagnose        read-only agent: root cause, plus a reproducing test
 │
 ▼
Red + Freeze    prove that test fails on the broken code, then freeze it
 │
 ▼
Fix             source-only agent; no shell, cannot touch the frozen test
 │
 ▼
Gate            deterministic; refuses rather than judges
 │
 ▼
PR + Review     independent read-only agent
 │
 ▼
Merge ──▶ Deploy ──▶ Verify ─── regression ───▶ auto-revert
 │
 ▼
Record          the incident, which the next cycle reads back
```

1. **Diagnose** — an agent finds the root cause and writes a reproducing test that fails on the broken code (red).
2. **Freeze + Fix** — that test is frozen; a second agent fixes the source to make it pass (green). The fixer cannot touch the frozen test, and never has a shell.
3. **Gate** — no test that was passing may fail, no test may be weakened, the frozen test must be untouched, and no test-runner config may be edited. Failures that were already there do not block: a repo carrying one would otherwise veto every fix forever.
4. **PR + Review** — a PR opens; an independent agent reviews it. Independent means a separate cold process that did not write the fix and runs read-only, not a different model: one model id serves all three roles.
5. **Merge + Verify** — on approval it merges, deploys if the target has a deploy command, and re-checks; on regression it reverts automatically, on whichever branch the cycle is running (the default branch for a scheduled run). Verification runs whether or not anything deployed, because push-triggered targets have no deploy command and would otherwise skip it.

Everything the agent emits is scrubbed of secrets before it reaches GitHub. The agent never runs `git`/`gh` and has no shell: all GitHub writes are plain workflow steps that fire only after the gate and the review both pass.

## What ships

- **Two harnesses, and the model behind them is a repo variable.** A harness is config rather than framework code, so fitting a new one is authoring a config, not editing the loop.
- **One adapter per target.** The target answers where its own failures surface; the core never changes to fit a new project.
- **A deterministic gate.** It refuses rather than judges, and every verdict names the check, the file and the evidence it rested on.
- **A red-then-green frozen test.** The fix is proved against a test shown failing first, which the fixer cannot edit.
- **Incident memory.** What was diagnosed and what became of it, read back before the next Diagnose.
- **A per-cycle evidence bundle.** Each role's exact prompt and raw output, scrubbed, which is the only way to see *why* the agent did what it did.

## Repo layout

The published repo's root is the skill. Everything in it ships.

```text
self-healing-loop/
│
├── README.md              this file
├── SKILL.md               the installer; what you point an agent at
├── reference/             what the installer reads, one branch at a time
├── artifacts/             templates it fills and copies into your repo
├── workflows/             watch.yml + heal.yml, copied to .github/workflows/
├── loop_context/          copied to .shl/CLAUDE.md for the loop's own agent
│
│   everything below is vendored whole into your repo's .shl/ —
│   this is the product code the risk note means
│
├── loop.py                the cycle entrypoints
├── role.py                prompt assembly, structured-output parsing
├── log_compact.py         compaction: one slot per distinct failure
├── gh_state.py            state read back from GitHub, not from the runner
├── evidence.py            the per-cycle evidence bundle
├── adapters/              the TargetAdapter contract, plus your generated target.py
├── agent/                 the AgentAdapter: one config per harness
├── guardrails/            the deterministic gate, incident memory, the CLI
├── templates/             the three role prompts
├── opencode.json          per-role permissions, read by the opencode harness only
│
├── docs/                  development depth; never installed
├── tests/                 the framework's own suite; never installed
└── CLAUDE.md, AGENTS.md   development guide
```

## Requirements

In the target repo, before a cycle can run:

- **GitHub, with Actions enabled.** The loop is GitHub-native: an `origin` on `github.com`, and a working `gh` at install time.
- **A default branch the bot can merge into.** Protected with no bot bypass, every cycle spends three agent calls and then dies at the merge, forever and loudly. Workflow permissions also need `default_workflow_permissions=write` and `can_approve_pull_request_reviews=true`; the second is off by default on repos created since 2023, and no workflow `permissions:` block overrides it.
- **A readable failure signal**, per *What it does* above.
- **A model credential** the harness can read, held as a repo secret. The install hands you the exact `SHL_*` variables and secrets to set.
- **Python 3.11** on the runner. The vendored core is stdlib only.

The installer checks these in Phase 0. A miss is either a stop or a recorded gap that leaves the later phases pending; it does not quietly install a loop that cannot run.

## Install

### 1. Get the installer in front of an agent

```bash
git clone https://github.com/Xd06eR/self-healing-loop.git
```

Clone it anywhere and `@`-mention the folder, or clone it into your agent's skills directory to invoke it by name.

### 2. Ask, from inside the target repo

```text
install the self-healing loop into this repo
```

**Start the agent in a permission mode that prompts before it writes.** Nothing in this repo can check that for you, and it is the first risk below.

### 3. What the install actually does

**It is a consultation and it needs you present.** It reads what the repo can answer for itself (language, test commands, test layout), then interviews you one question at a time about everything it cannot safely infer: where failures actually surface, whether a client-only app should gain a way to report them, log retention against cron cadence, what is off-limits, and what the gate rests on if the project has no tests. Every answer is recorded with its provenance in `.shl/SETUP.md`, and nothing is written until you agree — **provided you started the agent in a mode that asks before it writes.** That condition is not decoration: see the risk note below.

Only then does it vendor the portable core into `.shl/`, generate the target adapter, close any prerequisite gaps, drop the two workflows, and hand you the `SHL_*` env and secret setup. The loop's own agent reads `.shl/CLAUDE.md` (copied from `loop_context/CLAUDE.md`) as its operating context.

**What stays yours.** The installer writes, commits and pushes its own branch, and sets repo variables. Three things it deliberately does not do, because each changes what your repo does when nobody is watching:

- **You supply the secret values.** It never asks for one in conversation.
- **You review and merge the install PR.** It opens the PR and stops there.
- **You uncomment the cron.** `watch.yml` ships with its `schedule` block commented out, so a fresh install is dormant until you turn it on.

There is no headless install **by design**. Fitting a loop to a project settles product decisions its owner has to make, and installing can write product code into the repo — a test suite that becomes the gate guarding the default branch, and possibly a public error-reporting endpoint. With nobody to ask, the installer stops and says why.

**By design is the whole of the guarantee.** That behaviour lives in `SKILL.md`'s instructions, and an instruction binds only a cooperating agent in a permission mode that asks. What happens in one that does not is the risk note below.

### 4. Verify that it heals, optionally

Not part of a default install. The installer never seeds a defect into anyone's repo; proving a loop heals needs a failure, and manufacturing one is the operator's decision. The procedure is [`reference/verifying-the-install.md`](reference/verifying-the-install.md), and the install report states what it did and did not prove.

**On a stack other than Python or JS/TS, treat its must-pass check as required rather than optional.** It is the only thing that confirms the gate blocks a weakened test there; see [Stack coverage](#stack-coverage).

## What has run

**On a real GitHub Actions runner, against real failures:** reading a host's logs, deciding idle versus dispatch, diagnosing a root cause from a fully minified stack, writing a reproducing test and proving it red, and fixing the source. One run went through the gate to an open pull request. Both fixes were byte-identical to what a person would have written.

Those runs predate substantial rewrites of the gate, the guardrail CLI and the heal workflow, so what they evidence is the design rather than this implementation.

**Covered by unit tests and workflow linting rather than by a runner:** everything from the merge onward (merge, deploy, post-deploy verification, automatic rollback, the incident record), plus the gate refusing a bad fix, the attempt cap, the escalation path, and OpenCode as a harness. The first group is reachable only when the step before it succeeds, so no runner has reached it. That split is settled: no further runner testing is planned.

### What the unit tests cover

The harness/model config, the merge gate (frozen-test, no-weakening, config-untouched, regression set), the confidentiality filter, the log compactor, the GitHub-derived state backend, incident memory and the per-cycle evidence bundle, plus structural tests over the shipped workflows and the installer.

What no unit test can cover is a cycle: the workflows have to run on a real runner against a real failure. The must-pass case (a deliberately bad fix that weakens a test must be BLOCKED) is exercised against the gate directly here, and end to end only by the optional operator procedure in [`reference/verifying-the-install.md`](reference/verifying-the-install.md).

### Stack coverage

The gate polices unevenly, and the gaps are silent. Which files count as tests is glob-driven, so it works anywhere `SHL_TEST_GLOBS` is set correctly. What is language-aware only for **Python and JS/TS** is everything downstream: the assertion pattern misses Go's `t.Error`/`t.Fatal`; the skip pattern misses `xit(`, RSpec's `pending`, Rust's `#[ignore]` and JUnit's `@Disabled`; and the test-config protection covers Python, JS, and the Ruby, Rust and JVM manifests, so a stack configuring its runner anywhere else (Go among them) can still edit that file to exclude the frozen test. Separately, failure fingerprinting parses Python and V8 traces, and incident recall, issue dedup and the attempt cap all die together when it extracts nothing.

Installing on another stack is not blocked. On one, treat the must-pass check in the optional verification as required: it is the only thing that confirms the gate blocks a weakened test there.

The fix is not a longer hard-coded list — that just moves the cliff to the next language. These conventions belong at the installer seam, supplied per target like `SHL_TEST_GLOBS` already is, with the gate failing **closed** when they are absent.

## Risks to accept before installing

Properties of the design, not defects awaiting a fix.

- **The consultation is enforced by a prompt, not by code.** The installer interviews you and writes nothing until you agree — but that is an instruction to an agent, and what actually decides is **your harness's permission mode**. Launched in a mode that auto-approves file writes, an installer can vendor the core, generate a test suite and stand up a public HTTP endpoint without asking once, while every document here says it asked. **Start it in a mode that prompts, and confirm that before you begin**: nothing in this repo can check it for you, and this is the one risk you control entirely from outside.
- **It writes product code into your repository.** A test suite that becomes the gate guarding your default branch, and, on the browser-only branch, a public error-reporting endpoint. Both are yours to read at the install PR. Neither is undone by removing the loop.
- **The gate is heuristic and line-based.** It is a backstop, not a proof. Several ways past it have been found and closed: forged diff headers via bytes git treats as content, a frozen test whose path carries a non-ASCII character, a `.gitattributes` that blinds every content check, and a `.git/config` that reaches a shell. None was the last of its kind — AST-diff is the named upgrade and is not built.
- **It is only as good as what your logs record, and every shortfall is silent.** A failure your code swallows, a message logged without a stack trace, minified frames with no sourcemaps, or a retention window shorter than the cron — each one leaves the loop idling, and idle looks exactly like healthy. Past a certain point, improving the loop means improving your own logging rather than this framework.

Two further risks are described where they belong rather than restated here: the loop **merges, deploys and reverts with no human in the happy path** by design (*How it works*), and on stacks other than Python and JS/TS **parts of the gate silently police nothing** (*Stack coverage*).

## Development

For anyone working ON the framework rather than installing it. Deliberately separate from the operator's optional verification in [Install](#4-verify-that-it-heals-optionally); neither is part of a default install.

```bash
PYTHONPATH=. python3 -B -m unittest discover -s tests -v
actionlint -no-color -oneline workflows/*.yml
```

Both from this directory. `actionlint` is a required manual step: the suite skips its workflow check when the binary is absent, so a machine without it runs green having never linted the two templates that are the product.

The real test is installing into a repo and running a full cycle against it. There is deliberately no local simulation of the workflow: a simulation reimplements the workflow rather than running it, so it reports green on its own reimplementation while the shipped workflow is free to be broken end to end.

## Docs

**Read to install:**

- **`SKILL.md`** — the installer, Phases 0–11. Start here.
- **`reference/`** — what the installer *reads*, loaded on the branch it is on: `adapter.md` (which adapter methods this target needs), `harnesses.md` (the two harnesses and their credential paths), `platforms.md` (log surfaces, retention, per-platform recipes), `closing-gaps.md` (generating a test suite or an error relay), `updating.md` (refreshing a loop that is already installed), and `verifying-the-install.md`, which it reads only when asked and never runs.

**Written into your repo:**

- **`artifacts/`** — what the installer *produces*: `setup.md` becomes the target's decision record, `report.md` becomes its install report, and `readme.md` becomes the `README.md` inside the loop directory for whoever meets it without expecting it. Filled in and copied; none is vendored.
- **`templates/`** — the three role prompts (diagnose, fix, review) the loop uses at runtime. Vendored whole.
- **`loop_context/CLAUDE.md`** — the operating doc copied into each target for the loop's own agent.

**Read to work on the framework:**

- **`CLAUDE.md`** / **`AGENTS.md`** — the development guide: what it is, the loop, hard rules, and a map to everything below.
- **`docs/`** — development depth reached from that map: `architecture.md` (the adapter seams, harness restriction, the installer's determine-versus-ask rule), `subsystems.md` (compaction, failure identity, incident memory, evidence, the gate's internals) and `risks.md` (what the design does not cover).
- **`workflows/CLAUDE.md`** and **`tests/CLAUDE.md`** — orders for those two directories, loaded when an agent works in them.