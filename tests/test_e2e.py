"""End-to-end wiring test — the loop composes, and the safety property holds.

A real-agent e2e needs a harness + model + runner and is out of reach offline.
This proves what DOES compose without those: run_watch -> run_diagnose ->
run_fix -> run_review against a fake adapter + a fake agent that returns
role-appropriate structured output. Contract drift between stages — a field a
stage omits that the next one needs — surfaces here even with fakes, because
validate_contract runs at every stage. The gate's must-pass property is owned
by test_gate (the predicate) and test_cli (the CLI seam the workflow calls).
"""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from agent.harness import AgentRun
from loop import build_agent_from_env, run_diagnose, run_fix, run_review, run_watch


class _FakeAdapter:
    def __init__(self, log):
        self._log = log

    def read_log(self):
        return self._log


def _fake_agent_runner():
    """Returns role-appropriate JSON by inspecting argv (prompt + role flags)."""

    def runner(argv, env, cwd):
        prompt = " ".join(argv)
        if "acceptEdits" in argv:  # FIX role
            return AgentRun(
                stdout='```json\n{"summary":"guard against missing key",'
                '"files_changed":["app.py"],"tests_added":["tests/test_app.py"]}\n```'
            )
        if "# Review agent" in prompt:  # REVIEW role
            return AgentRun(
                stdout='```json\n{"approved": true, "reason": "addresses the root cause"}\n```'
            )
        # DIAGNOSE role (read-only, plan mode).
        return AgentRun(
            stdout='```json\n{"issue_title":"KeyError on missing key",'
            '"issue_body":"db[\\\"user\\\"] raises when key absent",'
            '"reproducible": true, "confidence": "high",'
            '"repro_test":{"path":"tests/test_repro.py","code":"def test_r(): assert False"}}\n```'
        )

    return runner


_ENV = {
    "SHL_HARNESS": "claude-code",
    "SHL_MODEL": "example-model-v1",
    "SHL_AUTH_TOKEN": "sk-test-key",
}


class TestEndToEndWiring(unittest.TestCase):
    def setUp(self):
        self.agent = build_agent_from_env(_ENV, runner=_fake_agent_runner())
        self.repo = Path("/repo")
        # Every real cycle has this set — heal.yml exports it at job level, and
        # Phase 1 discovers it per target. Stated here rather than inherited
        # from a default, because there is none: no repro-test path is correct
        # across languages, so an unset one raises instead of guessing pytest.
        self._prior = os.environ.get("SHL_REPRO_PATH")
        os.environ["SHL_REPRO_PATH"] = "tests/test_repro_issue_{}.py"
        self.addCleanup(self._restore_repro_path)

    def _restore_repro_path(self):
        if self._prior is None:
            os.environ.pop("SHL_REPRO_PATH", None)
        else:
            os.environ["SHL_REPRO_PATH"] = self._prior

    def test_full_chain_produces_contract_valid_output(self):
        # Watch: error in the log becomes a non-empty signal.
        signal = run_watch(_FakeAdapter("[INFO] boot\n[ERROR] KeyError: 'user'\n"))
        self.assertIn("KeyError", signal)

        # Diagnose: reads the signal, returns a valid issue.
        diag = run_diagnose(self.agent, self.repo, signal)
        self.assertEqual(diag["issue_title"], "KeyError on missing key")
        self.assertTrue(diag["reproducible"])

        # Fix: gets the issue, returns a valid fix.
        fix = run_fix(self.agent, self.repo, issue=diag["issue_body"], repro="")
        self.assertIn("app.py", fix["files_changed"])

        # Review: judges a diff, approves.
        rev = run_review(self.agent, self.repo, diff="+ git diff main...fix/issue-1")
        self.assertTrue(rev["approved"])

class ContractDriftBetweenStagesSurfacesHere(unittest.TestCase):
    """The chain's own claim: a stage that omits a required field stops it.

    The happy path above cannot show this, because the fake always returns
    valid payloads — so it reads back the fake's literals and `validate_contract`
    is never the thing under test. One stage returning an incomplete answer is
    what a real model does, and it is the drift the module docstring says this
    file catches.
    """

    def setUp(self):
        # Every real cycle has this set; run_diagnose raises without it.
        patcher = mock.patch.dict(os.environ, {"SHL_REPRO_PATH": "tests/test_repro_{}.py"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _agent_omitting(self, field: str):
        full = {
            "issue_title": "KeyError on missing key",
            "issue_body": "db[\"user\"] raises when key absent",
            "reproducible": False,
            "confidence": "high",
        }
        full.pop(field)

        def runner(argv, env, cwd):
            return AgentRun(stdout="```json\n" + json.dumps(full) + "\n```")

        return build_agent_from_env(_ENV, runner=runner)

    def test_a_missing_required_field_stops_the_chain(self):
        for field in ("issue_body", "reproducible", "confidence"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as caught:
                    run_diagnose(self._agent_omitting(field), Path("/repo"), "boom")
                self.assertIn(field, str(caught.exception))


if __name__ == "__main__":
    unittest.main()