# Updating an installed loop

> Read this when Phase 2 finds a loop already installed. Phase 4 also loads it on a fresh install, for the manifest generator alone — nothing else here applies to one. Loaded by the installer; never vendored into the target.

The target holds a **copy** of the core, so a framework fix does nothing there until the copy is refreshed, and a cycle run against a stale copy reports a confident wrong result. This branch refreshes it.

One rule governs everything below:

> **Compare content. Never trust a version stamp.**

A stamp is a claim. An operator who patched a vendored file leaves it reading current, and the update then either destroys their patch or skips a needed refresh — silently, either way. Comparing content asks the real question, and it works on an install old enough to predate every convention this file describes.

## Detect which situation you are in

Four states. Check them in this order, because the first two are stops.

- [ ] **Two loop directories** — both `.shl/` and a longer-named one exist. **Stop.** The workflows name one path, so one loop is live and the other is debris that still answers `import`. Report both, let the operator say which to keep, and do nothing until they have.
- [ ] **A loop directory whose name is not `.shl/`** — an install predating the rename. Run *Migrating an older install* below, then continue here.
- [ ] **`.shl/SETUP.md` exists** — an update. Continue.
- [ ] **None of the above** — a fresh install. Leave this file and return to the install phases.

Key the detection on `SETUP.md` rather than on the directory: an aborted install can leave a directory behind, while the record is written only once the decisions are settled.

## Read the manifest, and say so when there is none

`.shl/manifest.json` records the hash of every file the install wrote **from the framework** — the vendored core and the two workflows, not `adapters/target.py`, not `adapters/tests/`, and not `SETUP.md`, which the framework does not own and never overwrites.

With it, each framework-owned file falls into exactly one state:

| installed vs recorded | recorded vs framework | meaning | action |
|---|---|---|---|
| same | same | current | leave |
| same | differs | stale | overwrite |
| differs | same | locally modified | leave, and report it |
| differs | differs | modified **and** stale | **stop and ask**, showing both diffs |

**An install with no manifest cannot tell those apart, and must say so instead of guessing.** Any install predating manifest support is in this state. Show the operator the diff of every file that differs from the framework, one at a time, and ask each; do not overwrite on the assumption that a difference is staleness. Guessing wrong here silently reverts a deliberate local change, and a loop that behaves differently than its own tree says is the worst state this project can leave a repo in.

**Keys are paths relative to the repo root, not to `.shl/`.** The manifest covers both the vendored core (`.shl/loop.py`) and the two workflows (`.github/workflows/heal.yml`), and those two live in different trees — so a `.shl`-relative key cannot express the second, and mixing conventions makes the check below report every entry `GONE` while the install is perfectly intact. Nothing generates this file for you, so write it with the snippet below rather than by hand:

```bash
python3 -B -c "
import hashlib, json, pathlib
# The framework does not own these, so they must not be recorded as though it
# did: the adapter is written for THIS target, and the three documents are the
# install's own record. The state table above sends an unmodified
# framework-owned file to OVERWRITE, so filing them here makes the next update
# replace the operator's adapter with the framework's stub.
target_owned = {'.shl/adapters/target.py', '.shl/SETUP.md',
                '.shl/README.md', '.shl/INSTALL-REPORT.md'}
paths = [str(p) for p in pathlib.Path('.shl').rglob('*') if p.is_file()]
paths += ['.github/workflows/watch.yml', '.github/workflows/heal.yml']
skip = ('.txt', '.json', '.diff', '.raw', '.pyc')
# `adapters/tests/` is excluded whole, by directory rather than by name or
# extension: it holds the adapter's own tests AND the captured fixtures that
# drive them, and a fixture carries whatever extension the log format
# suggested. `adapters/__init__.py` and `adapters/base.py` sit one level up
# and ARE the framework's, so the exclusion stops at the tests directory.
rec = {p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
       for p in sorted(paths)
       if p not in target_owned
       and not (p.endswith(skip) and not p.endswith('opencode.json'))
       and '/adapters/tests/' not in p
       and '/evidence/' not in p and '/incident_memory/' not in p
       and '__pycache__' not in p}
pathlib.Path('.shl/manifest.json').write_text(json.dumps(rec, indent=2) + '\n')
print(f'manifest: {len(rec)} files')
"
```

Run it from the repo root, and note it deliberately excludes what the framework does not own (cycle scratch, evidence, incident memory) plus `manifest.json` itself, which cannot record its own hash.

Recompute hashes from the files themselves rather than trusting the recorded values to still describe them:

From the repo root, like the generator above — both read the same repo-relative keys, and running one from `.shl/` and the other from the root is how the two conventions drift apart:

```bash
python3 -B -c "
import hashlib, json, pathlib
rec = json.load(open('.shl/manifest.json'))
for path, want in sorted(rec.items()):
    p = pathlib.Path(path)
    if not p.exists():
        print(f'GONE      {path}'); continue
    have = hashlib.sha256(p.read_bytes()).hexdigest()
    print(f'{\"ok       \" if have == want else \"MODIFIED \"}{path}')
"
```

Write the manifest again at the end of the update, covering exactly what you wrote. A manifest that describes the previous update is worse than none, because it reads as authoritative.

**`manifest.json` must be exempted in `.shl/.gitignore`.** That file ignores `*.json` so raw agent output never reaches a commit, and an ignored manifest is not committed, not present on the next clone, and read as "no install" by the check above — which restarts a fresh install over a live loop. The `!opencode.json` line already there is the same exemption for the same reason. A current install ships `!manifest.json` beside it; an install old enough to need this file may not, so check rather than assume, and add it if it is missing.

## What the framework owns, and what it must never touch

| path | owner | on update |
|---|---|---|
| `adapters/target.py`, `adapters/tests/` | the target | **never touched.** Written against this project's own log format and test runner; replacing it destroys the install. The framework ships no `adapters/tests/`, so a difference there is never staleness — the captured fixture under it is the only thing proving the adapter parses a real log rather than an imagined one |
| `SETUP.md` | the target | **appended, never rewritten.** Add an evolution entry; every prior decision stays readable |
| `INSTALL-REPORT.md` | the target | left in place; the update writes its own report |
| `evidence/`, `incident_memory/` | the cycles | **never touched.** `incident_memory/log.jsonl` is what the loop has learned, and a reverted entry in it is the only thing telling a later cycle that the obvious fix was already tried and made things worse |
| `README.md` | the target | **re-filled, never copied.** It is generated from `artifacts/readme.md` with this install's values, so copying the framework's own `README.md` over it lands the wrong document entirely, and copying the raw template lands unfilled `{{…}}`. Refresh it by filling the template again from the current `SETUP.md`, and only when the installed copy is unmodified |
| the vendored core | the framework | overwrite when unmodified, stop when not |
| `CLAUDE.md` and `AGENTS.md` | the framework, **from `loop_context/CLAUDE.md`** | overwrite when unmodified, stop when not — and re-vendor from that source, never from the framework root. The root holds a `CLAUDE.md` of its own, its development guide, with `AGENTS.md` symlinked to it; copying that pair across replaces the operating doc all three roles auto-load from cwd `.shl/`, taking with it the JSON output contract the driver parses and the rules keeping the agent out of `.shl/`, off the frozen test and out of test-runner config. The gate still refuses, so the loop dies there every cycle with nothing in the diff naming the cause |
| `.github/workflows/watch.yml`, `heal.yml` | the framework | overwrite when unmodified — with the exception below |

### The cron must survive the re-vendor

`watch.yml` ships with its `schedule` block commented out, and the operator uncomments it once the install is verified. A straight overwrite comments it out again, which stops a running loop while leaving every file looking correct — and a stopped cron is indistinguishable from a quiet week.

Read the installed `schedule` block before overwriting, restore it after, and name the cron you carried across in the report so the operator can check it against what they set.

An older install may also carry `env:` lines added by hand to reach an adapter variable. Those are the opposite case, so do not carry them across: repo variables reach the adapter through `SHL_VARS`, which is what makes the hand-added lines redundant. Confirm the variable is genuinely readable after the re-vendor rather than assuming the mechanism covers it.

## Migrating an older install

Keyed on what you can detect, never on a version number: a condition is a command that answers itself, and a version is a claim.

| condition | what it means | action |
|---|---|---|
| a loop directory not named `.shl/` | predates the rename | `git mv` it, then re-vendor **both** workflows — `heal.yml` alone names the path dozens of times |
| `opencode.json` denies edits under the old directory name | the deny points at a path that no longer exists | re-vendor it |
| `heal.yml` has no `SHL_VARS` line | predates variable hydration | re-vendor the workflows |
| a `SHL_*` name the workflows or the adapter read is unset on the repo | the loop reads an empty string at runtime, which for most of these names is the correct state | check each against `SETUP.md`'s own `gh variable set` block; hand over a command only for a name that block sets |

**The `opencode.json` row is the one that fails open.** That deny is what stops the Fix agent editing the loop's own tree — the tree holding the gate about to judge its work. Pointed at a directory that no longer exists, it denies nothing and reports nothing.

Re-vendoring the workflows closes the other two rows on its own, so the migration is mostly one `git mv` plus a copy. Verify rather than assume it: `grep -rn` the old directory name across the repo and expect zero hits outside git history.

### Variables the older install never set

A repo variable that no step names is invisible at runtime, and the loop goes quiet rather than failing. Compute the gap instead of eyeballing it:

```bash
comm -23 \
  <(cat .github/workflows/*.yml .shl/adapters/target.py \
      | grep -vE '^[[:space:]]*#' | grep -ohE 'SHL_[A-Z_]+' | sort -u \
      | grep -vxE 'SHL_(CYCLE_ID|VARS|AUTH_TOKEN|LOG_TOKEN|DEPLOY_TOKEN)') \
  <(gh variable list --json name --jq '.[].name' | sort)
```

Left column only: names something reads that nothing sets. It writes no file, deliberately — the gate stages the whole worktree, so scratch left at the repo root during an update lands in the next cycle's commit.

**Five names are excluded above, and two of the exclusions are a security boundary rather than noise.** `SHL_CYCLE_ID` and `SHL_VARS` are set by the workflow itself, so reporting them sends the operator to set a variable the next run overwrites. `SHL_AUTH_TOKEN`, `SHL_LOG_TOKEN` and `SHL_DEPLOY_TOKEN` are **secrets**, and the whole output of this command is a list the operator is about to run `gh variable set` against. A secret in that list is an instruction to store a credential as a plaintext repo variable — readable by every step of both workflows, including the ones running test code the loop's own agents wrote. `SHL_VARS` hands the whole variable set to every step as one job-level env var — hydration skips secret names, but the raw blob skips nothing. Check those three with `gh secret list` instead; neither `gh` nor the workflow can read a secret's value back, which is the point of it being one.

**Strip comment lines before matching, as above.** A name discussed in a comment is not a name the workflow reads, and reporting one as missing sends the operator to set a variable nothing consumes. A check that cries wolf is a check people learn to skip.

**What it prints is a candidate list, not a work list, and most of it is optional.** The command reads every `SHL_*` the workflows mention, and the majority are guarded so that unset is the intended state: `SHL_SETUP_CMD`, `SHL_DEPLOY_CMD` and `SHL_EVIDENCE_UPLOAD` each sit behind an `if:` comparing against the empty string, the gate's glob and pattern variables are appended only when non-empty, `SHL_ADAPTER` falls back to `adapters.target`, and `SHL_BASE_URL` and `SHL_AUTH_ENV` are empty on a harness's native provider. So resolve the output against `SETUP.md`'s own `gh variable set` block, the record of what **this** install decided to set, and report the remainder as optional and deliberately unset rather than as work. Handing the raw list over as commands is worse than reporting nothing: `gh` prompts on an empty `--body` and the prompt consumes the next line of the paste, so a list of mostly-spurious lines is also a list that mis-sets whatever follows it.

## What the operator is told

An update that lands changes without naming them is asking for blind consent. Three sections, and the second is the one that needs their attention:

1. **What refreshed** — the files, with a one-line summary each. Generate it from the diff. A maintained changelog drifts from the code; a diff cannot.
2. **What needs a decision** — the subset requiring them to act: a new repo variable, a new secret, a new adapter method, a changed Actions permission. Everything else is a fix that needs no consent, and burying the four items that do among forty that do not is how consent stops meaning anything.
3. **What was left alone, and why** — locally modified files, the target adapter, incident memory, and the cron you carried across.

Append the same three to `SETUP.md` as an evolution entry, so the record answers "what is installed here" without needing this conversation.

## Verifying the update

All of it, and none of it changes behaviour:

- [ ] The manifest check from the install phase passes: no extra file, none missing.
- [ ] The vendored core imports clean:
    `cd .shl && PYTHONPATH=. python3 -B -c "import loop, role, evidence, log_compact, gh_state; from guardrails import cli, gate, incident_memory; from adapters.base import TargetAdapter; print('ok')"`
- [ ] The **target's own** adapter tests still pass. The core moved under them, and `adapters/target.py` did not.
- [ ] `actionlint` is clean on both workflows.
- [ ] The `schedule` block reads exactly what it read before, and `grep` finds no reference to an old loop directory name.
- [ ] `git status` shows the update's own files and nothing from a cycle: no `evidence/`, no scratch.

Commit the update to its own branch and open a PR, for the reason the install does: the diff is the artifact gate, and this one silently replaces code that merges to the default branch unattended.
