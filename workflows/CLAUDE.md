# Editing the workflows

Read this before changing `heal.yml` or `watch.yml`. These two files **are** the product: no unit test runs them end to end, and `actionlint` is the only thing that reads them as YAML. Run it before any release.

`heal.yml`'s own header comment owns the invariants an editor trips over first, and they are not repeated here: the secret discipline (no step running the test suite or the repro test may hold a secret), the `PYTHONPATH=<loop root> python -B -m guardrails.cli` invocation rule, and the rule that every intermediate file goes inside `.shl/`. This file owns the guard and the pins.

## The loop-tree guard

**Fix runs with cwd `.shl/`, which is the loop's own code.** Two things keep it out that a tamper cannot reach: whichever harness is running denies edits there, and a `git`-only step runs immediately after Fix, before anything imports from that tree. `gate.is_loop_tree_touched` covers the same ground but is imported FROM the tree it judges, so it is a third check rather than a second control.

Moving Fix to the repo root would not help, since `.shl/` sits inside the repo root and an edit-permitting mode auto-approves it either way. A cwd-relative deny is what closes this.

### It asks git three questions and needs all three

**Modification.** `git diff` reports modified TRACKED files only.

**Creation.** Invisible to `git diff`, and the cheaper tamper: an interpreter started with `.shl` on `PYTHONPATH` imports `.shl/sitecustomize.py` at startup, before `-m guardrails.cli` loads, and `-B` disables bytecode rather than `site`. The vendored `.gitignore` covers scratch, not `*.py`, so such a file would be committed and run every later cycle. `git ls-files --others --exclude-standard` answers this half and honours that same `.gitignore`, so ordinary cycle scratch stays quiet.

**Git's own execution surface.** `.git/config` and `.git/hooks/` are executable by git itself, live outside every diff, and are invisible to `gate.py` and to the Review agent alike. The step compares them against a hash taken immediately before Fix and published as that step's output, since a later step cannot rewrite a completed step's outputs, whereas `$RUNNER_TEMP` is outside the checkout but not outside the agent's write reach. The comparison runs before the guard itself calls git, because `core.fsmonitor` executes on the next git invocation and would otherwise fire, restore the config, and erase the evidence of itself.

### Position is load-bearing, and the git-only half runs at two of them

Nothing that imports from `.shl/` may run between Fix and this guard, by `PYTHONPATH`, a changed cwd, or a script path, or the tamper executes before the check that would catch it.

`Green` and `Run suite` then run agent-authored code again, so a second step repeats the git-only half between `Run suite` and `Gate`. The one `.shl` import in between is `Run suite`'s own scrub, which holds no credential. Five `.shl`-importing invocations sit earlier in the job and therefore upstream of Fix, with nothing planted for them to execute: the harness-install probe, `find-issue`, the two issue scrubs and the fingerprint marker. Name them, because a reader who counts the imports without that qualifier finds five apparent violations and no way to tell whether the rule or the workflow is wrong.

**Only the git-only half repeats.** `git diff --quiet HEAD` and `git ls-files --others` are fresh queries against the working tree with no baseline, so a second instance cannot disagree with an earlier measurement, whereas repeating the surface hash would compare a value across a step boundary. **A cross-step comparison of a runner-allocated path is the one thing this guard must never do.**

`tests/test_loop_dir.py` runs the shipped step against a real repository in both directions: it refuses a planted `sitecustomize.py` and passes on the scratch a real cycle leaves.

The steps downstream are what this protects: the gate that judges the fix, `Commit + PR` and `Publish review verdict` under `GH_TOKEN`, and `Review` under the provider token.

### Three gaps it does not reach, accepted rather than closed

Every candidate fix is a claim about runner behaviour, and a guard failing closed on every cycle blocks the loop entirely while looking like a working check.

- **Git also takes executable configuration from the ENVIRONMENT.** `GIT_CONFIG_COUNT` with `GIT_CONFIG_KEY_*=core.hooksPath` runs a hook from an attacker path, and `GIT_SSH_COMMAND`, `GIT_EXTERNAL_DIFF` and `GIT_PROXY_COMMAND` each name a program git executes, none of which touches a file this block hashes. Fix reaches that surface by appending to its own `$GITHUB_ENV`, which applies to every later step including the one running `git push` under `GH_TOKEN`. Comparing env across the two steps is not reliable, and hashing the `$GITHUB_ENV` path is exactly the forbidden cross-step comparison.
- **The baseline is measured after untrusted code has already run.** Diagnose's reproducing test executes under the target's test runner, with a full shell, before the snapshot is taken, so a hook that test plants is already in the baseline and the guard compares tampered against tampered. Moving the snapshot earlier is the fix and is unverified.
- **The hooks measurement is bounded by what `find` can see.** Symlinked hook *files* are measured; a `.git/hooks` **directory** replaced by a symlink is not, because `find` does not follow a symlinked start point.

A fourth sits one step later: nothing re-measures `.git/config` or `.git/hooks/` after `Green` and `Run suite`, so a hook planted by that code executes on the next `git` call, which carries `GH_TOKEN`.

## Pins

**`actions/checkout` is pinned to v5, and the guard above is the reason.** From v6.0.0 the persisted credential moves out of the local git config into `$RUNNER_TEMP`, with `.git/config` pointing at it through an `includeIf`. An included file IS git config and can set `core.hooksPath`, so the bump leaves a third config file unhashed, in a directory this workflow already documents as writable by the agent. v5 is already Node 24, and the pin floats, so it takes that major's backports. Re-open if v5 stops receiving them.

**The action pins are floating majors; the harness pins are exact.** All three actions sit on `@vN` and take backports, which is why `setup-python` moved to v7 for its silent-manifest-fetch fix: its failure mode is loud, so the trade is a loud risk for a quiet one.

The harnesses are pinned exactly because what breaks across their releases is argv and permission semantics, which fail quietly, and the runner installs the harness fresh every cycle, so a floating spec would make the loop's behaviour a function of the date. The cost is accepted: upgrading is a manual edit, and a pin left alone for a year is a harness a year out of date. `tests/test_harness.py` refuses an unpinned spec so this stays a choice.

## Branch behaviour

Every cycle follows the ref it is dispatched on: checkout, the PR base, the merge, the post-deploy verify and the incident record all use that branch. A scheduled run gets the default branch. That is what makes a branch self-test safe against a real repo.

Verify's suite half runs on every ref, but the `health_check` probe runs **only on the default branch**. `SHL_HEALTH_URL` names one deployment, so probing it from a branch cycle reads a different commit and reverts a correct fix as a regression. A per-ref URL does not fix that, since preview deployments are commonly behind authentication and answer a probe with a redirect. Both stand-downs print to the run log rather than passing silently.
