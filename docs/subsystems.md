# Subsystems — what each module guarantees

Read this when changing `log_compact.py`, `guardrails/gate.py`, `guardrails/incident_memory.py`, `evidence.py` or `adapters/__init__.py`. The seams those modules sit behind are in [`architecture.md`](architecture.md).

These files are vendored into every target, so a change here reaches installed loops only after a re-vendor.

## The gate asks two separate questions

They must stay separate: *is the bug fixed* (the frozen reproducing test flips red to green) and *did the fix break anything* (`gate.new_failures`: no test that was PASSING now fails). Answering both with one all-green check makes a single pre-existing failure veto every correct fix, which is unusable on any real repo. Baseline failures come from the optional `TargetAdapter.failing_tests()`; without it the strict all-green rule applies.

The predicates return the violation rather than a bare boolean, so the CLI has something to print and `None` stays falsy for every truthiness caller. The verdict format itself is pinned in the root [`CLAUDE.md`](../CLAUDE.md) and asserted against a real refusal by `tests/test_cli.py`.

**Fix may add regression tests, which grows the suite the gate rests on.** Taken deliberately: a fix without regression cover is a worse fix. A merged cycle can therefore add assertions the fixing agent wrote, reviewed by the Review role and by whoever reads the PR. The gate encodes the same split, `is_test_helper_touched` refusing *modifications* beside the frozen test and excepting *additions*, so `templates/fix.md`, `loop_context/CLAUDE.md` and the gate draw the line in one place: add freely, never edit what exists.

**The reviewer's `reason` is published on either verdict.** It is the field every role reports prompt injection in, so reading it only on a BLOCK means an approval flagging an injected log reaches nobody, while the `issue_body` carrying that injection travels on into incident memory and replays into every later cycle matching the same failure. Both paths scrub it and comment it on the PR. The evidence bundle holds a copy either way, and that is a second copy rather than the channel: an artifact nobody downloads on a green cycle is not where a warning gets read. A failed comment warns instead of blocking, because refusing to ship a fix that passed both gates trades a real merge for a cosmetic one.

## Log compaction keeps one slot per distinct failure

Not a raw tail cut. A health check that 500s on every page render otherwise buries a rarer bug under dozens of identical tracebacks, and a tail cut drops it entirely, so the loop never sees it and never heals it. Repeats collapse to their most recent occurrence plus a count.

Three things make "distinct" mean what it should:

- A chained traceback is ONE block. The intermediate exception before `During handling of the above exception` is a library step, not the failure.
- The identifying frame is the deepest one the PROJECT owns, not the deepest overall. Two unrelated bugs both bottoming out in the same line of starlette would otherwise be one failure.
- Truncation drops whole blocks rather than slicing mid-traceback, because a sliced traceback loses the frames that give it an identity.

## Every failure identity derives from the RAW log

Never from the compacted signal. The two views are not interchangeable and the split is load-bearing.

Compaction keeps error lines plus their INDENTED continuation, and a Go panic puts its trace behind a blank line, which ends the block so everything after it is dropped however it is indented. Identifying from compacted text therefore hands `TargetAdapter.failure_ids` a message with no frames, on precisely the runtimes that method exists to serve: it returns `[]`, `unfingerprintable` reports the failure unreadable, and the cycle is refused on every tick forever.

So `loop.py watch` takes a path and writes the log as read; `fingerprint-marker`, `find-issue`, `recall_incidents` and `record_cycle` all read that file, and only the agent's prompt gets the compacted signal. A missing raw path is refused rather than defaulted to the signal. Pinned at the workflow as well as in Python: the Python keys correctly on whatever it is given, so the only place the defect can live is the argument, and that is a seam no unit test crosses.

**Fingerprinting reads Python and V8 stacks, takes anything else from the target, and refuses what neither can identify.** A Go, Ruby, Rust or Java traceback yields no `Type@path:line`, which would leave issue dedup, incident recall and the attempt cap keyed on nothing. `TargetAdapter.failure_ids` supplies the identity, resolved through `adapters.optional_ids_fn` by every consumer so the marker, the recall and the record cannot key differently, and handed the RAW log because the frames it keys on do not survive compaction. `log_compact.unfingerprintable` separates a log with no failure from a log whose failure could not be read, stopping the second in `watch.yml` before any agent call is spent.

The published identity is **not scrubbed**, and must not be: `panic@handler.go:42` is character-for-character the shape of an email address, so redaction would rewrite it and collapse every distinct failure onto one key.

## Incident memory matches on fingerprints, never on the issue title

`log_compact.failure_fingerprints` derives `ExceptionType@path:line` from the same raw log on both sides, so an identical failure keys identically, and paths are made repo-relative so an incident recorded on a laptop still matches on a runner. A title cannot work: it is prose a model writes fresh each cycle.

Exact-set intersection is also the primary defence against context pollution, since an unrelated incident never enters the prompt however many accumulate. `loop.MAX_RECALLED_INCIDENTS` and `MAX_ROOT_CAUSE_CHARS` cap the pathological case where one failure recurs dozens of times, with reverted incidents ranked first so a cap never drops a KNOWN-BAD warning.

The store is bounded too: `record_incident` prunes to 500 records once over the cap, rewriting only then so the normal path stays an append and each incident commit stays a one-line diff. A `reverted` record is exempt at any age, because it is the one thing telling a later cycle that the obvious fix was tried and made things worse.

## The evidence bundle

Each role writes its exact prompt, raw output, stderr and metadata to `.shl/evidence/<cycle>/`, alongside the cycle's signal, diff, gate and suite output. Scrubbed identically local and cloud, uploaded as an Actions artifact under `if: always()`, so a cycle dying at the gate still ships its evidence.

`SHL_CYCLE_ID` (the Actions run id) pins all three role processes to one directory. Locally Diagnose opens a UTC-timestamped directory (`YYYYMMDD-HHMMSS`) and Fix and Review join the most recent one, which is why that format must keep lexicographic and chronological order identical.

Two invariants are pinned by tests: env VALUES never reach disk, only key names, since that dict holds the provider token; and the prompt is not duplicated into the metadata.

## The adapter reads the whole `vars` context

Not a per-variable allowlist. Workflow templates can only name the `SHL_*` variables the framework knows about, so a variable an adapter invents is correctly set on the repo and absent at runtime, and hand-added `env:` lines are dropped by the next re-vendor. `SHL_VARS: ${{ toJSON(vars) }}` plus `adapters.hydrate_repo_vars` deletes both failures rather than detecting them; explicit entries still win, and `secrets.*` is a separate context that never travels this path.

Hydration is confined to the `SHL_` namespace and skips `SECRET_ENV_VARS`, for two different reasons. `vars` carries organization-level variables inherited by every repo in the org, so an unfiltered fold would let an org-wide `NODE_OPTIONS` or `GIT_SSH_COMMAND` change how this loop's subprocesses run. And a provider token stored as a variable rather than a secret would otherwise be delivered into the one step built to run without it.

A blob that is present but unreadable raises rather than degrading, because degrading quietly reinstates the failure the mechanism exists to remove.
