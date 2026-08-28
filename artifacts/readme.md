# This repo has a self-healing loop

> Written by the installer, for whoever finds this directory without expecting it.

A GitHub Actions workflow watches this project for failures. When it finds one it opens an issue, writes a test that reproduces the bug, fixes the source, checks the fix against a gate, opens a pull request, reviews it, and merges. No human is in the happy path.

Its decisions are recorded in [SETUP.md](SETUP.md); what the install did and did not prove is in [INSTALL-REPORT.md](INSTALL-REPORT.md), and `git log` for `.shl/` shows when it arrived.

**Two things to know before you read further.** The unattended merge is deliberate, not a misconfiguration — this loop is designed to merge, deploy and revert without waiting for a person, and the sections below say how to stop it if that is not what your team agreed to. And it is experimental: if it does something surprising here, a defect in the framework is a real possibility rather than a remote one. `INSTALL-REPORT.md` lists what was actually verified on this repo, and everything not on that list is unproven here.

## Is it running right now?

Ask, rather than trusting this file — a written-down answer goes stale the moment someone edits the workflow. Both ask GitHub rather than your checkout, which matters because cron reads the default branch and a local edit you have not pushed changes nothing:

```bash
gh workflow list --all                            # --all, or a disabled one is simply absent
gh run list --workflow=watch.yml --limit 5       # scheduled runs listed = the cron is live
```

While the `schedule:` block is commented out, nothing runs unless someone triggers it by hand.

## What it touches

- `.shl/` — its own code, config and memory. Nothing here is part of your application.
- `.github/workflows/watch.yml` and `heal.yml` — the two workflows.
- **Your source and tests**, when it is fixing something. A fix always arrives as a pull request, never as a direct push. Two things it does push directly, both after a fix has already merged: reverting a fix that broke the deployment, and appending to its own incident log.

It also wrote files into the project itself, which stay if the loop is removed — [INSTALL-REPORT.md](INSTALL-REPORT.md) § *Files written into the PROJECT* lists them.

Repository and organization **variables** are readable by the code the loop runs, including tests its agents wrote. Variables are not secrets in GitHub's model, and secrets travel a separate path the loop's test steps never see — but if something sensitive is stored as a variable rather than a secret, treat it as visible here.

## Why is a bot opening pull requests?

Because it found a real failure in {{LOG_SURFACE}}. Before any PR opens, the change has already cleared a deterministic gate:

- **where the failure reduced to a test** — a reproducing test that failed on the broken code and passes on the fix. Many runtime failures do not reduce to one; on those cycles there is no such test, the gate says so in its output, and the remaining checks are what the PR rests on;
- no test weakened, no assertion removed, no skip marker added;
- no test-runner config edited — and **where the reproducing test sits in a dedicated test directory**, nothing already beside it changed either. On layouts that keep tests next to source that second half cannot apply, and the gate's output names which way it went;
- nothing touched under `.shl/`, under `.github/workflows/`, or in any `.gitattributes` — so a fix cannot rewrite the check that judges it, delete the step that runs it, or blind it by changing how git renders diffs;
- no test that was passing now failing, compared as a set where this project can list its failing tests and as a fully green suite where it cannot.

A second agent reads the diff against the issue **after** the PR is open, and that verdict is what decides the merge. So an open PR is not by itself an endorsed one: when the reviewer blocks, it says why in a comment on the PR and stops there, leaving it open and unmerged.

A PR that reaches you has cleared the gate. It is still a machine's work and still yours to reject.

## How do I stop it?

Pick by how hard you need it to stop.

```bash
# Stop it now. Takes effect immediately; nothing to commit.
gh workflow disable watch.yml
gh workflow disable heal.yml

# Or pause the schedule for good: comment out the `schedule:` block,
# then COMMIT AND PUSH it to the default branch. Cron reads that branch
# and nothing else, so an uncommitted local edit pauses nothing at all.
$EDITOR .github/workflows/watch.yml
```

Disabling the workflows in *Actions → … → Disable workflow* does the same thing from the UI. Neither touches code that already merged.

## Something it did is wrong

- **A bad PR is open.** Close it. That alone records nothing: attempts are counted from comments on the linked *issue*, so add `fix attempt N failed: <why>` there if you want it to count. The loop records its own failed attempts, and escalates after 2 instead of retrying.
- **A bad fix already merged.** Revert it like any other commit. The loop records reverted fixes and warns itself not to try the same thing again.
- **It keeps filing the same issue.** The failure fingerprints differently every run — an unstable identity, such as a line number that moves or an ephemeral port or id inside the stack frame. Not a missing one: a failure it cannot fingerprint at all is refused before any issue is filed. Check `failure_ids` in `adapters/target.py`, and `INSTALL-REPORT.md` under NOT verified.
- **It is doing nothing at all.** Idle and healthy look identical from outside, and the Actions log does not separate them either — it prints `idle — nothing to heal` both when the log was clean and when nothing was read at all. Ask the adapter directly, with the same `SHL_*` values the workflow uses plus `SHL_LOG_TOKEN` in your environment:

  ```bash
  cd .shl && PYTHONPATH=. python3 -B -c "from adapters import load_adapter; print(len(load_adapter().read_log()))"
  ```

  `0` means the loop is blind — the wrong log surface, or a missing credential — rather than satisfied.

## Where to look when you need detail

- Each run uploads an **evidence bundle** as a workflow artifact: every agent's exact prompt and raw output, the diff, the gate's verdict and the suite output for that cycle. It is the only place that says *why* the loop did what it did.
- `SETUP.md` records every decision made at install, with who decided and on what basis.
