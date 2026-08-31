# Architecture — the two seams and the installer

Read this when changing an adapter contract, adding a harness, or touching the installer's determine-versus-ask split. The loop sequence and the guardrail list are in the root [`CLAUDE.md`](../CLAUDE.md); module internals are in [`subsystems.md`](subsystems.md).

## TargetAdapter — one per target

`read_log` is required. `failing_tests`, `failure_ids` and `health_check` are optional, and `None` falls back to what the framework does alone. What that costs differs per method, and only one of the three is quiet:

- `failing_tests` reverts the gate to the strict all-green rule, so a repo carrying any pre-existing failure merges nothing, ever.
- `failure_ids` falls back to built-in Python and V8 parsing, which on a Go or Ruby target yields nothing. Returning `None` there to mean "cannot answer" silently selects a parser that cannot read the log, so `unfingerprintable` refuses the cycle in `watch.yml` rather than healing blind.
- `health_check` leaves the workflow relying on the suite, so a green suite beside a dead deployment passes verification. That one fails by looking healthy.

`read_log` reads wherever the target's failures surface: a log on disk, the host's logs, the browser via a relay, or CI going red. Which one is **asked** in the Phase 2 interview and never inferred, because a wrong answer here is invisible.

The adapter deliberately does not deploy or roll back. The workflow runs the operator's `SHL_DEPLOY_CMD`, which may carry deploy credentials no agent-adjacent module should hold, and reverts with `git revert`. Abstract `deploy` and `rollback` methods here would force every installer to write two implementations nothing calls.

A new target is a new adapter, never a new pipeline.

## AgentAdapter — one per harness

A harness is **data**: one `HarnessConfig` (install, argv template, per-role restriction flags, the verification it offers, model env vars, auth and base-url env) plus one `render()`. Adding a harness is authoring a config, with zero framework code.

A GitHub Actions runner is a disposable PC: a step installs the harness, invokes it headless, and authenticates from a repo secret. The framework never uses a harness's full-access escape hatch.

### The two restriction models

- **`claude-code`** restricts through argv: Diagnose and Review read-only, Fix editing files with no shell, and **every** role denying `Bash,WebFetch,WebSearch,Agent,Edit(./**)`. That covers the shell, both egress paths, spawning a subagent whose own frontmatter could carry a different permission mode, and writes anywhere in the loop's tree. The deny set is what restricts, not the mode: a deny rule is the only thing a permission mode cannot override.
- **`opencode`** has no read-only flag, so restriction lives in per-agent `permission` blocks in `opencode.json`, selected by `--agent`. Rules are last-match-wins: each agent opens with `"*": "deny"` and allows back only what the role needs, with Fix's `edit` scoped to deny `**/.shl/**` last so the loop tree wins. `external_directory` is set explicitly on every agent because its default is `ask`, and a headless `ask` never returns.

### Verification differs because the two express restriction differently

A harness declares either an argv-shaped `required_denial`, checked against the rendered command line, or a per-role `preflight_argv` that must exit 0. OpenCode needs the second: its denial lives in a file, and `run` treats an unresolved `--agent` as a warning and continues under the unrestricted default, while `debug agent` exits non-zero on the same input. A harness offering neither cannot run any role.

`tests/test_harness.py` refuses an unpinned harness spec, so the exact-pin policy stays a choice rather than a habit; the reasoning is in [`../workflows/CLAUDE.md`](../workflows/CLAUDE.md) beside the action pins.

### Choosing a model

A config string (`--model` or `ANTHROPIC_MODEL`) plus a provider credential, plus a base URL when non-native. Third-party models are the primary path. Per-harness credential paths, model-id formats and the traps an operator meets: `reference/harnesses.md`.

## The installer is a consultation

`SKILL.md` determines what the repo can answer, interviews the operator about everything else one question at a time, and writes nothing until they agree. It invokes no other skill, because it installs onto machines configured nothing like this one, so any skill it called would be a dependency that is missing.

One rule divides the two tiers, and `SKILL.md` names it the same way, because the installer and this guide have to mean the same thing by it — **the visibility rule**:

> A fact may be **determined** only if getting it wrong would be **visible**.

A wrong test command dies loudly, so it is determined. A wrong log surface makes the loop idle, and idle is indistinguishable from healthy, so it is asked. The same rule is why the deploy target is never inferred from config: a PaaS connected through a Git integration leaves nothing in the repo to sniff.

Two things settle the split rather than taste. Fitting a loop to one small, well-understood project takes hours of dense conversation about where failures surface, whether the app should gain a backend, retention against cron cadence, and what the gate rests on. And installing writes **product code** into the operator's repo: a test suite that becomes the gate guarding the default branch, and possibly a public endpoint. A rubber-stamped table is consent for neither.

Full env contract and per-stage detail: `SKILL.md` and `artifacts/setup.md`. Branch-specific installer detail: `reference/`.
