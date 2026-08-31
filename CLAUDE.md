# Self-healing loop framework — development guide

For an agent working **on** this framework. The loop's own agents read `loop_context/CLAUDE.md` instead, and an operator installing it reads `SKILL.md`.

## Doc map

One home per fact. Put new content in the file that owns the topic; the others link rather than repeat.

| tier | file | audience | owns |
|---|---|---|---|
| root | this file (`AGENTS.md` symlinks to it) | any session on the framework | what this is, layout, the loop, hard rules, out of scope, commands |
| depth | [`docs/architecture.md`](docs/architecture.md) | changing a seam | the adapter contracts, harness restriction, the installer's tier rule |
| depth | [`docs/subsystems.md`](docs/subsystems.md) | changing a module | compaction, failure identity, incident memory, evidence, variable hydration, gate internals |
| depth | [`docs/risks.md`](docs/risks.md) | judging what the design does not cover | residual risks outside the workflows |
| subtree | [`workflows/CLAUDE.md`](workflows/CLAUDE.md) | editing `heal.yml` or `watch.yml` | the git guard, the action and harness pins, branch behaviour |
| subtree | [`tests/CLAUDE.md`](tests/CLAUDE.md) | writing or reading a test | mutation discipline, what actually backs this code |
| shipped | `README.md` · `SKILL.md` · `reference/` · `artifacts/` · `templates/` · `loop_context/CLAUDE.md` | evaluators, the installing agent, the loop's roles | not development docs; see *Layout* |

A subtree file auto-loads when an agent works in that directory, so it owns its topic outright and nothing here restates it.

## What this is

A skill plus a portable core that installs a fully autonomous self-healing loop into a target project's own repo. A GitHub Actions cron watches the target's failures; on a real one a headless agent diagnoses it, writes a red reproducing test, fixes the source, gates, opens and reviews a PR, merges, deploys, verifies and records an incident. No human in the happy path.

Universal across harnesses and models: the harness is data, the model is a config string.

Two intents govern every design call. **No human in the happy path**, and **no token-burn thrash** (bounded prompts, structured output, an attempt cap, a job timeout, one cycle at a time per branch).

## Layout

The published repo's root is this directory. Everything in it ships.

| path | what it is |
|---|---|
| `SKILL.md` | the installer skill |
| `reference/` | loaded by the installer per branch; `verifying-the-install.md` is read only when asked and never run |
| `artifacts/` | templates the installer fills and copies into the target; none is vendored |
| `loop_context/CLAUDE.md` | copied to `target/.shl/CLAUDE.md` at install |
| `adapters/ agent/ guardrails/ templates/` + the root `.py` modules | the portable core, vendored into the target |
| `opencode.json` | per-role permissions, read by the opencode harness only |
| `workflows/` | `watch.yml` and `heal.yml`, the Actions templates |
| `docs/` | development depth, per the doc map |
| `tests/` | framework unit tests, never installed |

The repo name `self-healing-loop` matches the skill's `name:` and the `SHL_*` env prefix. That name is what an operator types to invoke the installer, so anything else splits one identity across three places.

## The loop

Watch → Triage (compact the log; idle if nothing) → Diagnose (read-only) → Red and Freeze (write the repro test, prove RED, freeze it) → Fix (source-only, no shell) → Gate → PR → Review (read-only) → Merge → Deploy (only with a deploy command) → Verify (always, auto-revert on regression) → Record.

The gate refuses in this order, and the order is load-bearing — a violation of reach is named before a violation of content: `loop tree untouched`, `workflow untouched`, `diff rendering untouched` (a `.gitattributes` that blinds every content check), `test content readable` (the same blinding by a route that leaves no diff at all: `core.attributesFile`, or one NUL byte), `no test weakened`, `no test config touched`, then, only when a frozen test exists, `frozen test untouched` and `frozen test's helpers untouched`. Plus `no test that was passing now fails`.

**Every gate verdict states its grounds.** A refusal names the check, the file and the evidence (`BLOCKED [no test weakened]: tests/x.py: assertion removed: assert f() == 1`); a pass names the checks it rested on. Without it an empty `gate.txt` means both "passed" and "blocked".

**The loop performs its own git and GitHub operations** (commit, push, open a PR, merge, revert), and so does the installer, which lands its install on a branch and opens a PR. That autonomy is the product rather than an oversight. The agent does none of it: every GitHub write is a plain workflow step, running only after the gate and the review both pass.

**Auto-merge is unconditional.** `heal.yml` runs Merge, Deploy, Verify, Rollback and Record as sequential steps in one job, so a mode that stopped at the PR would remove post-deploy verification, auto-rollback and the incident record with it. A protected default branch with no bot bypass is therefore a Phase 0 blocking check: without one, every cycle spends three agent calls and dies at the merge. An operator who cannot accept unattended merges sets `SHL_DEPLOY_CMD` empty and keeps the loop off that service.

Verify is not gated on `SHL_DEPLOY_CMD`, and the step says why. Its suite half runs on every ref while the `health_check` probe runs only on the default branch; branch behaviour and the reason for that split are in [`workflows/CLAUDE.md`](workflows/CLAUDE.md).

## The two seams

The core (loop, guardrails, structured-output parser) never changes. Two adapter seams fit any target and any harness.

- **TargetAdapter**, one per target. `read_log` required; `failing_tests`, `failure_ids` and `health_check` optional, each with a different cost when absent. A new target is a new adapter, never a new pipeline.
- **AgentAdapter**, one per harness. A harness is data: one `HarnessConfig` plus one `render()`. Adding a harness is authoring a config, with zero framework code.

Contracts, what `None` costs per method, and how each harness proves its restriction: [`docs/architecture.md`](docs/architecture.md).

**Guardrails:** deterministic gate (the real merge gate), red-then-green frozen test, attempt cap (2 then escalate), confidentiality scrub, post-deploy health with auto-rollback, incident memory read before every Diagnose, separation of duties (Fix never reviews itself), and state kept in GitHub rather than on the runner. Internals: [`docs/subsystems.md`](docs/subsystems.md).

## Hard rules

1. **Uppercase means a person opens it first; lowercase means a machine loads it on demand.** `SKILL.md`, `README.md`, `CLAUDE.md` and `AGENTS.md` are spellings external convention fixes, so this project does not choose their case. `tests/test_file_naming.py` enforces the rule, including that a template renamed without its write-out target fails.
2. **One home per fact.** Every claim has one owner, per the doc map, and everything else links to it rather than keeping a second copy to maintain. Deliberate restatement is allowed where the reader cannot reach the owner (`artifacts/` is written into a target, `reference/` loads one branch at a time), and it names the owner and says to change it there. Vendored code names its owner rather than linking it, because the same relative path resolves to a different file once copied: `../CLAUDE.md` is this guide here and the loop agent's operating doc under a target's `.shl/`. What drifts is two copies that both read as authoritative.

## Out of scope, by design

- **No local simulation of the workflow.** A driver that reimplements the step sequence reports green on its own reimplementation while the shipped workflow is free to be broken end to end. Use `loop.py`'s `run_diagnose`, `run_fix` and `run_review`, which spawn real cold subprocesses; the sequence itself is proved only by running the real workflow against a real repo.
- **No headless install.** Without an operator the installer stops and reports why.
- **No `deploy` or `rollback` on the adapter**, and no per-invocation spend cap: the job timeout is the hard stop.
- **No LICENSE and no repo CI.** This ships as a small personal skill rather than a contribution repo.

## Commands

Both run from this directory, which is where `SKILL.md` sits.

```bash
PYTHONPATH=. python3 -B -m unittest discover -s tests -v
actionlint -no-color -oneline workflows/*.yml
```

All green, or the change is not done. **`actionlint` is a required manual step before any release**, because the suite cannot substitute for it. Why not, and what evidence actually backs this code: [`tests/CLAUDE.md`](tests/CLAUDE.md).
