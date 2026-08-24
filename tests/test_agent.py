import unittest

from agent.base import extract_structured


class TestExtractStructured(unittest.TestCase):
    def test_extracts_json_block(self):
        out = 'reasoning...\n```json\n{"approved": true}\n```\n'
        self.assertEqual(extract_structured(out), {"approved": True})

    def test_takes_last_block_when_multiple(self):
        out = '```json\n{"a": 1}\n```\nmore\n```json\n{"a": 2}\n```'
        self.assertEqual(extract_structured(out), {"a": 2})

    def test_untagged_fence_still_parses(self):
        out = "```\n{\"ok\": true}\n```"
        self.assertEqual(extract_structured(out), {"ok": True})

    def test_raises_when_no_block(self):
        with self.assertRaises(ValueError):
            extract_structured("just prose, no fenced block")

    def test_raises_on_unparseable_json(self):
        with self.assertRaises(ValueError):
            extract_structured("```json\n{not valid}\n```")


class ADiagnoseAnswerMayContainAFence(unittest.TestCase):
    """`repro_test.code` is SOURCE, and source can contain a fence.

    A non-greedy `(.*?)```  stops at the first fence INSIDE the JSON string, so
    no candidate block parses and a structurally valid answer raises. Not
    exotic: any repro test for a markdown renderer, a docs tool or a
    prompt-handling path plausibly has one in a fixture.

    The failure lands upstream of issue filing, so no issue exists, no
    fingerprint marker is written and no attempt is recorded — the next cron
    tick re-diagnoses the same failure and spends another agent call, with no
    per-invocation cost cap to stop it.

    JSON forbids a literal newline inside a string, so a fence carried in a
    value is always mid-line while a real closing fence is alone on its line.
    That is the discriminator, and it is why scanning lines works where a
    character-wise regex cannot.
    """

    def test_a_repro_test_containing_a_fence_still_parses(self):
        import json

        payload = {
            "reproducible": True,
            "repro_test": {
                "path": "tests/test_render.py",
                "code": "def test_fence():\n    assert render('```js\\nx\\n```') != ''\n",
            },
        }
        out = "Here is my analysis.\n\n```json\n" + json.dumps(payload) + "\n```\n"
        self.assertEqual(extract_structured(out)["repro_test"]["path"], "tests/test_render.py")

    def test_an_indented_closing_fence_still_closes(self):
        out = "```json\n{\"ok\": true}\n  ```\n"
        self.assertEqual(extract_structured(out), {"ok": True})

    def test_the_last_parseable_block_still_wins(self):
        # The scan order is load-bearing: trailing prose after the answer is
        # tolerated, and an earlier draft must not beat the final block.
        out = '```json\n{"a": 1}\n```\nthinking again\n```json\n{"a": 2}\n```\nokay\n'
        self.assertEqual(extract_structured(out), {"a": 2})


if __name__ == "__main__":
    unittest.main()