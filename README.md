# Self-healing loop framework

A framework that installs a fully autonomous self-healing CI/CD loop into a project's own GitHub repo. It watches logs for failures and, for each one, files an issue, writes a fix, tests it, opens and reviews a PR, merges, deploys, and verifies — with no human in the happy path. A harness is config rather than framework code, and the model behind it is a repo variable. Two harnesses ship: Claude Code and OpenCode.

> **Status: experimental. The split below is final — no further runner testing is planned.**
>
> **Ran on a real GitHub Actions runner, against real failures:** reading a host's logs, deciding idle versus dispatch, diagnosing a root cause from a fully minified stack, writing a reproducing test and proving it red, and fixing the source. One run went further, through the gate to an open pull request. Both fixes were byte-identical to what a person would have written.
>
> **Those runs predate the code in this tree**, and that qualifier is the important one. The gate, the guardrail CLI and the heal workflow were substantially rewritten afterwards. What the runs evidence is the design; they are not evidence about this implementation.
>
> **Never completed on a runner:** everything from the merge onward (merge, deploy, post-deploy verification, automatic rollback, the incident record), plus the gate actually refusing a bad fix, the attempt cap, the escalation path, and OpenCode as a harness. The first group is reachable only when the step before it succeeds, and no cycle has got that far.
>
> **So the evidence is unit tests, workflow linting, and two partial runs against older code.** No cycle has ever completed. Treat every claim below against that.

> **Risks to accept before installing.** Properties of the design and of its maturity, not defects awaiting a fix.
>
> - **The consultation is enforced by a prompt, not by code.** The installer interviews you and writes nothing until you agree — but that is an instruction to an agent, and what actually decides is **your harness's permission mode**. Launched in a mode that auto-approves file writes, an installer can vendor the core, generate a test suite and stand up a public HTTP endpoint without asking once, while every document here says it asked. **Start it in a mode that prompts, and confirm that before you begin**: nothing in this repo can check it for you, and this is the one risk you control entirely from outside.
> - **It writes product code into your repository.** A test suite that becomes the gate guarding your default branch, and, on the browser-only branch, a public error-reporting endpoint. Both are yours to read at the install PR. Neither is undone by removing the loop.
> - **The gate is heuristic and line-based.** It is a backstop, not a proof. Several ways past it have been found and closed: forged diff headers via bytes git treats as content, a frozen test whose path carries a non-ASCII character, a `.gitattributes` that blinds every content check, and a `.git/config` that reaches a shell. None was the last of its kind — AST-diff is the named upgrade and is not built.
> - **It is only as good as what your logs record, and every shortfall is silent.** A failure your code swallows, a message logged without a stack trace, minified frames with no sourcemaps, or a retention window shorter than the cron — each one leaves the loop idling, and idle looks exactly like healthy. Past a certain point, improving the loop means improving your own logging rather than this framework.
> - **Very few projects have installed this.** The install path is its least exercised part, and edge cases are likely.
>
> Two further risks are described where they belong rather than restated here: the loop **merges, deploys and reverts with no human in the happy path** by design (*How it works*), and on stacks other than Python and JS/TS **parts of the gate silently police nothing** (*What the gate polices*).

## The problem

Projects produce failures that someone has to triage and fix by hand. This framework makes that loop autonomous, and guards it so the agent doing the work cannot game its way to green or leak secrets.

Where the failure signal comes from is the target's own answer, settled at install: a log the project writes, a log the host keeps, an error tracker, or simply the test suite going red. The last of those needs no deployment at all, so a library works as well as a service.

## How it works

On a schedule, a GitHub Actions job reads the target's failure signal and, if there is a real failure:

1. **Diagnose** — an agent finds the root cause and writes a reproducing test that fails on the broken code (red).
2. **Freeze + Fix** — that test is frozen; a second agent fixes the source to make it pass (green). The fixer cannot touch the frozen test, and never has a shell.
3. **Gate** — no test that was passing may fail, no test may be weakened, the frozen test must be untouched, and no test-runner config may be edited. Failures that were already there do not block: a repo carrying one would otherwise veto every fix forever.
4. **PR + Review** — a PR opens; an independent agent reviews it. Independent means a separate cold process that did not write the fix and runs read-only, not a different model: one model id serves all three roles.
5. **Merge + Verify** — on approval it merges, deploys if the target has a deploy command, and re-checks; on regression it reverts automatically, on whichever branch the cycle is running (the default branch for a scheduled run). Verification runs whether or not anything deployed, because push-triggered targets have no deploy command and would otherwise skip it.

Everything the agent emits is scrubbed of secrets before it reaches GitHub. The agent never runs `git`/`gh` and has no shell — all GitHub writes are plain workflow steps that fire only after the gate and the review both pass.

## What is covered

The framework ships unit tests for the harness/model config, the merge gate (frozen-test, no-weakening, config-untouched, regression set), the confidentiality filter, the log compactor, the GitHub-derived state backend, incident memory and the per-cycle evidence bundle, plus structural tests over the shipped workflows and the installer.

What no unit test can cover is a cycle: the workflows have to run on a real runner against a real failure. The must-pass case (a deliberately bad fix that weakens a test must be BLOCKED) is exercised against the gate directly here, and end to end only by the optional operator procedure in [`reference/verifying-the-install.md`](reference/verifying-the-install.md).

**Which capabilities are demonstrated against a real project and which are still only reasoned about is the Status block at the top of this file.** Read it before trusting anything below.

## How to test

Two different things, deliberately kept apart. Neither is part of a default install.

**Development testing — verifying this framework.** For anyone working ON it:

- Unit tests, offline, no network or model: `PYTHONPATH=. python3 -B -m unittest discover -s tests -v` from this directory
- The real thing: install into a repo and run a full cycle against it. There is deliberately no local simulation of the workflow: a simulation reimplements the workflow rather than running it, so it reports green on its own reimplementation while the shipped workflow is free to be broken end to end.

**Installation testing — verifying one installed loop.** For the operator, and **optional**. The installer never seeds a defect into anyone's repo; proving a loop heals needs a failure, and manufacturing one is the operator's decision. The procedure lives in [`reference/verifying-the-install.md`](reference/verifying-the-install.md), and the install reports what it did and did not prove.

## What the gate polices

Unevenly, and the gaps are silent. Which files count as tests is glob-driven, so it works anywhere `SHL_TEST_GLOBS` is set correctly. What is language-aware only for **Python and JS/TS** is everything downstream: the assertion pattern misses Go's `t.Error`/`t.Fatal`; the skip pattern misses `xit(`, RSpec's `pending`, Rust's `#[ignore]` and JUnit's `@Disabled`; and the test-config protection covers Python, JS, and the Ruby, Rust and JVM manifests, so a stack configuring its runner anywhere else (Go among them) can still edit that file to exclude the frozen test. Separately, failure fingerprinting parses Python and V8 traces, and incident recall, issue dedup and the attempt cap all die together when it extracts nothing.

Installing on another stack is not blocked. On one, treat the must-pass check in the optional verification as required: it is the only thing that confirms the gate blocks a weakened test there.

The fix is not a longer hard-coded list — that just moves the cliff to the next language. These conventions belong at the installer seam, supplied per target like `SHL_TEST_GLOBS` already is, with the gate failing **closed** when they are absent.

## Install into a project

### Getting the installer in front of an agent

Clone it anywhere and `@`-mention the folder, or clone it into your agent's skills directory to invoke it by name.

Either way, ask from inside the target repo: *"install the self-healing loop into this repo"*.

### What the install actually does

**It is a consultation and it needs you present.** It reads what the repo can answer for itself (language, test commands, test layout), then interviews you one question at a time about everything it cannot safely infer: where failures actually surface, whether a client-only app should gain a way to report them, log retention against cron cadence, what is off-limits, and what the gate rests on if the project has no tests. Every answer is recorded with its provenance in `.shl/SETUP.md`, and nothing is written until you agree — **provided you started the agent in a mode that asks before it writes.** That condition is not decoration: see the risk note at the top.

Only then does it vendor the portable core into `.shl/`, generate the target adapter, close any prerequisite gaps, drop the two workflows, and hand you the `SHL_*` env and secret setup. The loop's own agent reads `.shl/CLAUDE.md` (copied from `loop_context/CLAUDE.md`) as its operating context.

There is no headless install **by design**. Fitting a loop to a project settles product decisions its owner has to make, and installing can write product code into the repo — a test suite that becomes the gate guarding the default branch, and possibly a public error-reporting endpoint. With nobody to ask, the installer stops and says why.

**By design is the whole of the guarantee.** That behaviour lives in `SKILL.md`'s instructions, and an instruction binds only a cooperating agent in a permission mode that asks. What happens in one that does not is the risk note at the top.

## Docs

- **`SKILL.md`** — the installer, Phases 0–11. Start here to install.
- **`reference/`** — what the installer *reads*, loaded on the branch it is on: `adapter.md` (which adapter methods this target needs), `harnesses.md` (the two harnesses and their credential paths), `platforms.md` (log surfaces, retention, per-platform recipes), `closing-gaps.md` (generating a test suite or an error relay), `updating.md` (refreshing a loop that is already installed), and `verifying-the-install.md`, which it reads only when asked and never runs.
- **`artifacts/`** — what the installer *produces*: `setup.md` becomes the target's decision record, `report.md` becomes its install report, and `readme.md` becomes the `README.md` inside the loop directory for whoever meets it without expecting it. Filled in and copied; none is vendored.
- **`templates/`** — the three role prompts (diagnose, fix, review) the loop uses at runtime. Vendored whole.
- **`CLAUDE.md`** / **`AGENTS.md`** — the development guide for anyone working ON the framework: what it is, the loop, hard rules, and a map to everything below.
- **`docs/`** — development depth reached from that map: `architecture.md` (the adapter seams, harness restriction, the installer's determine-versus-ask rule), `subsystems.md` (compaction, failure identity, incident memory, evidence, the gate's internals) and `risks.md` (what the design does not cover).
- **`workflows/CLAUDE.md`** and **`tests/CLAUDE.md`** — orders for those two directories, loaded when an agent works in them.
- **`loop_context/CLAUDE.md`** — the operating doc copied into each target for the loop's own agent.
- This README — human overview.
