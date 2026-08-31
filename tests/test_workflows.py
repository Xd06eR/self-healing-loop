"""Step ORDER in the shipped workflows, which is load-bearing and easy to break.

These are ordering invariants, not syntax checks. A baseline step placed after
Fix makes "before" and "after" the same measurement, so regression detection is
silently inert; a workflow with no step installing the target's dependencies
kills every suite step on a runner with ModuleNotFoundError. Neither breaks YAML
parsing, and neither is visible to a local driver standing in for the workflow —
which is why these read the shipped file directly.

Parsed by hand rather than with PyYAML: the framework is stdlib-only, and step
order is all this needs.
"""
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import os
import sys
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


class EveryStepStillParsesAsShell(unittest.TestCase):
    """Every `run:` body must parse for EVERY value an SHL_* var can hold.

    GitHub substitutes `${{ vars.X }}` textually before a shell exists, so the
    step's syntax depends on the VALUE. A guard written as
    `if [ -n '${{ vars.SHL_DEPLOY_CMD }}' ]; then ...; fi` is valid prose and a
    parse error the moment that var is empty — which is the push-triggered case
    it is written for. It kills the rollback step between `git revert` and its
    push: the bad fix stays live, the URGENT escalation never fires, and the
    `reverted` incident is never recorded, so the loop can re-ship the same bad
    fix. A rollback aborting before its push is this repo's most repeated defect.

    A grep cannot catch this class — the existing test for `-m 1` passes while
    the step cannot parse two lines below it. Only rendering and parsing can.
    """

    # EVERY var, not a curated subset of the ones the framework calls optional.
    #
    # This list used to name the five that are legitimately empty in a real
    # install, which encoded a false premise: GitHub substitutes the empty
    # string for any variable that is not set, whether or not the framework
    # considers it required. `SHL_TEST_CMD` is required and was therefore never
    # rendered empty — and empty it turns `Run suite` into a redirect with no
    # command (rc 0, an empty log, so the gate approves a fix on a suite that
    # never ran) and the whole `Verify` body into a bash parse error, which
    # takes Rollback and Record down with it because both are gated on success.
    #
    # A step whose syntax depends on a value is broken for whoever mistypes the
    # variable name once. Rendering the required ones empty costs nothing and is
    # the only way this class of defect is visible.
    MAY_BE_EMPTY = tuple(
        sorted(
            set(re.findall(r"vars\.(SHL_[A-Z_]+)", (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")))
            | set(re.findall(r"vars\.(SHL_[A-Z_]+)", (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")))
        )
    )
    # The quote-bearing form is what an ordinary curl-based deploy or a test
    # command with a filter argument looks like.
    VALUES = ("npm run deploy", "curl -sf -d '{\"ok\":1}' https://example.test")

    # An UNBALANCED quote, which is the only value class that can turn a valid
    # step into a parse error. Both entries in VALUES quote themselves evenly,
    # so neither can — rendering a step with one proves the step parses, never
    # that it survives a hostile value.
    #
    # Applied to `vars.SHL_*` only, and the asymmetry is the point rather than
    # a convenience: those come from an operator typing into a repo settings
    # box, so their shape is not the workflow's to assume. `steps.*`,
    # `github.*` and `secrets.*` are produced by the job itself and are
    # legitimately inlined into quoted shell all over both files; feeding those
    # a broken quote would fail the unmutated workflow and prove nothing.
    #
    # The current templates pass because every SHL variable reaches the shell
    # through `env:`. That is exactly the invariant under test: inline one into
    # a `run:` body and this catches it.
    HOSTILE = 'deploy --msg "ship it'

    def render(self, body: str, value: str, operator_value: str | None = None) -> str:
        """Substitute Actions expressions: operator-supplied vars, then the rest.

        `operator_value` is what every `vars.SHL_*` renders as — empty by
        default, since an unset variable is what produced the rollback parse
        error. Everything else renders as `value`.
        """

        def sub(match: re.Match) -> str:
            expr = match.group(0)
            if any(name in expr for name in self.MAY_BE_EMPTY):
                return operator_value or ""
            return value

        return re.sub(r"\$\{\{[^}]*\}\}", sub, body)

    def run_bodies(self, workflow: str) -> list[tuple[str, str]]:
        """(step name, run: body) for every step that has one."""
        text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
        out = []
        for chunk in re.split(r"^ {6}- (?=name:|uses:)", text, flags=re.MULTILINE)[1:]:
            name = re.match(r"name: (.+)", chunk)
            body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
            inline = re.search(r"^ {8}run: (?!\|)(.+)$", chunk, re.M)
            if not name:
                continue
            if body:
                out.append((name.group(1).strip(), textwrap.dedent(body.group(1))))
            elif inline:
                out.append((name.group(1).strip(), inline.group(1)))
        return out

    def _assert_parses(self, workflow: str, step: str, rendered: str, note: str) -> None:
        proc = subprocess.run(["bash", "-n"], input=rendered, text=True, capture_output=True)
        self.assertEqual(
            proc.returncode, 0,
            f"{workflow} step {step!r} does not parse with {note}: {proc.stderr.strip()}",
        )

    def test_every_run_body_parses_for_every_value_class(self):
        for workflow in ("heal.yml", "watch.yml"):
            for step, body in self.run_bodies(workflow):
                for value in self.VALUES:
                    with self.subTest(workflow=workflow, step=step, value=value):
                        self._assert_parses(
                            workflow, step, self.render(body, value), "an unset operator variable"
                        )

    def test_no_step_lets_an_operator_supplied_value_become_syntax(self):
        """A repo variable is prose someone typed, and it must stay data.

        Reaching the shell through `env:` makes a broken quote a broken string;
        substituted into a `run:` body it is a broken SCRIPT, and the step dies
        before running for whoever typed it. The blank pass above cannot see
        this — it renders the same variables as the empty string, which parses
        anywhere.
        """
        for workflow in ("heal.yml", "watch.yml"):
            for step, body in self.run_bodies(workflow):
                with self.subTest(workflow=workflow, step=step):
                    self._assert_parses(
                        workflow, step,
                        self.render(body, self.VALUES[0], operator_value=self.HOSTILE),
                        "an unbalanced quote in a repo variable",
                    )

    def test_the_renderer_actually_found_the_steps(self):
        # Per workflow. With heal.yml alone, a step regex that stopped matching
        # watch.yml would make its whole bash -n pass vacuous in silence — and
        # watch.yml is where the token-withholding step lives.
        for workflow, minimum in (("heal.yml", 8), ("watch.yml", 3)):
            with self.subTest(workflow=workflow):
                self.assertGreaterEqual(len(self.run_bodies(workflow)), minimum)

# A job step is `      - name: X` / `      - uses: X` at exactly six spaces.
_STEP_RE = re.compile(r"^ {6}- (?:name|uses): (.+?)\s*$", re.MULTILINE)


def step_names(workflow: str) -> list[str]:
    return _STEP_RE.findall((WORKFLOWS / workflow).read_text(encoding="utf-8"))


def executable(text: str) -> str:
    """Drop comment lines.

    Every guard here states its own rule in a nearby comment, so a check run
    over raw text flags its own documentation.
    """
    return "\n".join(
        ln for ln in text.splitlines() if not ln.strip().startswith("#")
    )


def job_steps(workflow: str) -> list[dict]:
    """Each step as {name, body, workdir, cond}, comments already stripped."""
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    out = []
    for chunk in re.split(r"^ {6}- (?=name:)", text, flags=re.MULTILINE)[1:]:
        name = re.match(r"name: (.+)", chunk)
        if not name:
            continue
        wd = re.search(r"^ {8}working-directory: (\S+)", chunk, flags=re.MULTILINE)
        cond = re.search(r"^ {8}if: (.+)$", chunk, flags=re.MULTILINE)
        out.append({
            "name": name.group(1).strip(),
            "body": executable(chunk),
            "workdir": wd.group(1) if wd else "",
            "cond": cond.group(1) if cond else "",
        })
    return out


class WorkflowStepOrder(unittest.TestCase):
    def assert_before(self, names: list[str], earlier: str, later: str) -> None:
        self.assertIn(earlier, names)
        self.assertIn(later, names)
        self.assertLess(
            names.index(earlier),
            names.index(later),
            f"{earlier!r} must run before {later!r}",
        )

    def test_heal_installs_target_deps_before_running_anything(self):
        names = step_names("heal.yml")
        self.assert_before(names, "Install target dependencies", "Re-read log")
        self.assert_before(
            names, "Install target dependencies", "Baseline — tests already failing"
        )

    def test_watch_installs_target_deps_before_reading_the_log(self):
        names = step_names("watch.yml")
        self.assert_before(names, "Install target dependencies", "Read log + compact")

    def test_baseline_is_measured_before_the_fix_edits_anything(self):
        # Reversing these makes new_failures() permanently empty: the "before"
        # snapshot would be taken from an already-fixed tree.
        names = step_names("heal.yml")
        self.assert_before(names, "Baseline — tests already failing", "Fix")

    def test_the_pr_exists_before_the_review_that_comments_on_it(self):
        # Load-bearing twice over. The review-failure step comments on
        # `pr_url.txt`, which only the PR step writes, so a reversal breaks the
        # block path. And it is what `artifacts/readme.md` describes to a person
        # meeting a bot PR: the gate is what a PR opening attests to, the review
        # runs afterwards, and the merge is what says it passed.
        names = step_names("heal.yml")
        self.assert_before(names, "Commit + PR", "Review")
        self.assert_before(names, "Review", "Merge")

    def test_baseline_is_measured_before_the_repro_test_is_written(self):
        # The repro test is red by construction. Counting it as pre-existing
        # would let the gate treat the very failure being healed as acceptable.
        names = step_names("heal.yml")
        self.assert_before(
            names, "Baseline — tests already failing", "Red — write + run repro test"
        )


class GitsOwnExecutionSurfaceIsWatched(unittest.TestCase):
    """`.git/` never appears in a diff, so every diff-based check is blind to it.

    git executes what its own config tells it to: `core.fsmonitor`,
    `core.pager`, `core.sshCommand`, `diff.external`, a `filter.*.clean`, or
    anything dropped into `.git/hooks/`. A fix that writes one of those has
    arbitrary shell on the next `git` invocation — and the next invocations are
    `git add -A` in the gate step and `git commit`/`git push` in Commit + PR,
    which carries `GH_TOKEN`.

    Neither the gate nor the Review agent can see it: `.git/` is not tracked, so
    it is in no diff either of them reads. The control therefore cannot live in
    `gate.py` at all — it has to be the workflow comparing before against after.

    The comparison is deliberately a whole-content hash rather than a list of
    dangerous keys. An enumeration of executable git settings goes stale the
    next time git adds one, and a check that silently stops covering a setting
    is this project's signature defect.
    """

    def _heal(self):
        return {s["name"]: s for s in job_steps("heal.yml")}

    def _snapshot_step(self):
        for step in job_steps("heal.yml"):
            if "git-exec-surface" in step["body"] and "Loop tree" not in step["name"]:
                return step
        return None

    def test_the_snapshot_is_taken_before_fix_runs(self):
        step = self._snapshot_step()
        self.assertIsNotNone(step, "nothing records git's execution surface")
        names = step_names("heal.yml")
        self.assertLess(
            names.index(step["name"]), names.index("Fix"),
            "a snapshot taken after Fix records the tampered state as the baseline",
        )

    def test_the_snapshot_is_stored_outside_the_repository(self):
        # Inside the repo, the fix agent can edit the record as easily as the
        # thing it records. RUNNER_TEMP is outside the checkout.
        step = self._snapshot_step()
        self.assertIn("RUNNER_TEMP", step["body"])

    def test_the_guard_step_compares_it(self):
        # The COMPARISON, not the mention. The guard also re-snapshots into
        # `.after`, and those lines carry both "git-exec-surface" and
        # "RUNNER_TEMP" — so a check for either string passes a guard that
        # records the tampered state and never looks at it. Deleting the
        # comparison was NOT CAUGHT until this asserted the diff itself.
        guard = next(
            s for s in job_steps("heal.yml") if s["name"].startswith("Loop tree")
        )
        self.assertRegex(
            guard["body"],
            r"diff\b[^\n]*git-exec-surface\.before[^\n]*git-exec-surface\.after",
            "the guard never compares the before and after snapshots",
        )

    def test_a_mismatch_actually_fails_the_step(self):
        # A comparison whose result is discarded is the same as no comparison.
        guard = next(
            s for s in job_steps("heal.yml") if s["name"].startswith("Loop tree")
        )
        # Bounded to the surface comparison's own `if`. An unbounded remainder
        # is satisfied by the loop-tree refusal further down, which is a
        # different check entirely — the same run-past-the-block defect this
        # file has filed twice.
        after_compare = guard["body"].partition("git-exec-surface.after")[2]
        block = after_compare.partition("\n          fi")[0]
        self.assertIn(
            "exit 1", block,
            "the surface comparison reports a mismatch and lets the step succeed",
        )

    def test_the_guard_still_runs_immediately_after_fix(self):
        names = step_names("heal.yml")
        guard = next(n for n in names if n.startswith("Loop tree"))
        self.assertEqual(
            names.index(guard), names.index("Fix") + 1,
            "a step between Fix and the guard runs git before the check does",
        )

    def test_the_guard_runs_on_every_cycle_that_reaches_a_commit(self):
        """Position is not reachability: an `if:` can retire the step in place.

        All three checks in this class — git's execution surface, the loop
        tree's tracked files, and its untracked ones — live in this one step,
        so a condition that never matches disables the whole set at once while
        every other test here still passes on the body it no longer runs.

        Pinned as equality with the step that commits, because that is the
        actual invariant: the surface must be measured on exactly the cycles
        whose output reaches a branch. Asserting the condition is merely
        non-empty, or naming one output it mentions, both survive rewriting it
        to a comparison that is never true.
        """
        steps = {s["name"]: s for s in job_steps("heal.yml")}
        guard = next(s for n, s in steps.items() if n.startswith("Loop tree"))
        self.assertEqual(
            guard["cond"], steps["Commit + PR"]["cond"],
            "the guard and the commit run under different conditions, so there "
            "is a cycle that pushes a fix the guard never inspected",
        )

    def test_both_config_and_hooks_are_covered(self):
        step = self._snapshot_step()
        self.assertIn(".git/config", step["body"])
        self.assertIn(".git/hooks", step["body"])

    def test_the_surface_reaches_outside_the_checkout(self):
        """git executes more than the checkout's own `.git/`.

        `$HOME/.gitconfig` carries the same `core.fsmonitor` reach and is read
        by every git call — a global config planted there executes while both
        snapshots of the checkout come back byte-identical.

        Both snapshot blocks, pre-Fix and guard: the comparison only protects
        what BOTH of them hash, and the two blocks are separate text that has
        to be kept in step by hand.

        The ceiling, stated because it used to be asserted as coverage: the
        Actions file commands (`$GITHUB_ENV`, `$GITHUB_PATH`) reach every later
        step and are NOT covered here. Their paths are per-step, so neither end
        of a before/after comparison can name the file Fix actually wrote.
        `test_nothing_measured_is_reallocated_per_step` pins them OUT.
        """
        blocks = [self._snapshot_step()["body"], self._guard()["body"]]
        for i, body in enumerate(blocks):
            with self.subTest(block="pre-fix" if i == 0 else "guard"):
                self.assertIn("$HOME/.gitconfig", body)

    def _guard(self):
        return next(s for s in job_steps("heal.yml") if s["name"].startswith("Loop tree"))

    def test_the_comparison_runs_before_the_guard_calls_git(self):
        """A guard that calls git before measuring git hands the tamper its cue.

        `core.fsmonitor` executes on the NEXT git invocation, whichever step
        makes it — including this one. Reproduced against git 2.43.0 with the
        step body run verbatim: a planted fsmonitor fired on `git ls-files`,
        restored `.git/config`, rewrote the record, and the guard exited 0 with
        the payload already executed.

        `sha256sum` and `find` are not git, so the measurement can and must come
        first.
        """
        body = self._guard()["body"]
        lines = body.splitlines()
        compare = next(
            i for i, line in enumerate(lines)
            if "git-exec-surface.after" in line and not line.strip().startswith("#")
        )
        first_git = next(
            i for i, line in enumerate(lines)
            if re.search(r"(?<![-\w])git\s+[a-z]", line) and not line.strip().startswith("#")
        )
        self.assertLess(
            compare, first_git,
            f"the guard runs `{lines[first_git].strip()}` at line {first_git} before "
            f"measuring git's execution surface at line {compare}, so a planted "
            f"hook or config executes and can erase itself first",
        )

    def test_the_verdict_rests_on_a_step_output_not_on_a_file(self):
        """$RUNNER_TEMP is outside the checkout and still writable by the agent.

        The snapshot comment used to offer "outside the checkout" as the reason
        the record was safe. It is not a permission boundary: nothing in either
        harness denies writes there, so the payload that restores `.git/config`
        can rewrite the baseline in the same breath. A completed step's outputs
        cannot be rewritten by a later step, which is the property this needs.

        Asserted as the whole conditional — the measurement feeding `$now`, the
        comparison against a step output, and a non-zero exit inside that
        branch. Three separate mutations survived weaker forms of this test: a
        bare `steps\\.\\w+\\.outputs\\.` search matched the step's own `if:`
        condition, an `exit 1` search matched the loop-tree check further down,
        and `if false; then` left every searched string in place. A string that
        survives the change is not evidence about the change.
        """
        self.assertRegex(
            self._snapshot_step()["body"], r'git_surface=[^\n]*>> "\$GITHUB_OUTPUT"',
            "the pre-Fix snapshot publishes no step output, so the only baseline "
            "is a file the agent can rewrite",
        )
        run = self._guard()["body"].partition("run: |")[2]
        self.assertRegex(
            run, r'now="\$\(sha256sum < "\$RUNNER_TEMP/git-exec-surface\.after"',
            "nothing computes the current surface, so the comparison below has "
            "nothing real on its left-hand side",
        )
        self.assertRegex(
            run,
            re.compile(
                r'if \[ "\$now" != "\$\{\{ steps\.[a-z_]+\.outputs\.[a-z_]+ \}\}" \]; then'
                r'(?:(?!\n          fi\b).)*?exit 1',
                re.DOTALL,
            ),
            "the guard does not compare the measured surface against the "
            "pre-Fix step output and exit non-zero when they differ",
        )

    # The two blocks below are the seam between the pre-Fix snapshot and the
    # guard. Both sides had structural tests and neither had a test of the
    # agreement between them, which is how a measurement that can never match
    # shipped and failed every cycle on every repo.

    MEASUREMENT_OPEN = "{ sha256sum"
    MEASUREMENT_CLOSE = '} > "$RUNNER_TEMP/git-exec-surface.'

    def _measurements(self, step: dict) -> list[str]:
        """The measurement commands inside one `{ … } > file` block."""
        lines = step["body"].splitlines()
        start = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith(self.MEASUREMENT_OPEN)
        )
        end = next(
            i for i, line in enumerate(lines[start:], start)
            if line.strip().startswith(self.MEASUREMENT_CLOSE)
        )
        return [line.strip() for line in lines[start:end] if line.strip()]

    def test_both_sides_measure_the_same_surface(self):
        """A baseline and a comparison that measure different things is not a
        check, it is a coin flip — and the direction it lands is unconditional.

        Neither side is authoritative, so this pins them equal rather than
        pinning either to a literal. The first line carries the `{` and the
        last carries no `}`, so the sets compare as written.
        """
        before = self._measurements(self._snapshot_step())
        after = self._measurements(self._guard())
        self.assertEqual(
            before, after,
            "the pre-Fix snapshot and the guard measure different surfaces, so "
            "the comparison between them cannot mean what it claims",
        )

    def test_nothing_measured_is_reallocated_per_step(self):
        """Actions gives each STEP its own random-UUID file for the file
        commands, and `sha256sum <path>` prints the path beside the digest.

        Measuring one is therefore two unrelated files under two names: the
        comparison fails on every cycle, on every repo, unconditionally, and
        the loop can diagnose and fix and never ship. Hashing their contents
        instead would pass always and prove as little — Fix's own step file is
        a third path neither measurement ever sees.
        """
        per_step = ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")
        for step in (self._snapshot_step(), self._guard()):
            for line in self._measurements(step):
                for name in per_step:
                    self.assertNotIn(
                        name, line,
                        f"{step['name']!r} measures ${name}, which Actions "
                        f"reallocates per step: {line}",
                    )


class VerifyAsksTheSameTwoQuestionsTheGateDoes(unittest.TestCase):
    """Post-merge verification must not reinstate the all-green rule.

    The gate deliberately answers two separate questions — is the bug fixed,
    and did the fix break anything — because a single pre-existing failure
    would otherwise veto every correct fix. Verify ran `eval "$TEST_CMD" ||
    bad=1`, which is that rule exactly, at the point where the remedy is a
    rollback rather than a block.

    The consequence compounds instead of repeating. On a repo carrying one
    unrelated failure: the gate correctly passes a good fix, it merges, Verify
    sees the unrelated failure, rolls back, and records `outcome="reverted"` —
    which is prune-exempt at any age and ranked first in every later prompt as
    KNOWN-BAD. Each correct fix permanently teaches the loop that the correct
    fix was wrong.
    """

    def _verify(self):
        return next(
            s for s in job_steps("heal.yml") if s["name"].startswith("Verify")
        )

    def test_verify_branches_on_the_baseline_mode(self):
        body = self._verify()["body"]
        self.assertIn(
            "steps.base.outputs.mode", body,
            "Verify judges the merged branch without asking whether "
            "pre-existing failures can be separated",
        )
        # The CONDITION, not the mention. Replacing the guard with `if false`
        # leaves every string this class searches for exactly where it was
        # while making the regression branch unreachable — and that mutation
        # went NOT CAUGHT until this assertion existed.
        self.assertRegex(
            body, r'if \[ "\$MODE" = "regression" \]',
            "the regression branch is present but not reachable from $MODE",
        )

    def test_verify_compares_against_the_cycles_own_baseline(self):
        self.assertIn("baseline_failures.txt", self._verify()["body"])

    def test_a_bare_all_green_test_is_not_the_whole_check(self):
        # The strict fallback legitimately still uses a plain exit code, so the
        # claim is not "no all-green anywhere" — it is that the regression mode
        # exists and is reached. A bare containment check is satisfied by the
        # step's own `regression=` output name, which survives deleting the
        # branch; this asserts the comparison that selects the mode.
        body = self._verify()["body"]
        self.assertRegex(
            body, r'"\$MODE" = "regression"',
            "nothing in Verify selects the regression mode, so the strict "
            "all-green rule is the only path left",
        )

    def test_verify_measures_the_merged_branch_and_not_the_fix_branch(self):
        """Post-merge verification has to move to what was merged.

        Without the checkout Verify re-runs the suite on the fix branch — the
        same tree the gate already passed — so a regression that only appears
        once the fix meets the rest of the branch is invisible, and the
        rollback it should have triggered never fires. Pinned here because the
        one test that EXECUTES Verify strips this line before running it, to
        keep the snippet runnable off a runner.
        """
        body = self._verify()["body"]
        self.assertRegex(
            body, r'git checkout[^\n]*TARGET_BRANCH',
            "Verify never checks out the target branch, so it re-tests the fix branch",
        )


class TheAgentIsToldAboutTheSurfaceTheGuardWatches(unittest.TestCase):
    """The guard refuses on a `.git/` change the gate cannot see, and that
    refusal records no attempt — it is neither the gate step nor the suite
    step, so the attempt cap does not advance and the failure would re-diagnose
    on every tick. The only cheaper defence is Fix knowing the rule before it
    spends the cycle: `fix.md` is the one document Fix reads that carries reach
    rules, so the rule lives there.
    """

    def test_fix_is_told_not_to_write_into_git(self):
        fix = (
            Path(__file__).resolve().parents[1] / "templates" / "fix.md"
        ).read_text(encoding="utf-8")
        lines = [l for l in fix.splitlines() if ".git/" in l or "`.git`" in l]
        self.assertTrue(lines, "fix.md never mentions .git/ to the agent")
        self.assertTrue(
            any(re.search(r"(?i)never|do not|may not", l) for l in lines),
            f".git/ is mentioned but never forbidden: {lines}",
        )


class VerifyRefusesASuiteItCouldNotRun(unittest.TestCase):
    """An empty failure list means "all green" or "nothing could be parsed".

    Only the suite's exit code separates them, and `new_failures(before, [])` is
    empty by construction — so an adapter that cannot answer produced exactly
    the same verdict as a clean run. Post-merge, that means a fix which breaks
    the suite outright is never rolled back and is recorded `outcome="merged"`.

    The asymmetry is what makes it a defect rather than a judgement call. The
    same shape is refused twice elsewhere in this codebase: `guardrails/cli.py`
    turns it away before the merge ("suite exited N but no failing tests were
    parsed"), and the Baseline step raises `SystemExit` on a `None` adapter.
    Verify is the one place where the remedy is a rollback, and it did neither.

    Driven by EXECUTING the snippet the workflow actually embeds, against stub
    adapters. Asserting on its text cannot tell a snippet that refuses `None`
    from one that mentions it.
    """

    SNIPPET_RE = re.compile(r'python -B -c "(.*?)(?<!\\)"', re.S)

    def _snippet(self):
        body = next(s for s in job_steps("heal.yml") if s["name"].startswith("Verify"))["body"]
        for match in self.SNIPPET_RE.finditer(body):
            if "baseline_failures" in match.group(1):
                # YAML strips the block indent before the shell ever sees this,
                # so the snippet reaches python at column 0. Reading it out of
                # the raw file keeps the indent, and running it without
                # dedenting reproduces an IndentationError that the workflow
                # does not have.
                return textwrap.dedent(match.group(1)).replace("${SHL_CYCLE_ID}", "c1")
        self.fail("Verify embeds no snippet reading the cycle's baseline")

    def _run(self, failing, baseline=("tests/test_old.py::test_known",)):
        """Run the real snippet with `load_adapter().failing_tests()` stubbed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "adapters").mkdir()
            (root / "adapters" / "__init__.py").write_text(
                "class _A:\n"
                f"    def failing_tests(self):\n        return {failing!r}\n"
                "def load_adapter():\n    return _A()\n",
                encoding="utf-8",
            )
            (root / "evidence" / "c1").mkdir(parents=True)
            (root / "evidence" / "c1" / "baseline_failures.txt").write_text(
                "\n".join(baseline) + "\n", encoding="utf-8"
            )
            return subprocess.run(
                [sys.executable, "-B", "-c", self._snippet()],
                cwd=root, capture_output=True, text=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            )

    def test_an_adapter_that_cannot_answer_stops_the_step(self):
        result = self._run(None)
        self.assertNotEqual(
            result.returncode, 0,
            "a None adapter produced a verdict; `or []` reads 'cannot answer' as "
            "'nothing is failing', which is the merge-and-never-roll-back case",
        )

    def test_a_genuine_regression_is_reported(self):
        result = self._run(["tests/test_old.py::test_known", "tests/test_new.py::test_broke"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tests/test_new.py::test_broke", result.stdout)

    def test_a_pre_existing_failure_alone_is_not_a_regression(self):
        # The whole reason Verify has a regression mode: a repo carrying one
        # unrelated failure must not have every correct fix reverted.
        result = self._run(["tests/test_old.py::test_known"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("test_known", result.stdout.partition("\n")[2])

    def test_the_step_can_tell_an_empty_suite_from_a_clean_one(self):
        # The count is what lets the shell apply `cli.py`'s rule: a non-zero
        # exit code with zero parsed failures means the suite did not run.
        result = self._run([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.partition("\n")[0].strip(), "0")

    def test_the_shell_treats_an_unparseable_suite_as_bad(self):
        body = next(s for s in job_steps("heal.yml") if s["name"].startswith("Verify"))["body"]
        run = body.partition("run: |")[2]
        # The CONDITION, and scoped to its own block. `if false; then` and a
        # deleted `bad=1` both leave every searchable string in place, and a
        # first version of this bounded the search at the OUTER `fi`, so the
        # lazy match ran past the branch and found the `|| bad=1` in the strict
        # fallback below — reporting a branch that no longer sets anything as
        # sound. The block is the unit; nothing outside it may vouch for it.
        block = re.search(
            r'elif \[ "\$suite_rc" != "0" \] && \[ "\$parsed" = "0" \]; then\n'
            r"(.*?)\n {12}fi",
            run,
            re.DOTALL,
        )
        self.assertIsNotNone(
            block,
            "the regression branch has no condition reacting to a suite that "
            "exited non-zero while parsing no failures at all",
        )
        self.assertIn(
            "bad=1", block.group(1),
            "that branch runs and changes nothing, so an unparseable suite is "
            "still recorded as a clean verification",
        )


class BaselineRefusesAnAdapterThatCannotAnswer(unittest.TestCase):
    """`None` and `[]` mean opposite things and read the same downstream.

    `failing_tests()` returning `None` means "this adapter cannot list failing
    tests", which is what selects the gate's strict all-green rule. Collapse it
    to an empty list and the step reports `mode=regression` with an empty
    baseline instead — so `new_failures(baseline, current)` treats every
    pre-existing failure in the repo as a regression this fix caused, and the
    gate blocks every correct fix forever. Its sibling in Verify is executed by
    `VerifyRefusesASuiteItCouldNotRun`; this half was only ever read.
    """

    SNIPPET_RE = re.compile(r'python -B -c "(.*?)(?<!\\)"', re.S)

    def _snippet(self) -> str:
        body = next(
            s for s in job_steps("heal.yml") if s["name"].startswith("Baseline")
        )["body"]
        for match in self.SNIPPET_RE.finditer(body):
            if "failing_tests" in match.group(1):
                return textwrap.dedent(match.group(1))
        self.fail("the Baseline step embeds no snippet asking the adapter")

    def _run(self, failing):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "adapters").mkdir()
            (root / "adapters" / "__init__.py").write_text(
                "class _A:\n"
                f"    def failing_tests(self):\n        return {failing!r}\n"
                "def load_adapter():\n    return _A()\n",
                encoding="utf-8",
            )
            return subprocess.run(
                [sys.executable, "-B", "-c", self._snippet()],
                cwd=root, capture_output=True, text=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            )

    def test_an_adapter_that_cannot_answer_selects_the_strict_rule(self):
        result = self._run(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "UNSUPPORTED",
            "a None adapter did not report UNSUPPORTED, so the step takes the "
            "regression branch with an empty baseline and the gate then reads "
            "every pre-existing failure as caused by this fix",
        )

    def test_a_genuinely_clean_repo_is_not_confused_with_it(self):
        # The direction that makes the check meaningful: an adapter that CAN
        # answer and finds nothing failing must not select the strict rule.
        result = self._run([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_the_listed_failures_come_back_sorted(self):
        result = self._run(["tests/test_b.py::t", "tests/test_a.py::t"])
        self.assertEqual(
            result.stdout.split(), ["tests/test_a.py::t", "tests/test_b.py::t"]
        )


class EvidenceSurvivesTheCycleThatProducedIt(unittest.TestCase):
    """The bundle matters most on the cycles that fail.

    Gated on `success()` the upload is skipped exactly when a human needs it —
    a gate refusal, a crashed Review, a rollback — leaving the operator a red
    job and no prompt, no raw output and no gate verdict to read.
    """

    def test_the_upload_runs_whatever_the_cycle_did(self):
        chunk = step_chunk("heal.yml", "Upload evidence")
        cond = re.search(r"^\s*if: (.+)$", chunk, re.MULTILINE).group(1)
        self.assertTrue(
            cond.startswith("always()"),
            f"evidence is uploaded conditionally on the cycle succeeding: {cond}",
        )

    def test_the_operator_can_still_turn_it_off(self):
        chunk = step_chunk("heal.yml", "Upload evidence")
        self.assertIn("SHL_EVIDENCE_UPLOAD != 'false'", chunk)


class TheMergeEscalationAsksWhetherTheMergeHAPPENED(unittest.TestCase):
    """`steps.merge.outcome` answers a different question than it looks like.

    Merge runs `gh pr merge` and then refuses if it cannot read the resulting
    commit, because a rollback needs that SHA and `git revert -m 1` will
    happily revert the wrong commit instead. That refusal is `exit 1`, which
    sets the step's `outcome` to `failure` — so gating the post-merge
    escalation on `outcome == 'success'` silences it in exactly the state its
    own error message describes: merged, live, and unrollbackable.

    The fix records the fact at the moment it becomes true, rather than
    inferring it from the step's exit status.
    """

    def _step(self, prefix):
        return next(s for s in job_steps("heal.yml") if s["name"].startswith(prefix))

    def test_the_merge_step_records_that_it_merged(self):
        body = self._step("Merge")["body"]
        merged_at = body.find("merged=true")
        self.assertNotEqual(merged_at, -1, "Merge records no merged-happened fact")
        # Before the SHA check, or it is lost in exactly the failure it exists
        # to report.
        self.assertLess(merged_at, body.find("could not read the merge commit"))

    def test_the_escalation_reads_that_fact_not_the_step_outcome(self):
        cond = self._step("Escalate a post-merge failure")["cond"]
        self.assertIn("steps.merge.outputs.merged", cond)
        self.assertNotIn("steps.merge.outcome", cond)


    def test_the_flag_is_written_after_the_merge_not_before(self):
        """`merged=true` is a record of something that happened, not a plan.

        Written above `gh pr merge`, it claims a merge that the very next line
        may fail to perform — and everything downstream keyed on it (the
        escalation, the rollback's reason for existing) then fires on a PR that
        is still open. Moving the line up left all 553 tests green, because
        every assertion here searched for the string rather than its position.
        """
        body = next(s for s in job_steps("heal.yml") if s["name"] == "Merge")["body"]
        run = body.partition("run: |")[2]
        merge = run.index("gh pr merge")
        flag = run.index('echo "merged=true"')
        self.assertLess(
            merge, flag,
            "the merge flag is set before the merge is attempted, so it records "
            "an intention rather than an outcome",
        )


class DeployIsScopedToTheRefItsCommandTargets(unittest.TestCase):
    """The documented self-test runs on a branch; the deploy command does not.

    `reference/verifying-the-install.md` tells the operator to dispatch a
    verification cycle on a branch. Deploy was gated only on approval and a
    non-empty command, so that cycle runs the operator's PRODUCTION deploy
    command. The health probe forty lines below is already ref-scoped for the
    closely related reason that a branch cycle must not be judged against
    production; the deploy that precedes it needs the same scoping.

    Guarded in the shell rather than in `if:`, matching the probe: the same
    two job-level env vars are already proven to work there, and an expression
    context nobody has exercised on a runner is not the place to find out.
    """

    def _deploy(self):
        return next(s for s in job_steps("heal.yml") if s["name"] == "Deploy")

    def test_deploy_refuses_when_the_default_branch_is_unknown(self):
        """Fail closed, like the probe: an empty comparand must not silently
        become "deploy anyway".

        Asserted as the CONDITION with its exit, not as a mention of the
        variable name: `assertIn("DEFAULT_BRANCH")` passed with the refusal
        deleted, because the ref-scope comparison two lines down names the same
        variable. A test that any correct body satisfies by vocabulary is a
        test of nothing.
        """
        run = self._deploy()["body"].partition("run: |")[2]
        self.assertRegex(
            run,
            re.compile(
                r'if \[ -z "\$DEFAULT_BRANCH" \]; then'
                r'(?:(?!\n          fi\b).)*?exit 1',
                re.DOTALL,
            ),
            "Deploy has no empty-DEFAULT_BRANCH refusal that fails the step",
        )

    def test_deploy_skips_off_the_default_branch(self):
        body = self._deploy()["body"]
        self.assertIn("TARGET_BRANCH", body)
        self.assertRegex(body, r'\$TARGET_BRANCH"?\s*!=\s*"?\$DEFAULT_BRANCH')

    def test_the_deploy_command_still_runs_somewhere(self):
        # A guard that skipped unconditionally would pass the checks above
        # while disabling deployment entirely.
        self.assertIn('eval "$DEPLOY_CMD"', self._deploy()["body"])

    def test_every_site_that_runs_the_deploy_command_is_ref_scoped(self):
        """Rollback re-runs the same command, and the guard was added to Deploy only.

        Verify's suite half runs on every ref, so a branch verification cycle
        can set `regression=true` and reach Rollback — which then runs the
        operator's PRODUCTION deploy from a branch checkout, the precise outcome
        Deploy's own comment says it prevents.

        The membership is COMPUTED rather than listed. Naming the two steps
        would pass forever the moment a third call site appears, which is how
        this defect existed: the fix was applied at the site the finding named
        and not at its sibling.
        """
        sites = [
            step for step in job_steps("heal.yml")
            if 'eval "$DEPLOY_CMD"' in step["body"]
        ]
        self.assertGreaterEqual(len(sites), 2, f"expected Deploy and Rollback, got {[s['name'] for s in sites]}")
        for step in sites:
            with self.subTest(step=step["name"]):
                self.assertRegex(
                    step["body"], r'"?\$TARGET_BRANCH"?\s*(!=|=)\s*"?\$DEFAULT_BRANCH',
                    f"{step['name']} runs the operator's deploy command without "
                    f"checking which ref the cycle is on",
                )


class ReproPathIsWorkflowComposed(unittest.TestCase):
    """The repro file's path must never come from agent output.

    Diagnose writes it from an untrusted log, so an injected path could target
    a workflow file or escape the repo. Validating the agent's path against
    `^tests/test_[A-Za-z0-9_]+\\.py$` closes that and breaks everything else: the
    regex is a pytest convention, and it rejects every non-Python target
    outright. So the path is composed from the operator's `SHL_REPRO_PATH`
    pattern and the GitHub issue number, neither of which the agent controls,
    and there is nothing left to filter.
    """

    def setUp(self):
        self.text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")

    def test_agent_supplied_path_is_never_read(self):
        self.assertNotIn(".repro_test.path", self.text)

    def test_path_is_built_from_the_pattern_and_the_issue_number(self):
        # On the LINE that computes it. Both names appear in comments elsewhere
        # in this file, so a whole-file containment check passes a heal.yml
        # that mentions each somewhere and composes the path from neither.
        composed = [
            line.strip()
            for line in executable(self.text).splitlines()
            if "loop.py repro-path" in line
        ]
        self.assertEqual(len(composed), 1, f"expected one composition site: {composed}")
        self.assertIn("steps.issue.outputs.number", composed[0])
        # The pattern half reaches loop.py through the environment rather than
        # the command line, so it is the job env that has to carry it.
        self.assertRegex(self.text, r"(?m)^\s*SHL_REPRO_PATH:")

    def test_the_code_is_still_read_from_diagnose(self):
        # Removing the path must not accidentally remove the test body too.
        self.assertIn(".repro_test.code", self.text)


def step_chunk(workflow: str, name: str) -> str:
    """The YAML text of one step, from its `- name:` to the next step."""
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    for chunk in re.split(r"^ {6}- (?=name:|uses:)", text, flags=re.MULTILINE):
        match = re.match(r"name: (.+)", chunk)
        if match and match.group(1).strip() == name:
            return chunk
    raise AssertionError(f"no step named {name!r} in {workflow}")


class PostDeployVerificationIsNotOptional(unittest.TestCase):
    """Verify and rollback must not depend on there being a deploy COMMAND.

    Deploy is skipped when `SHL_DEPLOY_CMD` is empty, which is correct — there
    is nothing to run. Gating Verify on the same condition is not: a
    push-triggered deploy (Vercel, Netlify, Fly — the most common way anything
    ships) has no deploy command, so the suite-on-merged-main check and the
    health probe are skipped, `steps.verify.outputs.regression` is never set,
    and rollback can never fire. The loop merges and deploys with post-deploy
    verification silently disabled. Fails OPEN.
    """

    def test_verify_does_not_depend_on_a_deploy_command(self):
        chunk = step_chunk("heal.yml", "Verify — test the merged target branch")
        condition = re.search(r"^\s*if: (.+)$", chunk, re.MULTILINE).group(1)
        self.assertNotIn("SHL_DEPLOY_CMD", condition)

    def test_deploy_itself_still_skips_when_there_is_no_command(self):
        chunk = step_chunk("heal.yml", "Deploy")
        condition = re.search(r"^\s*if: (.+)$", chunk, re.MULTILINE).group(1)
        self.assertIn("SHL_DEPLOY_CMD", condition)

    def test_rollback_is_reachable(self):
        # It keys off Verify's output, so Verify running is its precondition.
        chunk = step_chunk("heal.yml", "Rollback on regression")
        self.assertIn("steps.verify.outputs.regression", chunk)

    def test_rollback_fires_on_a_regression_and_not_on_a_clean_verification(self):
        """The comparison, not the output name it compares.

        Searching for `steps.verify.outputs.regression` passes on the inverted
        condition, and inverting it breaks BOTH directions at once. A real
        regression never rolls back: the bad fix stays merged and live, with no
        revert, no escalation and no `reverted` incident. And every HEALTHY
        cycle now rolls back — reverting its own correct fix, re-running the
        operator's deploy command, and recording `outcome="reverted"`, which is
        prune-exempt at any age and ranked first in every later prompt. Each
        success would permanently teach the loop that the correct fix was wrong.
        """
        cond = re.search(
            r"^\s*if: (.+)$", step_chunk("heal.yml", "Rollback on regression"), re.MULTILINE
        ).group(1)
        self.assertIn(
            "steps.verify.outputs.regression == 'true'", cond,
            f"rollback does not trigger on a regression being reported: {cond}",
        )

    def test_the_incident_records_which_of_the_two_outcomes_happened(self):
        # `outcome` is what a later cycle reads back as KNOWN-BAD. Collapsed to
        # a constant `merged`, a fix proven to regress is remembered as one that
        # worked, and the next cycle proposes it again with nothing to warn it.
        chunk = step_chunk("heal.yml", "Record incident")
        self.assertRegex(
            executable(chunk),
            r"outcome=\"\$\{\{ steps\.verify\.outputs\.regression == 'true' "
            r"&& 'reverted' \|\| 'merged' \}\}\"",
            "the recorded outcome does not depend on whether a regression was found",
        )


class TheGateStepsVerdictReachesTheJob(unittest.TestCase):
    """A refusal the step swallows is worse than no gate at all.

    The gate runs under `set +e` so its output can be scrubbed into the
    evidence bundle before the step dies. That makes the exit code a value
    passed by hand through three later commands, and every one of them
    overwrites `$?`. Capture it one command late and `gate_rc` holds `cat`'s
    status, which is always 0.

    Nothing downstream notices. `On gate/suite fail, record attempt` is gated
    on `steps.gate.outcome == 'failure'`, so no attempt is recorded and the cap
    never advances; `Commit + PR`, `Review` and `Merge` all run on `success()`.
    A fix the gate refused for weakening a test merges to the default branch
    and deploys, with the refusal sitting unread in `gate.txt`.

    Executed rather than read. The step's own text is the same either way — the
    defect is in the ORDER of two adjacent lines, which no search distinguishes.
    """

    def _tail(self) -> str:
        body = next(s for s in job_steps("heal.yml") if s["name"].startswith("Gate"))["body"]
        _, marker, rest = body.partition("set +e")
        self.assertTrue(marker, "the gate step no longer suspends errexit around the CLI call")
        return textwrap.dedent(marker + rest).strip("\n")

    def _run(self, gate_rc: int) -> int:
        """Run the step's real tail with `python` stubbed to exit `gate_rc`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".shl").mkdir()
            (root / "ev").mkdir()
            stub = root / "bin"
            stub.mkdir()
            (stub / "python").write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *scrub*) exit 0 ;;\n"
                "esac\n"
                'exit "$GATE_RC"\n',
                encoding="utf-8",
            )
            (stub / "python").chmod(0o755)
            script = f'set -e\nev="{root / "ev"}"\nargs=()\n{self._tail()}\n'
            return subprocess.run(
                ["bash"], input=script, text=True, capture_output=True, cwd=root,
                env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}",
                     "GATE_RC": str(gate_rc)},
            ).returncode

    def test_a_refusal_fails_the_step(self):
        self.assertNotEqual(
            self._run(1), 0,
            "the gate refused and the step succeeded, so every step gated on "
            "success() runs and the blocked fix merges",
        )

    def test_the_exact_status_survives_the_scrub(self):
        # Not merely non-zero: `cli.py` distinguishes a refusal (1) from a
        # usage or read error (2), and the evidence bundle is read on that.
        self.assertEqual(self._run(2), 2)

    def test_a_clean_gate_still_passes(self):
        # The inverse, or "always fail" would satisfy the two above while
        # stopping every correct fix at the gate.
        self.assertEqual(self._run(0), 0)


class TheMergeRestsOnTheReviewsAnswer(unittest.TestCase):
    """Separation of duties is this one output, and nothing else enforces it.

    Fix must not be its own judge, so Review runs as a separate cold agent and
    `steps.rev.outputs.approved` is the whole of what its verdict becomes. Emit
    that flag without consulting `review.json` and the block path goes quiet —
    no PR comment, no recorded attempt, no non-zero exit — while `Merge` reads
    the same flag and merges. The loop then self-approves every cycle, and an
    open PR carrying a BLOCKED review looks identical to an approved one.
    """

    def setUp(self):
        self.review = next(s for s in job_steps("heal.yml") if s["name"] == "Review")

    def test_the_approval_is_read_out_of_the_reviews_own_output(self):
        writes = [
            line for line in self.review["body"].splitlines()
            if "approved=true" in line
        ]
        self.assertTrue(writes, "the Review step publishes no approval at all")
        for line in writes:
            self.assertIn(
                "review.json", line,
                "the approved flag is written without reading the review, so "
                f"every cycle self-approves: {line.strip()}",
            )

    def test_a_review_that_does_not_approve_is_representable(self):
        # Without the false branch there is no verdict to block on: an
        # unapproved review leaves the output unset, which `Merge` reads as
        # not-'true' today but which nothing pins.
        self.assertIn("approved=false", self.review["body"])

    def test_the_merge_requires_the_approval_and_the_block_path_requires_its_absence(self):
        steps = {s["name"]: s for s in job_steps("heal.yml")}
        self.assertIn("steps.rev.outputs.approved == 'true'", steps["Merge"]["cond"])
        self.assertIn(
            "steps.rev.outputs.approved != 'true'",
            steps["Block + record attempt on review fail"]["cond"],
        )

    def test_the_block_path_actually_fails_the_cycle(self):
        # Commenting on the PR and then exiting 0 leaves Merge's `success()`
        # satisfied, so a blocked review merges anyway.
        self.assertIn("exit 1", steps_body("Block + record attempt on review fail"))

    def test_the_merge_completes_before_the_step_does(self):
        # `--auto` queues the merge and returns, so Deploy, Verify and Record
        # all run against a branch that has not been merged yet — and the merge
        # commit the SHA read below depends on does not exist. The step's own
        # comment says this; nothing enforced it.
        body = steps_body("Merge")
        self.assertRegex(
            body, r"gh pr merge --merge\b",
            "the merge is queued rather than performed, so everything "
            "downstream verifies an unmerged branch",
        )
        self.assertNotIn("--auto", body)


def steps_body(name: str, workflow: str = "heal.yml") -> str:
    return next(s for s in job_steps(workflow) if s["name"] == name)["body"]


class IssueDedupIsFingerprintKeyed(unittest.TestCase):
    """`CLAUDE.md`: "Keying on the title cannot work — it is prose a
    model writes fresh each cycle." That rule binds issue dedup exactly as it
    binds incident memory: here a miss files a duplicate issue for a failure
    already being worked, which resets the attempt cap to zero.
    """

    def setUp(self):
        self.chunk = step_chunk("heal.yml", "Find or file issue")
        # The step's own comment quotes the `in:title` query while explaining
        # why it is wrong, so the guard has to look at what actually runs.
        self.code = "\n".join(
            line for line in self.chunk.splitlines() if not line.lstrip().startswith("#")
        )

    def test_open_issues_are_not_searched_by_title(self):
        self.assertNotIn("in:title", self.code)

    def test_lookup_goes_through_the_shared_fingerprint_code_path(self):
        # `self.code`, not `self.chunk`. setUp strips comments precisely because
        # this step's own commentary quotes the calls it describes — so asserting
        # against the raw chunk is satisfied by a comment mentioning the call
        # while the call itself is gone.
        self.assertIn("loop.py find-issue", self.code)

    def test_a_new_issue_is_stamped_so_the_next_cycle_can_match_it(self):
        # `self.code` for the same reason: without the marker, every cycle files
        # a fresh issue, `count_attempts` reads zero, and the attempt cap can
        # never fire — while a comment mentioning the command reads as proof it
        # is still there.
        self.assertIn("loop.py fingerprint-marker", self.code)


class EveryIdentityIsDerivedFromTheRawLog(unittest.TestCase):
    """The workflow must hand the identity commands the RAW log, never the signal.

    `loop.py` is proven to key correctly on whatever text it is given, so the
    only place this defect can now live is the argument. Compaction keeps error
    lines and their INDENTED continuation; a Go panic puts its trace behind a
    blank line, which ends the collected block, so identifying from `signal.txt` hands a
    target's `failure_ids` a message with no frames. It returns `[]`, the cycle
    is refused as unfingerprintable, and the loop stalls on every tick — on
    exactly the runtimes the seam exists to serve.

    Pinned at the workflow because that is the seam the product crosses. Every
    Python test stays green with `signal.txt` written here, which is this
    project's recurring failure: verified at a seam the product does not use.
    """

    # command -> the file it must be given. `diagnose` takes both: the signal
    # is what the AGENT reads and is bounded on purpose.
    IDENTITY_COMMANDS = ("fingerprint-marker", "find-issue")

    def _runs(self, workflow):
        # Comments stripped: every rule below is stated in a comment beside the
        # command it governs, so a raw-text check reads its own documentation.
        return executable((WORKFLOWS / workflow).read_text(encoding="utf-8"))

    def test_no_identity_command_is_handed_the_compacted_signal(self):
        checked = 0
        for workflow in ("watch.yml", "heal.yml"):
            for line in self._runs(workflow).splitlines():
                for command in self.IDENTITY_COMMANDS:
                    if f"loop.py {command}" in line:
                        checked += 1
                        with self.subTest(workflow=workflow, command=command):
                            self.assertNotIn(
                                "signal.txt", line, f"{command} keys on compacted text: {line}"
                            )
                            self.assertIn("signal.raw", line, line)
        # Refuse to pass vacuously: with the commands renamed or removed the
        # loop above never runs, and a check that examined nothing reads
        # exactly like one that examined everything (L8).
        self.assertEqual(checked, 3, "expected find-issue once and the marker twice")

    def test_watch_is_asked_to_write_the_raw_log(self):
        # Nothing else produces it, so a `watch` invocation with no path leaves
        # every consumer below reading a file that does not exist.
        for workflow in ("watch.yml", "heal.yml"):
            with self.subTest(workflow=workflow):
                self.assertIn('loop.py watch "$RUNNER_TEMP/signal.raw"', self._runs(workflow))

    def test_the_raw_log_survives_the_whole_watch_step(self):
        """Executed, because the argument spellings cannot answer this.

        Every check in this class reads the text of a command. None of them
        sees a LATER line overwriting the file those commands name — and
        `printf '%s' "$signal" > signal.raw` is a one-character edit that
        replaces the raw log with the compacted signal while leaving every
        reader still correctly saying `signal.raw`. The suite stays green and
        the Go stall this seam exists to remove comes straight back.

        So: run the step and look at the file afterwards.
        """
        # BOTH workflows read the log; each carries its own copy of this step,
        # so testing one leaves the other free to clobber the file silently.
        for workflow, step, verdict_key in (
            ("watch.yml", "Read log + compact", "signal=true"),
            ("heal.yml", "Re-read log", "go=true"),
        ):
            with self.subTest(workflow=workflow):
                self._assert_raw_survives(workflow, step, verdict_key)

    def _assert_raw_survives(self, workflow: str, step: str, verdict_key: str) -> None:
        chunk = step_chunk(workflow, step)
        body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
        self.assertIsNotNone(body, f"{step} has no run body")
        script = re.sub(r"\$\{\{[^}]*\}\}", "7", textwrap.dedent(body.group(1)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binaries = root / "bin"
            binaries.mkdir()
            # `loop.py watch <path>` writes the uncompacted log and prints the
            # compacted signal; `fingerprint-marker` reads the file and is
            # silent. Two distinguishable strings, so the assertion can tell
            # which one ended up on disk.
            stub = (
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *watch*) eval "f=\\${$#}"; printf "RAW-UNCOMPACTED-TRACE" > "$f";'
                ' printf "COMPACTED-SIGNAL";;\n'
                "  *) : ;;\n"
                "esac\n"
            )
            path = binaries / "python"
            path.write_text(stub)
            path.chmod(0o755)
            out = root / "gh_output"
            out.write_text("")
            runner_temp = root / "runner_temp"
            runner_temp.mkdir()
            proc = subprocess.run(
                ["bash", "-e"], input=script, text=True, capture_output=True, cwd=root,
                env={"PATH": f"{binaries}:/usr/bin:/bin", "GITHUB_OUTPUT": str(out),
                     "RUNNER_TEMP": str(runner_temp)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.strip())
            raw = (runner_temp / "signal.raw").read_text()
            signal = (root / "signal.txt").read_text()
            verdict = out.read_text()

        self.assertEqual(raw, "RAW-UNCOMPACTED-TRACE", "the raw log was overwritten")
        self.assertEqual(signal, "COMPACTED-SIGNAL", "the prompt got the wrong text")
        # Without this the two file assertions could both hold on a step that
        # took the IDLE branch and never reached its own verdict.
        self.assertIn(verdict_key, verdict)

    def test_diagnose_gets_both_and_fix_gets_the_raw_log(self):
        heal = self._runs("heal.yml")
        self.assertIn('loop.py diagnose signal.txt "$RUNNER_TEMP/signal.raw"', heal)
        self.assertRegex(heal, r'loop\.py fix "\$\{\{[^}]*\}\}" "\$RUNNER_TEMP/signal\.raw"')

    def test_the_recorded_incident_is_keyed_from_the_raw_log(self):
        # The other half of the round trip. A record written from compacted text
        # carries fewer identities than the recall derives, so the repeat this
        # record exists to catch never matches it — and both halves look correct
        # read on their own.
        record = step_chunk("heal.yml", "Record incident")
        self.assertIn("raw_log=open(os.environ['RUNNER_TEMP'] + '/signal.raw')", record)
        self.assertNotIn("signal.txt", record)


class TheReviewersReasonReachesAHumanOnEitherVerdict(unittest.TestCase):
    """An APPROVED fix must not swallow the reviewer's paragraph.

    `loop_context/CLAUDE.md` tells every role to report suspicious content in
    the field it returns, and for Review that field is `reason`. Reading it
    only on the block path means a reviewer who approves a fix *while flagging
    an injected log* is heard by nobody — and the `issue_body` that carried the
    injection travels on into incident memory, to be replayed into the prompt
    of every later cycle that matches the same failure.

    The evidence bundle keeps a copy either way. That is a second copy, not the
    channel: it is a downloadable artifact nobody opens on a green cycle, while
    the PR is where a person actually looks.
    """

    STEP = "Publish review verdict"

    def _body(self, step: str) -> str:
        chunk = step_chunk("heal.yml", step)
        body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
        self.assertIsNotNone(body, f"{step} has no run body")
        # Actions substitutes `${{ }}` textually before any shell exists, so a
        # body run as-is dies on a bad substitution. Standing in one issue
        # number keeps the surrounding shell — the arithmetic, the quoting —
        # under test rather than edited away.
        return re.sub(r"\$\{\{[^}]*\}\}", "7", textwrap.dedent(body.group(1)))

    def _run(self, step: str, *, reason: str = "REVIEW SAYS THE LOG CARRIED AN INJECTION",
             gh_rc: int = 0):
        """Execute the step for real and report what `gh` was actually handed.

        Grepping cannot answer this. Every string check passes while a LATER
        line overwrites the file, posts a different one, or hands over a
        literal — which is exactly the class of mutation that survived here.
        The stubs are deliberately distinguishable: the scrubber rewrites RAW
        to SCRUBBED, so the posted body says which file it came from.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".shl").mkdir()
            (root / ".shl" / "pr_url.txt").write_text("https://example.invalid/pr/1")
            record = root / "posted.txt"
            binaries = root / "bin"
            binaries.mkdir()
            for name, script in {
                # Emulates the one jq behaviour this step turns on: `-e` exits
                # 1 when the result is null or empty. Without that the stub
                # cannot tell `jq -er` from `jq -r '.reason // ""'`, and the
                # test passes on the command that strands an approved fix.
                "jq": (
                    "#!/bin/sh\n"
                    f'out="{reason}"\n'
                    'printf "%s" "$out"\n'
                    'case "$*" in *-e*) [ -n "$out" ] || exit 1;; esac\n'
                ),
                # Two shapes reach `python` in these steps: the scrubber,
                # which names its input with `--text`, and a `-c` one-liner
                # reading the attempt count. Branching on the flag rather than
                # on position keeps the stub honest about which is which.
                "python": (
                    "#!/bin/sh\n"
                    'case "$*" in\n'
                    '  *--text*) eval "f=\\${$#}"; sed "s/RAW/SCRUBBED/g" "$f";;\n'
                    "  *) echo 0;;\n"
                    "esac\n"
                ),
                "gh": (
                    "#!/bin/sh\n"
                    'while [ $# -gt 0 ]; do\n'
                    '  case "$1" in\n'
                    f'    --body-file) cat "$2" > "{record}"; shift 2;;\n'
                    f'    --body) printf "%s" "$2" > "{record}"; shift 2;;\n'
                    "    *) shift;;\n"
                    "  esac\n"
                    "done\n"
                    f"exit {gh_rc}\n"
                ),
            }.items():
                path = binaries / name
                path.write_text(script)
                path.chmod(0o755)
            proc = subprocess.run(
                ["bash", "-e"], input=self._body(step), text=True, capture_output=True,
                cwd=root, env={"PATH": f"{binaries}:/usr/bin:/bin"},
            )
            posted = record.read_text() if record.exists() else None
            return proc, posted

    def test_what_is_posted_is_the_scrubbed_text_and_not_the_raw_file(self):
        # The assertion is on the CONTENT `gh` received, so posting the raw
        # file, or a hardcoded string, or nothing at all each fail here while
        # leaving every command in the step present and correctly ordered.
        proc, posted = self._run(self.STEP, reason="RAW SECRET FROM THE LOG")
        self.assertEqual(proc.returncode, 0, proc.stderr.strip())
        self.assertIn("SCRUBBED SECRET FROM THE LOG", posted)
        # A reader has to be able to tell the paragraph below the line was
        # written by a model whose context carried untrusted log text.
        self.assertTrue(posted.startswith("Automated review verdict"), posted)

    def test_an_empty_reason_does_not_strand_an_approved_fix(self):
        # The silent one. This step runs between Review and Merge, so a
        # non-zero exit here skips the merge, the deploy, the verification and
        # the incident record — while the handler that would record a failed
        # attempt is scoped to gate and suite outcomes and never fires. The
        # cycle stalls and the cap never advances.
        proc, posted = self._run(self.STEP, reason="")
        self.assertEqual(proc.returncode, 0, proc.stderr.strip())
        self.assertIsNone(posted, "an empty reason was posted as a comment")
        self.assertIn("::warning::", proc.stdout)

    def test_a_failed_comment_does_not_block_the_merge(self):
        # Executed, not grepped: `::warning::` being present in the text says
        # nothing about whether the step still exits 0. Under `bash -e` a
        # non-zero here skips Merge, Deploy, Verify, Rollback and Record.
        proc, _ = self._run(self.STEP, gh_rc=1)
        self.assertEqual(proc.returncode, 0, "a rejected comment failed the step")
        self.assertIn("::warning::", proc.stdout)

    def test_the_block_path_publishes_the_scrubbed_reason_too(self):
        # That step ends in `exit 1` by design, so the verdict is what it
        # posted before exiting, not its return code.
        _, posted = self._run("Block + record attempt on review fail",
                              reason="RAW BLOCK REASON")
        self.assertEqual(posted, "SCRUBBED BLOCK REASON")

    def test_it_runs_on_the_approve_path(self):
        self.assertIn("steps.rev.outputs.approved == 'true'",
                      executable(step_chunk("heal.yml", self.STEP)))

    def test_it_runs_before_the_merge(self):
        # After the merge the PR is closed, and a comment on a closed PR is
        # where review notes go to be missed.
        whole = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        self.assertLess(whole.index(f"name: {self.STEP}"), whole.index("name: Merge"))


class CheckoutStaysWhereTheGitGuardCanSeeIt(unittest.TestCase):
    """`actions/checkout` is held at v5 on purpose, and the purpose is invisible.

    v6 moved the persisted credential out of the local git config into
    `$RUNNER_TEMP`, leaving `.git/config` pointing at it through an `includeIf`.
    An included file IS git config — it can set `core.hooksPath` — so the bump
    puts a third executable-config file outside the surface the pre-Fix guard
    hashes, in a directory that guard's own comment records as writable by the
    agent. Nothing about that failure is loud.

    The usual reason to upgrade does not apply: v5 is already Node 24, and the
    floating major keeps taking security backports. So this is a decision, not
    a stale pin, and a routine bump would undo it without anyone reading why.
    Reasoning in `CLAUDE.md` under residual risks.
    """

    def test_both_workflows_still_check_out_at_v5(self):
        found = []
        for name in ("watch.yml", "heal.yml"):
            for line in executable((WORKFLOWS / name).read_text(encoding="utf-8")).splitlines():
                if "actions/checkout@" in line:
                    found.append(line.strip())
        self.assertEqual(len(found), 2, f"expected one checkout per workflow, got {found}")
        for line in found:
            self.assertIn(
                "actions/checkout@v5", line,
                "checkout was bumped; v6+ moves the credential out of .git/config, "
                "which is the surface the pre-Fix git guard hashes. Read CLAUDE.md "
                "before changing this.",
            )


class SecretDiscipline(unittest.TestCase):
    """No secret on any step that executes agent-authored code.

    Diagnose writes the reproducing test from an untrusted log, and that code
    then runs under the test runner. A provider token or a GH_TOKEN visible to
    that step is a token an injected log line can exfiltrate. This is the one
    security invariant of the workflow, and prose cannot hold it: an account
    saying each agent step holds only SHL_AUTH_TOKEN reads as correct while Fix
    deliberately holds GH_TOKEN too, and nothing catches the mismatch.
    """

    # Steps that execute code Diagnose or Fix authored.
    RUNS_AGENT_CODE = (
        "Red — write + run repro test",
        "Green — frozen repro test must now pass",
        "Run suite",
        "Gate — no weakening, frozen test + config untouched",
        "Verify — test the merged target branch",
        "Baseline — tests already failing",
    )

    def test_no_step_running_agent_authored_code_holds_a_secret(self):
        text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        # Split on the six-space step marker so each chunk is one step.
        chunks = re.split(r"^ {6}- (?=name:|uses:)", text, flags=re.MULTILINE)
        checked = []
        for chunk in chunks:
            name = re.match(r"name: (.+)", chunk)
            if not name or name.group(1).strip() not in self.RUNS_AGENT_CODE:
                continue
            checked.append(name.group(1).strip())
            self.assertNotIn(
                "secrets.",
                chunk,
                f"step {name.group(1).strip()!r} runs agent-authored code and must hold no secret",
            )
        # Guards against the step names drifting and this test silently checking nothing.
        self.assertCountEqual(checked, self.RUNS_AGENT_CODE)

    # The inverse direction. Without it the suite checks only that agent-code
    # steps hold nothing, so a NEW step quietly gaining the write credential is
    # invisible — and this list is the one place the deliberate exception is
    # recorded rather than argued in a docstring.
    MAY_HOLD_GH_TOKEN = (
        "Find or file issue",
        "Escalate if repro not red",
        # Deliberate: `loop.py fix` reads the attempt cap from issue comments
        # before it constructs the agent, and the agent subprocess then inherits
        # the whole environment. The deny set is what bounds that.
        "Fix",
        "On gate/suite fail, record attempt",
        "Commit + PR",
        "Block + record attempt on review fail",
        # Its approve-path twin. Same shape and same reason: it posts one
        # comment and runs no agent-authored code — the reviewer's paragraph
        # reaches it as data, through jq and the scrubber, never as a command.
        "Publish review verdict",
        "Merge",
        "Rollback on regression",
        "Record incident",
        "Escalate a post-merge failure",
    )

    def test_only_named_steps_hold_the_github_write_credential(self):
        self.assertCountEqual(self._holders("GH_TOKEN:"), self.MAY_HOLD_GH_TOKEN)

    # The provider credential needs the same inverse, and for a sharper reason.
    # `RUNS_AGENT_CODE` is a fixed list of six names, so it detects a rename and
    # nothing else: a NEW step that runs `eval "$TEST_CMD"` while holding
    # SHL_AUTH_TOKEN is in neither list and passes both checks above. That is
    # the workflow's one stated security invariant failing open — Diagnose
    # authors the repro test from an untrusted log, and that code runs under the
    # target's test runner, where the agent deny set does not reach.
    #
    # Only the three roles that construct an agent. The token is what pays for
    # a model call; a step making none has no use for it.
    MAY_HOLD_AUTH_TOKEN = ("Diagnose", "Fix", "Review")

    def _holders(self, key: str, workflow: str = "heal.yml") -> list:
        text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
        return [
            match.group(1).strip()
            for chunk in re.split(r"^ {6}- (?=name:|uses:)", text, flags=re.MULTILINE)
            for match in [re.match(r"name: (.+)", chunk)]
            if match and key in chunk
        ]

    def test_only_the_agent_roles_hold_the_provider_credential(self):
        self.assertCountEqual(self._holders("SHL_AUTH_TOKEN:"), self.MAY_HOLD_AUTH_TOKEN)

    def test_the_watch_runs_no_step_holding_the_provider_credential(self):
        # `watch.yml` constructs no agent at all — it decides whether to
        # dispatch one. A token there would sit in the same job as `read_log`,
        # which parses whatever the target's host returns.
        self.assertEqual(self._holders("SHL_AUTH_TOKEN:", "watch.yml"), [])


class NeitherWorkflowAsksForMoreThanItUses(unittest.TestCase):
    """A granted-and-unused scope is a standing offer to whatever runs next.

    Both files had one. `heal.yml` asked for `actions: write` and makes no
    Actions API call; `watch.yml` asked for `issues: write` and writes no issue
    — its only `gh` call is the dispatch, and the step running loop.py holds no
    GH_TOKEN at all, so nothing there could authenticate one.
    """

    def _permissions(self, workflow: str) -> set[str]:
        text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
        block = re.search(r"^permissions:\n((?:[ #].*\n)+)", text, re.M)
        self.assertIsNotNone(block, f"{workflow} declares no permissions block")
        return set(re.findall(r"^  ([\w-]+):", block.group(1), re.M))

    def test_heal_asks_for_exactly_what_its_gh_calls_need(self):
        # contents: push/merge · issues: file+comment · pull-requests: open/merge
        self.assertEqual(
            self._permissions("heal.yml"), {"contents", "issues", "pull-requests"}
        )

    def test_watch_asks_for_exactly_what_its_gh_calls_need(self):
        # contents: checkout · actions: the `gh workflow run` dispatch
        self.assertEqual(self._permissions("watch.yml"), {"contents", "actions"})


class WatchShipsDisabled(unittest.TestCase):
    """The shipped `watch.yml` must not carry a live cron.

    `SKILL.md` tells the installer to keep watch on `workflow_dispatch` until
    the operator has validated on the install branch AND read any generated
    test suite; `artifacts/setup.md` likewise says to enable the cron only
    after that passes. If the template ships with the schedule active, the
    safe state depends on the installer remembering to comment it out, and a
    live cron is indistinguishable from a disabled one until it fires.

    Failure mode: install completes, the disabling step is missed, and within
    one cron interval the loop begins filing issues and merging to the default
    branch unattended — gated by generated tests nobody has read yet, which is
    exactly what the artifact gate exists to prevent.
    """

    def test_watch_has_no_active_cron(self):
        text = (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")
        live = [
            line
            for line in text.splitlines()
            if re.match(r"\s*-\s*cron:", line)  # a commented cron starts with '#'
        ]
        self.assertEqual(
            live, [], "watch.yml ships with a live cron; it must ship disabled"
        )

    def test_watch_can_still_be_triggered_by_hand(self):
        # Disabling the cron must not leave the workflow untriggerable, which
        # would break the Phase 9 self-test.
        text = (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", text)

    def test_watch_says_how_to_enable_the_cron(self):
        # A commented-out block with no instruction reads as dead code, and the
        # operator enables it by editing this file.
        # Anchored to the commented cron block, not to the word anywhere: the
        # instruction is only useful beside the thing it tells you to edit.
        text = (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")
        head = text.split("permissions:", 1)[0]
        self.assertRegex(head, r"(?i)uncomment")
        self.assertRegex(head, r"(?m)^\s*#\s*schedule:")


class ActionlintAcceptsTheShippedWorkflows(unittest.TestCase):
    """The Actions SCHEMA, which nothing else here checks.

    Every other test in this file reads the YAML as text or as shell. None of
    them knows what GitHub Actions considers legal: a misspelled job key, an
    `if:` expression that does not parse, a `${{ }}` naming a property that
    does not exist in that context. All of those are valid YAML and valid
    bash, and all of them fail on a runner — the only place this class of
    defect surfaces at all, and each instance makes a cycle impossible.

    Skipped when the binary is absent, because the framework is stdlib-only
    and must test anywhere. A skip is honest; a silent pass would not be.
    """

    BINARY = shutil.which("actionlint")

    def _run(self, *paths: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.BINARY, "-no-color", "-oneline", *map(str, paths)],
            capture_output=True,
            text=True,
        )

    @unittest.skipUnless(BINARY, "actionlint not installed")
    def test_both_shipped_workflows_lint_clean(self):
        result = self._run(WORKFLOWS / "watch.yml", WORKFLOWS / "heal.yml")
        self.assertEqual(
            result.returncode, 0, f"actionlint findings:\n{result.stdout}"
        )

    @unittest.skipUnless(BINARY, "actionlint not installed")
    def test_actionlint_would_actually_catch_something(self):
        """A clean result only means anything if the checker can fail.

        Without this, an `actionlint` silently pointed at nothing, or invoked
        with a flag it rejects, produces the same green as a genuinely clean
        file. That is the vacuous-test failure mode (L8), so the check gets
        checked.
        """
        broken = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8").replace(
            "runs-on: ubuntu-latest", "runs-on: ubuntu-latest\n    timeout-minuts: 60", 1
        )
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "heal.yml"
            planted.write_text(broken, encoding="utf-8")
            result = self._run(planted)
        self.assertNotEqual(
            result.returncode, 0, "actionlint passed a workflow with an invalid job key"
        )
        self.assertIn("timeout-minuts", result.stdout)


class RunawayJobsAreBounded(unittest.TestCase):
    """Every job declares `timeout-minutes`.

    Nothing else bounds a single agent invocation. The attempt cap bounds
    CYCLES, not the wall-clock of one call, and this CLI has no turn cap:
    `claude --help` lists `--max-budget-usd` but no `--max-turns`. So an agent
    that stalls runs to GitHub's default job limit of six hours, three times
    over in one heal cycle, and the operator's first signal is the bill.

    GitHub's own Actions guidance names workflow-level timeouts as the way to
    avoid runaway jobs. This is that, and it is also what makes the concurrency
    group below terminate: a queued cycle behind a wedged one is only bounded
    if the wedged one is.
    """

    def test_every_job_declares_a_timeout(self):
        for name in ("watch.yml", "heal.yml"):
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text(encoding="utf-8")
                self.assertRegex(
                    text,
                    r"(?m)^\s{4}timeout-minutes:\s*\d+",
                    f"{name} has no job-level timeout-minutes; a stalled agent "
                    f"runs to GitHub's six-hour default",
                )


class CyclesCannotOverlap(unittest.TestCase):
    """One heal cycle at a time, per branch, across BOTH workflows.

    `watch.yml` runs on a cron and dispatches `heal.yml`. A heal cycle is three
    agent calls plus a suite plus a deploy, and nothing else stops the next
    cron tick from starting a second one on top of it: the log still shows the
    unfixed failure, so watch dispatches again. Two cycles then race on the
    same issue and the same repro path, both reading the attempt cap from issue
    comments, both opening PRs, both merging to the same branch.

    The group spans both workflows deliberately — a watch that runs while a
    heal is mid-cycle is not merely wasteful, it is what CREATES the second
    cycle. Keyed by ref because the cycle is branch-scoped (TARGET_BRANCH is
    `github.ref_name`), so a self-test on the install branch is not blocked by
    production and vice versa.

    `cancel-in-progress` must be FALSE. Cancelling mid-cycle is worse than
    queueing: it can kill a heal between merge and deploy, or between
    `git revert` and its push — the exact blast radius the rollback guards
    elsewhere in this file exist to prevent.
    """

    GROUP = "self-healing-loop-${{ github.ref_name }}"

    def _concurrency_block(self, name: str) -> str:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        match = re.search(
            r"(?m)^concurrency:\n((?:[ \t]+.*\n)+)", text
        )
        self.assertIsNotNone(
            match, f"{name} declares no top-level concurrency group"
        )
        return match.group(1)

    def test_both_workflows_share_one_group_keyed_by_ref(self):
        for name in ("watch.yml", "heal.yml"):
            with self.subTest(workflow=name):
                self.assertIn(f"group: {self.GROUP}", self._concurrency_block(name))

    def test_a_running_cycle_is_never_cancelled(self):
        for name in ("watch.yml", "heal.yml"):
            with self.subTest(workflow=name):
                self.assertRegex(
                    self._concurrency_block(name),
                    r"cancel-in-progress:\s*false",
                    f"{name} may cancel a cycle mid-flight; a heal killed "
                    f"between revert and push leaves the bad fix live",
                )


class ShellSemanticsOnARunner(unittest.TestCase):
    """Defects that only appear when the YAML's shell actually runs.

    Every one of these is invisible to the rest of the suite, because the tests
    and the local harness invoke the same code a different way than the shipped
    workflow does — the L9 shape, three drivers deep.
    """

    def heal(self) -> str:
        return (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")

    def test_cli_is_never_invoked_as_a_script_path(self):
        """`python cli.py` puts guardrails/ on sys.path, not the loop root.

        `guardrails/cli.py` does `from guardrails.confidentiality_filter import
        scrub`. Invoked as a script, its own directory becomes sys.path[0], so
        the `guardrails` package sits one level above where Python looks and
        every call dies with ModuleNotFoundError — the first at step 5, every
        cycle, forever. Must run as `-m guardrails.cli` with PYTHONPATH set to
        the vendored root.
        """
        offenders = [
            line.strip()
            for line in executable(self.heal()).splitlines()
            if re.search(r"python[3]?\s+(?:-\S+\s+)*\S*guardrails/cli\.py", line)
        ]
        self.assertEqual(offenders, [], "cli.py invoked as a script path")

    def test_every_cli_call_sets_pythonpath(self):
        calls = [ln for ln in executable(self.heal()).splitlines() if "guardrails.cli" in ln]
        self.assertTrue(calls, "no cli invocations found; this test would be vacuous")
        for line in calls:
            with self.subTest(line=line.strip()[:60]):
                self.assertIn("PYTHONPATH=", line)

    def test_red_and_green_run_the_repro_test_the_same_way(self):
        """Red and Green must resolve SHL_TEST_ONE identically.

        Red running from `.shl` with `../$path` while Green runs
        from the repo root with `$path` is a different cwd, so a different
        sys.path and a different config root: Red fails on imports whatever the
        bug did, `red=true` is unconditional, and `Escalate if repro not red` —
        the only check that catches a bad repro spec — can never fire.
        """
        subs = re.findall(r'cmd="([^"]+)"', self.heal())
        self.assertEqual(len(subs), 2, f"expected Red and Green substitutions, got {subs}")
        self.assertEqual(subs[0], subs[1], f"Red and Green disagree: {subs}")

    def test_red_and_green_run_it_from_the_same_directory(self):
        """The command string is half of "the same way"; cwd is the other half.

        Comparing only the `cmd=` substitutions passes when one of the two
        steps gains a `working-directory`, and cwd is what the docstring above
        is actually about — sys.path and the test runner's config root both
        resolve from it.

        Green is the fail-open direction. Run from `.shl`, its
        `[ -f .shl/frozen_path.txt ]` is false, so it writes `rc=none`; the Gate
        then omits `--repro-rc` entirely while still passing `--frozen`, and
        prints a pass naming the checks it rested on with "is the bug fixed"
        silently absent from that list. Every cycle would merge on "nothing
        regressed" alone, with nothing erroring and nothing in the evidence
        bundle looking wrong.
        """
        steps = {s["name"]: s for s in job_steps("heal.yml")}
        red = next(s for n, s in steps.items() if n.startswith("Red"))
        green = next(s for n, s in steps.items() if n.startswith("Green"))
        self.assertEqual(
            red["workdir"], green["workdir"],
            f"Red runs from {red['workdir'] or 'the repo root'!r} and Green from "
            f"{green['workdir'] or 'the repo root'!r}, so the frozen test resolves differently",
        )

    def test_the_gate_is_told_the_repro_result_whenever_a_frozen_test_exists(self):
        # `--frozen` without `--repro-rc` is the shape that lets the pass line
        # attest to a check that never ran. The two flags are appended under
        # conditions that must stay able to agree.
        body = next(s for s in job_steps("heal.yml") if s["name"].startswith("Gate"))["body"]
        self.assertIn('args+=(--frozen "$frozen")', body)
        self.assertRegex(
            body,
            r'\[ "\$\{\{ steps\.green\.outputs\.rc \}\}" != "none" \] && '
            r'args\+=\(--repro-rc "\$\{\{ steps\.green\.outputs\.rc \}\}"\)',
            "the gate is not handed the frozen test's own exit code, so it can "
            "pass without ever answering whether the bug is fixed",
        )

    def test_diagnose_can_see_the_repro_path_pattern(self):
        """Without the var, Diagnose is told the pytest default path, which on
        a JS target is a strong steer to emit the wrong language entirely."""
        # Comment lines stripped first: the header documents SHL_REPRO_PATH in
        # prose, which would satisfy this check while the env block stayed empty.
        head = self.heal().split("steps:")[0]
        executable = "\n".join(
            ln for ln in head.splitlines() if not ln.strip().startswith("#")
        )
        self.assertIn(
            "SHL_REPRO_PATH", executable, "must be job-level so Diagnose sees it too"
        )

    def test_rollback_can_revert_a_merge_commit(self):
        """`gh pr merge --merge` always makes a merge commit, and a plain
        revert refuses one. The step then dies before the push: the bad fix
        stays live, the URGENT escalation never fires, and Record incident is
        skipped, losing the `reverted` memory that stops the loop re-shipping
        the same fix."""
        text = self.heal()
        self.assertIn("git revert", text)
        self.assertRegex(text, r"git revert[^\n]*-m 1")

    def test_no_scratch_file_is_written_to_the_repo_root(self):
        """The gate stages everything from the repo root.

        `.shl/.gitignore` only governs paths inside that
        directory, so scratch written beside it is staged, committed and
        merged. `suite_raw.txt` is raw suite output — scrubbed on its way into
        the evidence bundle precisely because it can carry a key from a fixture
        repr, then committed unscrubbed in the same cycle.
        """
        bad = []
        for step in job_steps("heal.yml"):
            if step["workdir"] == ".shl":
                continue  # a bare name here is already inside the ignored dir
            for line in step["body"].splitlines():
                for m in re.finditer(
                    r">\s*([A-Za-z0-9_.-]+\.(?:txt|raw|json|diff))\b", line
                ):
                    bad.append(f"{step['name']}: {m.group(1)}")
        self.assertEqual(bad, [], "scratch at the repo root gets staged by the gate")

    def test_post_fix_steps_are_guarded_on_go_not_only_on_escalated(self):
        """`escalated != 'true'` is TRUE when the run was idle.

        On an idle heal dispatch the early steps skip, `escalated` is unset,
        and Green/Run suite/Gate/Commit run anyway — executing the whole suite
        for nothing, then failing with no title file to commit with.
        """
        chunks = re.split(r"^ {6}- (?=name:)", self.heal(), flags=re.MULTILINE)
        checked = 0
        for chunk in chunks:
            name = re.match(r"name: (.+)", chunk)
            cond = re.search(r"^ {8}if: (.+)$", chunk, flags=re.MULTILINE)
            if not name or not cond or "escalated != 'true'" not in cond.group(1):
                continue
            checked += 1
            with self.subTest(step=name.group(1).strip()):
                self.assertIn(
                    "steps.w.outputs.go == 'true'",
                    cond.group(1),
                    "guarded only on escalated, so it also runs on an idle cycle",
                )
        self.assertTrue(checked, "no escalated-guarded steps found; test is vacuous")

    def test_gate_args_are_not_word_split(self):
        """An unquoted `--test-globs *__tests__/*` is glob-expanded by the
        shell, silently rewriting the very convention the gate was told to
        police — the fail-open case the glob defaults exist to close.

        Asserted on the expansion FORM, not on one spelling of the mistake.
        `assertNotIn("gate $args")` was satisfied by `gate ${args[@]}`, which is
        the same defect written the way someone actually writes it when the
        variable is already an array.
        """
        text = executable(self.heal())
        expansions = re.findall(r"guardrails\.cli gate (\S+)", text)
        self.assertTrue(expansions, "no gate invocation found to check")
        for expansion in expansions:
            with self.subTest(expansion=expansion):
                self.assertEqual(
                    expansion,
                    '"${args[@]}"',
                    "the gate's arguments must be passed as a quoted array "
                    "expansion; anything else lets the shell glob-expand a "
                    "pattern the gate was told to match literally",
                )

    def test_the_expansion_survives_a_glob_bearing_value(self):
        # Behavioural, not textual: run the real expansion form against a value
        # containing a glob, in a directory where that glob would match, and
        # confirm the argument arrives intact.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "__tests__").mkdir()
            (Path(tmp) / "__tests__" / "a.ts").write_text("x", encoding="utf-8")
            script = (
                'args=(--test-globs "*__tests__/*")\n'
                'printf "%s\\n" "${args[@]}"\n'
            )
            proc = subprocess.run(
                ["bash", "-e"], input=script, text=True, capture_output=True, cwd=tmp
            )
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "*__tests__/*")


class TheCycleStaysOnTheBranchItWasDispatchedOn(unittest.TestCase):
    """Every branch operation keys off the dispatched ref, not the repo default.

    Scheduled runs always fire on the default branch, so in production the
    dispatched ref IS the default branch. What this buys is the Phase 9
    self-test: dispatched with `--ref self-healing-loop-install`, the whole
    cycle — checkout, PR base, merge, verify, incident record — happens on that
    branch and the default branch is never touched.

    Off the dispatched ref there is nowhere else to key from: `gh workflow run`
    with no ref runs the workflow from the default branch and checks the default
    branch out, so a bug seeded on a side branch is invisible; and `gh pr create`
    with no `--base` opens and merges into the default branch, which is the
    branch the self-test checklist says to stay off.
    """

    def test_heal_derives_the_working_branch_from_the_dispatched_ref(self):
        env = executable((WORKFLOWS / "heal.yml").read_text(encoding="utf-8")).split("steps:")[0]
        target = [ln for ln in env.splitlines() if "TARGET_BRANCH:" in ln]
        self.assertEqual(len(target), 1, "TARGET_BRANCH must be defined exactly once")
        self.assertIn("github.ref_name", target[0])
        self.assertNotIn("default_branch", target[0])

    def test_the_default_branch_is_read_for_comparison_and_nothing_else(self):
        # The job may know which branch is the default — the health probe has to
        # ask, since its URL describes one deployment. What it must never do is
        # ACT on it: a checkout, PR base, merge or revert keyed off the default
        # branch silently moves the self-test onto the branch it exists to
        # avoid.
        # Reading it is fine — the health probe has to know, and an emptiness
        # guard has to check. ACTING on it is what moves the cycle off the
        # branch it was dispatched on, so the ban is on the verbs.
        acts_on_a_branch = ("git checkout", "git merge", "git push", "git revert",
                            "gh pr create", "gh pr merge", "gh workflow run")
        # Continuations joined first: `git checkout \\` on one line and
        # `"$DEFAULT_BRANCH"` on the next is one command, and a per-line scan
        # sees neither half as a violation.
        text = executable((WORKFLOWS / "heal.yml").read_text(encoding="utf-8"))
        joined = re.sub(r"\\\n\s*", " ", text)
        for line in joined.splitlines():
            if "DEFAULT_BRANCH" not in line or "DEFAULT_BRANCH:" in line:
                continue
            for verb in acts_on_a_branch:
                self.assertNotIn(
                    verb, line,
                    f"DEFAULT_BRANCH drives a branch operation: {line.strip()}",
                )

    def test_pr_is_opened_against_that_branch(self):
        text = executable((WORKFLOWS / "heal.yml").read_text(encoding="utf-8"))
        create = [ln for ln in text.splitlines() if "gh pr create" in ln]
        self.assertTrue(create, "no `gh pr create`; test would be vacuous")
        for line in create:
            with self.subTest(line=line.strip()[:60]):
                self.assertIn("--base", line)

    def test_watch_dispatches_heal_on_the_same_ref(self):
        text = executable((WORKFLOWS / "watch.yml").read_text(encoding="utf-8"))
        dispatch = [ln for ln in text.splitlines() if "gh workflow run" in ln]
        self.assertTrue(dispatch, "no dispatch line; test would be vacuous")
        for line in dispatch:
            with self.subTest(line=line.strip()[:60]):
                self.assertIn("--ref", line)


class SecretDisciplineCoversBothWorkflows(unittest.TestCase):
    """watch.yml holds the token too, so the invariant has to be asserted here.

    `loop.py` states that watch runs no agent and needs no provider token, and
    withholding it is what lets a suite-as-signal `read_log` — which executes
    already-merged, agent-authored repro tests — run without the API key in
    its environment.
    """

    def test_watch_read_log_step_holds_no_provider_token(self):
        text = (WORKFLOWS / "watch.yml").read_text(encoding="utf-8")
        chunks = re.split(r"^ {6}- (?=name:)", text, flags=re.MULTILINE)
        checked = False
        for chunk in chunks:
            name = re.match(r"name: (.+)", chunk)
            if not name or "Read log" not in name.group(1):
                continue
            checked = True
            self.assertNotIn("SHL_AUTH_TOKEN", executable(chunk))
        self.assertTrue(checked, "no 'Read log' step found; test would be vacuous")




class AHealthProbeOnlyRunsWhereItsUrlPoints(unittest.TestCase):
    """`SHL_HEALTH_URL` names one deployment, but a cycle runs on any ref.

    The verification procedure tells operators to dispatch on a branch, and
    every step of that cycle is scoped to it. The health probe is the one thing
    that is not: it reads a fixed URL, which is the DEFAULT branch's deployment.
    On a branch cycle the probe therefore reports on code the cycle never
    touched, the commit it names differs from the one just merged, and a correct
    fix is reverted as a post-deploy regression.

    Platforms that put preview deployments behind authentication make it worse
    rather than better: the branch URL answers a redirect the probe cannot read,
    so pointing the variable at the branch is not the fix either. Skip the probe
    off the default branch, and say so rather than reporting a silent `skip`
    that reads identically to an adapter with no `health_check`.
    """

    def setUp(self):
        self.chunk = executable(step_chunk("heal.yml", "Verify — test the merged target branch"))

    def test_the_job_knows_which_branch_is_the_default(self):
        text = executable((WORKFLOWS / "heal.yml").read_text(encoding="utf-8"))
        self.assertIn("DEFAULT_BRANCH:", text)
        self.assertIn("github.event.repository.default_branch", text)

    def test_the_probe_is_conditional_on_running_there(self):
        self.assertIn("DEFAULT_BRANCH", self.chunk)
        self.assertRegex(
            self.chunk,
            r'\$TARGET_BRANCH"?\s*!=\s*"?\$DEFAULT_BRANCH',
            "the health probe runs on any ref, so a branch cycle reverts a correct fix",
        )

    def test_a_skipped_probe_names_the_reason(self):
        # A bare `skip` is what an adapter without health_check also reports.
        self.assertIn("not the default branch", self.chunk)

    def test_the_suite_still_runs_on_every_ref(self):
        # Only the deployment probe is ref-scoped. Testing the merged code is
        # valid on any branch and must not be skipped with it.
        self.assertIn("SHL_TEST_CMD", self.chunk)


class EveryAdapterStepSeesTheRepoVariables(unittest.TestCase):
    """A step that loads the adapter must carry the whole `vars` context.

    An adapter reads whatever `SHL_*` names its target needs, and the templates
    can only name the ones the framework knows about. A variable the template
    never names is set correctly in the repo and absent at runtime, so the
    adapter reads None: `read_log` raises every tick, or `health_check` verifies
    nothing and says so in a way that reads like an adapter which simply has no
    health check.

    Hand-adding the missing `env:` lines is what re-vendoring silently undoes.
    Passing the context whole means there is no per-variable wiring for a fresh
    copy of the template to drop, and this check is what stops a step added
    later from reintroducing the gap.
    """

    ADAPTER_MARKERS = ("load_adapter", "loop.py watch")

    def _job_env(self, workflow: str) -> str:
        return executable((WORKFLOWS / workflow).read_text(encoding="utf-8")).split("steps:")[0]

    def test_every_step_touching_the_adapter_carries_shl_vars(self):
        checked = 0
        for workflow in ("heal.yml", "watch.yml"):
            job_env = self._job_env(workflow)
            for name in step_names(workflow):
                if name.startswith(("actions/", "./")):
                    continue
                chunk = executable(step_chunk(workflow, name))
                if not any(m in chunk for m in self.ADAPTER_MARKERS):
                    continue
                checked += 1
                self.assertTrue(
                    "SHL_VARS" in chunk or "SHL_VARS" in job_env,
                    f"{workflow} step {name!r} loads the adapter without SHL_VARS, "
                    "so any variable the template does not name is invisible to it",
                )
        self.assertGreaterEqual(checked, 3, "no adapter steps found; the check is vacuous")

    def test_the_blob_is_the_whole_vars_context(self):
        for workflow in ("heal.yml", "watch.yml"):
            text = executable((WORKFLOWS / workflow).read_text(encoding="utf-8"))
            self.assertRegex(
                text, r"SHL_VARS:\s*\$\{\{\s*toJSON\(vars\)\s*\}\}",
                f"{workflow} must pass the whole vars context, not a hand-picked subset",
            )


CHECKOUT_RE = "git checkout[^\n]*\n"


class EmbeddedPythonMustActuallyRun(unittest.TestCase):
    """A `python -B -c "..."` body inside a `run:` block has TWO indent layers.

    YAML strips the block's own indent; whatever remains is Python's. So a
    snippet indented to line up with the surrounding shell arrives with leading
    whitespace and dies on IndentationError before importing anything. `bash -n`
    cannot see it, because to the shell the snippet is a quoted string, and
    actionlint does not parse embedded languages either.

    The failure hides on the path nobody rehearses: the branch-scoped
    verification procedure takes a different branch of the same `if`, so only a
    default-branch cycle reaches it — and there a crashed Verify makes both
    Rollback and Record unreachable, since each is gated on `success()`. Merged,
    deployed, unverified, unrecorded.
    """

    # The required newline after the opening quote used to exclude every
    # SINGLE-LINE `python -B -c "..."` — three of them, one on the post-merge
    # path where a crash is unrecoverable. Those cannot carry the block-indent
    # defect this exists for, but `assertGreaterEqual(found, 3)` read as "all of
    # them" while it was half.
    SNIPPET_RE = re.compile(r'python -B -c "(.*?)(?<!\\)"', re.S)

    def test_every_embedded_snippet_compiles(self):
        found = 0
        for workflow in ("heal.yml", "watch.yml"):
            text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
            for chunk in re.split(r"^ {6}- (?=name:|uses:)", text, flags=re.MULTILINE)[1:]:
                name = re.match(r"name: (.+)", chunk)
                body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
                if not name or not body:
                    continue
                for match in self.SNIPPET_RE.finditer(textwrap.dedent(body.group(1))):
                    found += 1
                    with self.subTest(workflow=workflow, step=name.group(1).strip()):
                        try:
                            # Two things the runner does before python sees
                            # the argument, and both change whether it parses:
                            # the shell joins backslash-continuations, and
                            # Actions substitutes every `${{ }}` textually. A
                            # snippet is only compilable in its SUBSTITUTED
                            # form, so `1` stands in for whatever the
                            # expression yields — valid both bare and quoted.
                            source = re.sub(r"\\\n\s*", " ", match.group(1))
                            compile(
                                re.sub(r"\$\{\{.*?\}\}", "1", source),
                                "<embedded>", "exec",
                            )
                        except SyntaxError as exc:
                            self.fail(
                                f"embedded python does not compile: {exc.msg} "
                                f"(line {exc.lineno}). Dedent it to column 0, as the "
                                f"other snippets in this file are."
                            )
        # The exact count, not a floor: a snippet dropping out of the match is
        # the failure mode, and a floor cannot see it.
        self.assertEqual(
            found, 8,
            "a python -B -c snippet stopped matching; the check is silently narrower",
        )


class AFailedHealthProbeStillReachesRollback(unittest.TestCase):
    """`down` must become `regression=true`, or rollback can never fire.

    The chain is down -> bad=1 -> regression=true -> `Rollback on regression`.
    Nothing pinned it, so an edit changing the consequence rather than the
    condition would leave the suite green with auto-rollback disabled.

    Executed rather than grepped: the step is shell, and the question is what it
    writes to GITHUB_OUTPUT for each health value.
    """

    PROBE_RE = re.compile(r'health="\$\(cd .*?\)"', re.S)

    def _verify_body(self) -> str:
        chunk = step_chunk("heal.yml", "Verify — test the merged target branch")
        body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
        self.assertIsNotNone(body, "Verify step has no run body")
        return textwrap.dedent(body.group(1))

    def _stubbed(self, health: str | None = None) -> str:
        body = re.sub(CHECKOUT_RE, "", self._verify_body(), count=1)
        if health is not None:
            body = self.PROBE_RE.sub(f'health="{health}"', body)
        return body

    def _run(self, health, suite_ok, target, default) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            out.write_text("")
            proc = subprocess.run(
                ["bash", "-e"], input=self._stubbed(health), text=True,
                capture_output=True,
                # The suite command arrives through `env:`, so the stub supplies
                # it the same way the runner does rather than by substituting
                # the expression out of the body. That keeps the empty-value
                # guard in the body under test instead of editing it away.
                env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out),
                     "TARGET_BRANCH": target, "DEFAULT_BRANCH": default,
                     "TEST_CMD": "true" if suite_ok else "false"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.strip())
            return out.read_text()

    def test_run_suite_refuses_rather_than_reporting_a_suite_that_never_ran(self):
        """The quieter half, and the more dangerous one.

        Interpolated directly, an empty command left `> .shl/suite_raw.txt 2>&1`
        — a bare redirect. That is valid bash: rc 0, a zero-byte log, and the
        gate's strict arm reads `[ "0" = "0" ]` and approves the fix. Unlike the
        Verify body it does not crash, so nothing anywhere reports a problem;
        the cycle merges on the authority of a suite that was never executed.
        """
        chunk = step_chunk("heal.yml", "Run suite")
        body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
        self.assertIsNotNone(body, "Run suite has no run body")
        script = textwrap.dedent(body.group(1))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            out.write_text("")
            proc = subprocess.run(
                ["bash", "-e"], input=script, text=True, capture_output=True, cwd=tmp,
                env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out),
                     "SHL_CYCLE_ID": "1", "TEST_CMD": ""},
            )
            written = out.read_text()
        self.assertNotEqual(proc.returncode, 0, "an unset test command must fail the step")
        self.assertIn("SHL_TEST_CMD", proc.stdout + proc.stderr)
        self.assertNotIn("rc=0", written, "reported a passing suite without running one")

    def test_an_unset_test_command_refuses_instead_of_verifying_nothing(self):
        """Empty, this body used to be a parse error that took Rollback with it.

        Both are gated on this step succeeding, so the cycle merged, deployed,
        and recorded nothing — the same end state as a crashed Review, from a
        single unset variable.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh_output"
            out.write_text("")
            proc = subprocess.run(
                ["bash", "-e"], input=self._stubbed("ok"), text=True, capture_output=True,
                env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out),
                     "TARGET_BRANCH": "main", "DEFAULT_BRANCH": "main", "TEST_CMD": ""},
            )
            written = out.read_text()
        self.assertNotEqual(proc.returncode, 0, "an unset test command must fail the step")
        self.assertIn("SHL_TEST_CMD", proc.stdout + proc.stderr)
        self.assertNotIn("regression=", written)

    def test_a_down_probe_on_the_default_branch_triggers_rollback(self):
        self.assertIn("regression=true", self._run("down", True, "main", "main"))

    def test_a_healthy_probe_with_a_green_suite_does_not(self):
        self.assertIn("regression=false", self._run("ok", True, "main", "main"))

    def test_a_red_suite_triggers_rollback_whatever_the_probe_says(self):
        self.assertIn("regression=true", self._run("ok", False, "main", "main"))

    def test_a_branch_cycle_skips_the_probe_and_still_judges_the_suite(self):
        self.assertIn("regression=false", self._run("ok", True, "feature", "main"))
        self.assertIn("regression=true", self._run("ok", False, "feature", "main"))

    def test_rollback_reverts_the_merge_commit_and_never_head(self):
        """`-m 1` does not refuse a non-merge commit, so HEAD is not a safe target.

        Every commit has a parent 1, so `git revert -m 1 <anything>` succeeds.
        The deploy command may commit — a version bump, a lockfile, a changelog
        — and Verify pulls after Deploy, so HEAD can be a different commit by
        the time a rollback runs. The revert then succeeds against the wrong
        one, pushes, comments "reverted", and records outcome=reverted while the
        bad fix is still live. That is worse than the abort it replaced, which
        at least failed where someone could see it.
        """
        rollback = step_chunk("heal.yml", "Rollback on regression")
        self.assertNotRegex(
            rollback,
            r"git revert[^\n]*\bHEAD\b",
            "rollback targets HEAD, which is not necessarily the merge commit",
        )
        self.assertRegex(rollback, r"git revert -m 1 --no-edit \"\$MERGE_SHA\"")
        merge = step_chunk("heal.yml", "Merge")
        self.assertIn("mergeCommit", merge, "Merge never records which commit it produced")
        self.assertIn('echo "sha=$sha"', merge)

    def test_a_rollback_with_no_known_merge_commit_escalates_instead(self):
        # Reverting "something" is worse than reverting nothing: it reports
        # success and leaves the bad fix live.
        rollback = step_chunk("heal.yml", "Rollback on regression")
        # Positions, not a `then(.*?)fi` capture: the escalation message itself
        # contains the word "fix", so a non-greedy match to `fi` stops inside it
        # and reports the guard as empty.
        start = rollback.find('if [ -z "$MERGE_SHA" ]')
        revert = rollback.find("git revert")
        self.assertNotEqual(start, -1, "no guard for an unknown merge commit")
        self.assertNotEqual(revert, -1, "no revert found")
        self.assertLess(start, revert, "the guard must precede the revert")
        guard = rollback[start:revert]
        self.assertIn("URGENT", guard)
        self.assertIn("exit 1", guard)

    def test_deploy_can_receive_a_credential(self):
        """`secrets` is a separate context and does not travel through vars.

        `toJSON(vars)` carries no secret, and a `${{ secrets.X }}` written
        inside a variable's VALUE is not re-evaluated. So without an explicit
        env block a deploy command referencing a token receives the empty
        string — while the rollback step re-runs that same command in a step
        that does hold a token, giving one command two different privileges.
        """
        deploy = step_chunk("heal.yml", "Deploy")
        self.assertIn("secrets.SHL_DEPLOY_TOKEN", deploy)
        rollback = step_chunk("heal.yml", "Rollback on regression")
        self.assertIn(
            "secrets.SHL_DEPLOY_TOKEN", rollback,
            "the rollback re-runs the deploy command and must give it the same credential",
        )

    def test_a_crash_after_the_merge_reaches_a_human(self):
        """Everything after Merge is success()-gated, so one crash skips them all.

        Deploy, Verify, Rollback and Record are each `success()`; the only other
        failure handler is scoped to the gate and the suite, both pre-merge. A
        crashed Review has already put a real cycle in this window: merged,
        unverified, unrecorded, and nothing on the issue — and because no
        attempt is recorded, the cap never advances either.
        """
        text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        # Keyed on "a handler that asks about the merge", not on WHICH merge
        # fact it asks about. Pinning `steps.merge.outcome` here encoded the
        # defect: that field answers "did the step succeed", and the step exits
        # 1 on a merge that happened. Which fact it must read is pinned by
        # `TheMergeEscalationAsksWhetherTheMergeHAPPENED`; this test owns the
        # weaker claim that some handler exists at all.
        handlers = re.findall(r"if: always\(\)[^\n]*steps\.merge\.[^\n]*", text)
        self.assertTrue(handlers, "no handler fires when a step after Merge fails")
        self.assertIn("job.status == 'failure'", handlers[0])
        chunk = step_chunk("heal.yml", "Escalate a post-merge failure")
        self.assertIn("URGENT", chunk)
        self.assertIn("gh issue comment", chunk)

    def test_an_empty_default_branch_refuses_rather_than_skipping(self):
        proc = subprocess.run(
            ["bash", "-e"], input=self._stubbed("ok"), text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": "/dev/null",
                 "TARGET_BRANCH": "main", "DEFAULT_BRANCH": "", "TEST_CMD": "true"},
        )
        self.assertNotEqual(proc.returncode, 0, "an empty comparand skipped the probe silently")



class TheRedStepRefusesATestOneWithNoPlaceholder(unittest.TestCase):
    """Without `{}` in `SHL_TEST_ONE`, Red and Green run the WHOLE suite.

    Fail-closed, and unreadable: on any repo carrying a pre-existing failure
    Green's exit code is non-zero forever, so the gate blocks every cycle and
    the symptom reads as "the loop is broken" rather than "one variable is
    missing two characters". The guard is executed here, not grepped, because
    it is a bash `case` and the question is what it does with each value.
    """

    GUARD_RE = re.compile(r'case "\$TEST_ONE" in\n.*?esac', re.S)

    def _guard(self) -> str:
        chunk = step_chunk("heal.yml", "Red — write + run repro test")
        body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
        self.assertIsNotNone(body, "Red step has no run body")
        guard = self.GUARD_RE.search(textwrap.dedent(body.group(1)))
        self.assertIsNotNone(guard, "the Red step no longer validates SHL_TEST_ONE")
        return guard.group(0)

    def _run(self, test_one: str) -> int:
        return subprocess.run(
            ["bash", "-e"], input=self._guard(), text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "TEST_ONE": test_one},
        ).returncode

    def test_a_command_without_the_placeholder_is_refused(self):
        self.assertNotEqual(self._run("npx vitest run"), 0)

    def test_a_command_with_the_placeholder_proceeds(self):
        self.assertEqual(self._run("npx vitest run {}"), 0)


class ScrubbedValuesComeFromAFileNotAProcessSubstitution(unittest.TestCase):
    """`<(jq ...)`'s exit status is not the pipeline's, so `set -e` never sees it.

    A missing key then yields the literal string `null`, which becomes the issue
    body, the PR title and the PR review comment — the agent's whole account of
    what it did, replaced by four characters, with the step reporting success.
    """

    def test_no_scrub_reads_a_process_substitution(self):
        text = executable((WORKFLOWS / "heal.yml").read_text(encoding="utf-8"))
        offenders = [ln.strip() for ln in text.splitlines() if "scrub --text <(" in ln]
        self.assertEqual(offenders, [], "a scrub reads a process substitution")

    def test_every_jq_feeding_a_scrub_fails_on_a_missing_key(self):
        # `-e` is what turns a missing key into a non-zero exit instead of the
        # string "null". Only the extractions that feed a scrub are in scope.
        text = executable((WORKFLOWS / "heal.yml").read_text(encoding="utf-8"))
        scrubbed = {
            m.group(1)
            for m in re.finditer(r"scrub --text (\S+\.raw)", text)
        }
        self.assertTrue(scrubbed, "no scrub reads a .raw file; this check is vacuous")
        for target in sorted(scrubbed):
            with self.subTest(target=target):
                writer = re.search(rf"^.*> {re.escape(target)}\b.*$", text, re.M)
                self.assertIsNotNone(writer, f"nothing writes {target}")
                self.assertRegex(
                    writer.group(0).strip(), r"^jq -er ",
                    f"{target} is written by a jq that tolerates a missing key",
                )


class TheWatchRefusesBeforeItAnnouncesASignal(unittest.TestCase):
    """The refusal has to run before the output that triggers the dispatch.

    Written the other way round it is safe only through GitHub's implicit
    `success()` on a step whose `if:` names no status function — a default that
    is invisible in a file spelling `success() &&` on twelve conditions.
    """

    def _body(self) -> str:
        chunk = step_chunk("watch.yml", "Read log + compact")
        body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
        self.assertIsNotNone(body, "the watch step has no run body")
        return textwrap.dedent(body.group(1))

    def test_the_fingerprint_refusal_precedes_the_signal_output(self):
        body = self._body()
        refusal = body.find("loop.py fingerprint-marker")
        announce = body.find('signal=true')
        self.assertNotEqual(refusal, -1, "the watch no longer refuses an unreadable failure")
        self.assertNotEqual(announce, -1, "the watch no longer announces a signal")
        self.assertLess(
            refusal, announce,
            "signal=true is written before the refusal that is supposed to stop the dispatch",
        )

    def test_the_dispatch_states_its_success_requirement(self):
        chunk = step_chunk("watch.yml", "Dispatch heal")
        self.assertIn("success() && steps.watch.outputs.signal == 'true'", chunk)

    def test_the_refusal_is_allowed_to_fail_the_step(self):
        """Order is not refusal: `|| true` keeps the position and drops the veto.

        The step above proves the marker call comes first. It stays first with
        its status discarded, and then the watch dispatches on a stack whose
        frames it cannot fingerprint — so issue dedup, incident recall and the
        attempt cap are all keyed on nothing. The same failure is re-diagnosed
        every tick, a fresh issue each time, `count_attempts` reads zero
        forever, and the cap never fires: three agent calls per tick,
        indefinitely, on exactly the Go/Ruby/Rust/JVM targets this step exists
        to protect.
        """
        line = next(
            ln for ln in self._body().splitlines()
            if "loop.py fingerprint-marker" in ln and not ln.strip().startswith("#")
        )
        for swallow in ("|| true", "|| :", "; true"):
            self.assertNotIn(
                swallow, line,
                f"the fingerprint refusal cannot stop the dispatch: {line.strip()}",
            )


class EveryCycleStaysOnTheRefItWasDispatchedOn(unittest.TestCase):
    """Branch scoping is what makes a self-test safe to run at all.

    `artifacts/setup.md` tells the operator to verify an install by dispatching
    against a side branch. That promise rests entirely on two arguments naming
    the cycle's own ref rather than a literal: `gh workflow run --ref` in the
    watch, and `gh pr create --base` in heal. Replace either with `main` and a
    run the operator believes is branch-confined merges a bot fix into the
    default branch of their repo.

    `test_the_default_branch_is_read_for_comparison_and_nothing_else` does not
    cover this: it bans the DEFAULT_BRANCH variable from branch verbs, and a
    hardcoded literal names no variable.
    """

    def test_the_pr_targets_the_branch_the_cycle_is_running_on(self):
        body = steps_body("Commit + PR")
        self.assertRegex(
            body, r'gh pr create --base "\$TARGET_BRANCH"',
            "the PR base is not the cycle's own branch",
        )

    def test_the_dispatch_propagates_the_watchs_ref(self):
        self.assertRegex(
            steps_body("Dispatch heal", "watch.yml"),
            r'gh workflow run heal\.yml --ref "\$\{\{ github\.ref_name \}\}"',
            "the watch dispatches heal against a fixed ref, so a branch self-test "
            "silently heals the default branch instead",
        )

    def test_the_fix_reaches_the_branch_only_through_a_pull_request(self):
        # A direct push to TARGET_BRANCH makes Review a commentary on already
        # merged code and leaves Merge operating on a PR that does not exist —
        # while every ordering test still passes, because they compare step
        # NAMES. The bot branch is the only thing heal may push a fix to.
        pushes = [
            ln.strip() for ln in executable(steps_body("Commit + PR")).splitlines()
            if re.search(r"(?<![-\w])git\s+push\b", ln)
        ]
        self.assertTrue(pushes, "Commit + PR pushes nothing")
        for line in pushes:
            self.assertIn(
                'origin "fix/issue-', line,
                f"the fix is pushed somewhere other than the bot branch: {line}",
            )


class TheRedProofEscalationActuallyStopsTheCycle(unittest.TestCase):
    """A bad repro spec must end the cycle, not annotate it.

    The step fires when the reproducing test PASSED on broken code, which means
    the proof is worthless. Commenting and then succeeding lets the cycle run
    on to Fix, Green (`rc=0`), and a Gate that reads `--repro-rc 0` as "the bug
    is fixed" — so the loop merges a fix for a bug it never reproduced, with an
    escalation comment on the issue that nobody was paged for.
    """

    def test_the_escalation_exits_non_zero(self):
        body = steps_body("Escalate if repro not red")
        self.assertIn(
            "exit 1", body,
            "the escalation comments and then succeeds, so the cycle continues "
            "with an unproven reproducing test",
        )

    def test_it_fires_exactly_when_the_repro_did_not_go_red(self):
        cond = next(
            s for s in job_steps("heal.yml") if s["name"] == "Escalate if repro not red"
        )["cond"]
        self.assertIn("steps.red.outputs.red == 'false'", cond)


if __name__ == "__main__":
    unittest.main()
