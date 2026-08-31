# Platforms — where failures surface, and how to read them

> Read this when the target deploys somewhere. Loaded by the installer; never vendored into the target.

Nothing here names a platform as the expected case. Every hosted platform answers the same handful of questions differently, so what this file carries is the questions, the rules that do not vary, and a blank recipe you fill from the platform's own docs.

## The four log-surface families

The choice decides which class of bug the loop can ever heal. They **compose**: `read_log` returns a string, so it can concatenate sources.

**With one exception, and it is a credential boundary rather than a preference.** A `read_log` that both runs the target's test suite and reads a hosted log is the one combination to refuse. Running the suite inside `read_log` executes agent-authored code (previous cycles' merged reproducing tests, written from untrusted logs), and the step that calls `read_log` is handed `SHL_LOG_TOKEN` so a host-log adapter can authenticate. Composing those two puts a live platform credential in reach of code an agent wrote. Use two sources of any other pair freely; for this one, pick a side. Full reasoning: [adapter.md](adapter.md).

| family | what it is | catches | costs |
|---|---|---|---|
| **Read from disk** | the project writes log files | everything the process logs | nothing, but needs something long-running that you host |
| **Pull from the host** | a PaaS log API, a cloud log service, the system journal | real user failures in production | credentials, and **retention bounds the cron cadence** |
| **Manufacture it** | `read_log` runs the suite; failure means CI went red | regressions only | nothing, works even on a library that never deploys |
| **Push to a sink** | an error tracker | failures nobody went looking for | an account |

Family 3 is the sane floor. Families 1 and 2 are what give the loop eyes on production.

### When the failures are client-side, there is no family yet

A host logs what runs on **its own** machines. Client-side JavaScript runs on the visitor's device, so no host log, no matter how good, ever holds it — which is why "client-side only" is not a fifth family but the statement that none of the four applies yet.

Draw that before offering the options, because the fix changes the product. Today: the browser throws and nothing records it anywhere. With a relay: a listener on `error` and `unhandledrejection` POSTs the stack to a **new route in the operator's own repo**, that route logs it, the host's log now holds it, and `read_log` reads the host — family 2, with that host's retention.

So the operator is not picking where to read. They are deciding whether their project gains a public HTTP endpoint. Show the flow, name the file that would be created, and let them answer that question rather than a menu.

**Quality and coverage are different axes**, and conflating them wastes time. A synthetic monitor driving a real browser produces a traceback indistinguishable from an error tracker's: same *quality*. What it cannot do is exercise inputs nobody thought of, which is *coverage*. Only real error reporting has coverage that is not bounded by imagination.

## Retention sets the cron cadence

This is the failure nothing announces. If the platform keeps runtime logs for an hour and the cron runs every three, most failures expire unseen and the loop idles looking perfectly healthy. Short retention windows are common on free and entry tiers, and they are usually measured in hours.

Rules:

- Ask the operator which platform and which plan tier, then look the retention up in that platform's own docs and show them the number to confirm. Provenance in the record is *asked* for the tier and *looked up* for the value, because neither half is safe alone: nothing in the repo names the tier, and an operator's recollection of a retention window is not a source.
- **Ask it on the relay branch too.** A generated relay writes into the host's log, so choosing one *creates* a host-log surface with that host's retention. Treating the relay as a separate case skips the question and leaves the loop on a cadence nobody chose.
- Set `watch.yml`'s cron **well inside** it.
- Have `read_log` query a **fixed** window wider than the interval, written as a constant in the adapter. Overlap is intended: the same failure read twice keys to the same fingerprint, so dedup collapses it and nothing is filed twice.
- Do **not** derive that window from the last run's timestamp. Every source for it shells out to `gh`, and the step that calls `read_log` holds no `GH_TOKEN` on purpose, because on a suite-as-signal target `read_log` executes previously merged, agent-authored tests and no credential belongs in that environment. Such a call raises and takes the whole watch down on every tick, which reads as a healthy quiet project. A fixed window needs no credential and no state file.

## Detecting the platform: do not rely on it

**A project deployed through a Git integration can add no files to the repo at all.** Where the platform builds straight from a connected repository, there may be no config file, no manifest entry, and no marker of any kind — anything the CLI writes locally is typically gitignored. Config sniffing therefore finds nothing, falls through to "no runtime log", and builds an adapter that reads the test suite instead of the host. The loop then idles forever, and idle looks healthy.

Whatever you find in the repo is evidence for a recommendation, not a conclusion.

Weak signals worth gathering before you ask, in rough order of usefulness:

- an existing CI workflow that deploys, or that names a platform CLI;
- a platform SDK or client in the dependency manifest;
- config files where they do exist: `fly.toml`, `render.yaml`, `Procfile`, `Dockerfile`, `netlify.toml`, `app.yaml`, k8s manifests, a systemd unit.

## Rules that hold on every platform

**A health check must prove the merged commit is serving**, not merely that the site answers. A push-triggered deploy is asynchronous: the merge returns, the platform builds for a minute, and a probe fired immediately reads the **previous** deployment and reports healthy no matter what just shipped. Render the deployed commit SHA into the served output at build time (most platforms expose it as a build-time environment variable) and poll until the served value equals `git rev-parse HEAD`, with a timeout. This is the single most common way a health check lies.

**Where the platform serves minified code, sourcemaps are load-bearing rather than a nicety.** `read_log` must resolve minified frames through the published sourcemaps before returning. Unresolved frames point at a build-hashed chunk filename, and that hash changes every build — so an identical failure fingerprints differently on every deploy and incident memory is permanently dead. Resolve on the runner, where the repo and the published maps are both reachable.

**Build output directories are not vendor paths.** `log_compact` deliberately does not treat `.next/`, `dist/` or `build/` as third-party, because they hold the project's own compiled code. Marking them vendor would leave a trace with zero project-owned frames, collapsing the fingerprint onto a build-hashed chunk name. Sourcemap resolution is the fix; vendor-listing is not.

## The recipe — fill one per platform

Read the platform's own docs for two facts, then answer the rows.

1. The log command or API, and whether it takes a `--since`-style watermark.
2. The runtime log retention, which sets the cron.

Only three rows are platform facts. Everything else the loop needs (the setup and test commands, the repro path, the test globs, whether a deploy command exists at all) comes from Phase 1 discovery and is owned by `artifacts/setup.md`, which is the table the operator actually runs. Restating them here is how the two copies drift.

| row | value |
|---|---|
| log command | the command `read_log` shells, with its watermark flag |
| retention → cron | the documented retention, and an interval well inside it |
| health strategy | how `health_check` proves the merged commit is live |

**The log credential always goes in `SHL_LOG_TOKEN`**, whatever the platform calls it. The workflow exposes that one secret to the two `read_log` steps and to nothing else. Read it inside `read_log` and hand it to the platform's CLI or SDK however that tool expects — exporting it under the vendor's own variable name is fine. Do not invent a per-platform secret name: the workflows plumb exactly one, and a secret the workflow never passes is a credential the adapter cannot see.

Record what you could not verify without live credentials. An unverified `read_log` is normal at install time and gets its first real exercise when the operator runs it; say so rather than implying it was tested.

## Shelling out to a platform CLI

- **Quote stderr's TAIL, not its head, when reporting a failure.** A package runner prints its own noise first (`npx` emits deprecation warnings before the command runs at all), so a head-truncated error reports the runner's chatter and hides the tool's actual message. Debugging then starts from a fragment that says nothing.
- **Do not assume the CLI exists on a runner.** GitHub's images carry a fixed toolset; a platform CLI usually is not in it. Invoke it through the package runner (`npx --yes <cli>`) or install it in a step, and confirm which, because a missing binary surfaces as `FileNotFoundError` on every read.
- **Read the argument shape from the CLI's own help, not from its docs page or from memory.** Positional slots are routinely narrower than they look, and passing a project name where a deployment id is expected queries the wrong thing successfully.
- **A token's scope is part of its shape.** Some CLIs resolve the account before the resource, so a resource-scoped token fails with an error naming the *user*, not the resource. Test the token you will actually give the runner, not the credentials your machine already has cached: those are a different seam.
