# Self-healing loop — decision record and setup

> Written here by the installer. It records what was decided about this project, who decided it, and how each value was established. Read it before enabling the cron; re-read it before changing anything.

Two kinds of content, deliberately kept apart. **Decisions** are choices someone made and could have made differently. **Variables** are the values those choices produced, plus the mechanical facts the repo answered on its own.

## How to read the provenance column

Every row says where its value came from. A record that cannot tell you which is which cannot be audited later.

| provenance | means |
|---|---|
| **looked up** | read from a named source rather than chosen: a file in this repo, or a platform's own documentation where the repo cannot know the answer. The source is named either way |
| **asked** | the operator answered; their answer is recorded |
| **defaulted** | nobody chose it; the shipped default applied, and it is named |

A value marked *defaulted* has never been agreed to by anyone. Treat every one as an open question until someone confirms it.

---

## Decisions

### 1. Does this project have a machine-checkable notion of correct?

**Choice:** `{{TEST_ORIGIN}}`

Options were: tests already exist · they must be generated at install · this project genuinely cannot be gated. The third is not installable: the deterministic gate is the only thing between a headless agent and the default branch, and it works by running tests.

If the answer was *generated*, the gate now rests on tests an agent wrote from a codebase it had just met. See decision 8.

### 2. Where do this project's failures surface?

**Choice:** `{{LOG_SURFACE}}`

Options were: a log on disk · the host's logs · the visitor's browser only · CI only.

This is the decision most worth re-reading. Getting it wrong does not break anything visibly: the loop reads the wrong place, finds nothing, and idles, which is indistinguishable from a healthy project.

### 3. Browser-only failures: how do we make them visible?

**Choice:** `{{BROWSER_FIX}}`

Options were: generate a relay endpoint · adopt an error tracker · accept regression-only healing. This decision is only reached when decision 2 was "the visitor's browser only", so `n/a` above means it was never live rather than never answered.

If a relay was generated, this repo now serves a public, unauthenticated endpoint that writes into the loop's own input. It is hardened, and it is still product code that was not here before.

### 4. What is the log retention, and what cron does it force?

**Retention:** `{{RETENTION}}` · **Cron:** `{{CRON}}`

Retention is a hard bound on cadence. If the platform keeps runtime logs for one hour and the cron runs every three, most failures expire unseen and the loop idles looking healthy.

Retention bounds the interval from above; **Actions minutes bound it from below.** Every tick is a runner job, and on a private repo those minutes are billed against a monthly allowance — a fifteen-minute cron is ~2,900 ticks a month before a single cycle runs. Check the cadence above against this repo's allowance before enabling it, because the bill is the one failure mode here that nothing in the loop reports. `read_log` queries a **fixed** window wider than the interval, and fingerprint dedup absorbs the overlap — it must not watermark from the last run, because that needs `gh` and the step deliberately holds no token.

### 5. What deploys this project?

**Choice:** `{{DEPLOY_CMD}}` *(empty means push-triggered, or nothing)*

### 6. How do we know the merged commit is live?

**Strategy:** `{{HEALTH_STRATEGY}}`

Push-triggered deploys are asynchronous. A probe fired straight after a merge reads the *previous* build and reports healthy whatever just shipped, so `health_check` must prove the merged commit is the one serving.

### 7. What is off-limits to the loop?

**Off-limits:** `{{OFF_LIMITS}}`

Paths, services, and anything with irreversible side effects. Rollback restores code, not consequences.

**Nothing enforces this list, and it is important to know that.** There is no `SHL_OFF_LIMITS` and no check that reads it: the gate polices the loop's own tree, the workflows, `.gitattributes`, test files and test config, and knows nothing about your project's sensitive paths. This answer reaches the loop in exactly two ways — the adapter was written to respect it at install, and it is recorded here for whoever reads this file later. A path you need genuinely protected has to be protected by something that does not depend on an agent's cooperation: branch protection, a required review on those paths, or keeping them out of this repo.

### 8. Who reviews the generated tests, and when?

**Choice:** `{{TEST_REVIEW}}`

Only reached when decision 1 was *generated*, so `n/a` above means the project already had tests. Where it was reached, those assertions are now the gate guarding the default branch, and until someone has read them against intent nothing has checked whether they describe correct behaviour or merely current behaviour.

### 9. Escalation: who is told, and how?

**Escalation:** `{{ESCALATION}}`

The loop stops and asks for a human after the attempt cap. That request has to reach someone, or the loop simply goes quiet.

### 10. Harness, model, and whose token?

Harness: `{{HARNESS}}`. Credential path: `{{AUTH_PATH}}` — for Claude Code one of subscription token, Anthropic API key, or third-party provider; for OpenCode, the provider plan. Values in the table below.

The credential lives in the `SHL_AUTH_TOKEN` repo secret; `SHL_AUTH_ENV` names the variable the harness reads it from, and `SHL_BASE_URL` is set only on the third-party path.

---

## Discovery — what the repo answered on its own

| what | value | provenance |
|---|---|---|
| Language / package manager | `{{LANGUAGE}}` | |
| Dependency install | `{{SETUP_CMD}}` | |
| Test command | `{{TEST_CMD}}` | |
| Single-file test command | `{{TEST_ONE}}` | |
| Test files live in | `{{TEST_PATH}}` | |
| Suite currently | `{{SUITE_STATE}}` | |

If the suite is red, the loop heals **only NEW failures**. Every cycle measures which tests are already failing before it changes anything, and those never block that cycle's fix — so they stay failing until someone fixes them by hand. Nothing is captured at install time; the baseline is taken fresh each cycle.

That depends on the adapter implementing `failing_tests()`. Without it the gate cannot tell a pre-existing failure from one the fix caused, so it falls back to demanding a **fully green suite** — and on a repo that is already red, every fix is then vetoed forever.

---

## Variables

Set as GitHub repository **variables** (non-secret), except where marked. Run these from this repo, or use *Settings → Secrets and variables → Actions*.

| Var | Value | Notes |
|---|---|---|
| `SHL_HARNESS` | `{{HARNESS}}` | `claude-code` or `opencode`. Must match a shipped recipe. |
| `SHL_MODEL` | `{{MODEL}}` | The model id your provider expects. |
| `SHL_BASE_URL` | `{{BASE_URL}}` | Provider endpoint for non-native models. Omit for the harness's native provider. |
| `SHL_AUTH_ENV` | `{{AUTH_ENV}}` | The env var **your provider** reads the key from, e.g. `ANTHROPIC_API_KEY`. Determined by the provider, not the harness: omitting it falls back to the harness default, which is right for that harness's native provider and an authentication failure on the first agent call for anything else. |
| `SHL_ADAPTER` | `adapters.target` | The module the installer created. Optional: the loader falls back to exactly this value, so set the variable only if you rename the module. |
| `SHL_SETUP_CMD` | `{{SETUP_CMD}}` | Installs **this project's own** dependencies on a bare runner. Empty only if nothing third-party is imported. |
| `SHL_TEST_CMD` | `{{TEST_CMD}}` | Full suite command. |
| `SHL_TEST_ONE` | `{{TEST_ONE}}` | Single-file runner **with a literal `{}`** where the path goes. |
| `SHL_DEPLOY_CMD` | `{{DEPLOY_CMD}}` | Deploy command, or empty to skip the deploy step. Verify and rollback run regardless. |
| `SHL_REPRO_PATH` | `{{REPRO_PATH}}` | Where each cycle's reproducing test is written, with a literal `{}` for the issue number. Must be a directory the suite collects from, in the suite's language. |
| `SHL_TEST_GLOBS` | `{{TEST_GLOBS}}` | Comma-separated **extra** globs, added to the built-in `test_*`, `*_test.*`, `*.test.*`, `*.spec.*`. Needed for a **directory** convention such as `*__tests__/*`, and for a separator the built-ins miss — RSpec's `foo_spec.rb` is not matched by `*.spec.*`. |
| `SHL_TEST_CONFIG_GLOBS` | `{{TEST_CONFIG_GLOBS}}` | Comma-separated **extra** globs naming this project's test-runner config, added to the built-in list. Editing one silences a test as surely as deleting its assertions. The built-ins cover Python, JS, and the Ruby, Rust and JVM manifests (`.rspec`, `Rakefile`, `Cargo.toml`, `pom.xml`, `build.gradle`); a runner this project configures anywhere else is unpoliced until named here. |
| `SHL_ASSERT_PATTERN` | `{{ASSERT_PATTERN}}` | Regex for what an assertion looks like here, alternated with the built-in `assert`/`expect`/`raises`/`should_`. A runtime that reports failure as `t.Errorf` has no assertion the gate can see, so removing every one of them reads as no change at all. |
| `SHL_SKIP_PATTERN` | `{{SKIP_PATTERN}}` | Regex for switching a test off here, alternated with the built-in `skip`/`xfail`/`expectedFailure`. Covers `xit(`, `pending`, `#[ignore]`, `@Disabled`. |
| `SHL_EVIDENCE_UPLOAD` | *(omit)* | Omit to upload each cycle's evidence artifact, which is what makes a failed cycle debuggable. |
| `SHL_EXTRA_SCRUB_PATTERNS` | *(omit)* | One regex per line, naming anything only you know is confidential — client names above all. Added to the built-in secret and PII shapes, and applied to every scrubbed surface: issue bodies, PR text, the fix diff and the evidence bundle. A pattern that does not compile stops the step rather than being skipped. |

**Secrets**

| Secret | What | Needed when |
|---|---|---|
| `SHL_AUTH_TOKEN` | the provider API key | always |
| `SHL_LOG_TOKEN` | the credential `read_log` needs to reach a **hosted** log source (a Vercel token, an AWS key, a Sentry token) | only when the failure surface is the host's logs, directly or via a relay |
| `SHL_DEPLOY_TOKEN` | whatever credential `SHL_DEPLOY_CMD` needs — a platform CLI token, a registry key | only when the deploy command authenticates to something |

`SHL_LOG_TOKEN` is exposed only to the two steps that call `read_log`, never to a step that runs the suite. If it is missing on a host-log target, `read_log` returns nothing, the loop idles, and idle looks exactly like healthy — so set it before enabling the cron, and confirm one non-empty read.

`SHL_DEPLOY_TOKEN` reaches the Deploy step and the rollback's redeploy, and nothing else. It is a **secret rather than a variable** for a reason worth knowing: secrets are a separate context, so a credential written into a repo *variable* would travel to every step reading the bulk variable context — including the ones deliberately built to hold no credential at all.

**Run these one at a time, not as a pasted block.** `gh` reads an empty `--body ""` as "no value supplied" and drops into an interactive prompt, which then consumes the following lines of the paste as its answer. One command per line, each confirmed, costs a minute and cannot silently swallow the rest.

**Omit any variable whose value is empty rather than setting it to `""`.** An unset repo variable and one set to the empty string are the same thing to the workflow (`vars.X != ''` is satisfied by both), so setting it buys nothing and triggers the prompt above.

```bash
gh variable set SHL_HARNESS        --body "{{HARNESS}}"
gh variable set SHL_MODEL          --body "{{MODEL}}"
# SHL_SETUP_CMD: SKIP THIS LINE if this project imports nothing third-party,
# because then the value is empty and there is nothing to install.
gh variable set SHL_SETUP_CMD      --body "{{SETUP_CMD}}"
gh variable set SHL_TEST_CMD       --body "{{TEST_CMD}}"
gh variable set SHL_TEST_ONE       --body "{{TEST_ONE}}"
gh variable set SHL_REPRO_PATH     --body "{{REPRO_PATH}}"
# SHL_DEPLOY_CMD: SKIP THIS LINE ENTIRELY on a push-triggered target. An unset
# variable is exactly what "the platform deploys on push" means to the workflow.
gh variable set SHL_DEPLOY_CMD     --body "{{DEPLOY_CMD}}"
gh variable set SHL_TEST_GLOBS     --body "{{TEST_GLOBS}}"   # omit unless tests live in a marked DIRECTORY
gh variable set SHL_TEST_CONFIG_GLOBS --body "{{TEST_CONFIG_GLOBS}}"  # omit on Python/JS
# SHL_ASSERT_PATTERN / SHL_SKIP_PATTERN: SKIP BOTH LINES on a Python or JS
# target — the built-ins already cover those, so the value renders empty.
gh variable set SHL_ASSERT_PATTERN --body '{{ASSERT_PATTERN}}'  # single quotes: regex
gh variable set SHL_SKIP_PATTERN   --body '{{SKIP_PATTERN}}'
# SHL_BASE_URL: SKIP THIS LINE on the harness's native provider. It describes a
# third-party endpoint, so the value renders empty otherwise — and an empty
# --body is what triggers the prompt warned about above.
gh variable set SHL_BASE_URL       --body "{{BASE_URL}}"
# SHL_AUTH_ENV: SKIP only on claude-code, which defaults to ANTHROPIC_AUTH_TOKEN.
# opencode has NO default, so skipping it there leaves no credential in the
# agent's environment — and an unauthenticated run hangs to the job timeout.
gh variable set SHL_AUTH_ENV       --body "{{AUTH_ENV}}"
gh secret   set SHL_AUTH_TOKEN
gh secret   set SHL_LOG_TOKEN      # only for a hosted log source
# SHL_DEPLOY_TOKEN: only when the deploy command needs its own credential.
# A SECRET, never a variable. `SHL_VARS: ${{ toJSON(vars) }}` hands the whole
# variable set to every step of both workflows as one job-level env var —
# including the steps that run agent-written test code and were built to hold
# no deploy credential. Variable hydration skips secret names, but the raw
# blob is in the environment regardless, so a deploy credential stored as a
# variable is readable exactly where it was kept out.
gh secret   set SHL_DEPLOY_TOKEN
```

## Two GitHub identities, and only one of them is yours

The install and the loop authenticate as different principals, which is what makes the loop's reach knowable.

- **Installing** runs under the credential your `gh` already holds. It reads repo settings, commits the install branch, and opens the PR. For most people that credential reaches every repo they can access, so a mistargeted command succeeds against the wrong repo instead of failing. A fine-grained personal access token scoped to this one repository turns that into a 404. Recommended, not required: an install must not fail because someone declined to mint a token.
- **Every cycle** runs under `GITHUB_TOKEN`, which GitHub Actions generates per run and scopes to this repository by construction. It is what pushes the merge, opens and comments on issues, and reverts. Its permissions are exactly the two settings below.

The loop therefore never holds a credential of yours. Revoking your own token stops you installing; it does not stop or weaken a running loop.

## Repository settings the loop cannot set for itself

Two toggles under *Settings → Actions → General → Workflow permissions*. Both default to the restrictive value on repos created since 2023, and with either one wrong every cycle runs to the gate — spending agent calls — and then dies at the PR or the merge, forever.

- **Read and write permissions**, not *Read repository contents and packages permissions*. This is the scope of the automatic `GITHUB_TOKEN`.
- **Allow GitHub Actions to create and approve pull requests.** Despite the name, this also gates *creating* a PR from a workflow, which `heal.yml` does. It is a policy toggle, so no `permissions:` block in the workflow can override it.

Tick both in the UI, or:

```bash
gh api -X PUT "repos/{owner}/{repo}/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=true
```

`-F` rather than `-f` on the second: `-f` would send the string `"true"`, which the API rejects.

## Checking the target adapter

The adapter is the one file written specifically for this project. Nothing runs its tests automatically, so run them by hand after any change:

```bash
cd .shl && PYTHONPATH=. python3 -B -m unittest adapters.tests.test_target -v
```

## Notes that bite if ignored

- **Claude Code with a third-party model:** the `CLAUDE_CODE` recipe in `agent/harness.py` writes `ANTHROPIC_MODEL` plus every `ANTHROPIC_DEFAULT_*_MODEL` tier to your `SHL_MODEL` id. Required, because the harness fires background small-model calls; if those tiers still point at a missing model the loop half-fails. Set `SHL_MODEL`; the recipe handles the tiers.
- **Branch protection:** the bot (`github-actions[bot]`) pushes the merge and any rollback to the default branch. Without a bypass on a protected branch, every cycle runs to completion and then fails at merge, after burning three agent calls.
- **`SHL_TEST_ONE` must contain `{}`.** The workflow substitutes the repro-test path into it, and the Red step refuses the cycle outright when the placeholder is missing. It has to refuse: bash leaves a pattern that matches nothing unchanged, so the command would run whatever `SHL_TEST_ONE` already names — usually the whole suite. Red and green would then be measuring the suite rather than the frozen test, any pre-existing failure would satisfy RED, and the escalation that catches a useless reproducing test would never fire.
- **`SHL_SETUP_CMD` is what makes the runner able to run your tests at all.** The runner is a clean checkout; whatever your ecosystem gitignores — `node_modules`, `.venv`, `vendor/` — never arrives. Leave it empty on a project with real dependencies and every suite step dies on a missing import. Nothing bad merges, because the gate blocks on an unparseable baseline, but it blocks *every* cycle, and the symptom reads as "the loop is broken" rather than "one variable is unset".
- **Evidence artifacts are downloadable by anyone with repo read access.** Contents are scrubbed of secret and PII patterns, but the scrubber does not know your client names. Set `SHL_EXTRA_SCRUB_PATTERNS` — one regex per line — before pointing the loop at a client-confidential target. `SHL_EVIDENCE_UPLOAD=false` is a narrower control and not a substitute: it stops the artifact, while issue bodies and PR text still carry agent-written prose about your code.
- **The loop's `.gitignore` is load-bearing.** The gate step runs `git add -A`; without it, a cycle's own evidence lands in the fix diff.
- **`failure_ids()` in the adapter is what lets a non-Python/JS failure be healed at all.** Built-in parsing reads Python tracebacks and V8 stacks; anything else yields no `Type@path:line`, and issue dedup, the attempt cap, incident recall and the incident record all key on that identity. Missing it, `watch.yml` refuses the failure before spending an agent call and says so — loud, but the loop heals nothing on this runtime until the method is restored. Anyone editing the adapter should treat it as required rather than optional.
- **On a restricted network the runner needs three hosts, and the one that gets forgotten is `registry.npmjs.org`.** Both shipped harnesses install through `npm i -g`, so without it the harness install fails before anything else runs — and no provider's network-requirements page lists it, because it is not their host. The other two are your provider's API endpoint (`SHL_BASE_URL`, or the provider's native host) and GitHub's API for the issue, PR, merge and rollback steps. GitHub-hosted runners have open egress, so this only bites on a self-hosted runner behind a firewall. What the loop does *not* need is an allowlist entry for a harness's web-tool domain preflight, the usual trap on Bedrock/Vertex setups: every role here denies web fetch and web search outright, whichever harness is running, so that call never happens.

## Before going live

The installer skill is not installed into this repo, so what it checked is recorded here.

**What the install verified**, without changing this repo's behaviour: {{VERIFIED}}. Evidence in `INSTALL-REPORT.md`.

That list is filled from what actually ran, not from what was meant to. Each item is allowed to be absent for an ordinary reason: `actionlint` may not have been installed, and the dispatched watch is only reachable after this install merges, so a first install commonly reports it as not yet run. An item missing here is a check nobody performed, which is worth knowing precisely because it does not look like anything.

**What it did not.** No cycle has ever completed. The agent has never been invoked, the gate has never blocked anything, and the merge path has never run — proving any of that needs a failure, and manufacturing one is your call rather than the installer's.

Required:

- [ ] **If the tests were generated at install, read them.** They are the boundary the gate enforces, and nobody has yet checked their assertions against what this project is supposed to do. The install PR is the place to do it.
- [ ] Merge the install PR.

Only then enable the cron. It ships **disabled**: open `.github/workflows/watch.yml` and uncomment the `schedule` block. Until you do, the workflow runs only when you trigger it by hand.

## The evidence bundle expires

Every cycle uploads its bundle — each role's exact prompt, raw output and stderr — as a GitHub Actions artifact, under this repo's artifact retention. **Nothing here copies it anywhere durable.** It is the only record of *why* an agent did what it did, and the only material that can tell you whether a role's prompt needs changing. Download and keep the bundle from any cycle you care about, especially a first one or a failure, before retention takes it.

## Optional: prove it actually heals

Skip this and the loop still works — you simply have not watched it work, and the install report says so under NOT verified.

**What a branch self-test does not exercise.** Dispatching on a branch keeps the whole cycle off your default branch, which is what makes this safe to run against a real repo — and it means two steps deliberately stand down. The **deploy command does not run**, and the **post-deploy health probe is skipped**, because both describe the default branch's deployment and judging a branch against it would revert a correct fix as a regression. Both say so in the run log. So this proves diagnosis, the red test, the fix, the gate, the review and the merge; it proves nothing about deployment or about `health_check`, and the first exercise of those is a real cycle on the default branch.

**One exception, and it is not a formality.** If this project is not Python or JS/TS, the must-pass check below is **required**, because the gate's weakening detection is only partly language-aware (see *Residual risks*) and nothing else confirms it holds on your stack.

**What it costs, before you decide.** Everything happens on the install branch, it spends three agent calls per cycle, and it briefly writes a real defect into this repo. **Never against real client data**, and never on a branch anything deploys from.

The whole cycle follows the ref it is dispatched on, so the default branch is untouched: checkout, the PR base, the merge, the post-deploy verify and the incident record all use that branch. A scheduled run gets the default branch instead, which is why this changes nothing about normal operation.

```bash
gh workflow run watch.yml --ref self-healing-loop-install
```

- [ ] Seed one small, self-contained defect plus a test that fails on it, on the install branch. Put it where this project's log surface will actually record it — a failure nothing logs does not exist to the loop, and the loop will idle looking healthy.
- [ ] **Make it fail like an error, and confirm the log holds it.** Compaction keeps only lines carrying error vocabulary and a trace, so a defect that logs a plain sentence is discarded, the watch reports IDLE, and it reads as a broken loop rather than a badly-shaped probe. Run `read_log()` and see the failure in its output before spending a dispatch.
- [ ] Dispatch the watch as above (or *Actions → sh-watch → Run workflow*, selecting that branch). Confirm the whole chain: issue filed, reproducing test written and RED, fix applied, gate green, PR opened, review passed, merge, deploy if configured, verify, incident recorded.
- [ ] **Must-pass: prove the gate BLOCKS a weakened test.** There is no way to inject a fix into a cycle — the agent authors it — so run the gate against a weakened diff directly. Delete an assertion from any test file, then:

  ```bash
  git add -A && git diff --cached --unified=0 > weakened.diff
  PYTHONPATH=.shl python3 -B -m guardrails.cli gate --diff weakened.diff \
    --test-globs '{{TEST_GLOBS}}' --assert-pattern '{{ASSERT_PATTERN}}'
  ```

  **Pass the flags, not just the variables.** `heal.yml` converts the `SHL_*` values into exactly these flags on every cycle; the CLI itself reads no environment, so a bare invocation runs a narrower gate than the real one. Omit any flag whose value is empty.

  It must exit non-zero and name the file and the assertion it lost. Restore the assertion afterwards. A zero exit means one of two things, and the pass line says which: `0 test file(s) matched the test globs` is the glob failing to recognise the file, while a non-zero count with a pass is the assertion form going unrecognised. Correct the value **and re-run with the flag** — setting the repo variable alone changes nothing about this command. Until this blocks, nothing has confirmed the check can read this project's tests at all.
- [ ] Remove the seeded defect and its test before the branch merges.

## Evolution log

Re-installs append here. A prior decision is never overwritten; it is superseded by a dated entry naming what changed and why.

## Residual risks

What this loop does **not** protect you from. None is a bug to be fixed later; they are the boundaries of the design, worth knowing before the cron runs unattended against something you care about.

- **The merge gate compares lines, not meaning.** It reliably catches a deleted assert, a skipped or expected-to-fail test, an edited test-runner config, and any edit to the frozen reproducing test. A determined semantic rewrite of some *other* test, one that still runs and still asserts but asserts something weaker, can pass it. The frozen test itself is tamper-proof, which is what makes the red-then-green proof trustworthy.
- **The gate's language coverage is uneven, and it degrades quietly.** Three separate checks, each with a different reach:
    - *Which files count as tests* — driven by globs, so it works on any language **provided `SHL_TEST_GLOBS` matches your convention**. Get this wrong and every check below polices nothing at all.
    - *Removing an assertion* — recognised where the assertion reads `assert`, `expect`, `raises` or `should_`, which covers Python, JS/TS, RSpec and most JVM matchers. **Go's `t.Error`/`t.Fatal` are not recognised.**
    - *Silencing a test* — recognised for `skip` and `xfail` forms, including Go's `t.Skip` and jest's `.skip`. **Not recognised: `xit(`, RSpec's `pending`, Rust's `#[ignore]`, JUnit's `@Disabled`.**
    - *Editing runner config to exclude a test* — covered for Python, JS, and the Ruby, Rust and JVM manifests. A stack that configures its runner somewhere else, Go among them, walks past the gate until `SHL_TEST_CONFIG_GLOBS` names that file.

    Separately, failure fingerprinting parses Python and V8 stack formats; other languages may yield nothing, which quietly disables incident recall, issue dedup and the attempt cap together.

    On any stack that is not Python or JS/TS this is what makes the must-pass check non-optional; see *Optional: prove it actually heals* above.
- **Red-then-green only covers failures that reduce to a test.** A race condition, a rate limit, or an upstream outage cannot be frozen into a reproducing test. For those the loop marks `reproducible: false` and leans on the review agent, the suite, and post-deploy rollback instead: weaker evidence than a passing repro.
- **Rollback restores code, not consequences.** A reverted deploy puts the previous commit back. It does not un-send an email, un-charge a card, or un-write a row. If a cycle can reach an irreversible side effect, gate that path yourself: set `SHL_DEPLOY_CMD` empty and merge manually, or keep the loop off that service.
- **Generated tests were validated against nothing.** A reproducing test is proven red on broken code and green after the fix. A test generated at install only describes what the code already did, so if the code had a bug that day, the test now defends it. This is why reading them is on the checklist above.
