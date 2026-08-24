import json
import os
import tempfile
import unittest
from pathlib import Path

from agent.base import AgentRole
from agent.harness import CLAUDE_CODE, AgentRun, ConfiguredAgent, ModelConfig
from evidence import cycle_dir, record_agent_run, record_artifact, record_json

# A token shaped like the real thing, so the scrub patterns actually fire.
_SECRET = "sk-abcdefghijklmnop0123456789"
_ENV = {"ANTHROPIC_AUTH_TOKEN": _SECRET, "ANTHROPIC_MODEL": "example-model-v1"}


def _run(stdout="```json\n{\"ok\": true}\n```", stderr="", rc=0):
    return AgentRun(stdout=stdout, stderr=stderr, returncode=rc, duration_s=1.25)


class TestCycleDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        # Restore rather than pop: leaving it unset leaks into whatever test
        # runs next in the same process, and a set-cycle-id test later in the
        # run would silently pass for the wrong reason here.
        self._cycle_id = os.environ.pop("SHL_CYCLE_ID", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("SHL_CYCLE_ID", self._cycle_id)
            if self._cycle_id is not None
            else os.environ.pop("SHL_CYCLE_ID", None)
        )

    def test_uses_env_cycle_id_when_set(self):
        # The workflow sets SHL_CYCLE_ID to the Actions run id so a cloud cycle
        # is addressable by the run that produced it.
        os.environ["SHL_CYCLE_ID"] = "998877"
        self.addCleanup(os.environ.pop, "SHL_CYCLE_ID", None)
        self.assertEqual(cycle_dir(self.repo).name, "998877")

    def test_new_cycle_is_named_for_its_utc_start_time(self):
        d = cycle_dir(self.repo, new=True)
        # YYYYMMDD-HHMMSS, and it must sort chronologically for "join the latest".
        self.assertRegex(d.name, r"^\d{8}-\d{6}$")

    def test_later_roles_join_the_current_cycle(self):
        # Diagnose starts the cycle; Fix and Review are separate processes and
        # must land in the same dir, not open their own.
        started = cycle_dir(self.repo, new=True)
        self.assertEqual(cycle_dir(self.repo).name, started.name)
        self.assertEqual(cycle_dir(self.repo).name, started.name)

    def test_a_later_new_cycle_sorts_after_an_earlier_one(self):
        # "join the latest" picks the lexicographic max, so the naming format
        # has to keep lexicographic and chronological order identical.
        (self.repo / ".shl" / "evidence" / "20200101-000000").mkdir(parents=True)
        newer = cycle_dir(self.repo, new=True)
        self.assertEqual(cycle_dir(self.repo).name, newer.name)

    def test_lands_under_the_installed_loop(self):
        d = cycle_dir(self.repo)
        self.assertEqual(d.parent, self.repo / ".shl" / "evidence")
        self.assertTrue(d.is_dir())


class TestRecordAgentRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _record(self, prompt="diagnose the failure", run=None, argv=None):
        record_agent_run(
            self.dir,
            AgentRole.DIAGNOSE,
            prompt,
            argv or ["claude", "-p", prompt, "--permission-mode", "dontAsk"],
            _ENV,
            Path("/repo/.shl"),
            run or _run(),
        )

    def test_writes_prompt_raw_and_meta(self):
        self._record()
        for name in ("diagnose.prompt.txt", "diagnose.raw.txt", "diagnose.meta.json"):
            self.assertTrue((self.dir / name).exists(), name)

    def test_stderr_file_only_when_there_is_stderr(self):
        self._record()
        self.assertFalse((self.dir / "diagnose.stderr.txt").exists())
        self._record(run=_run(stderr="auth failed", rc=1))
        self.assertEqual((self.dir / "diagnose.stderr.txt").read_text(), "auth failed")

    def test_env_values_are_never_written(self):
        # The env dict carries the provider token. Evidence records key NAMES so
        # a cycle is reproducible, never the values.
        self._record()
        for f in self.dir.iterdir():
            self.assertNotIn(_SECRET, f.read_text(), f.name)
        meta = json.loads((self.dir / "diagnose.meta.json").read_text())
        self.assertEqual(meta["env_keys"], ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"])

    def test_prompt_is_not_duplicated_into_meta(self):
        # The prompt rides in argv. Storing it twice doubles the disclosure
        # surface, so meta keeps only its size.
        self._record(prompt="a very distinctive prompt body")
        meta = (self.dir / "diagnose.meta.json").read_text()
        self.assertNotIn("a very distinctive prompt body", meta)
        self.assertIn("<prompt: 30 chars>", meta)

    def test_secrets_in_prompt_and_stdout_are_scrubbed(self):
        # A failure log can carry a leaked key straight into the prompt, and the
        # agent can echo it back. Both paths are redacted.
        self._record(prompt=f"log said {_SECRET}", run=_run(stdout=f"I saw {_SECRET}"))
        self.assertNotIn(_SECRET, (self.dir / "diagnose.prompt.txt").read_text())
        self.assertNotIn(_SECRET, (self.dir / "diagnose.raw.txt").read_text())
        self.assertIn("[REDACTED]", (self.dir / "diagnose.raw.txt").read_text())

    def test_meta_records_exit_code_and_duration(self):
        # Without these, a crashed agent looks identical to one that returned
        # no json block.
        self._record(run=_run(stdout="", stderr="boom", rc=127))
        meta = json.loads((self.dir / "diagnose.meta.json").read_text())
        self.assertEqual(meta["returncode"], 127)
        self.assertEqual(meta["duration_s"], 1.25)


class TestRecordArtifacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_artifact_is_scrubbed(self):
        record_artifact(self.dir, "signal.txt", f"ERROR key={_SECRET}")
        self.assertNotIn(_SECRET, (self.dir / "signal.txt").read_text())

    def test_json_artifact_stays_valid_json_after_scrubbing(self):
        # Scrubbing raw JSON text can eat a closing quote (the key=value rule is
        # greedy) and corrupt the file, so json is scrubbed value-by-value.
        record_json(
            self.dir,
            "diagnose.json",
            {"issue_body": f"token={_SECRET}", "reproducible": True, "steps": [_SECRET]},
        )
        payload = json.loads((self.dir / "diagnose.json").read_text())
        self.assertTrue(payload["reproducible"])
        self.assertNotIn(_SECRET, json.dumps(payload))
        self.assertIn("[REDACTED]", payload["steps"][0])


class TestAgentWritesEvidence(unittest.TestCase):
    """The seam that matters: a real ConfiguredAgent leaves a bundle behind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _agent(self, evidence_dir):
        def runner(argv, env, cwd):
            return AgentRun(
                stdout='```json\n{"ok": true}\n```', stderr="warning", returncode=0
            )

        return ConfiguredAgent(
            CLAUDE_CODE,
            ModelConfig(model="example-model-v1", auth_token=_SECRET),
            runner=runner,
            evidence_dir=evidence_dir,
        )

    def test_run_writes_the_bundle_and_still_returns_stdout(self):
        out = self._agent(self.dir).run("find it", AgentRole.DIAGNOSE, Path("/repo"))
        self.assertIn('{"ok": true}', out)  # contract unchanged for callers
        for name in (
            "diagnose.prompt.txt",
            "diagnose.raw.txt",
            "diagnose.stderr.txt",
            "diagnose.meta.json",
        ):
            self.assertTrue((self.dir / name).exists(), name)

    def test_provider_token_never_reaches_disk(self):
        # ModelConfig.auth_token becomes an env value inside run(); no evidence
        # file may contain it.
        self._agent(self.dir).run("find it", AgentRole.DIAGNOSE, Path("/repo"))
        for f in self.dir.iterdir():
            self.assertNotIn(_SECRET, f.read_text(), f.name)

    def test_no_evidence_dir_means_the_run_still_returns_its_output(self):
        # The temp dir is never handed to the agent in this configuration, so
        # asserting it stays empty holds by construction. What matters is that
        # a local run with no bundle still WORKS — recording is optional, and
        # the guard that makes it optional is what this covers.
        agent = self._agent(None)
        out = agent.run("find it", AgentRole.DIAGNOSE, Path("/repo"))
        self.assertIn("ok", out)


if __name__ == "__main__":
    unittest.main()
