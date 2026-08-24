import io
import json
import os
import sys
import tempfile
from unittest import mock
import unittest
from pathlib import Path

import loop
from gh_state import _marked_fingerprints
from agent.harness import CLAUDE_CODE, AgentRun, ConfiguredAgent, ModelConfig
from gh_state import _gh
from loop import build_agent_from_env, recall_incidents, run_watch, under_attempt_cap
from role import _CONTEXT_LABELS
from guardrails.incident_memory import IncidentRecord


def _capture():
    calls = []

    def runner(argv, env, cwd):
        calls.append((list(argv), dict(env), cwd))
        return AgentRun(stdout='```json\n{"ok": true}\n```')

    return calls, runner


class _FakeAdapter:
    def __init__(self, log):
        self._log = log

    def read_log(self):
        return self._log


class TestBuildAgentFromEnv(unittest.TestCase):
    def test_claude_code_third_party(self):
        env = {
            "SHL_HARNESS": "claude-code",
            "SHL_MODEL": "example-model-v1",
            "SHL_AUTH_TOKEN": "sk-test-key",
            "SHL_BASE_URL": "https://api.example-provider.test/v1",
        }
        calls, runner = _capture()
        agent = build_agent_from_env(env, runner=runner)
        agent.run("fix it", __import__("agent.base", fromlist=["AgentRole"]).AgentRole.FIX, Path("/r"))
        argv, e, _ = calls[0]
        self.assertIn("acceptEdits", argv)
        self.assertEqual(e["ANTHROPIC_AUTH_TOKEN"], "sk-test-key")
        self.assertEqual(e["ANTHROPIC_BASE_URL"], "https://api.example-provider.test/v1")
        self.assertEqual(e["ANTHROPIC_MODEL"], "example-model-v1")

    def test_opencode_uses_provider_auth_env(self):
        env = {
            "SHL_HARNESS": "opencode",
            "SHL_MODEL": "exampleprovider/example-model",
            "SHL_AUTH_TOKEN": "provider-key",
            "SHL_AUTH_ENV": "EXAMPLEPROVIDER_API_KEY",
        }
        from agent.base import AgentRole
        from agent.harness import OPENCODE, render

        # The wiring this file owns: SHL_AUTH_ENV reaching ModelConfig.auth_env.
        # What render() then does with it is test_harness's
        # test_opencode_auth_env_comes_from_model_not_harness.
        agent = build_agent_from_env(env, runner=_capture()[1])
        self.assertIs(agent._harness, OPENCODE)
        self.assertEqual(agent._model.auth_env, "EXAMPLEPROVIDER_API_KEY")

    def test_missing_required_env_raises(self):
        with self.assertRaises(KeyError):
            build_agent_from_env({"SHL_HARNESS": "claude-code"})


class TestRunWatch(unittest.TestCase):
    def test_signal_when_error_present(self):
        out = run_watch(_FakeAdapter("[ERROR] boom\n[INFO] ok\n"))
        self.assertIn("boom", out)
        # The whole noise line: "ok" alone is two characters that occur inside
        # ordinary words, so the negative would hold for the wrong reason.
        self.assertNotIn("[INFO]", out)

    def test_idle_when_only_noise(self):
        self.assertEqual(run_watch(_FakeAdapter("[INFO] ok\n[DEBUG] beat\n")), "")


class TheAttemptCapCountsBetweenItsEndpoints(unittest.TestCase):
    """The cap is the only thing that stops an unfixable bug being retried forever.

    Covering 0 (allow) and the cap itself (block) leaves the case in between
    untested, which is the one that has to keep working: a second attempt on a
    failure whose first attempt was refused. An off-by-one there either burns
    every cycle on one bug or gives up after a single try.
    """

    def _runner(self, attempts: int):
        body = json.dumps(
            {"comments": [{"body": f"fix attempt {n} failed"} for n in range(1, attempts + 1)]}
        )
        return lambda argv: body

    def test_one_prior_attempt_still_allows_a_retry(self):
        self.assertTrue(under_attempt_cap(5, cap=2, gh_runner=self._runner(1)))

    def test_the_cap_itself_blocks(self):
        self.assertFalse(under_attempt_cap(5, cap=2, gh_runner=self._runner(2)))

    def test_an_unreadable_gh_response_raises_rather_than_counting_zero(self):
        # Reading it as zero would reset the cap on every cycle a `gh` hiccup
        # happened to coincide with, which is the failure the cap exists to
        # stop. Actions runs each step under `bash -e`, so raising kills the
        # step and the cycle — loud, and the correct direction.
        with self.assertRaises(json.JSONDecodeError):
            under_attempt_cap(5, cap=2, gh_runner=lambda argv: "not json at all")


class TestRecallIncidents(unittest.TestCase):
    """Recall takes a LOG, not a title. End-to-end matching: test_incident_recall."""

    def test_empty_when_no_match(self):
        self.assertEqual(recall_incidents("nothing"), "")

    # The reverted/merged rendering is owned by test_incident_recall.ContextBudget,
    # which drives it from real records rather than from a stub that returns one
    # fixed row whatever it is asked for.


class TestReproPath(unittest.TestCase):
    """One implementation of "where does the repro test go", for every driver.

    L9: when two drivers must do the same thing they drift, and the untested
    one is the one that ships. The workflow shell and loop.py both need this
    path, so they call one function rather than each substituting the pattern.
    """

    def setUp(self):
        self.original = os.environ.get("SHL_REPRO_PATH")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.original is None:
            os.environ.pop("SHL_REPRO_PATH", None)
        else:
            os.environ["SHL_REPRO_PATH"] = self.original

    def test_unset_pattern_raises_rather_than_guessing_a_language(self):
        """There is no safe default, so there is no default.

        Falling back to `tests/test_repro_issue_{}.py`, a pytest convention, is
        a wrong answer that looks like a working one on any non-Python target:
        Diagnose is told to write a `.py` file, which is a strong steer to emit
        the wrong language entirely, and the file lands somewhere the runner
        never collects — so the red-then-green proof silently never happens.
        Unset is a misconfiguration; say so.
        """
        os.environ.pop("SHL_REPRO_PATH", None)
        with self.assertRaises(RuntimeError) as cm:
            loop.repro_path(42)
        self.assertIn("SHL_REPRO_PATH", str(cm.exception))

    def test_pattern_is_taken_from_the_environment(self):
        os.environ["SHL_REPRO_PATH"] = "tests/repro-issue-{}.test.ts"
        self.assertEqual(loop.repro_path(7), "tests/repro-issue-7.test.ts")

    def test_cli_prints_it_without_needing_a_provider_token(self):
        # The Red step runs agent-authored code and therefore holds NO secret,
        # so anything it invokes must not try to build an authenticated agent.
        os.environ["SHL_REPRO_PATH"] = "spec/repro_{}_spec.rb"
        captured = io.StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            rc = loop.main(["repro-path", "13"])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertEqual(captured.getvalue().strip(), "spec/repro_13_spec.rb")


class TestRunReviewContext(unittest.TestCase):
    def test_issue_and_repro_reach_the_reviewer(self):
        calls = []

        def runner(argv, env, cwd):
            calls.append((list(argv), dict(env), cwd))
            return AgentRun(stdout='```json\n{"approved": true, "reason": "ok"}\n```')

        agent = ConfiguredAgent(
            CLAUDE_CODE, ModelConfig(model="m", auth_token="k"), runner=runner
        )
        loop.run_review(
            agent, Path("/repo"), diff="+ fixed",
            issue="parse() drops attachments",
            # The shape `main()` actually supplies: Diagnose's `repro_test`
            # object, JSON-encoded. A bare string was never reachable from the
            # driver, and a fixture the product cannot produce tests a contract
            # nothing has (L8).
            repro=json.dumps({"code": "expect(x).toBe(1)"}),
        )
        prompt = " ".join(calls[0][0])
        self.assertIn("+ fixed", prompt)
        self.assertIn("parse() drops attachments", prompt)
        self.assertIn("expect(x).toBe(1)", prompt)


class TestFingerprintMarkerCli(unittest.TestCase):
    """The seam the workflow uses to stamp an issue with its failure identity.

    heal.yml is shell, so the marker has to be reachable from a command. Doing
    the derivation in shell instead would be a second implementation of the
    fingerprint rule, and two implementations drift: issue dedup keying on
    something incident memory does not is exactly what that produces.
    """

    def test_marker_is_derived_from_the_signal_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "signal.txt"
            signal.write_text(
                "TypeError: Cannot read properties of undefined (reading 'parts')\n"
                "    at parseExport (/srv/app/lib/parser.js:47:12)\n"
            )
            captured = io.StringIO()
            old = sys.stdout
            sys.stdout = captured
            try:
                rc = loop.main(["fingerprint-marker", str(signal)])
            finally:
                sys.stdout = old
        self.assertEqual(rc, 0)
        out = captured.getvalue().strip()
        self.assertTrue(out.startswith("<!-- shl-fingerprint:"), out)
        self.assertTrue(any(f.startswith("TypeError@") for f in _marked_fingerprints(out)), out)
        self.assertTrue(
            any("parser.js:47" in f for f in _marked_fingerprints(out)), out
        )

    def test_a_failure_it_cannot_fingerprint_stops_the_cycle(self):
        """An unparsed stack must fail loudly, not file a fresh issue every time.

        The marker is the dedup key AND the attempt-cap anchor. With no
        fingerprint the workflow finds no existing issue, opens a new one, and
        counts zero prior attempts — so the same failure is diagnosed, fixed and
        merged again on every cron tick, with the cap that exists to stop that
        never accumulating. Nothing looks broken from outside; the loop just
        works on one bug forever.

        Frame parsing covers Python and V8, so any other runtime lands here.
        """
        stacks = {
            "go": (
                "panic: runtime error: index out of range [3]\n\n"
                "goroutine 1 [running]:\nmain.process(0x0, 0x3)\n"
                "\t/app/handler.go:42 +0x1d\n"
            ),
            "ruby": (
                "app/services/invoice.rb:42:in `total': undefined method (NoMethodError)\n"
                "\tfrom app/x.rb:19:in `run'\n"
            ),
        }
        for name, stack in stacks.items():
            with self.subTest(stack=name), tempfile.TemporaryDirectory() as tmp:
                signal = Path(tmp) / "signal.txt"
                signal.write_text(stack)
                stderr, sys.stderr = sys.stderr, io.StringIO()
                try:
                    rc = loop.main(["fingerprint-marker", str(signal)])
                    message = sys.stderr.getvalue()
                finally:
                    sys.stderr = stderr
                self.assertEqual(rc, 1, f"{name} stack was accepted without a fingerprint")
                self.assertIn("fingerprint", message.lower())

    def test_a_python_failure_still_produces_a_marker(self):
        # The guard must not fire on a stack the compactor does understand.
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "signal.txt"
            signal.write_text(
                'Traceback (most recent call last):\n'
                '  File "/app/x.py", line 7, in go\n    boom()\n'
                "ValueError: bad\n"
            )
            captured, sys.stdout = sys.stdout, io.StringIO()
            try:
                rc = loop.main(["fingerprint-marker", str(signal)])
                out = sys.stdout.getvalue()
            finally:
                sys.stdout = captured
        self.assertEqual(rc, 0)
        self.assertTrue(any(f.startswith("ValueError@") for f in _marked_fingerprints(out)), out)

    def test_a_signal_with_no_recognisable_failure_yields_an_empty_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "signal.txt"
            signal.write_text("[INFO] everything is fine\n")
            captured = io.StringIO()
            old = sys.stdout
            sys.stdout = captured
            try:
                loop.main(["fingerprint-marker", str(signal)])
            finally:
                sys.stdout = old
        # No fingerprints means no dedup key; the workflow must still be able to
        # file the issue rather than crashing on a missing marker.
        self.assertEqual(captured.getvalue().strip(), "<!-- shl-fingerprint:  -->")


class TestUnderAttemptCap(unittest.TestCase):
    def test_under_cap_allows(self):
        fake_gh = lambda argv: '{"comments": []}'  # 0 attempts
        self.assertTrue(under_attempt_cap(5, cap=2, gh_runner=fake_gh))

    def test_at_cap_blocks(self):
        fake_gh = lambda argv: '{"comments": [{"body": "fix attempt 2 failed"}]}'
        self.assertFalse(under_attempt_cap(5, cap=2, gh_runner=fake_gh))


class ReadArgOrStdinTests(unittest.TestCase):
    """The invocation form `heal.yml` actually uses.

    Shipped broken: `loop.py review -` raised FileNotFoundError('-') on a real
    runner, after Diagnose, Red and Fix had already been spent. Structural tests
    covered the workflow's YAML and the module's functions, but nothing covered
    the argument form passing between them.
    """

    def test_the_helper_reads_stdin_on_a_dash_and_a_path_from_disk(self):
        # One test for the helper. Everything else here goes through main(),
        # because the helper being correct is not what broke.
        with mock.patch("sys.stdin", io.StringIO("diff from the pipe")):
            self.assertEqual(loop._read_arg_or_stdin(["review", "-"]), "diff from the pipe")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal.txt"
            path.write_text("from the file", encoding="utf-8")
            self.assertEqual(loop._read_arg_or_stdin(["diagnose", str(path)]), "from the file")


class FixIsToldAboutTheFrozenTestOnlyWhenOneExists(unittest.TestCase):
    """The workflow writes that file only when Diagnose said it could reproduce.

    `heal.yml` guards the write on `reproducible == "true"`, and both role
    prompts say most runtime failures — a 500, a rate limit, an upstream null,
    a timeout — do not reduce to a deterministic test. So the majority cycle has
    no frozen file, and naming a path anyway tells a cold agent that a proven-red
    test is waiting at somewhere that does not exist. It then either hunts for
    it or reasons from a file it never read.
    """

    def _frozen_for(self, repro):
        captured = {}

        def fake_run_role(role, context, agent, cwd):
            captured.update(context)
            return {"summary": "s", "files_changed": []}

        with mock.patch.object(loop, "run_role", fake_run_role), \
             mock.patch.object(loop, "recall_incidents", lambda *a, **k: ""), \
             mock.patch.dict(os.environ, {"SHL_REPRO_PATH": "tests/issue-{}.test.ts"}):
            loop.run_fix(object(), Path("/repo"), "issue", repro, issue_number="12")
        return captured.get("frozen", "")

    def test_a_reproducible_cycle_names_the_frozen_path(self):
        self.assertEqual(
            self._frozen_for(json.dumps({"code": "expect(x).toBe(1)"})),
            "tests/issue-12.test.ts",
        )

    def test_a_non_reproducible_cycle_names_nothing(self):
        self.assertEqual(self._frozen_for("{}"), "")

    def test_an_empty_code_field_counts_as_no_repro(self):
        self.assertEqual(self._frozen_for(json.dumps({"code": ""})), "")

    def test_malformed_repro_json_does_not_invent_a_path(self):
        # Degrading toward "no frozen test" is the safe direction: the gate
        # still refuses edits to whatever the workflow froze, and Fix is simply
        # not told about a file this cycle cannot vouch for.
        self.assertEqual(self._frozen_for("not json at all"), "")


class AnAbsentReproIsNotRenderedAsAnEmptyOne(unittest.TestCase):
    """`json.dumps({})` is the string `{}`, and a non-empty string is truthy.

    `main()` hands both Fix and Review `json.dumps(diagnose["repro_test"])`,
    defaulted to `{}`, so a cycle where Diagnose could not reproduce still
    renders the heading *Reproducing test Diagnose specified* with `{}` under
    it. The heading is the claim, and it is false: Diagnose specified nothing.
    Review is then told to judge the fix against a test that does not exist,
    on what both prompts describe as the MAJORITY path.

    Asserted on the rendered prompt rather than on the context dict, because
    the truthiness that produces this lives in `build_prompt` — a check on the
    dict would pass while the heading still shipped.
    """

    LABEL = _CONTEXT_LABELS["repro"]

    # Satisfies both roles' contracts at once, so one runner serves Fix and
    # Review and neither call dies in `validate_contract` before the prompt
    # this class is actually inspecting has been recorded.
    _PAYLOAD = '```json\n{"summary": "s", "files_changed": [], ' \
               '"approved": true, "reason": "ok"}\n```'

    def _prompt(self, call, repro: str) -> str:
        calls = []

        def runner(argv, env, cwd):
            calls.append(list(argv))
            return AgentRun(stdout=self._PAYLOAD)

        agent = ConfiguredAgent(
            CLAUDE_CODE, ModelConfig(model="m", auth_token="k"), runner=runner
        )
        with mock.patch.object(loop, "recall_incidents", lambda *a, **k: ""), \
             mock.patch.dict(os.environ, {"SHL_REPRO_PATH": "tests/issue-{}.test.ts"}):
            call(agent)
        return " ".join(calls[0])

    def _fix_prompt(self, repro: str) -> str:
        return self._prompt(
            lambda agent: loop.run_fix(
                agent, Path("/repo"), "issue", repro, issue_number="12"
            ),
            repro,
        )

    def _review_prompt(self, repro: str) -> str:
        return self._prompt(
            lambda agent: loop.run_review(
                agent, Path("/repo"), diff="+ fixed", issue="issue", repro=repro
            ),
            repro,
        )

    def test_the_label_is_the_one_the_prompt_builder_actually_uses(self):
        """The label must belong to the KEY this class renders — the earlier
        form checked only that the string existed among the label VALUES, which
        any label satisfies and a renamed key sails past.

        Verified by rendering: the heading appears in a prompt whose context
        carries that key and only that key.
        """
        key = next(k for k, v in _CONTEXT_LABELS.items() if v == self.LABEL)
        prompts = []

        class Stub:
            def run(self, prompt, role, cwd):
                prompts.append(prompt)
                return '```json\n{"approved": true, "reason": "ok"}\n```'

        # A REAL repro: run_review gates the section through _has_repro, so a
        # bare string is correctly dropped and the label would never render —
        # which is exactly the behaviour the sibling tests in this class pin.
        loop.run_review(
            Stub(), ".", diff="x", issue="i",
            repro=json.dumps({"path": "t", "code": "assert x"}),
        )
        self.assertIn(self.LABEL, prompts[0])

    def test_a_reproducible_cycle_still_shows_the_reviewer_the_test(self):
        prompt = self._review_prompt(json.dumps({"code": "expect(x).toBe(1)"}))
        self.assertIn(self.LABEL, prompt)
        self.assertIn("expect(x).toBe(1)", prompt)

    def test_a_reproducible_cycle_still_shows_the_fixer_the_test(self):
        prompt = self._fix_prompt(json.dumps({"code": "expect(x).toBe(1)"}))
        self.assertIn(self.LABEL, prompt)
        self.assertIn("expect(x).toBe(1)", prompt)

    def test_a_non_reproducible_cycle_gives_the_reviewer_no_repro_heading(self):
        self.assertNotIn(self.LABEL, self._review_prompt("{}"))

    def test_a_non_reproducible_cycle_gives_the_fixer_no_repro_heading(self):
        self.assertNotIn(self.LABEL, self._fix_prompt("{}"))

    def test_an_empty_code_field_is_not_a_repro_either(self):
        # Same rule as the frozen path above: the presence of the key is not
        # the question, the presence of runnable code is.
        self.assertNotIn(self.LABEL, self._review_prompt(json.dumps({"code": ""})))

    def test_malformed_repro_json_shows_the_reviewer_nothing(self):
        self.assertNotIn(self.LABEL, self._review_prompt("not json at all"))


class RunFixWithoutAnIssueNumber(unittest.TestCase):
    """The guard that keeps a pathless fix cycle from naming a frozen file.

    `run_fix` computes the frozen path as `repro_path(issue_number) if
    issue_number and has_repro else ""`. Removing only the `issue_number` half
    left the suite green, and the failure it guards is quiet: with no issue
    number the pattern substitutes to `tests/repro-issue-.test.ts`, a path that
    names an issue that does not exist, and Fix is told to treat it as frozen.
    """

    def test_no_issue_number_means_no_frozen_section(self):
        prompts = []

        class Stub:
            def run(self, prompt, role, cwd):
                prompts.append(prompt)
                return '```json\n{"summary": "s", "files_changed": []}\n```'

        payload = {"reproducible": True,
                   "repro_test": {"path": "t", "code": "assert x"}}
        with mock.patch.dict(os.environ, {"SHL_REPRO_PATH": "tests/repro-issue-{}.py"}):
            loop.run_fix(
                Stub(), ".",
                issue=json.dumps(payload),
                repro=loop.frozen_repro(payload),
                signal="",
            )
        # The HEADING, not the phrase — fix.md's standing prose says "frozen".
        self.assertNotIn("### FROZEN", prompts[0])
        self.assertNotIn("repro-issue-", prompts[0])



class TheAttemptCapDefaultIsBehavioural(unittest.TestCase):
    """The README states the cap as the literal 2; the shipped default must be
    that number, pinned by behaviour rather than by agreeing with a doc."""

    def _runner(self, bodies):
        def runner(argv):
            return json.dumps(bodies)
        return runner

    def test_two_recorded_attempts_block_a_third(self):
        comments = {"comments": [{"body": "fix attempt 1 failed"},
                                 {"body": "fix attempt 2 failed"}]}
        self.assertFalse(
            under_attempt_cap(5, gh_runner=self._runner(comments))
        )

    def test_one_recorded_attempt_still_allows(self):
        comments = {"comments": [{"body": "fix attempt 1 failed"}]}
        self.assertTrue(
            under_attempt_cap(5, gh_runner=self._runner(comments))
        )


class TheDiagnosePromptCarriesItsWholeContext(unittest.TestCase):
    """Diagnose is the role the render pin skipped: Fix and Review had one, it did not.

    `run_diagnose` hands the template three values — the log, the repro-path
    pattern the workflow composes the frozen file from, and any recalled
    incidents. Dropping `repro_path` from the context dict left every test
    green, because nothing rendered Diagnose's prompt at all; the template then
    points at a section ("shown in your context under *Reproducing test will be
    written to*") that is not there, and Diagnose writes the test to a path of
    its own choosing on the one cycle where the placement matters.

    Pinned by RENDERING, and against the label list in `role.py` so a fourth
    context value appears here by failing, not by being forgotten.
    """

    def _render(self, **env_overrides):
        prompts = []

        class Stub:
            def run(self, prompt, role, cwd):
                prompts.append((prompt, role))
                return '```json\n{"issue_title": "t", "issue_body": "b",\n "reproducible": false, "confidence": "low"}\n```'

        env = {"SHL_REPRO_PATH": "tests/test_issue_{}.py", **env_overrides}
        with mock.patch.dict(os.environ, env):
            loop.run_diagnose(Stub(), ".", log="Traceback: ValueError at app/x.py:12")
        return prompts[0]

    # The three values run_diagnose supplies; every other key in
    # role._CONTEXT_LABELS belongs to Fix or Review, and asserting their labels
    # here would pass on Diagnose's prompt forever.
    DIAGNOSE_CONTEXT = ("log", "repro_path", "incident_memory")

    def test_every_context_value_is_rendered_under_its_label(self):
        """`incident_memory` is absent-by-design when nothing matches, so its
        label is pinned with the recall stubbed to a hit: the wiring under test
        is context value -> rendered section, and that only shows on a match."""
        from role import _CONTEXT_LABELS

        prompt, role = self._render()
        self.assertEqual(role.value, "diagnose")
        for key in ("log", "repro_path"):
            with self.subTest(key=key):
                self.assertIn(_CONTEXT_LABELS[key], prompt)
        with mock.patch.object(loop, "recall_incidents", return_value="- #1 (merged): prior"):
            with_memory, _ = self._render()
        self.assertIn(_CONTEXT_LABELS["incident_memory"], with_memory)
        self.assertIn("- #1 (merged): prior", with_memory)

    def test_the_repro_pattern_is_composed_not_literal(self):
        # The literal `{}` must reach the prompt: it is the placeholder the
        # workflow substitutes the issue number into, and Diagnose works out
        # the file's imports from where that path lands.
        prompt, _ = self._render()
        self.assertIn("tests/test_issue_{}.py", prompt)


class TheDashInvocationWorksThroughMain(unittest.TestCase):
    """`main()` is the seam that crashed, so `main()` is what these drive.

    `heal.yml` pipes the diff and passes `-`. The crash was in main's dispatch
    reaching for `Path("-")`, and testing the helper it calls cannot see that:
    reinstating the exact shipped defect leaves a helper-only suite fully green.

    The rule this encodes, which is cheaper than remembering it: **every seam
    the workflow crosses gets one test that crosses it the same way.** For this
    module that is argv and stdin; `guardrails.cli` has its own, invoked as a
    module the way the workflow invokes it.
    """

    def setUp(self):
        self.captured = {}

        def fake_agent(*a, **k):
            return object()

        def fake_diagnose(agent, repo, log):
            self.captured["log"] = log
            return {"issue_title": "t", "issue_body": "b", "reproducible": False,
                    "confidence": "low"}

        def fake_review(agent, repo, diff, issue="", repro=""):
            self.captured["diff"] = diff
            return {"approved": True, "reason": "ok"}

        for name, repl in (
            ("build_agent_from_env", fake_agent),
            ("run_diagnose", fake_diagnose),
            ("run_review", fake_review),
            ("cycle_dir", lambda repo, new=False: Path(self.tmp)),
            ("record_artifact", lambda *a, **k: None),
            ("record_json", lambda *a, **k: None),
        ):
            patcher = mock.patch.object(loop, name, repl)
            patcher.start()
            self.addCleanup(patcher.stop)
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)
        # `review` reads Diagnose's output from the CURRENT DIRECTORY, which is
        # an unwritten contract with heal.yml: the workflow runs every step from
        # the repo root, so `diagnose.json` sits there. Running the command from
        # anywhere else raises FileNotFoundError before the agent is reached.
        original = os.getcwd()
        self.addCleanup(os.chdir, original)
        os.chdir(self.tmp)
        Path("diagnose.json").write_text(
            json.dumps({"issue_body": "parse() drops attachments", "repro_test": {"code": "x"}}),
            encoding="utf-8",
        )

    def _run(self, argv, piped):
        with mock.patch("sys.stdin", io.StringIO(piped)), \
             mock.patch("sys.stdout", io.StringIO()):
            return loop.main(argv)

    def test_review_dash_pipes_the_diff_through_to_the_reviewer(self):
        rc = self._run(["review", "-"], "+ the actual diff\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.captured["diff"], "+ the actual diff\n")

    def test_diagnose_dash_pipes_the_log_through_to_diagnose(self):
        rc = self._run(["diagnose", "-"], "TypeError: boom\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.captured["log"], "TypeError: boom\n")

    def test_review_still_accepts_a_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "fix.diff"
            p.write_text("+ from disk\n", encoding="utf-8")
            rc = self._run(["review", str(p)], "should not be read")
        self.assertEqual(rc, 0)
        self.assertEqual(self.captured["diff"], "+ from disk\n")


class TheReproThePromptsSeeIsTheOneTheWorkflowActedOn(unittest.TestCase):
    """`.reproducible` decides what the workflow does; `.code` decided what Fix saw.

    `heal.yml` writes the repro file, freezes it and passes `--frozen` to the
    gate on `jq -r .reproducible` alone. `_has_repro` read `repro_test.code`
    instead, and `role.validate_contract` constrains only the forward direction
    — `reproducible: true` requires a `repro_test` — so a payload carrying
    `reproducible: false` WITH populated code is accepted and the two disagree.

    Fix is then handed `### FROZEN reproducing test` naming a path nobody wrote,
    over a template that tells it the file is "already written… already proven
    red on the broken code". Review gets the same phantom. Verified by rendering
    the real prompt, not by reading the code.

    This is the defect the absent-repro fix was written for, surviving through
    the other field. One derivation now, in `frozen_repro`.
    """

    POPULATED = {"path": "tests/repro.test.ts", "code": "test('x',()=>{expect(f()).toBe(1)})"}

    def test_code_without_the_flag_is_not_a_repro(self):
        self.assertEqual(
            loop.frozen_repro({"reproducible": False, "repro_test": self.POPULATED}), "{}",
            "the workflow wrote no frozen file for this payload, so nothing "
            "downstream may act as though it did",
        )

    def test_a_missing_flag_is_not_a_repro_either(self):
        self.assertEqual(loop.frozen_repro({"repro_test": self.POPULATED}), "{}")

    def test_a_quoted_true_is_not_true(self):
        # `validate_contract` already refuses this, and the gate learned the
        # same lesson from `jq -e` treating the string "false" as truthy. The
        # derivation must not be the one place a quoted flag gets through.
        self.assertEqual(
            loop.frozen_repro({"reproducible": "true", "repro_test": self.POPULATED}), "{}"
        )

    def test_a_genuine_repro_survives(self):
        carried = loop.frozen_repro({"reproducible": True, "repro_test": self.POPULATED})
        self.assertIn("expect(f()).toBe(1)", carried)

    def test_fix_is_told_of_no_frozen_test_when_the_flag_is_false(self):
        prompts = []

        class Stub:
            def run(self, prompt, role, cwd):
                prompts.append(prompt)
                return '```json\n{"summary": "s", "files_changed": []}\n```'

        with mock.patch.dict(os.environ, {"SHL_REPRO_PATH": "tests/repro-issue-{}.test.ts"}):
            loop.run_fix(
                Stub(), ".",
                issue=json.dumps({"reproducible": False}),
                repro=loop.frozen_repro({"reproducible": False, "repro_test": self.POPULATED}),
                issue_number=41,
            )
        # The HEADING, not the phrase. `fix.md` explains what a frozen test is
        # in its standing prose, so searching the whole prompt for the words
        # matches the template every time and can never fail.
        self.assertNotIn("### FROZEN", prompts[0])
        self.assertNotIn("repro-issue-41", prompts[0])

    def test_no_call_site_derives_the_repro_for_itself(self):
        # The membership is computed, not listed. Two call sites each spelled
        # this out and it only had to drift in one of them; a third would have
        # inherited whichever version it was copied from.
        source = (Path(loop.__file__)).read_text(encoding="utf-8")
        # The derivation itself, not the words. `frozen_repro`'s own docstring
        # quotes the old expression to explain what it replaced, and a looser
        # scan flagged that prose while a real second call site would have read
        # identically.
        derivations = [
            line.strip()
            for line in source.splitlines()
            if 'json.dumps(diagnose.get("repro_test"' in line
        ]
        self.assertEqual(
            len(derivations), 1,
            f"the repro is derived in {len(derivations)} places; one of them will "
            f"drift and only one of them decides what Fix is told: {derivations}",
        )
        self.assertTrue(
            derivations[0].startswith("return"),
            f"the single derivation is not `frozen_repro`'s return, so a caller "
            f"is still doing this for itself: {derivations[0]}",
        )


if __name__ == "__main__":
    unittest.main()
