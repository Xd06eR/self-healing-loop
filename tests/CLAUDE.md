# Testing the framework

Read this when writing or reading a test here. The commands are in the root [`CLAUDE.md`](../CLAUDE.md); this file owns what makes a test in this repo worth anything.

Two activities, kept apart deliberately. **Development testing** verifies this framework and is ours. **Installation testing** verifies one installed loop, belongs to that loop's operator, and is optional, because proving a loop heals needs a failure and manufacturing one is the operator's call. Its procedure is `reference/verifying-the-install.md`, which the installer offers and never runs.

## What actually backs this code, stated because the green suite overstates it

Two partial cycles have run on real runners against real failures, and both predate substantial rewrites of the gate, `guardrails/cli.py` and `heal.yml`, so they evidence the design rather than this tree. **No cycle has ever completed.** Everything from the merge onward, the gate refusing a bad fix, the attempt cap, the escalation path and OpenCode as a harness are covered by unit tests and by nothing else.

That is what makes the rules below load-bearing rather than good practice.

## Mutation is the only proof available

No integration test sits behind this suite, so it is only as good as its ability to fail. Plant the defect, watch the suite go red, and **assert the unmutated control exits 0 first**, or the run proves nothing.

Target the mutation precisely. A replace that hits the first of two similar sites tests the wrong one, and the run still looks green-then-red.

A stub must emulate the behaviour under test. A `jq` stub that always exits 0 cannot tell `jq -er` from `jq -r '// ""'`, so the test passes on the command that strands a cycle.

## Assert the condition, not its vocabulary

Anchor a check on the thing it names, never on text that can move. A test that searches a whole document for a flag also matches the prose describing that flag, so stripping it from the command block leaves the test green.

## Verify at the seam the product uses

Calling the function is not running the loop. Prove every claim from a cycle's evidence bundle (`diagnose.prompt.txt`), never from a direct call. Where two drivers must do the same thing, give them one function to call rather than one rule to follow, which is why `run_diagnose` and `run_fix` compute the incident recall themselves instead of accepting it as a parameter.

Every Python test can be green while the workflow passes the wrong file, so a claim about the workflow is proved by executing the step, not by inspecting its text.

## Drive at least one test with a genuine captured artifact

Wherever the input is produced by another part of the system. A suite can be green for months against short hand-written strings while the feature it covers is dead, because the tests encode the intended contract rather than the real one.

## Rules for running anything here

- **This tree is read-only during testing.** All test work lives in scratch. The framework is the product. This one hides: running `loop.py` or `evidence.py` from here creates `.shl/evidence/<cycle>/` beside them, and a cycle that wrote nothing leaves the directory **empty**, which git does not track and `git status` reports as clean. A clean status is not evidence that nothing was written.
- **Run the loop only against a scratch clone, or a repo it is genuinely installed into.** Every cycle mutates the target: the fix and its reproducing test land in tracked files and have to be policed out of every commit.
- **Re-vendor after any framework change, before drawing any conclusion from a target.** The target holds a COPY of the core, so a framework fix does nothing there until re-vendored, and a cycle against the old copy reports a confident wrong result. Never hand-copy. `.shl/manifest.json` records a hash per vendored file and `reference/updating.md` compares it against both the installed files and the framework, which answers "is this copy current" and not "is it correct"; nothing runs it on a cycle.
- **Answer the installer's interview honestly; never volunteer the conclusions.** Phase 1 determines what the repo can answer for itself and Phase 2 asks the rest, so supplying "no runtime log" produces an adapter that reads the test suite instead of the real log. Building the answers in advance is worse: the installer is handed a finished job and passes while proving nothing.

## The `actionlint` skip is deliberate

The suite's Actions-schema check is guarded by `shutil.which`, so without the binary it skips and the run still reports `OK`: a real defect in `heal.yml` or `watch.yml` flips from caught to `OK (skipped=2)` purely on `PATH`. Hardening it into a failure would break the stdlib-only promise for anyone on a bare Python, so the binary is the developer's responsibility and the lint is a required manual step before any release. Nothing else validates the two files that **are** the product.

## Prose is not testable, and a check must never pretend otherwise

Every shipped reader-facing document is bound by the current-fact convention: no session narrative, nothing phrased relative to an earlier version, no anchor a later reader cannot resolve. Nothing in the suite enforces it and nothing in the suite can, because whether a document assumes context it does not supply is a semantic judgement while a regex matches a fixed vocabulary and no more.

A check named for that convention while implementing the vocabulary is worse than no check, because it reports coverage of a rule it does not hold. A mechanical guard is legitimate only for mechanical leaks: a date, a specific target repo's name, stage or session numbering. It is named for exactly those.

The convention is verified the one way it can be, by a fresh context reading the document with no history. For `SKILL.md` that is already the install's acceptance criterion: the interview must reach the right conclusions without being led. The same rule binds any doc-to-code agreement whose subject is meaning rather than a token: pin what is mechanically checkable, state the ceiling, and send the rest to a reader who was not there.
