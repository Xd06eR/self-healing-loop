# Verifying an installed loop end to end — optional

> **Optional, and the operator's to run, not the installer's.** Nothing in the install performs any of this. Read it when you want proof the loop actually heals, beyond the checks the install already ran. Loaded by the installer only if asked; never vendored into the target.

**Do this after the install PR has merged.** `workflow_dispatch` exposes a workflow only once the file exists on the default branch, so nothing here can be dispatched before the merge. Afterwards `--ref` targets any branch, which is what keeps the rest of this on a branch.

The install verified what it could without changing behaviour: both workflows lint, and `read_log()` returned something real. That leaves the important thing unproven — **no cycle has completed on this repo**. The agent has not been invoked, the gate has blocked nothing, and the merge path has not run.

Proving it needs a failure, and the only failure safe to use is one you create deliberately.

## What this touches, before you decide

- **Everything happens on the install branch.** Every cycle follows the ref it is dispatched on: checkout, the PR base, the merge, the post-deploy verify and the incident record all use that branch. The default branch is untouched, and so is anything deployed from it.
- **It spends agent calls.** A full cycle is three: Diagnose, Fix, Review. The must-pass case below spends more.
- **It writes a real defect into a real repo**, briefly. Removed before the branch merges. Never do this against real client data, and never on a branch anything deploys from.
- **If your target deploys on push**, confirm the install branch does not trigger a deployment before you start.

If any of that is unacceptable, do not run this. The loop still works; you simply have not watched it work, and the install report says so under NOT verified.

## The happy path

- [ ] **Give it something to heal.** One small, self-contained defect in a copy of a real source file, plus a test that fails on it, committed to the install branch. Keep it obvious and local — this is disposable self-test material, not a puzzle. Put it where your log surface will actually record it: a failure nothing logs does not exist to the loop, and the loop will idle looking healthy.
- [ ] **Make it fail like an error, and confirm the log actually holds it.** Compaction keeps only lines carrying error vocabulary and a trace, so a defect that logs a plain sentence is discarded, the watch reports IDLE, and it reads as a broken loop rather than a badly-shaped probe. Print what the adapter actually sees before spending a dispatch — supply `SHL_LOG_TOKEN` inline for the one call on a hosted source:

  ```bash
  cd .shl && PYTHONPATH=. python3 -B -c "from adapters import load_adapter; print(load_adapter().read_log())"
  ```
- [ ] **Dispatch the watch on that branch**, or use *Actions → sh-watch → Run workflow* and select it.

    ```bash
    gh workflow run watch.yml --ref self-healing-loop-install
    ```

- [ ] **Confirm the whole chain**, not just the ending: issue filed · reproducing test written and proven RED · fix applied · gate green · PR opened · review passed · merge · deploy if configured · verify · incident recorded.
- [ ] **Read the evidence bundle** from the run's artifacts. It carries each role's exact prompt and raw output, which is the only way to see *why* the agent did what it did.

## The must-pass case

A loop that only proves the happy path has not been verified. The gate is the single thing standing between a headless agent and your default branch, and its job is refusing bad work.

**One exception, and it is not a formality.** If this project is not Python or JS/TS, this section is **required** rather than optional: the gate's weakening detection is only partly language-aware, and nothing else confirms it holds on your stack.

You cannot inject a fix into a cycle — the agent authors it — so run the gate against a weakened diff directly. Delete an assertion from any test file, then:

**Pass this project's own conventions as flags.** `heal.yml` gives the gate `--test-globs`, `--test-config-globs`, `--assert-pattern` and `--skip-pattern` from the matching `SHL_*` variables on every cycle. **The CLI reads no environment at all** — those variables reach it only because the workflow turns them into flags — so a bare invocation here runs a *narrower* gate than the real one and reports a failure the real gate would not have. Omit any flag whose value is empty:

```bash
git add -A && git diff --cached --unified=0 > weakened.diff
PYTHONPATH=.shl python3 -B -m guardrails.cli gate --diff weakened.diff \
  --test-globs '{{TEST_GLOBS}}' --assert-pattern '{{ASSERT_PATTERN}}'
```

- [ ] It exits non-zero and names the file and the assertion it lost. Restore the assertion afterwards.

If it exits 0, stop and do not enable the cron. The pass line says which of the two things went wrong:

- `0 test file(s) matched the test globs` — the gate never recognised that file as a test. Either you omitted `--test-globs` above, or the value in `SHL_TEST_GLOBS` does not match this project's convention.
- A non-zero count, and still a pass — the file was recognised and the removed assertion was not. Your runtime's assertion form is not in the built-ins, so `--assert-pattern` (and `SHL_ASSERT_PATTERN`) has to name it.

**Setting the repo variable alone does not change this command.** Fix the value, then re-run with the flag.

## Afterwards

- [ ] Remove the seeded defect and its test.
- [ ] Confirm the branch carries nothing synthetic before you delete it.

## If the target has no failure surface yet

An interview that landed on *CI only* or *accept regression-only healing* has no runtime log to plant anything in. Make the suite go red instead: the seeded defect breaks an existing test, and that redness is the signal. The rest of the checklist is unchanged.
