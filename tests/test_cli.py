import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guardrails import cli
from guardrails.cli import main
from guardrails.stdio import read_text_arg


@contextlib.contextmanager
def _silenced():
    """Capture the gate's streams so a suite run does not bury real failures.

    The gate reports its refusals and states what a pass rested on, which is
    output the product needs and a test run does not.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def _gate_capturing(diff_text: str, *extra: str) -> tuple[int, str, str]:
    """Run the gate and return (rc, stdout, stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as f:
        f.write(diff_text)
        path = f.name
    try:
        with _silenced() as (out, err):
            rc = cli.main(["gate", "--diff", path, *extra])
    finally:
        Path(path).unlink()
    return rc, out.getvalue(), err.getvalue()


def _gate(diff_text: str, *extra: str) -> int:
    return _gate_capturing(diff_text, *extra)[0]


class TheGatePolicesWhateverConventionItIsGiven(unittest.TestCase):
    """The no-weakening check only polices conventions it was told about.

    Its built-in patterns describe Python and JS: `assert`/`expect` for an
    assertion, `skip`/`xfail` for a skip, and a config list naming conftest and
    the vitest/jest files. On any other stack each of those matches nothing, so
    the check runs, finds no violation, and passes a diff that silenced the
    suite. Every failure below is a fix merging to the default branch with the
    gate reporting success.
    """

    def test_a_go_assertion_removal_is_weakening(self):
        diff = (
            "diff --git a/pkg/sum_test.go b/pkg/sum_test.go\n"
            "-\tt.Errorf(\"want %d got %d\", want, got)\n"
        )
        self.assertEqual(
            _gate(diff, "--assert-pattern", r"t\.(Error|Fatal)"), 1,
            "a Go test lost its only failure report and the gate passed it",
        )

    def test_a_rust_ignore_marker_is_a_skip(self):
        diff = (
            "diff --git a/tests/parser_test.rs b/tests/parser_test.rs\n"
            "+#[ignore]\n"
        )
        self.assertEqual(
            _gate(diff, "--skip-pattern", r"#\[ignore\]"), 1,
            "a Rust test was switched off and the gate passed it",
        )

    def test_rspec_config_is_a_freeze_surface(self):
        diff = "diff --git a/.rspec b/.rspec\n+--exclude-pattern spec/repro_*\n"
        self.assertEqual(
            _gate(diff, "--test-config-globs", ".rspec,Gemfile"), 1,
            "the frozen test was excluded via runner config and the gate passed it",
        )


class TheGateProvesItCanReadThisTargetsAssertions(unittest.TestCase):
    """Supplying a convention is not the same as supplying a correct one.

    Every other check here trusts the patterns it was given. Unsupplied, or
    supplied wrong, they match nothing and the no-weakening check reports a
    clean diff for a fix that gutted the suite — the same silence the built-in
    patterns produce off their own stack, sourced from the interview instead of
    from core code.

    The frozen reproducing test is the sample that settles it without asking
    anyone. Red-then-green is not what makes it a usable sample — a test is
    equally red when the code under test simply throws, and such a file holds no
    assertion in any language. What makes it one is `templates/diagnose.md`
    requiring an explicit assertion in the repro, pinned by
    `test_prompt_contract.NoPromptContradictsTheGate`. Given that, a pattern
    finding nothing in it does not describe this language, and the gate is about
    to police nothing. Refusing costs one cycle and names both causes.
    """

    GO_TEST = 'func TestSum(t *testing.T) {\n\tif got != want {\n\t\tt.Errorf("bad")\n\t}\n}\n'

    def _gate_with_frozen_capturing(self, body: str, *extra: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            frozen = Path(tmp) / "sum_test.go"
            frozen.write_text(body, encoding="utf-8")
            diff = Path(tmp) / "fix.diff"
            diff.write_text("diff --git a/main.go b/main.go\n+\treturn a + b\n", encoding="utf-8")
            with _silenced() as (_, err):
                rc = cli.main(
                    ["gate", "--diff", str(diff), "--frozen", str(frozen),
                     "--repro-rc", "0", *extra]
                )
            return rc, err.getvalue()

    def _gate_with_frozen(self, body: str, *extra: str) -> int:
        return self._gate_with_frozen_capturing(body, *extra)[0]

    def test_it_refuses_when_it_cannot_find_an_assertion_in_the_frozen_test(self):
        self.assertEqual(
            self._gate_with_frozen(self.GO_TEST), 2,
            "gate accepted a target whose assertions it cannot recognise",
        )

    def test_it_proceeds_once_told_what_an_assertion_looks_like(self):
        self.assertEqual(
            self._gate_with_frozen(self.GO_TEST, "--assert-pattern", r"t\.(Error|Fatal)"), 0
        )

    def test_a_python_frozen_test_needs_no_extra_pattern(self):
        self.assertEqual(
            self._gate_with_frozen("def test_x():\n    assert add(1, 2) == 3\n"), 0
        )

    def test_the_refusal_names_the_other_cause_too(self):
        """Two different things produce this refusal, and only one is a pattern.

        A reproducing test that is red purely because the code under test
        throws contains no assertion in any language, so no `--assert-pattern`
        can rescue it — the repro is what has to change. A message naming only
        the pattern sends the operator to widen a regex that was never wrong,
        and the next cycle refuses identically.
        """
        rc, err = self._gate_with_frozen_capturing(
            "def test_parse_config():\n    parse_config({})\n"
        )
        self.assertEqual(rc, 2)
        self.assertIn("--assert-pattern", err)
        self.assertRegex(
            err, r"(?i)asserts nothing|no assertion.*throw|red only because",
            "the refusal offers the pattern as the only explanation",
        )


class TestGateTestGlobs(unittest.TestCase):
    """`--test-globs` lets Phase 1 hand the gate the target's own convention.

    The defaults cover filename conventions (`test_*`, `*.test.*`, …), but
    jest's `__tests__/` puts the marker in the DIRECTORY, which no filename
    rule can express. Without this flag such a target's tests are invisible to
    the weakening check and the gate silently passes a gutted suite.
    """

    def setUp(self):
        self.enterContext(_silenced())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.diff = Path(self.tmp.name) / "fix.diff"
        self.diff.write_text(
            "diff --git a/src/__tests__/parser.ts b/src/__tests__/parser.ts\n"
            "-  expect(parse(input)).toEqual(expected);\n"
        )

    def test_directory_convention_is_invisible_without_the_flag(self):
        self.assertEqual(main(["gate", "--diff", str(self.diff)]), 0)

    def test_supplying_the_glob_makes_the_gate_catch_it(self):
        rc = main(["gate", "--diff", str(self.diff), "--test-globs", "*__tests__/*"])
        self.assertEqual(rc, 1)

    def test_a_supplied_assert_pattern_extends_the_built_ins(self):
        """"Alternated, never replaced" is documented three times and was tested
        for globs only.

        The glob half and the pattern half are separate code paths, and this one
        guards the check that decides whether a fix gutted the suite. A mixed
        repo — a Go service beside a Python tool, which is ordinary — supplies a
        Go assertion form; if that replaced the built-ins rather than joining
        them, every Python and JS test in the same repo would become invisible
        to the no-weakening check, suite-wide, silently.
        """
        py = Path(self.tmp.name) / "py.diff"
        py.write_text(
            "diff --git a/tests/test_parser.py b/tests/test_parser.py\n"
            "-    assert parse(raw) == expected\n"
        )
        rc = main(["gate", "--diff", str(py), "--assert-pattern", r"t\.(Error|Fatal)"])
        self.assertEqual(
            rc, 1,
            "supplying a Go assertion form switched off the built-in Python one",
        )

    def test_a_pattern_that_does_not_compile_stops_the_gate(self):
        # An installer-supplied regex that will not compile must refuse rather
        # than leave the check quietly matching only the built-in half. Exit 2,
        # not 1: this is unusable input, not a violation.
        rc, _, err = _gate_capturing(
            "diff --git a/src/x.py b/src/x.py\n+ok\n",
            "--assert-pattern", "([unclosed",
        )
        self.assertEqual(rc, 2)
        self.assertIn("invalid pattern", err)

    def test_a_supplied_skip_pattern_extends_the_built_ins(self):
        rs = Path(self.tmp.name) / "skip.diff"
        rs.write_text(
            "diff --git a/tests/test_parser.py b/tests/test_parser.py\n"
            "+    @pytest.mark.skip(reason='flaky')\n"
        )
        rc = main(["gate", "--diff", str(rs), "--skip-pattern", r"#\[ignore\]"])
        self.assertEqual(
            rc, 1,
            "supplying a Rust skip form switched off the built-in Python one",
        )

    def test_supplied_globs_extend_the_defaults_rather_than_replacing_them(self):
        # An operator adding their directory convention must not thereby switch
        # OFF the built-in `test_*` / `*.test.*` coverage.
        other = Path(self.tmp.name) / "other.diff"
        other.write_text(
            "diff --git a/tests/parser.test.ts b/tests/parser.test.ts\n"
            "-  expect(parse(input)).toEqual(expected);\n"
        )
        rc = main(["gate", "--diff", str(other), "--test-globs", "*__tests__/*"])
        self.assertEqual(rc, 1)


class TestScrubCommand(unittest.TestCase):
    def test_scrub_prints_redacted_text(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("key=sk-abcdEFGH0123456789xyz boom\n")
            path = f.name
        captured = io.StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            rc = cli.main(["scrub", "--text", path])
        finally:
            sys.stdout = old
            Path(path).unlink()
        self.assertEqual(rc, 0)
        self.assertIn("[REDACTED]", captured.getvalue())
        self.assertNotIn("sk-abcdEFGH", captured.getvalue())


class TestGateBaselineMode(unittest.TestCase):
    """With --baseline/--current the gate judges regressions, not total green."""

    def setUp(self):
        self.enterContext(_silenced())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "fix.diff").write_text(
            "diff --git a/app/main.py b/app/main.py\n-    return {'status': STATIS}\n+    return {'status': 'ok'}\n"
        )

    def _write(self, name, ids):
        p = self.dir / name
        p.write_text("\n".join(ids))
        return str(p)

    def _gate(self, before, after, frozen=None, suite_rc=None, repro_rc=None):
        args = [
            "gate", "--diff", str(self.dir / "fix.diff"),
            "--baseline", self._write("before.txt", before),
            "--current", self._write("after.txt", after),
        ]
        if frozen:
            args += ["--frozen", frozen]
        if suite_rc is not None:
            args += ["--suite-rc", str(suite_rc)]
        if repro_rc is not None:
            args += ["--repro-rc", str(repro_rc)]
        with _silenced():
            return main(args)

    def test_pre_existing_failure_no_longer_blocks_a_good_fix(self):
        # The case the strict all-green rule blocks: a proven-correct fix on a
        # repo carrying an unrelated failure it never touched.
        rc = self._gate(
            before=["tests/test_main.py::test_lifespan", "tests/test_health.py::test_health"],
            after=["tests/test_main.py::test_lifespan"],
            frozen="tests/test_health.py",
            repro_rc=0,
        )
        self.assertEqual(rc, 0)

    def test_regression_blocks(self):
        rc = self._gate(
            before=["tests/test_main.py::test_lifespan"],
            after=["tests/test_main.py::test_lifespan", "tests/test_other.py::test_x"],
        )
        self.assertEqual(rc, 1)

    def test_crashed_suite_must_not_read_as_everything_green(self):
        # A collection error (bad import) makes pytest exit non-zero and print
        # ZERO "FAILED " lines, so the parsed set is empty. Without the exit
        # code, "the suite could not run" is indistinguishable from "perfect".
        rc = self._gate(before=[], after=[], suite_rc=2)
        self.assertEqual(rc, 1)

    def test_clean_run_with_zero_failures_still_passes(self):
        rc = self._gate(before=[], after=[], suite_rc=0)
        self.assertEqual(rc, 0)

    def test_repro_exit_code_is_the_proof_not_absence_from_a_list(self):
        # A test that errors at collection never appears as FAILED either, so
        # inferring "it passed" from absence declares the bug fixed when the
        # proof never ran. Positive evidence: run it, require rc 0.
        blocked = self._gate(before=[], after=[], frozen="tests/test_repro.py", repro_rc=2)
        passed = self._gate(before=[], after=[], frozen="tests/test_repro.py", repro_rc=0)
        self.assertEqual((blocked, passed), (1, 0))

    def test_half_supplied_baseline_pair_is_an_error_not_a_silent_skip(self):
        # Dropping one flag while adapting the workflow would otherwise disable
        # the regression check AND the fix-proof with no diagnostic.
        rc = main([
            "gate", "--diff", str(self.dir / "fix.diff"),
            "--baseline", self._write("before.txt", ["tests/test_a.py::t"]),
        ])
        self.assertEqual(rc, 2)

    def test_test_config_edit_is_caught_even_with_no_frozen_test(self):
        # A non-reproducible failure means no frozen test; the fix may still not
        # be allowed to silence the suite via conftest/pyproject.
        (self.dir / "cfg.diff").write_text(
            "diff --git a/conftest.py b/conftest.py\n+collect_ignore = ['tests']\n"
        )
        rc = main(["gate", "--diff", str(self.dir / "cfg.diff")])
        self.assertEqual(rc, 1)


class ABlockedCycleSaysWhatItCaught(unittest.TestCase):
    """`gate.txt` must distinguish a pass from a block, and name the violation.

    Three refusal paths returned a bare 1 with no output, so a blocked fix
    recorded no reason anywhere and the evidence bundle held an empty file
    whether the gate passed or blocked. That directly defeats the must-pass
    verification, whose whole point is watching a deliberately test-weakening
    fix get refused: the operator sees a red step and no stated cause.
    """

    def test_weakening_is_reported_with_the_file_and_the_assertion(self):
        diff = (
            "diff --git a/tests/test_parser.py b/tests/test_parser.py\n"
            "--- a/tests/test_parser.py\n"
            "+++ b/tests/test_parser.py\n"
            "-    assert parse(raw) == expected\n"
        )
        rc, _, err = _gate_capturing(diff)
        self.assertEqual(rc, 1)
        self.assertIn("tests/test_parser.py", err)
        self.assertIn("assert parse(raw) == expected", err)

    def test_a_touched_test_config_is_reported_with_the_file(self):
        diff = (
            "diff --git a/vitest.config.ts b/vitest.config.ts\n"
            "--- a/vitest.config.ts\n"
            "+++ b/vitest.config.ts\n"
            "+  exclude: ['tests/issue-2.test.ts'],\n"
        )
        rc, _, err = _gate_capturing(diff)
        self.assertEqual(rc, 1)
        self.assertIn("vitest.config.ts", err)

    def test_touching_the_frozen_test_is_reported(self):
        diff = (
            "diff --git a/tests/test_repro.py b/tests/test_repro.py\n"
            "--- a/tests/test_repro.py\n"
            "+++ b/tests/test_repro.py\n"
            "+# relaxed\n"
        )
        rc, _, err = _gate_capturing(diff, "--frozen", "tests/test_repro.py", "--repro-rc", "0")
        self.assertEqual(rc, 1)
        self.assertIn("tests/test_repro.py", err)

    def test_a_loop_tree_tamper_is_reported_at_the_cli(self):
        # The gate function is unit-tested, but nothing covered this refusal
        # through the CLI — so deleting the check from the CLI's own list left
        # the whole suite green while a tamper diff exited 0.
        diff = (
            "diff --git a/.shl/guardrails/gate.py b/.shl/guardrails/gate.py\n"
            "--- a/.shl/guardrails/gate.py\n"
            "+++ b/.shl/guardrails/gate.py\n"
            "+def is_test_weakened(*a, **k): return None\n"
        )
        rc, out, err = _gate_capturing(diff)
        self.assertEqual(rc, 1)
        self.assertIn(".shl/guardrails/gate.py", err)
        self.assertNotIn("PASSED", out)

    def _layout(self, files):
        """A temp repo, entered, so relative frozen paths resolve as on a runner."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in files:
            p = Path(tmp) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("assert f() == 1\n", encoding="utf-8")
        original = os.getcwd()
        self.addCleanup(os.chdir, original)
        os.chdir(tmp)
        return tmp

    def test_a_pass_without_a_frozen_test_says_the_pattern_was_never_proved(self):
        """The proof hangs off `--frozen`, which the workflow supplies rarely.

        `heal.yml` passes it only when Diagnose returned `reproducible: true`,
        and both prompts say most runtime failures do not reduce to a
        deterministic test. So on the majority cycle the assertion-pattern proof
        never runs — and a pass line that mentions it only when a frozen test
        WAS supplied reads, on every other cycle, as though the pattern had been
        checked. It had not been checked against anything.
        """
        clean = "diff --git a/src/x.py b/src/x.py\n+ok\n"
        _, out, _ = _gate_capturing(clean)
        self.assertIn("NOT proved", out)
        self.assertIn("no frozen test this cycle", out)

    def test_a_proved_pattern_claims_nothing_extra(self):
        # The inverse, so the message above cannot become unconditional noise
        # that a reader learns to skip.
        with tempfile.TemporaryDirectory() as tmp:
            frozen = Path(tmp) / "test_repro.py"
            frozen.write_text("def test_x():\n    assert f() == 1\n", encoding="utf-8")
            _, out, _ = _gate_capturing(
                "diff --git a/src/x.py b/src/x.py\n+ok\n", "--frozen", str(frozen),
                "--repro-rc", "0",
            )
        self.assertNotIn("NOT proved", out)

    def test_the_gate_blocks_a_helper_the_frozen_test_imports(self):
        """The wiring, not the predicate — the predicate has its own tests.

        `is_test_helper_touched` passing its unit tests says nothing about
        whether the gate consults it. Removing the call must fail here.
        """
        self._layout(["tests/test_repro_12.py", "tests/helpers.py"])
        diff = (
            "diff --git a/tests/helpers.py b/tests/helpers.py\n"
            "--- a/tests/helpers.py\n+++ b/tests/helpers.py\n"
            "@@ -1 +1 @@\n-def f(): return 1\n+def f(): return 0\n"
        )
        rc, out, err = _gate_capturing(diff, "--frozen", "tests/test_repro_12.py", "--repro-rc", "0")
        self.assertEqual(rc, 1)
        self.assertIn("helpers.py", err)
        self.assertNotIn("PASSED", out)

    def test_a_new_file_beside_the_frozen_test_still_passes(self):
        # Fix legitimately adds regression tests there; blocking those would
        # make the guard unusable and it would be turned off.
        self._layout(["tests/test_repro_12.py", "tests/helpers.py"])
        diff = (
            "diff --git a/tests/test_regression.py b/tests/test_regression.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/tests/test_regression.py\n"
            "@@ -0,0 +1 @@\n+assert f() == 1\n"
        )
        rc, out, _ = _gate_capturing(diff, "--frozen", "tests/test_repro_12.py", "--repro-rc", "0")
        self.assertEqual(rc, 0)
        self.assertIn("helpers untouched", out)

    def test_a_mixed_layout_says_the_check_stood_down(self):
        """Silence would read exactly like having checked and passed.

        Where tests sit beside source, freezing the directory would refuse every
        legitimate fix, so the check declines — and the pass line has to say so,
        or the evidence bundle records a guarantee nobody gave.
        """
        self._layout(["pkg/thing.go", "pkg/thing_test.go"])
        diff = (
            "diff --git a/pkg/thing.go b/pkg/thing.go\n"
            "--- a/pkg/thing.go\n+++ b/pkg/thing.go\n"
            "@@ -1 +1 @@\n-package p\n+package p // fixed\n"
        )
        rc, out, _ = _gate_capturing(
            diff, "--frozen", "pkg/thing_test.go", "--repro-rc", "0",
            "--test-globs", "*_test.*"
        )
        self.assertEqual(rc, 0, "a legitimate Go source fix must not be blocked")
        self.assertIn("NOT frozen", out)
        self.assertNotIn("helpers untouched", out)

    def test_the_cli_runs_as_a_module_from_an_installed_layout(self):
        """`python -B -m guardrails.cli`, exactly as the workflow spells it.

        Every test in this file calls `cli.main()` in-process, with the
        framework root already on `sys.path`. The workflow does something
        different eleven times: it starts a fresh interpreter with
        `PYTHONPATH=.shl` and asks for the module by name. Those differ in the
        one way that has already killed every cycle at step five — invoking the
        CLI as a script path put `.../guardrails` on `sys.path` instead of the
        loop root, so `cli.py`'s own package import raised ModuleNotFoundError.
        In-process tests could not see it; ten call sites shipped broken.

        Subprocess, and against a rebuilt install layout rather than this repo,
        because the thing under test is how the module resolves its own package.
        """
        import shutil
        import subprocess

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            loop_dir = Path(tmp) / ".shl"
            loop_dir.mkdir()
            for part in ("guardrails", "adapters", "agent"):
                shutil.copytree(root / part, loop_dir / part)
            for mod in ("role.py", "loop.py", "log_compact.py", "evidence.py", "gh_state.py"):
                shutil.copy(root / mod, loop_dir / mod)
            diff_path = Path(tmp) / "clean.diff"
            diff_path.write_text("diff --git a/src/x.py b/src/x.py\n+ok\n", encoding="utf-8")

            result = subprocess.run(
                ["python3", "-B", "-m", "guardrails.cli", "gate", "--diff", str(diff_path)],
                cwd=tmp,
                env={"PYTHONPATH": ".shl", "PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            result.returncode, 0,
            f"the CLI could not run as the workflow invokes it\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )
        self.assertIn("PASSED", result.stdout)

    def test_the_gate_itself_blocks_a_diff_that_edits_the_workflow(self):
        """Driven through the CLI, because a guard the CLI never calls is inert.

        `gate.is_workflow_touched` having its own unit tests proves the function
        works, not that the gate consults it — the exact gap that shipped a
        `loop.py review -` crash past a suite full of tests for both sides of
        that seam. Deleting the check's line from the list must fail here.
        """
        diff = (
            "diff --git a/.github/workflows/heal.yml b/.github/workflows/heal.yml\n"
            "--- a/.github/workflows/heal.yml\n"
            "+++ b/.github/workflows/heal.yml\n"
            "@@ -1,3 +1,1 @@\n"
            "-      - name: Gate\n"
            "-        run: python -B -m guardrails.cli gate\n"
        )
        rc, out, err = _gate_capturing(diff)
        self.assertEqual(rc, 1)
        self.assertIn(".github/workflows/heal.yml", err)
        self.assertNotIn("PASSED", out)

    def test_a_clean_pass_attests_to_the_workflow_check_by_name(self):
        # The attestation is derived from the check list, so a check dropped
        # from the list stops being claimed. That makes this the second half of
        # the wiring proof: the block above catches removal on a dirty diff,
        # this catches it on a clean one.
        clean = "diff --git a/src/x.py b/src/x.py\n+ok\n"
        _, out, _ = _gate_capturing(clean)
        self.assertIn("workflow untouched", out)

    def test_a_pass_attests_only_to_checks_that_ran(self):
        # The attestation is built from the check list itself, so it cannot
        # name a guarantee the code no longer provides.
        clean = "diff --git a/src/x.py b/src/x.py\n+ok\n"
        _, out, _ = _gate_capturing(clean)
        self.assertIn("loop tree untouched", out)
        self.assertIn("no test weakened", out)
        # Conditional CHECK LABELS must be absent when their input is. Matched
        # on the label rather than on "frozen test", because the pass line also
        # states that no frozen test existed — which is the opposite of claiming
        # the check ran, and must not be mistaken for it.
        self.assertNotIn("frozen test untouched", out)
        self.assertNotIn("frozen test's helpers untouched", out)
        self.assertNotIn("now fails", out)

    def test_a_pass_reports_how_much_of_the_suite_it_could_see(self):
        # A stack whose convention nobody supplied matches no glob, so every
        # check reports clean while the weakening check saw nothing. The count
        # is the only thing in the record that says so.
        ruby = (
            "diff --git a/spec/parser_spec.rb b/spec/parser_spec.rb\n"
            "--- a/spec/parser_spec.rb\n"
            "+++ b/spec/parser_spec.rb\n"
            "-    expect(parse(raw)).to eq(expected)\n"
        )
        rc, out, _ = _gate_capturing(ruby)
        self.assertEqual(rc, 0)
        self.assertIn("0 test file(s) matched", out)

    def test_an_unreadable_frozen_test_says_the_proof_did_not_run(self):
        # The path still matches against the diff, so the run is not refused —
        # but the assertion-pattern proof could not execute, and a proof that
        # quietly did not run is the exact failure that check exists to catch.
        clean = "diff --git a/src/x.py b/src/x.py\n+ok\n"
        rc, out, err = _gate_capturing(clean, "--frozen", "tests/not_on_disk.py", "--repro-rc", "0")
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", err)
        self.assertIn("assertion pattern NOT proved", out)

    def test_a_frozen_test_with_no_exit_code_is_refused_rather_than_attested(self):
        # "untouched" is proven by the diff; "passing" is proven only by running
        # it. The gate used to pass with a caveat, which puts a hedge in the
        # evidence bundle where a refusal belongs — a cycle whose fix proof
        # never ran must not reach a merge decision at all.
        clean = "diff --git a/src/x.py b/src/x.py\n+ok\n"
        rc, _, err = _gate_capturing(clean, "--frozen", "tests/not_on_disk.py")
        self.assertEqual(rc, 2)
        self.assertIn("--repro-rc", err)


class BothEntryPointsReadTheSameDashConvention(unittest.TestCase):
    """`-` means stdin, and the rule lives in one place for both CLIs.

    `loop.py` and this module are separate command-line surfaces the workflows
    drive with one convention, and each used to implement it — they had already
    drifted on a missing argument. The module docstring here advertises `-` for
    every path argument, so it is an interface, not an incidental.
    """

    def test_the_gate_reads_its_diff_from_a_pipe(self):
        weakening = (
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
            "-    assert compute() == 1\n"
        )
        with mock.patch("sys.stdin", io.StringIO(weakening)), _silenced():
            self.assertEqual(cli.main(["gate", "--diff", "-"]), 1)

    def test_scrub_reads_its_text_from_a_pipe(self):
        with mock.patch("sys.stdin", io.StringIO("token=supersecretvalue\n")):
            with _silenced() as (out, _):
                cli.main(["scrub", "--text", "-"])
        self.assertIn("[REDACTED]", out.getvalue())

    def test_a_missing_value_reads_stdin_rather_than_a_file_named_none(self):
        with mock.patch("sys.stdin", io.StringIO("piped")):
            self.assertEqual(read_text_arg(None), "piped")

    def test_a_real_path_is_still_read_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.txt"
            path.write_text("from disk", encoding="utf-8")
            self.assertEqual(read_text_arg(str(path)), "from disk")


class TheSuiteExitCodeIsCheckedWithoutABaselinePair(unittest.TestCase):
    """A crashed suite must not read as green just because no baseline was given.

    The cross-check used to sit inside the `--baseline`/`--current` block, so a
    hand invocation — which the module docstring documents as supported — got
    exit 0 whatever `--suite-rc` said. The condition it tests is about the
    PARSED failure list being empty, and an absent `--current` is the emptiest
    that list ever gets.
    """

    CLEAN = "diff --git a/src/x.py b/src/x.py\n+ok\n"

    def test_a_nonzero_suite_code_with_nothing_parsed_blocks(self):
        rc, _, err = _gate_capturing(self.CLEAN, "--suite-rc", "2")
        self.assertEqual(rc, 1)
        self.assertIn("could not run", err)

    def test_a_zero_suite_code_still_passes(self):
        self.assertEqual(_gate(self.CLEAN, "--suite-rc", "0"), 0)


class TheDevGuideQuotesTheRefusalFormatItActuallyEmits(unittest.TestCase):
    """`CLAUDE.md` ships, so a wrong example in it is a shipped defect.

    It quotes a refusal line to show that a block names its grounds. That
    example had dropped the check label the CLI puts in every refusal, which is
    the part telling a reader WHICH guard fired — so the doc illustrated a
    weaker line than the gate produces, in the section arguing the line is
    strong enough to verify against.

    Asserted against a real emitted refusal rather than against the f-string:
    reading the source proves the format string, not what reaches stderr.
    """

    DEV_GUIDE = Path(__file__).resolve().parents[1] / "CLAUDE.md"

    def _emitted_refusal(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            diff = Path(tmp) / "weakened.diff"
            diff.write_text(
                "diff --git a/tests/test_x.py b/tests/test_x.py\n"
                "--- a/tests/test_x.py\n"
                "+++ b/tests/test_x.py\n"
                "@@ -1,2 +1,1 @@\n"
                "-    assert f() == 1\n",
                encoding="utf-8",
            )
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main(["gate", "--diff", str(diff)])
            self.assertEqual(rc, 1, "the weakened diff must be refused")
            return err.getvalue().strip()

    def test_the_quoted_example_matches_a_real_refusal(self):
        emitted = self._emitted_refusal()
        self.assertRegex(
            emitted, r"^BLOCKED \[[^\]]+\]: \S+: .+",
            "the refusal no longer carries check, file and evidence",
        )
        quoted = re.findall(r"`(BLOCKED[^`]*)`", self.DEV_GUIDE.read_text(encoding="utf-8"))
        self.assertTrue(quoted, "the dev guide no longer shows a refusal example")
        for example in quoted:
            with self.subTest(example=example):
                self.assertRegex(
                    example, r"^BLOCKED \[[^\]]+\]: \S+: .+",
                    f"CLAUDE.md shows {example!r}; a real refusal reads "
                    f"{emitted!r}, so the example teaches the wrong shape",
                )


class TheBlindnessSignalAndTheRefusalOrderAreBothPinned(unittest.TestCase):
    """Two properties `cli.py` declares load-bearing and nothing asserted.

    The matched-file count is the ONLY thing in a passing record that says the
    no-weakening check could see anything at all — on a stack whose convention
    nobody supplied it reads 0 while every check reports clean. It was pinned
    only at zero, so a constant 0 passed the suite and made a real blindness
    indistinguishable from a healthy one.

    The refusal ORDER is declared load-bearing in the same file: the loop-tree
    and workflow checks must be reported first, because a diff that rewrote the
    judge should say so rather than reporting whichever content violation it
    also happens to carry.
    """

    TWO_TEST_FILES = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1 +1,2 @@\n def t():\n+    assert g() == 2\n"
        "diff --git a/tests/test_b.py b/tests/test_b.py\n"
        "--- a/tests/test_b.py\n+++ b/tests/test_b.py\n"
        "@@ -1 +1,2 @@\n def t():\n+    assert h() == 3\n"
    )

    def test_the_count_reports_how_many_test_files_it_saw(self):
        rc, out, _ = _gate_capturing(self.TWO_TEST_FILES)
        self.assertEqual(rc, 0, out)
        self.assertIn("2 test file(s) matched", out)

    def test_the_count_is_zero_when_the_convention_is_unknown(self):
        # The blindness signal itself, kept: a constant is what this pair rules
        # out, and one direction alone cannot.
        rc, out, _ = _gate_capturing(
            "diff --git a/spec/thing_spec.rb b/spec/thing_spec.rb\n"
            "--- a/spec/thing_spec.rb\n+++ b/spec/thing_spec.rb\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("0 test file(s) matched", out)

    def test_the_loop_tree_is_reported_before_a_content_violation(self):
        # A diff violating BOTH: the reach violation is the one to name.
        both = (
            "diff --git a/.shl/guardrails/gate.py b/.shl/guardrails/gate.py\n"
            "--- a/.shl/guardrails/gate.py\n+++ b/.shl/guardrails/gate.py\n"
            "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
            "@@ -1,2 +1,1 @@\n def t():\n-    assert f() == 1\n"
        )
        rc, _, err = _gate_capturing(both)
        self.assertEqual(rc, 1)
        self.assertIn("loop tree untouched", err)
        self.assertNotIn("no test weakened", err)


class TheFrozenTestIsRefusedByBasenameToo(unittest.TestCase):
    """The wider of the two frozen-path branches, and the case only it covers.

    The path branch handles the absolute-header case already, so this one is
    live purely to refuse a same-named file in a different directory — a
    deliberate over-refusal that had no test, meaning deleting it left the
    suite green.
    """

    def _diff(self, path):
        return (
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            "@@ -1 +1 @@\n-x\n+y\n"
        )

    def _run(self, diff_text, frozen):
        """The frozen file must EXIST at the path passed, or the gate refuses on
        the assertion-pattern proof before reaching the freeze check at all —
        which is a different refusal wearing the same exit code."""
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / frozen
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("def test_x():\n    assert g() == 1\n", encoding="utf-8")
            d = root / "fix.diff"
            d.write_text(diff_text, encoding="utf-8")
            os.chdir(root)
            try:
                with _silenced() as (out, err):
                    rc = cli.main(
                        ["gate", "--diff", str(d), "--frozen", frozen, "--repro-rc", "0"]
                    )
            finally:
                os.chdir(original)
            return rc, err.getvalue()

    def test_a_same_named_file_elsewhere_is_still_refused(self):
        rc, err = self._run(
            self._diff("other/pkg/test_repro_issue_7.py"),
            "tests/test_repro_issue_7.py",
        )
        self.assertEqual(rc, 1, "the basename branch refused nothing")
        self.assertIn("frozen test untouched", err)

    def test_an_unrelated_test_file_is_not_refused_by_it(self):
        # The over-refusal must be bounded by the NAME, or it blocks every fix.
        # Also a different DIRECTORY, or the neighbour freeze answers first and
        # this proves nothing about the basename branch.
        rc, err = self._run(
            self._diff("other/pkg/test_something_else.py"),
            "tests/test_repro_issue_7.py",
        )
        self.assertEqual(rc, 0, err)


class EveryGateCheckIsReachedThroughTheCli(unittest.TestCase):
    """A predicate the CLI never calls is inert, and the pass line still names it.

    This project has now shipped that defect twice: `is_diff_config_touched`
    was added with three unit tests and no test of its wiring, so replacing its
    call with a constant `None` left the whole suite green while the real CLI
    passed a `.gitattributes` blinding diff and attested "diff rendering
    untouched". `test_prompt_contract` cannot catch it — it harvests labels
    from a CLEAN diff's pass line, and the label survives the unwiring.

    So the membership is COMPUTED from the pass line and every check is driven
    to its OUTCOME here. A new check added without a refusal case fails this
    class rather than shipping unwired.
    """

    # One diff per check that must make the CLI exit 1, keyed by the label the
    # pass line uses. Extra flags where the check needs them.
    VIOLATIONS = {
        "loop tree untouched": (
            "diff --git a/.shl/guardrails/gate.py b/.shl/guardrails/gate.py\n"
            "--- a/.shl/guardrails/gate.py\n+++ b/.shl/guardrails/gate.py\n"
            "@@ -1 +1 @@\n-x = 1\n+x = 2\n", ()),
        "workflow untouched": (
            "diff --git a/.github/workflows/heal.yml b/.github/workflows/heal.yml\n"
            "--- a/.github/workflows/heal.yml\n+++ b/.github/workflows/heal.yml\n"
            "@@ -1 +1 @@\n-on: x\n+on: y\n", ()),
        "diff rendering untouched": (
            "diff --git a/tests/.gitattributes b/tests/.gitattributes\n"
            "--- /dev/null\n+++ b/tests/.gitattributes\n"
            "@@ -0,0 +1 @@\n+* -diff\n", ()),
        "test content readable": (
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "index 4ae9200..6143e74 100644\n"
            "Binary files a/tests/test_x.py and b/tests/test_x.py differ\n", ()),
        "no test weakened": (
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
            "@@ -1,2 +1,1 @@\n def t():\n-    assert f() == 1\n", ()),
        "no test config touched": (
            "diff --git a/conftest.py b/conftest.py\n"
            "--- a/conftest.py\n+++ b/conftest.py\n"
            "@@ -1 +1 @@\n-x = 1\n+x = 2\n", ()),
    }

    def _pass_labels(self):
        rc, out, _ = _gate_capturing(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        )
        self.assertEqual(rc, 0, out)
        line = out.partition("PASSED: ")[2]
        return [
            s.strip() for s in line.split(";")
            if s.strip() and "(" not in s and "NOT" not in s and not s.strip()[0].isdigit()
        ]

    def test_every_label_on_the_pass_line_has_a_refusal_case_here(self):
        # Computed, so a seventh check cannot ship with no outcome test.
        self.assertEqual(
            set(self._pass_labels()), set(self.VIOLATIONS),
            "the gate's check set and this class's refusal cases disagree; a "
            "check with no case here can be unwired without any test noticing",
        )

    def test_each_check_actually_blocks_through_the_cli(self):
        for label, (diff, extra) in self.VIOLATIONS.items():
            with self.subTest(check=label):
                rc, out, err = _gate_capturing(diff, *extra)
                self.assertEqual(
                    rc, 1,
                    f"{label!r} did not block through the CLI; "
                    f"stdout={out!r} stderr={err!r}",
                )
                self.assertIn(label, err)


class TheGateRefusesADiffItCannotReadTheContentOf(unittest.TestCase):
    """Driven through `cli.main`, because a predicate the CLI never calls is inert.

    The sibling `.gitattributes` check shipped with three unit tests and no test
    of its wiring, so replacing its call with a constant `None` left the suite
    green: calling the function is not exercising the seam the product uses,
    inside the file whose whole job is to exercise that seam. This class exists
    so the same cannot be true of this check.

    The scenario is real rather than constructed: verified against git 2.43.0,
    removing an assertion from `tests/test_x.py` BLOCKS as a text diff and
    PASSES once one NUL byte is added to the same file, because git then prints
    `Binary files … differ` in place of every content line.
    """

    WEAKENED_BUT_BINARY = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "index 4ae9200..6143e74 100644\n"
        "Binary files a/tests/test_x.py and b/tests/test_x.py differ\n"
    )

    def test_the_cli_blocks_it(self):
        rc, _, err = _gate_capturing(self.WEAKENED_BUT_BINARY)
        self.assertEqual(rc, 1, f"the gate passed a test file it could not read: {err}")
        self.assertIn("test content readable", err)
        self.assertIn("tests/test_x.py", err)

    def test_the_pass_line_names_this_check_when_it_holds(self):
        # The attestation is built from the refusal list, so a check dropped from
        # that list disappears from the pass line too. Asserting the label is
        # present on a clean diff is what makes deleting the entry visible.
        rc, out, _ = _gate_capturing(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        )
        self.assertEqual(rc, 0)
        self.assertIn("test content readable", out)

    def test_a_binary_file_that_is_not_a_test_still_passes(self):
        # A fix adding a fixture is ordinary work. If this check blocked it, the
        # first operator to hit it would widen the globs until it stopped firing.
        rc, _, err = _gate_capturing(
            "diff --git a/assets/logo.png b/assets/logo.png\n"
            "index 4ae9200..6143e74 100644\n"
            "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
        )
        self.assertEqual(rc, 0, err)


class AnUnreadableDiffIsARefusalNotACrash(unittest.TestCase):
    """A gate that dies reading its input exits 1 with a traceback and no
    BLOCKED line — which in the evidence bundle is indistinguishable from a
    refusal that forgot to state its grounds, the exact ambiguity the whole
    verdict-states-its-reason design exists to remove.

    Exit 2, because this is input the gate cannot judge on, not a violation.
    """

    def test_a_missing_diff_file_exits_two_with_a_reason(self):
        with _silenced() as (_, err):
            rc = cli.main(["gate", "--diff", "/nonexistent/path/fix.diff"])
        self.assertEqual(rc, 2, "a missing diff was not a clean refusal")
        self.assertIn("cannot read diff", err.getvalue())

    def test_a_readable_diff_is_unaffected(self):
        rc, _, _ = _gate_capturing(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x\n+y\n"
        )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
