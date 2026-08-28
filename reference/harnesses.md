# Harnesses — which agent runs the loop, and how it authenticates

Two harnesses ship. A harness is a config entry, not framework code, so this is the whole list: `claude-code` and `opencode`. Both install through `npm i -g`, so a restricted network needs `registry.npmjs.org` reachable from the runner.

Ask the harness first. The credential question depends on it, and the answer decides which environment variable the workflow sets.

## Claude Code

Restriction rides argv, so nothing beyond the variables below is needed.

Three credential paths. All three set `SHL_AUTH_TOKEN` as the repo secret; they differ in what `SHL_AUTH_ENV` names and whether a base URL applies.

| path | `SHL_AUTH_ENV` | `SHL_BASE_URL` | `SHL_MODEL` |
|---|---|---|---|
| Subscription | `CLAUDE_CODE_OAUTH_TOKEN` | empty | an Anthropic model id |
| Anthropic API key | `ANTHROPIC_API_KEY` | empty | an Anthropic model id |
| Third-party provider | `ANTHROPIC_AUTH_TOKEN` **or** `ANTHROPIC_API_KEY` | the provider's endpoint | the provider's model id |

- **Subscription** tokens come from `claude setup-token`, run on a machine already logged in. The token is long-lived, and it is the whole subscription: scope it like any other credential.
- **Third-party** is the framework's default assumption, and the only path where `SHL_BASE_URL` is set. Which variable carries the key differs by provider — some read `ANTHROPIC_AUTH_TOKEN`, others `ANTHROPIC_API_KEY` — so take it from that provider's own setup instructions and put it in `SHL_AUTH_ENV`. Guessing produces an authentication failure on the first agent call of the first real cycle.
- `SHL_MODEL` sets `ANTHROPIC_MODEL` **and every model-tier alias the harness exposes**, including the subagent model. A third-party endpoint serves one catalogue while the harness still resolves its own aliases internally, so any alias left at its default names a model that provider has never heard of. The failure lands mid-cycle rather than at startup, which reads as an unreliable agent instead of a missing variable.
- **The fan-out does not reach every internal call**, and no variable exists for the ones it misses. Observed on a runner: each agent call emitted `[claude-code:unrecognized_model] {"query_source":"generate_session_title"}` on stderr while the cycle itself ran normally. Harmless against a provider that *warns* — and every agent call breaks against one that **errors** on an unknown model. Worth one check against a new provider before trusting a cycle to it.
- **A model id the harness does not recognise gets a silently ASSUMED context window, and this one bites.** Claude Code falls back to 200k and auto-compacts to stay inside it, so a role's context is truncated mid-run: Diagnose loses source it was told to read, Fix loses the frozen test it is being judged against, and nothing in the cycle reports it. Degradation that looks exactly like an unreliable model. Observed on a runner, on both roles, from a bare `glm-5.2`:

    > `"glm-5.2" is not a model this version of Claude Code recognizes, so auto-compact will keep this session within 200k tokens (the context window it assumes).`

    The tool names three remedies and **only one is reachable from here**: append the window to the model id in `SHL_MODEL` (the `[1m]` suffix for a 1M window). The other two work but have no repo-variable route: hydration folds only `SHL_`-prefixed names, so `CLAUDE_CODE_MAX_CONTEXT_TOKENS` has to reach the runner's environment another way (a setup step writing `$GITHUB_ENV` does it, since the agent inherits the whole environment), and `modelOverrides` is a settings key needing a file in the agent's cwd. Setting either as a plain repo variable changes nothing while the truncation continues silently. **The suffix closes this one and not the bullet above it** — with it, the same target emitted no context-window warning at all, while still reporting the model unrecognised for one internal query source. Two separate problems that read alike on stderr; a quieter second cycle is not proof the first was solved. **Check stderr against any new model id and read which warning you got**, because this one costs a whole cycle's quality and the other costs nothing.

## OpenCode

The provider's own environment variable carries the credential, so `SHL_AUTH_ENV` names it (`ZHIPUAI_API_KEY`, `OPENAI_API_KEY`, and so on).

**`SHL_BASE_URL` does nothing on this harness — do not set it and expect an effect.** The framework applies a base URL only through a harness's declared `base_url_env`, and OpenCode declares none, so the value is dropped with no error and no warning. Setting it produces a config that reads correct and a cycle that behaves as though it were unset, which on a wrong endpoint is the hang described below rather than a failure. A non-default endpoint has to be configured through OpenCode's own provider configuration instead; the framework has no path for it here.

`SHL_MODEL` is `provider/model`, and the provider half must name the **plan**, not the vendor. The plan also differs by region for the same model: `zai-coding-plan/glm-5.2` on the international plan, `zhipuai-coding-plan/glm-5.2` on the China one, never `zhipuai/glm-5.2`. A model id pointing at an endpoint the credential does not cover retries instead of erroring, so the symptom is a cycle that hangs until the job timeout rather than one that fails.

`opencode models` lists every id the installed CLI accepts, including which plans the machine has credentials for. Take the id from there rather than from documentation, and run it with the same binary the runner will install: credentials are stored per binary, so a second install of the same version can hold entirely different providers.

Restriction comes from `opencode.json` at the loop root, which the vendored core ships: it defines `shl-diagnose`, `shl-review` and `shl-fix`, each denying everything and allowing back only what the role needs. Argv selects one by name. An unresolved name would otherwise fall through to an unrestricted default, so every role is preflighted with `opencode debug agent <name>` and the cycle is refused if it does not resolve.

Do not edit that file to relax a role. Two of its rules are load-bearing in ways that are not obvious from reading them: `external_directory` is set explicitly on every agent because the built-in default is `ask`, and a headless `ask` never returns — the run simply hangs until the job timeout with no output naming the cause. And Fix's `edit` is scoped to deny `**/.shl/**`, written last because rules are last-match-wins; that is what stops a fix rewriting the gate about to judge it. `opencode debug agent <name>` prints the fully resolved rule list, which is how to check any change without spending a model call.

## Choosing between them

Neither is more capable for this job; both diagnose, edit and review under restriction. Decide on what the operator already has:

- A Claude subscription or an Anthropic key, and Claude Code is one variable away.
- A credential for some other provider, and OpenCode reaches it natively without a proxy endpoint.

Both restrictions are verified before a role runs; the argv form has one less moving part.
