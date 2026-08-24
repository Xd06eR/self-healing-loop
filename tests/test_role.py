import unittest
from pathlib import Path

from agent.base import AgentRole
from agent.harness import CLAUDE_CODE, AgentRun, ConfiguredAgent, ModelConfig
from role import build_prompt, run_role, validate_contract


def _agent_returning(json_block, calls=None):
    def runner(argv, env, cwd):
        if calls is not None:
            calls.append((list(argv), dict(env), cwd))
        return AgentRun(stdout=json_block)

    return ConfiguredAgent(
        CLAUDE_CODE,
        ModelConfig(model="example-model-v1", auth_token="k"),
        runner=runner,
    )


class TestBuildPrompt(unittest.TestCase):
    def test_includes_template_text_and_context(self):
        prompt = build_prompt(AgentRole.DIAGNOSE, {"log": "[ERROR] boom"})
        self.assertIn("# Diagnose agent", prompt)
        self.assertIn("[ERROR] boom", prompt)
        self.assertIn("## Context", prompt)

    def test_only_provided_context_keys_appear(self):
        prompt = build_prompt(AgentRole.DIAGNOSE, {"log": "ERR"})
        self.assertIn("ERR", prompt)
        self.assertNotIn("Diff to review", prompt)

    def test_review_prompt_includes_diff_label(self):
        prompt = build_prompt(AgentRole.REVIEW, {"diff": "+ bad"})
        self.assertIn("Diff to review", prompt)

    def test_review_prompt_carries_the_issue_and_the_repro(self):
        # A senior reviewing a PR reads the linked issue. Given the diff alone,
        # Review can judge whether the code looks sound but not whether it
        # fixes the failure that was actually reported.
        prompt = build_prompt(
            AgentRole.REVIEW,
            {"diff": "+ fixed", "issue": "parse() drops attachments", "repro": "expect(...)"},
        )
        self.assertIn("Diff to review", prompt)
        self.assertIn("parse() drops attachments", prompt)
        self.assertIn("expect(...)", prompt)

    def test_diagnose_prompt_carries_the_repro_path_pattern(self):
        # Diagnose does not choose the path, but it still has to write correct
        # relative imports — which depend on the directory the file will land
        # in. The issue number does not exist yet at this point, so the agent
        # is given the PATTERN.
        prompt = build_prompt(
            AgentRole.DIAGNOSE,
            {"log": "ERR", "repro_path": "tests/repro-issue-{}.test.ts"},
        )
        self.assertIn("tests/repro-issue-{}.test.ts", prompt)
        self.assertIn("Reproducing test will be written to", prompt)


class TestValidateContract(unittest.TestCase):
    def test_diagnose_ok(self):
        validate_contract(
            AgentRole.DIAGNOSE,
            {"issue_title": "t", "issue_body": "b", "reproducible": True, "confidence": "high",
             "repro_test": {"path": "tests/test_repro.py", "code": "def test_r(): assert False"}},
        )

    def test_diagnose_reproducible_without_repro_test_rejected(self):
        with self.assertRaises(ValueError):
            validate_contract(
                AgentRole.DIAGNOSE,
                {"issue_title": "t", "issue_body": "b", "reproducible": True, "confidence": "high"},
            )

    def test_diagnose_not_reproducible_ok_without_repro_test(self):
        validate_contract(
            AgentRole.DIAGNOSE,
            {"issue_title": "t", "issue_body": "b", "reproducible": False, "confidence": "high"},
        )

    def test_diagnose_missing_field_raises(self):
        with self.assertRaises(ValueError) as cm:
            validate_contract(AgentRole.DIAGNOSE, {"issue_title": "t"})
        self.assertIn("issue_body", str(cm.exception))

    def test_reproducible_needs_only_code_no_path(self):
        # N1 is answered by the workflow composing the path from the issue
        # number, so Diagnose supplies code alone. Validating an agent-supplied
        # path against `tests/test_*.py` would answer it too, but that regex is
        # a pytest convention: every non-Python target dies there and no cycle
        # can complete.
        validate_contract(
            AgentRole.DIAGNOSE,
            {
                "issue_title": "t", "issue_body": "b",
                "reproducible": True, "confidence": "high",
                "repro_test": {"code": "it('reproduces', () => { expect(f()).toBe(1) })"},
            },
        )

    def test_reproducible_without_code_is_still_rejected(self):
        for repro in ({}, {"code": ""}, {"path": "tests/test_x.py"}, "not-a-dict"):
            with self.assertRaises(ValueError, msg=repr(repro)):
                validate_contract(
                    AgentRole.DIAGNOSE,
                    {
                        "issue_title": "t", "issue_body": "b",
                        "reproducible": True, "confidence": "high",
                        "repro_test": repro,
                    },
                )

    def test_fix_requires_summary_and_files(self):
        validate_contract(AgentRole.FIX, {"summary": "s", "files_changed": ["a.py"]})
        with self.assertRaises(ValueError):
            validate_contract(AgentRole.FIX, {"summary": "s"})

    def test_review_requires_approved_and_reason(self):
        validate_contract(AgentRole.REVIEW, {"approved": True, "reason": "r"})
        with self.assertRaises(ValueError):
            validate_contract(AgentRole.REVIEW, {"approved": True})


class TestRunRole(unittest.TestCase):
    def test_parses_and_validates_diagnose_output(self):
        calls = []
        block = (
            '```json\n{"issue_title":"t","issue_body":"b",'
            '"reproducible":true,"confidence":"high",'
            '"repro_test":{"path":"tests/test_repro.py","code":"def test_r(): assert False"}}\n```'
        )
        agent = _agent_returning(block, calls)
        payload = run_role(
            AgentRole.DIAGNOSE,
            {"log": "[ERROR] boom"},
            agent,
            Path("/repo"),
        )
        self.assertEqual(payload["issue_title"], "t")
        argv, env, cwd = calls[0]
        self.assertEqual(cwd, Path("/repo"))
        self.assertIn("dontAsk", argv)  # DIAGNOSE role restriction applied
        # The prompt is one argv element; context is injected into it.
        self.assertIn("[ERROR] boom", " ".join(argv))
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "k")

    def test_invalid_output_raises(self):
        block = '```json\n{"issue_title":"t"}\n```'  # missing fields
        agent = _agent_returning(block)
        with self.assertRaises(ValueError):
            run_role(AgentRole.DIAGNOSE, {"log": "x"}, agent, Path("/repo"))

    def test_unparseable_output_raises(self):
        agent = _agent_returning("no json block at all")
        with self.assertRaises(ValueError):
            run_role(AgentRole.REVIEW, {"diff": "x"}, agent, Path("/repo"))


class ReproducibleMustBeABoolean(unittest.TestCase):
    """The workflow and this check must agree on the type, or the RED proof voids.

    `heal.yml` branches on `jq -r .reproducible` compared against the string
    "true", which the JSON string `"true"` satisfies. If validation accepts that
    string without demanding `repro_test`, the Red step runs `jq -r
    .repro_test.code` on a missing key, writes jq's literal `null` as the frozen
    reproducing test, and that file fails for reasons unrelated to the bug — so
    `red=true` is set on a meaningless proof and the escalation that catches a
    bad repro spec never fires.
    """

    def _payload(self, reproducible):
        return {
            "issue_title": "t", "issue_body": "b", "confidence": "high",
            "reproducible": reproducible,
        }

    def test_a_string_true_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_contract(AgentRole.DIAGNOSE, self._payload("true"))

    def test_a_non_reproducible_finding_needs_no_repro_test(self):
        validate_contract(AgentRole.DIAGNOSE, self._payload(False))


class ApprovedMustBeABoolean(unittest.TestCase):
    """`approved` decides whether agent-written code reaches the default branch.

    `heal.yml` consumes it with `jq -e '.approved'`, which exits 0 for anything
    that is not JSON `false` or `null` — so the strings "false" and "no", the
    number 0, an empty string and an empty list all read as approval, and Merge,
    Deploy, Verify and Record are all gated on that one output. A model emitting
    a quoted boolean is an ordinary formatting slip, not an attack.

    `reproducible` is guarded against exactly this one field over. The asymmetry
    was the defect: the lesser field, which decides whether a test gets written,
    was type-checked, and the one that decides whether a change ships was not.
    """

    def _payload(self, approved):
        return {"approved": approved, "reason": "checked the diff"}

    def test_a_string_false_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_contract(AgentRole.REVIEW, self._payload("false"))

    def test_a_string_true_is_rejected_too(self):
        # Not merely "reject the values that merge wrongly": a string "true"
        # means the model is not emitting booleans, and the next cycle's
        # "false" would merge. Reject the type, not the value.
        with self.assertRaises(ValueError):
            validate_contract(AgentRole.REVIEW, self._payload("true"))

    def test_the_values_jq_would_have_merged_are_rejected(self):
        for value in (0, "", [], "no", {}):
            with self.subTest(approved=value):
                with self.assertRaises(ValueError):
                    validate_contract(AgentRole.REVIEW, self._payload(value))

    def test_real_booleans_pass_both_ways(self):
        validate_contract(AgentRole.REVIEW, self._payload(True))
        validate_contract(AgentRole.REVIEW, self._payload(False))


class AnUnlabelledContextKeyIsAMistakeNotASilentDrop(unittest.TestCase):
    """`build_prompt` iterates the LABEL table, not the context it was handed.

    So a role gaining a context key and not gaining its label produces a prompt
    missing that context, with no error and no symptom anywhere: the agent just
    reasons without it and returns a worse answer that still validates. The
    context a role is given is the whole of what it can know, so a key going
    missing is not a formatting slip.
    """

    def test_a_key_with_no_label_raises(self):
        with self.assertRaises(KeyError) as caught:
            build_prompt(AgentRole.FIX, {"issue": "x", "sourcemap": "y"})
        self.assertIn("sourcemap", str(caught.exception))

    def test_every_key_the_loop_passes_is_labelled(self):
        # The three real call sites, so the guard above cannot be satisfied by
        # a caller that never runs.
        build_prompt(AgentRole.DIAGNOSE,
                     {"log": "e", "repro_path": "p", "incident_memory": "m"})
        build_prompt(AgentRole.FIX,
                     {"issue": "i", "repro": "r", "frozen": "f", "incident_memory": "m"})
        build_prompt(AgentRole.REVIEW, {"diff": "d", "issue": "i", "repro": "r"})


if __name__ == "__main__":
    unittest.main()
