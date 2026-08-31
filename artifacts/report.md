# Self-healing loop — install report

> Written by the installer at the end of Phase 11, copied to `.shl/INSTALL-REPORT.md`. It records what was installed, what was written into the project itself, and what nobody has checked yet. A chat summary dies with the session; the moment this matters is months later, when someone asks why this repo serves an error endpoint nobody remembers agreeing to.

**Installed on branch:** `{{INSTALL_BRANCH}}` · **Decision record:** `.shl/SETUP.md`

Every claim below names the check that produced it. Nothing here says "should work".

## What was installed

### The loop's own files

Everything under `.shl/`, plus `.github/workflows/watch.yml` and `heal.yml`. Removing the loop means deleting those; nothing else here depends on them.

{{LOOP_FILES}}

### Files written into the PROJECT

**This is product code in this repo. It survives uninstalling the loop, and it is yours now.**

{{PROJECT_FILES}}

If that list is empty, the project already had everything a loop needs and the installer added nothing of its own.

## What the gate now rests on

**Test origin:** `{{TEST_ORIGIN}}`

If the tests were generated at install, they were written by an agent from a codebase it had just met, and they describe what the code *did that day* rather than what it is supposed to do. They are the boundary the merge gate enforces. Until someone reads them against intent, nothing has checked whether they encode correct behaviour or a bug that happened to be present.

Behaviour that looked wrong and was asserted as-is anyway, if any:

{{ASSERTED_AS_IS}}

## How each decision was reached

| decision | choice | provenance |
|---|---|---|
{{DECISION_PROVENANCE}}

Anything marked *defaulted* was chosen by nobody. Treat each as an open question.

## Verified here, and how

One line per check that actually ran, each naming the command and what it returned. A check that was skipped belongs under *NOT verified* instead, not here with a hedge — the whole value of this section is that a reader months from now can tell the difference between something that was tested and something that looked fine.

{{VERIFICATION_EVIDENCE}}

## NOT verified

Normal at install time; listed so it is not mistaken for tested. **The first real cycle falsifies most of it** — whoever runs that cycle rewrites this section, or the report starts lying the moment the loop first works.

- **No cycle has ever completed.** The install writes no deliberate defect into this repo, and proving the loop heals needs a failure — so the agent has never been invoked here, the gate has never blocked anything, and the merge path has never run. The dispatched watch exercises checkout, the harness install, dependency install, the adapter import and `read_log`, but **not the agent**: `loop.py watch` constructs none, which is exactly what lets that step withhold the provider token — and on a first install it has commonly not run at all, being reachable only after this install merges, so it belongs under NOT verified until it has. `SETUP.md` § *Optional: prove it actually heals* is how to close this.
- **`health_check`** when there was nothing live to probe.
- **The gate's language coverage is whatever the install supplied.** Its built-in patterns describe Python and JS/TS; every other runtime is policed only through the `SHL_TEST_GLOBS`, `SHL_ASSERT_PATTERN` and `SHL_SKIP_PATTERN` values recorded in `SETUP.md`. Some assertion forms happen to match the built-ins (RSpec's `expect`, AssertJ's `assertThat`, NUnit's `Assert.That`), but a *coincidental* match is not coverage, and the test-file globs are the part most likely to be wrong: RSpec's `foo_spec.rb` matches none of them. Failure fingerprinting is separate and reads Python and V8 stacks only, with `TargetAdapter.failure_ids` covering the rest. If this project is not Python or JS, the must-pass case in `SETUP.md` is how to confirm the gate can see its tests at all.
- {{OTHER_UNVERIFIED}}

## Still open

Anything the install could not finish and left for later: a Phase 0 check that could not run, a repo setting someone else has to change, a decision postponed. Each entry says who has to act, because an open item with no owner is one nobody closes.

- {{DEFERRED}}

## Before the cron goes on

Follow `SETUP.md` § *Before going live*, which owns those steps; nothing in this report changes them. `SETUP.md` § *Optional: prove it actually heals* is the only way to watch a full cycle, including the must-pass case where a test-weakening fix must be BLOCKED — but run it **after this install has merged**, not before. `workflow_dispatch` cannot see a workflow until the file exists on the **default** branch, so a self-test attempted earlier has nothing to dispatch and reports a failure that says nothing about this loop.

## Residual risks

Boundaries of the design, not bugs awaiting a fix. **`SETUP.md` owns this list**; these four are the ones that bite most often, repeated here because whoever reads this report months from now may not be the person who ran the install. Change them there, not here.

- the merge gate compares lines, not meaning;
- red-then-green only covers failures that reduce to a test;
- rollback restores code, not consequences;
- the confidentiality scrubber knows secret and PII shapes, not your client names.
