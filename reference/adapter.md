# The target adapter

Everything the loop cannot know about this project in particular. One required method, three optional ones, each optional method covering a case the framework has no generic answer for. `adapters/base.py` carries the full contract in its docstrings and ships into the target; this file is the install decision: which methods **this** target needs, and how to prove each one works.

Skipping one has a different consequence per method, and only one of the three is quiet. Without `failing_tests` the gate demands a fully green suite, so on a repo carrying any pre-existing failure nothing merges, ever. Without `failure_ids` on a runtime the framework cannot read, the cycle refuses before spending an agent call. Without `health_check` a green suite beside a dead deployment passes verification — that one fails by looking healthy, which is why it is the one to be deliberate about.

## `read_log()` — required

Return the Phase 2 log surface as text.

Every log the project's own code writes, third-party and vendored trees excluded: Diagnose will happily root-cause a traceback from a tree the project does not own, then write a fix for somebody else's package. Tail or filter to recent error-bearing lines. The framework compacts further, but do not hand it a multi-MB raw log.

**One combination to refuse: never both run the test suite and read a hosted log from inside `read_log`.** Sources otherwise compose freely — this returns a string, so concatenating two is fine — but running the suite here executes **agent-authored code**, namely previous cycles' merged reproducing tests, which were written from untrusted logs. The workflow hands this step `SHL_LOG_TOKEN` so a host-log adapter can authenticate, so an adapter doing both puts a live platform credential in reach of code an agent wrote, on every tick, with no diff for the gate to inspect. Pick one: read the hosted log, or run the suite. If the target genuinely needs both signals, the suite half belongs in CI going red, which the loop reads as a log surface without holding a credential.

## `failing_tests()` — implement it

The set of test IDs failing right now, parsed from the test command's own output.

Every runner reports differently (`FAILED path::name`, `✕ name`, `--- FAIL: Name`, `rspec ./spec/x_spec.rb:12`), which is why the adapter supplies this rather than the framework guessing. Run the suite, read what it actually prints, and parse that.

With it, the gate blocks tests that were passing and now fail. Omit it only if the runner genuinely cannot enumerate failures.

## `failure_ids(raw_log)` — implement it unless this target is Python or JS/TS

The stable identity of each distinct failure in a log, one string per failure, shaped `Type@path:line`.

Built-in parsing reads Python tracebacks and V8 stacks. Every other runtime yields error text that produces no identity: a Go panic separates its trace from its message with a blank line, which ends the block the built-in line filter was collecting; a Ruby backtrace uses its own frame syntax. Issue dedup, the attempt cap, incident recall and the recorded incident all key on this identity.

**What you return is published, and it is not redacted for you.** The identity goes into the dedup marker on a GitHub issue and into the incident log the loop commits to the default branch. Running it through the scrubber is not an option: `panic@handler.go:42` is exactly the shape of an email address, so redaction would rewrite it and collapse every distinct failure onto one key. Key on the type and the frame; never let the message payload into the string. That is the same rule as "stable" below, seen from the other side — a value that varies between two occurrences of one bug is also a value carrying whatever the log happened to put there.

**It is handed the raw log, never the compacted signal the agent is prompted with.** The workflow carries both: compacted text into the prompt, raw text to everything that derives an identity. So key on whatever the log actually contains — frames, goroutine headers, whatever your runtime prints — without checking whether compaction would have kept it.

**Find out which case this target is in.** Save a real failure log the project has actually produced, then ask the seam the workflow asks:

```bash
cd .shl && PYTHONPATH=. python3 -B -c "
from adapters import optional_ids_fn
from log_compact import failure_fingerprints, unfingerprintable
raw = open('/path/to/a/real-failure.log').read()
print('REFUSED — implement failure_ids' if unfingerprintable(raw, ids_fn=optional_ids_fn())
      else failure_fingerprints(raw, ids_fn=optional_ids_fn()))
"
```

The three properties an identity needs — stable, specific, project-owned — and what `None` and `[]` each mean are in `adapters/base.py`'s docstring, beside the method you are implementing.

## `health_check()` — implement it if the target deploys anywhere

One cheap request to the deployed service: `True`, `False`, or `None` when there is nothing to probe. Follow `{{HEALTH_STRATEGY}}`.

The post-deploy suite runs on the runner against merged source and cannot tell you the deployed thing answers.

## Verifying it

Test first: write `adapters/tests/test_target.py` and `adapters/tests/__init__.py`, watch each test fail, and confirm it fails for the **right reason**: the behaviour is missing, not a typo or an import error. Then implement, then green.

```bash
cd .shl && PYTHONPATH=. python3 -B -m unittest adapters.tests.test_target -v
```

**Drive at least one test with a real captured log**, not a hand-written one. `read_log`, `failing_tests` and `failure_ids` all parse output produced by another program, and a test written against imagined output tests the imagination: a parser exercised only with short hand-written strings passes every assertion and still matches nothing when it meets a real log line, so the feature is dead behind a green suite.

**Name captured fixtures with a neutral extension.** `.log` and `.txt` are both swallowed — the first by many projects' own gitignore, the second by the loop's, which treats `*.txt` as cycle scratch. A fixture that is ignored passes locally and vanishes from the commit, so the adapter's tests fail on a fresh clone with no obvious cause. `.fixture` or `.captured` are safe.

Where `read_log` or `health_check` needs credentials or a live service, mock it here and flag the real path unverified until the install's own verification exercises it with the operator watching.
