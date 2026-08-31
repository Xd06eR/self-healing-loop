---
name: self-healing-loop
description: Install the self-healing loop into the current project, as a guided consultation. Determines what the repo can answer, interviews the operator about everything else, records every decision with its provenance, then vendors the core, generates the target adapter and the two GitHub Actions workflows, and guides the SHL_* env/secret setup. Use when the user asks to set up or install the self-healing loop, wire autonomous bug-fixing, or add self-healing CI/CD to a repo.
---

# Self-healing loop — installer

Installs the loop into the repo this skill runs in. After install, a GitHub Actions cron watches the target's failures; on a real one it diagnoses, writes a red reproducing test, fixes the source without touching the test, gates, opens and reviews a PR, merges, deploys and verifies, with no human in the happy path.

**This install is a consultation, and it requires a human. There is no headless path.** Fitting a loop to a project settles where failures surface, whether the app should gain a backend to make them visible, retention against cron cadence, and what the gate rests on when a project has no tests. Those are product decisions the operator owns, and several change what the project *is*. Installing also writes **product code** into their repo: a test suite that becomes the gate guarding the default branch, and possibly a public HTTP endpoint. Without someone to answer, you cannot install correctly, so do not install: stop and report why.

## Rules that govern every phase

**Self-contained.** This skill invokes no other skill. It installs onto machines whose skill set it cannot predict, so a dependency on another skill is one that will be missing. Use a skill from the operator's own environment only when one is present and clearly fits; never require one.

**The visibility rule** decides what you may determine and what you must ask:

> A fact may be **determined** only if getting it wrong would be **visible**. If a wrong answer is indistinguishable from a correct one, it is **asked**, however strong the evidence looks.

**Never spend a question on something the repo answers.** A question the filesystem could have answered trains the operator to stop reading the ones that matter. The mechanical half of the install — vendoring, the manifest, the gitignore, the workflows, the gate's own rules — is yours to do, never to ask about.

**Reliability over speed.** Several rounds is the expected shape, not a failure.

## Progressive disclosure

Load a reference file only on the branch you are on. None of them is vendored; the target gets `SETUP.md`.

- [reference/adapter.md](reference/adapter.md) — at Phase 5b: which adapter methods this target needs, and how to prove each one works.
- [reference/harnesses.md](reference/harnesses.md) — at decision 10: the two harnesses, their credential paths, model-id formats and traps.
- [reference/platforms.md](reference/platforms.md) — when the target deploys somewhere: log-surface families, retention against cadence, detection limits, and the recipe to fill per platform.
- [reference/closing-gaps.md](reference/closing-gaps.md) — when Phase 1 records a gap: generating a test suite or a log surface, with the hardening rules.
- [reference/updating.md](reference/updating.md) — when Phase 2 finds a loop already installed: what may be overwritten, what never may, and how to migrate an older install.
- [reference/verifying-the-install.md](reference/verifying-the-install.md) — **only if the operator asks** how to prove the loop heals. It seeds a deliberate defect, so it is theirs to run and never yours.

## What gets installed

- `.shl/` — the vendored core, copied as-is. Phase 4's `want` set is the authoritative list of it and is executed; nothing restates it. The framework's own `tests/` are **not** installed.
- `.shl/CLAUDE.md` + `AGENTS.md` — the loop agent's operating doc, under both names harnesses auto-load.
- `.shl/.gitignore`, `adapters/target.py` (must export an instance named `adapter`), `SETUP.md` the decision record, and `README.md` for whoever meets this directory without expecting it.
- `.github/workflows/watch.yml` + `heal.yml`; repo vars, plus the provider key and — only on a hosted log source — a log credential.

Plus, **only when Phase 1 records them missing**, the prerequisites a loop cannot run without: a test suite and its runner, a client-error relay, sourcemap and build-identifier config. The **project's** own files; Phase 11 lists them separately.

---

## Phase 0 — Can this repo have a loop at all?

**Required now.** Stop and report if either fails:

- [ ] `git rev-parse --show-toplevel` succeeds.
- [ ] `git status --porcelain` is empty, so the diff reads as "what this skill added".

**The four-condition veto.** All four must hold; any miss is a stop, not a warning.

- [ ] **Ask:** do these failures repeat often enough to be worth automating?
- [ ] **Ask:** can the token budget take a cycle per failure? A cycle is three agent calls, and a failure that recurs before it is fixed costs that again.

Both are asked here because a wrong answer stays invisible until the bill arrives. The other two settle elsewhere: **correctness is machine-checkable** at decision 1, which stops a project that cannot be gated; **the agent tools work** is carried rather than vetoed, since Phase 9b runs no agent and only a real failure or the optional verification exercises them.

**Can the bot act on this repo?** Two settings, both **blocking findings**. Both need a GitHub remote and a working `gh` — the two items below. If either of those is missing, this check is deferred with them rather than failing Phase 0: record it as unresolved and carry it into the Phases 7–10 pending list. Either one wrong and every cycle runs Diagnose, Red, Fix and the gate — spending agent calls — then dies at the PR or the merge. Forever, and loudly, so nothing bad merges; the loop simply never works.

Every `gh` call you make in this phase reads; say so, and say that the one write below is the operator's.

```bash
branch="$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)"
gh api "repos/{owner}/{repo}/branches/$branch" --jq '{protected}'   # the blocking signal
gh api "repos/{owner}/{repo}/rules/branches/$branch"                # rulesets, a separate mechanism
gh api "repos/{owner}/{repo}/actions/permissions/workflow"
```

Name the default branch explicitly rather than using `gh`'s `{branch}` placeholder, which resolves to whatever is checked out. Protected with no bot bypass: report it and let the operator grant one.

`heal.yml` also runs `gh pr create`, which needs `can_approve_pull_request_reviews: true`. That is a repo policy toggle, off by default on repos created since 2023, and **no workflow `permissions:` block overrides it**. The operator fixes both fields; `SETUP.md` carries this and the UI path for a machine with no working `gh`:

```bash
gh api -X PUT "repos/{owner}/{repo}/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=true
```

**Required before the loop runs**, not before writing files. If either is missing, install anyway, record the gap, and mark Phases 7–10 pending:

- [ ] `git remote get-url origin` is a `github.com` URL. This loop is GitHub-native.
- [ ] `gh auth status` succeeds.

## Phase 1 — Determine what the repo can answer

Read-only. Record each finding **with the file it came from**. Verify; never infer from convention.

- **Language / package manager** (`{{LANGUAGE}}`) — `package.json`, `pyproject.toml`, `requirements.txt`, `go.mod`, `Cargo.toml`, `Gemfile`. Multiple means a monorepo: that becomes a question, not a guess.
- **Dependency install** (`{{SETUP_CMD}}`) — what a clean checkout runs before the tests work. Take it from the CI workflow the test command came from; that job proves what a bare runner needs. A local `.venv` or `node_modules` does not count, both being gitignored.
- **Test commands** (`{{TEST_CMD}}`, `{{TEST_ONE}}`) — an existing test job in `.github/workflows/*.yml` is ground truth, because it demonstrably works on a bare runner. Failing that, the project's own script entry, then the runner's conventional invocation. Whatever the source, **run it before recording it**: a command nobody executed is a guess, and this one fires every cycle. Record the suite green or red (`{{SUITE_STATE}}`).
- **Test file layout** (`{{TEST_PATH}}`, `{{REPRO_PATH}}`) — where the suite actually collects from (`testpaths`, `testMatch`, a `spec/` convention). This sets where each cycle's reproducing test is written, repo-relative with a literal `{}` for the issue number. Point it at the wrong directory and the file is written but never run, so the red-then-green proof silently never happens.
- **Test conventions** (`{{TEST_GLOBS}}`, `{{TEST_CONFIG_GLOBS}}`, `{{ASSERT_PATTERN}}`, `{{SKIP_PATTERN}}`) — the gate polices only the forms it recognises and passes everything else **silently**. Its built-ins describe Python and JS: test files `test_*`/`*_test.*`/`*.test.*`/`*.spec.*`, runner config `conftest.py`/`vitest.config.*`/`package.json` plus the Ruby, Rust and JVM manifests, assertions `assert`/`expect`/`raises`/`should_`, skips `skip`/`xfail`/`expectedFailure`. Anything outside that needs the target's own forms — RSpec's `foo_spec.rb` matches none of the globs, Go reports failure as `t.Errorf`, Rust disables with `#[ignore]`. Each value is ADDED to the built-in, never replaces it, so setting one cannot disarm another. When in doubt, set them: an extra pattern costs nothing.

**Gather evidence for Phase 2 without concluding**: whether the project has tests, whether anything runs server-side (API routes, route handlers, server components, SSR, middleware, functions), what deploy configuration exists, the repo's own homepage field (`gh repo view --json homepageUrl`), any platform SDK in the dependency manifest. All input to a question. [reference/platforms.md](reference/platforms.md) explains why config sniffing cannot settle the deploy target.

## Phase 2 — The interview

**Mode first.** A loop directory already present — `.shl/` or an older name — means this is an update: load [reference/updating.md](reference/updating.md) and run its detection before asking anything, since two of its four states are stops. Then read `SETUP.md`, confirm what changed, append; never re-ask a settled decision. Announce the mode in one line; do not ask which to use.

How to ask:

- **Alone when the answer changes what you ask next.** Every decision below carrying a precondition gates the ones after it, so those go singly, in order, waiting for each answer.
- **Batched otherwise**, around four to an exchange, by whatever mechanism this harness offers for putting choices to a person. A decision that cannot change any other still costs a full round trip alone, and a run of those teaches the operator to click rather than read. Then read every answer for content belonging to a *different* question: batched replies land in the wrong slot, and a correction dropped that way is never seen again.
- **A recommended answer with every question**, and the reasoning. The operator is deciding, not doing the analysis.
- **Show the evidence** you gathered alongside it.
- **Do not act until shared understanding is confirmed.**
- **Re-walk a branch when an answer invalidates earlier discovery.** Choosing an error tracker changes both retention and cadence; carrying the old answer forward is how a record ends up describing an install that does not exist.

Show the Phase 1 table first, in one block, values plus provenance, and ask for corrections rather than approval. Then the decisions, in order, skipping any whose precondition does not hold and any the conversation has already answered:

1. **Does this project have a machine-checkable notion of correct?** Tests exist · must be generated · genuinely cannot. The third is a **stop**: the gate is the only thing between a headless agent and the default branch, and it works by running tests. Record as `{{TEST_ORIGIN}}`.
2. **Where do its failures surface?** A log on disk · the host's logs · CI only · nowhere readable yet, because they are client-side. A host logs what runs on **its own** machines, so a browser error reaches none of them — say that before listing the options, and on the last one load [reference/platforms.md](reference/platforms.md) and draw the data flow first, because the option selects a follow-up question rather than a log surface. Record as `{{LOG_SURFACE}}`. The decision the visibility rule exists for.
3. *(2 = client-side)* **How do we make them visible?** Generate a relay endpoint · adopt an error tracker · accept regression-only healing. **This one changes the product**: say so plainly, because option one adds a public HTTP endpoint to their repo. Record as `{{BROWSER_FIX}}`.
4. *(2 = host logs, **or** 3 = generate a relay)* **What is the log retention, and which plan tier sets it?** Ask the tier, look the value up in the platform's own docs, show the number to confirm. Record as `{{RETENTION}}` and set the cron from it (`{{CRON}}`). Wrong here loses failures silently, and idle looks healthy. Retention caps the interval; the repo's Actions-minute allowance prices it, so quote how many runs a month the cadence you recommend costs. Mechanics, including why the relay branch needs this too: [reference/platforms.md](reference/platforms.md).
5. **What deploys this, and how?** A command · push-triggered · nothing. Record as `{{DEPLOY_CMD}}`, empty for the last two.
6. *(5 ≠ nothing)* **How do we know the merged commit is live?** Record as `{{HEALTH_STRATEGY}}`. Push-triggered deploys are asynchronous, so a probe fired right after a merge reads the previous build and reports healthy whatever just shipped.
7. **What is off-limits to the loop?** Paths, services, anything with irreversible side effects. Record as `{{OFF_LIMITS}}`.
8. *(1 = must be generated)* **Who reviews the generated tests, and when?** Record as `{{TEST_REVIEW}}`.
9. **Escalation: who is told, and how?** Record as `{{ESCALATION}}`. The loop stops and asks for a human at the attempt cap, and that has to reach someone.
10. **Which harness, then whose credential?** In that order: `claude-code` or `opencode`, then the credential path, which decides the token's env var and whether a base URL applies. The second half is three values only they hold, so **ask for the values, not a menu of providers**. Paths, model-id formats and the traps: [reference/harnesses.md](reference/harnesses.md). Record as `{{HARNESS}}`, `{{MODEL}}`, `{{BASE_URL}}`, `{{AUTH_ENV}}`, `{{AUTH_PATH}}`. Not discoverable from the repo.

## Phase 3 — Write the decision record

First write of the install. Copy `artifacts/setup.md` to `.shl/SETUP.md` and substitute every `{{…}}` you have.

When decision 1 was *must be generated*, the test-command and repro-path values are not settled until Phase 5a creates the suite. Leave those placeholders and **backfill at the end of 5a** — a record shipped with a literal `{{TEST_CMD}}` reads as a broken install.

Every row carries its **provenance**: *looked up* with the file, *asked* with the operator's answer, or *defaulted* with what the default was. A record that cannot say which is which cannot be audited on re-install.

A decision earns a written reasoning paragraph only when all three hold: **hard to reverse**, **surprising without context**, **the result of a real trade-off**. Everything else is a row. On re-install, append an evolution entry; never overwrite a prior decision.

## Phase 4 — Vendor the portable core

- Copy the manifest into `.shl/`.
- Copy `loop_context/CLAUDE.md` to `.shl/CLAUDE.md`; it auto-loads because the loop runs the agent with cwd `.shl/`. **Also create `AGENTS.md` beside it**, a symlink or a copy. Claude Code auto-loads `CLAUDE.md`, most other harnesses `AGENTS.md`; ship one name and the agent still runs, just uninstructed, which reads as a bad model rather than a missing file.
- Copy **both** `adapters/__init__.py` and `adapters/base.py`. `__init__.py` is what finds and imports `adapters/target.py`; an install missing it dies on the first cycle.
- **Write `.shl/.gitignore`.** Load-bearing, not hygiene: the gate step runs `git add -A`, so without these the cycle's own evidence and raw agent output land in the fix diff.

    ```gitignore
    # self-healing loop: raw agent output + per-cycle evidence, never committed
    evidence/
    *.json
    # part of the install: the harness reads its per-role permissions from it
    !opencode.json
    # what a later update compares against; ignored, it reads as "no install"
    !manifest.json
    *.txt
    *.diff
    *.raw
    __pycache__/
    *.pyc
    ```

    Verify with `git check-ignore -v .shl/evidence/`. Incident memory (`incident_memory/log.jsonl`) is deliberately **not** ignored: `*.json` does not match `.jsonl`, and the loop commits that file so it learns across cycles.

- **Write `.shl/manifest.json`** — `{path: sha256}` for every file vendored here and both workflows. Without it an update cannot tell *stale* from *locally modified*. Shape: [reference/updating.md](reference/updating.md).
- Smoke-check the port, so a truncated copy fails here rather than three phases later:

    ```bash
    cd .shl && PYTHONPATH=. python3 -B -c "import loop, role, evidence, log_compact, gh_state; from guardrails import cli, gate, incident_memory; from adapters.base import TargetAdapter; print('vendored core imports clean')"
    ```

- **Verify, do not just follow.** An instruction is not a check: copy the framework's own `tests/` and, on a target that does not pin `testpaths`, collection walks into them and the gate blocks every cycle forever over files nobody meant to install.

    ```bash
    python3 -c "
    import os, sys
    want = {'.gitignore','CLAUDE.md','AGENTS.md','adapters','agent','guardrails','templates',
            'opencode.json','role.py','loop.py','gh_state.py','log_compact.py','evidence.py'}
    # Written later by Phase 3 and by cycles, so this stays re-runnable on an
    # installed loop. Cycle scratch is skipped by extension, not by name.
    later = {'SETUP.md','INSTALL-REPORT.md','README.md','evidence','incident_memory','__pycache__'}
    scratch = ('.txt','.json','.diff','.raw','.pyc')
    # A wanted file is never scratch: opencode.json ships and ends in .json.
    have = {n for n in os.listdir('.shl')
            if n not in later and (n in want or not n.endswith(scratch))}
    extra, missing = sorted(have - want), sorted(want - have)
    if extra or missing:
        sys.exit(f'FAIL manifest — extra: {extra or None}, missing: {missing or None}')
    print('manifest clean')
    "
    ```

## Phase 5 — Make the target able to run the loop

### 5a — Close the gaps

Only the gaps Phase 1 recorded and Phase 2 decided how to close. Full instructions, including the relay hardening rules and why generated tests must be green at install: [reference/closing-gaps.md](reference/closing-gaps.md).

### 5b — Generate the target adapter, test first

`TargetAdapter` has **one required method and three optional ones**, and skipping one costs something different each time: one refuses before spending an agent call, one burns a full cycle then blocks at the gate, one lets a dead deployment pass. Which of them this target needs, what each must return, and how to prove it: [reference/adapter.md](reference/adapter.md).

The adapter does not deploy or roll back: the workflow runs `{{DEPLOY_CMD}}` and reverts with `git revert`, and those credentials belong nowhere near an agent-adjacent module.

- [ ] Write `.shl/adapters/target.py`, ending with `adapter = <TargetAdapterImpl>()`. Honour `{{OFF_LIMITS}}`.
- [ ] Write its tests **first**, per the reference. Drive at least one with a log the project really produced.
- [ ] Put the adapter's test command in `SETUP.md`. Nothing else runs it — it sits outside the framework's discovery and outside the target's test config, so without a written-down command it runs once at install and never again.

## Phase 6 — Drop the workflows

- Copy `workflows/watch.yml` and `workflows/heal.yml` into `.github/workflows/`.
- **`watch.yml` ships with its `schedule` block commented out. Leave it that way.** Write the `{{CRON}}` value into that commented block so it is ready, but do not uncomment it: that is the operator's edit at Phase 9b. The loop merges to the default branch unattended, so the disabled state is the default rather than something an instruction has to remember to impose.
- Confirm the secret discipline is intact. The invariant is **no secret on any step that executes agent-authored code**: Diagnose writes the reproducing test from an untrusted log, and that code runs under the test runner, so Red, Green, Run-suite, Gate and Verify hold nothing.
    - Do not check this as "one token per step". The Fix step deliberately holds `SHL_AUTH_TOKEN` **and** `GH_TOKEN`, because it also posts the attempt-cap escalation comment and runs no test code itself.

## Phase 7 — Env and secrets

**You set the variables, the operator sets the secrets.** `gh variable set` carries values you already hold; `gh secret set` prompts locally for one only they have. **Never ask for a secret value in the conversation** — it would land in the transcript, and nothing needs it there.

`SETUP.md` carries the full variable table, the secrets and the exact `gh` commands; do not repeat the list here, because two copies of a config table drift. Run its `gh variable set` lines yourself and hand the operator the `gh secret set` lines.

Two things worth saying out loud rather than leaving in a table:

- **`SHL_LOG_TOKEN` is only needed for a hosted log source**, and its absence fails silently. Phase 9 confirms a non-empty read; do not skip that check on the grounds that the variable is set.
- **Omit an empty variable; never set it to `""`.** The workflow cannot tell the two apart, and `gh` prompts on an empty `--body`, eating the next pasted line.

## Phase 8 — Commit the install to a branch and push it

The loop is autonomous once running, but it cannot start from an uncommitted tree. It must not land on the default branch first either, so it lands on a branch and is reviewed there.

**`workflow_dispatch` exposes a workflow only once the file exists on the DEFAULT branch** — not on any pushed ref. On a first install that means nothing can be dispatched until the PR below is merged, which is why Phase 9 has a pre-merge half and a post-merge half. Once the file has reached the default branch, `--ref` can target any branch, which is what makes branch-scoped verification work later.

- [ ] Create `self-healing-loop-install`, commit everything the install wrote, and push it.
- [ ] Open a PR against the default branch. **Do not merge it** — Phase 10 is where the operator reviews and merges.

Commit the loop's files and the project's new files as **separate commits**. The second one is product code the operator is being asked to accept, and it reads very differently from vendored tooling in a combined diff.

## Phase 9a — Verify before the merge

**You do not write a defect into the operator's repo.** Verifying that the loop heals needs a failure, and manufacturing one is the operator's call, not part of installing. Offer [reference/verifying-the-install.md](reference/verifying-the-install.md) and let them decide; never run it yourself.

Both required, neither needs a runner; backfill `{{VERIFIED}}` in `SETUP.md`:

- [ ] `actionlint .github/workflows/watch.yml .github/workflows/heal.yml` — these have never executed. A YAML or expression error means every cycle dies before it starts. If it is not installed, install it or run it in a container; otherwise record it under NOT verified rather than skipping quietly.
- [ ] **Prove `read_log()` reads something.** Have the operator run it locally, supplying `SHL_LOG_TOKEN` for the one invocation — GitHub will not read a secret back out, so this cannot be done from the runner side. Take positive evidence only: non-empty output. An empty read is indistinguishable from a healthy project, and that is exactly the failure that leaves a loop idling forever looking fine.

## Phase 10 — The artifact gate: the PR review

Phase 2 approved a plan. This approves **code an agent wrote into the operator's repo**, which is a different thing: consent to "generate a test suite" is not consent to *these* assertions.

The PR from Phase 8 is that gate, and a real diff is the right place for it. **If Phase 5a generated a test suite, the review must include reading it.** Ask for it explicitly rather than assuming it was inferred from a file list.

Say why. A merge gate is a done-criterion **plus a boundary**: the frozen repro test flips red to green, and nothing was weakened, deleted or reconfigured to get it there. Without the boundary, "make the tests pass" is an instruction to delete tests. Those generated assertions are the boundary, and an unread one is a gate certifying its own author's work.

The operator reviews and merges. Record whether a fresh context audited that suite or they declined — a safeguard nobody can tell was skipped is not one.

## Phase 9b — Verify on a runner, after the merge

Only reachable now: until the merge, `workflow_dispatch` could not see either workflow.

- [ ] **Dispatch the watch once** (`gh workflow run watch.yml --ref <the install branch>`). First time anything runs on a runner: checkout, the harness install, `SHL_SETUP_CMD`, the adapter import and `read_log` with its credential.
- [ ] **Say what this can do before running it.** If `read_log` returns a readable failure, `watch.yml` dispatches `heal.yml` on that ref and a full cycle runs — three agent calls, the deploy command, and a merge into that branch — with no further approval. That is the loop working as designed, but the operator has not yet consented to unattended operation, so either dispatch on a window they confirm is clean or tell them plainly what may follow.

State the ceiling in the report rather than implying more was proved: `loop.py watch` constructs no agent, so **an IDLE result proves nothing about whether the harness runs on a runner.** That is first answered by a real failure, or by the optional verification above.

Then the operator uncomments the `{{CRON}}` block in `.github/workflows/watch.yml`.

## Phase 11 — Report

Evidence before assertions. Nothing here says "should work"; every claim names the check that produced it.

**Write it to a file, not only to the chat.** Copy `artifacts/report.md` to `.shl/INSTALL-REPORT.md`, fill every `{{…}}`, and commit it on the install branch; the template's own opening says why. Then summarise it in the conversation as well.

**Write it before the operator merges**, despite the number: phases order dependencies, not wall clock. The report is part of what they review, and a commit landing on the install branch after it merged reaches the default branch only via a second PR nobody planned. Its sections and the reason for each are in the template.

Also record any Phase 0 item deferred, and that Phases 7 onward stay pending until it is resolved.

**Then write the README and say so.** Copy `artifacts/readme.md` to `.shl/README.md`, fill it, and tell the operator it is the file to read first and the one to send a colleague asking why a bot opens pull requests. Ask before pointing at the project's own root README: that file is theirs.
