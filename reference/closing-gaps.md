# Closing gaps — generating what a loop needs but the project lacks

> Read this only when Phase 1 recorded a gap. Loaded by the installer; never vendored into the target.

Some projects cannot run a loop as they stand: nothing tells you whether a change is correct, or nothing records that a failure happened. The installer creates those prerequisites, because a loop only installable into a project already prepared for it is a loop almost nobody can install.

**Everything here is the project's own code**, in the project's own tree, in the project's style. It is not loop machinery and it does not go in `.shl/`. It stays behind if the loop is removed, which is correct: a project should keep its tests and its error reporting.

Phase 11 lists everything written here **separately from the loop's own files**.

---

## Gap: no test suite

The gate runs tests. With none, nothing sits between the fix agent and the default branch.

Before writing anything, **read the lockfile and the existing config**. A project often already depends on a runner nobody wired up, and adopting what is there beats adding a second one.

- [ ] Pick the runner the ecosystem already expects, preferring anything present in the lockfile or config: vitest for a Vite or Next project, jest where it is already configured, pytest for Python, `go test` for Go. Never introduce a second runner beside an existing one. Where the runner is not already a dependency, installing it **adds a package to the operator's manifest** — name it and get agreement first, rather than treating it as implied by the decision to generate a suite.
- [ ] Generate tests over the project's **pure logic first**: functions that take values and return values, with no I/O, network or DOM. They need no fixtures, they are where regressions actually bite, and they are the part you can characterise correctly from reading alone.
- [ ] **Drop anything you cannot justify from the code.** A test whose expected value you inferred rather than derived is worse than no test: it becomes a requirement the fix agent must satisfy forever.
- [ ] Wire the runner into the project's own conventions (its test script, its config, its test directory) and set `SHL_TEST_CMD`, `SHL_TEST_ONE` and `SHL_REPRO_PATH` to match what you created.
- [ ] **Run it, and require it fully green before moving on.** See below for why a red one is never a baseline.

### What these tests are, stated precisely

They are **characterization tests**: they record what the code does today, not what it ought to do. You did not write this project and cannot know its intent. Assert only on behaviour the code makes evident, label the suite as generated at install, and where behaviour looks wrong, leave it asserted as-is and say so in the report rather than encoding a guess as a requirement.

The risk is narrower than "an agent grading its own homework", and stating it loosely hides the part that matters:

> A **repro test** is proven red on broken code and green after the fix. It is validated against a failure that actually happened, and Diagnose writes it in a separate process from the one that fixes. That is what validates it.
>
> A **characterization test** is validated against nothing. If the code has a bug today, the test freezes that bug as a requirement, and the loop will defend it forever.

Hence the artifact gate before the cron starts: not because the author is untrusted, but because **nobody has checked the assertions against intent**. Three things keep it honest, and all three are required:

1. generated **once**, at install, never by a heal cycle;
2. asserting on observable behaviour, never on implementation;
3. **the cron does not start until a human has read them.**

### Generated tests must be green at install

A characterization test describes what the code already does, so a red one means your description is wrong. It does not mean you found a bug, and it does not mean you created a baseline.

Fix the test. If it stays red after you have re-read the code, you have found a genuine defect: **delete that test**, report the defect, and let the loop heal it later from a real failure signal.

Never leave a red test behind as a "baseline". A permanently-failing test that nobody chose is indistinguishable from one the gate should be blocking on.

A deliberately failing test is self-test scaffolding. The install never writes one; it belongs only to the operator's optional verification ([verifying-the-install.md](verifying-the-install.md)), on the install branch, removed before that branch merges. It is never installer output.

---

## Gap: the app produces no host-visible logs

Phase 1 found a deployed target where nothing runs server-side, so failures happen in the visitor's browser and the host records nothing.

This is the gap that **changes the product**: closing it adds a public HTTP endpoint to someone's repo. It is asked, never assumed, and the alternatives (adopt an error tracker, or accept regression-only healing) are real answers.

- [ ] Generate a minimal client-error relay in the project's own idiom: an endpoint that accepts a POSTed error and writes **one parseable line** to stdout, plus a small client-side listener for `error` and `unhandledrejection` that posts to it. Include the build identifier in the payload: stack frames point at hashed build chunks, and without knowing which build produced them they cannot be mapped back to source.
- [ ] **Harden it, because it is an unauthenticated public write into the loop's own input.** All four, not a subset:
    - cap the body size;
    - rate-limit per client;
    - reject anything not matching the expected shape;
    - accept same-origin requests only.

    Without these, anyone can post arbitrary text and make the loop diagnose it, file issues and burn agent calls. Note in the report that per-instance rate limiting only thins a flood on serverless.
- [ ] **Never let reporting an error throw.** The user is already looking at a broken page; a failure here turns one lost error into two.
- [ ] If the platform serves minified code, enable published sourcemaps and resolve frames **before `read_log` returns**. Load-bearing, not cosmetic: unresolved frames carry a per-build hash, so an identical failure fingerprints differently on every deploy and incident memory can never match.
- [ ] If the platform deploys on push, render the deployed commit into the served output so `health_check` can prove the merged commit is live rather than that the site answers.

Per-platform specifics: [platforms.md](platforms.md).

---

## The credential trap

**One `read_log` must not both run the suite and read a hosted log.**

Running the suite inside `read_log` means `read_log` executes **agent-authored code**: previous cycles' merged repro tests, written from untrusted logs. The workflow hands that step `SHL_LOG_TOKEN` so a host-log adapter can authenticate, so an adapter that does both puts a live platform credential in reach of code an agent wrote. That is the one combination to refuse.

Two consequences for the adapter you generate:

- If the failure surface is a hosted log, `read_log` reads **only** that. It never falls back to running the suite when the log comes back empty, however tempting that looks — an empty log is information, and the fallback is what turns a missing credential into a silent, permanent idle.
- If the failure surface is the suite, `read_log` runs the suite and the operator sets no `SHL_LOG_TOKEN` at all. Nothing to leak.

The provider key (`SHL_AUTH_TOKEN`) is on neither step. Watch and the log re-read run no agent, which is what lets the workflow withhold it there.
