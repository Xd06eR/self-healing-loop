# Residual risks

Boundaries of the design, not defects awaiting a fix. Risks belonging to the workflows are in [`../workflows/CLAUDE.md`](../workflows/CLAUDE.md), which owns the git guard and the action pins.

**Three role-prompt clauses answer observed behaviour and are verified by nothing.** Each came from a real cycle's evidence bundle: Fix asserting render outcomes it had no shell to produce and labelling the limitation while asserting anyway; Fix justifying an added test by mischaracterizing the frozen test quoted in its own prompt; and Diagnose closing its issue by confirming the log carried no injection, because the instruction to report injection read as a field to address rather than a condition to meet. The clauses are prose, so what they change is a model's behaviour, untestable here by construction. **Treat all three as unverified.**

What is pinned is narrower: `tests/test_prompt_contract.py` checks the operating doc against `role._CONTRACT`, catching a rename that leaves an instruction naming a field no role returns. It opens no workflow, so it cannot see the other half, a field on the contract that no step reads; the workflow tests cover that, and only because they execute the step.

**The pre-dispatch refusal judges the raw log while the agent is prompted with the compacted one.** `watch.yml` refuses a cycle whose failure yields no identity, which is correct, since identity comes from the raw log. But compaction can evict a failure the raw log carried, so a cycle can be keyed on something absent from every prompt.

Refusing unless BOTH are fingerprintable reinstates the defect this seam exists to remove: on a Go target the compacted signal is a panic line with no frames, so `unfingerprintable` is always true of it and every cycle would refuse. What is checked is the compacted signal being non-empty, which is the IDLE branch. The residual is a prompt-quality gap on a log carrying more than 8 KB of error text, not a fail-open.

**The gate's language knowledge is installer-supplied, and unsupplied means unpoliced.** Test-file globs, runner-config globs, and what an assertion and a skip look like all arrive as `SHL_*` values, each ADDED to a built-in describing Python and JS rather than replacing it, so setting one cannot disarm another. That moves the failure from "the check silently passed everything" to "nobody told the check what to look for", which is better and still quiet.

One check closes part of it: the gate proves the supplied assertion pattern matches something in the frozen reproducing test and refuses when it does not, so a pattern recognising nothing cannot police nothing. **That proof hangs off `--frozen`**, which the workflow supplies only when Diagnose could reproduce the failure, the minority of cycles, so on most it does not run and `cli.py` says so in the pass line. The rest of the suite is where a wrong regex still polices the wrong thing as confidently as a right one.

**Freezing the reproducing test does not freeze what it imports.** `is_frozen_test_touched` matches one path and `is_test_weakened` inspects only test-glob matches, so a helper beside the frozen test is neither: neuter it and the frozen test goes green with the bug intact.

`is_test_helper_touched` closes the common case by refusing any modification to a pre-existing file in the frozen test's directory, with additions excepted because Fix legitimately writes regression tests there. It applies only where that directory is a dedicated test tree, since a layout putting tests beside source would otherwise refuse every legitimate fix, and **where it declines the pass line says so**. What stays open: a helper outside that directory, and every mixed layout. The gate is line-based more generally, so a semantic rewrite of a *non-frozen* test dodges it too; AST-diff is the upgrade for both.

**Nothing checks that a supplied failure identity is any good**, only whether the method is needed. An adapter returning one string for every failure collapses unrelated bugs into one incident.

**The scrubber's shape patterns cannot cover every provider.** Literal-value redaction of `SECRET_ENV_VARS` closes that for secrets the process holds; a secret it does not hold is shape-matched only.

**A single agent call is bounded by wall clock only.** The job timeout is the hard stop; there is no per-invocation turn or spend cap. Against a third-party base URL the CLI has no pricing for, a spend cap would read as a bound while computing nothing.

**Red-then-green covers reproducible failures only**; non-reproducible ones lean on the gate, the review and the rollback.

**Rollback restores code, not external side effects a bad deploy already took.**
