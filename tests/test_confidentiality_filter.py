import re
import os
import unittest
from pathlib import Path
from unittest import mock

from guardrails.confidentiality_filter import SECRET_ENV_VARS, scrub


class SecretsInTheEnvironmentAreRedactedByValue(unittest.TestCase):
    """Pattern-matching cannot be the only defence for the loop's own tokens.

    The defaults recognise SHAPES: `sk-…`, `gh?_…`, `AKIA…`, JWT, `bearer …`.
    But this framework's documented primary path is third-party providers, and
    a third-party key that is bare hex, or base64, or carries a vendor prefix
    nobody listed, matches none of them.

    That matters because the scrubber is the last thing between an agent's text
    and a GitHub issue body, and between raw suite output and an evidence
    artifact anyone with repo read access can download. An agent that has been
    prompt-injected into echoing its own token produces exactly this text.

    Redacting the literal values the process is holding is the one rule that
    cannot be evaded by format. The regexes stay as the fallback for secrets
    this process does NOT hold.
    """

    def setUp(self):
        self.prior = {v: os.environ.get(v) for v in SECRET_ENV_VARS}
        self.addCleanup(self.restore)

    def restore(self):
        for var, value in self.prior.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def test_a_provider_key_of_unrecognised_shape_is_still_redacted(self):
        # Bare hex: matches no default pattern.
        os.environ["SHL_AUTH_TOKEN"] = "9f2c1ab77d0e4c318b5a6d2e4f7c1a03"
        leaked = "traceback: auth header was 9f2c1ab77d0e4c318b5a6d2e4f7c1a03 at line 12"
        out = scrub(leaked)
        self.assertNotIn("9f2c1ab77d0e4c318b5a6d2e4f7c1a03", out)
        self.assertIn("[REDACTED]", out)

    def test_every_secret_var_the_loop_uses_is_covered(self):
        for var in SECRET_ENV_VARS:
            with self.subTest(var=var):
                os.environ[var] = f"value-of-{var}-zz"
                self.assertNotIn(f"value-of-{var}-zz", scrub(f"leaked value-of-{var}-zz here"))

    def test_an_empty_or_unset_secret_does_not_redact_everything(self):
        # A blank value must not turn into a match-everything rule; that would
        # silently destroy the evidence bundle rather than protect it.
        for var in SECRET_ENV_VARS:
            os.environ[var] = ""
        self.assertEqual(scrub("ordinary text"), "ordinary text")

class TestScrub(unittest.TestCase):
    def test_redacts_sk_key(self):
        self.assertNotIn("sk-abcd" + "EFGH0123456789xyz", scrub("key=sk-abcdEFGH0123456789xyz boom"))

    def test_redacts_bearer_token(self):
        out = scrub("Authorization: Bearer abcdefghijklmnop0123456789")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("abcdefghijklmnop0123456789", out)

    def test_redacts_key_equals_value(self):
        out = scrub("api_key=ABC123 and password: secret456")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("ABC123", out)
        # Both halves of the fixture: `password:` is a separate alternative in
        # the same rule, and only the first was ever checked.
        self.assertNotIn("secret456", out)

    def test_no_secrets_unchanged(self):
        raw = "[ERROR] KeyError: 'user' at app.py:10"
        self.assertEqual(scrub(raw), raw)

    def test_redacts_email_pii(self):
        out = scrub("contact: client.name@example.com")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("client.name@example.com", out)

    def test_operator_supplied_client_name_pattern(self):
        # Operator adds client-specific deny patterns at install time. The name
        # is two words on purpose: the pattern has to survive the whitespace and
        # casing a real log applies to it.
        extra = [("client name", re.compile(r"(?i)Northwind\s+Traders"))]
        out = scrub("Log: northwind  traders saw an error", extra_patterns=extra)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("orthwind", out)


class ClientPatternsArriveAsARepoVariable(unittest.TestCase):
    """The operator's only way to reach this function is a repo variable.

    Nine of the scrub calls in the two workflows are `guardrails.cli scrub`,
    which exposes no flag for a pattern list, and `evidence.py` passes text
    only — so `extra_patterns` was a parameter no install could ever set while
    `setup.md` told operators to use it. Reading the variable inside `scrub`
    covers every caller at once, which is the point: issue bodies and PR text
    go through here too, and turning the evidence upload off does nothing
    for either.
    """

    def _scrub_with(self, value: str, text: str) -> str:
        with mock.patch.dict(os.environ, {"SHL_EXTRA_SCRUB_PATTERNS": value}, clear=True):
            return scrub(text)

    def test_a_client_name_from_the_variable_is_redacted(self):
        out = self._scrub_with(r"(?i)Northwind\s+Traders", "Log: northwind  traders failed")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("orthwind", out)

    def test_several_patterns_one_per_line(self):
        out = self._scrub_with("Contoso\nFabrikam", "Contoso and Fabrikam both")
        self.assertNotIn("Contoso", out)
        self.assertNotIn("Fabrikam", out)

    def test_an_unusable_pattern_refuses_rather_than_scrubbing_less(self):
        # Silently skipping it would leave the operator believing their client
        # names are being removed while the text ships unredacted.
        with self.assertRaises(re.error):
            self._scrub_with("Contoso\n(unclosed", "Contoso")

    def test_an_unset_variable_changes_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(scrub("plain text"), "plain text")


class EverySecretTheLoopHandlesIsInTheSecretList(unittest.TestCase):
    """`SECRET_ENV_VARS` does two jobs, and a name missing from it fails both.

    `confidentiality_filter` redacts these by VALUE, which is the only defence
    that does not depend on a secret matching a shape pattern. And
    `adapters.hydrate_repo_vars` skips them, so a credential an operator stored
    as a plaintext repo variable is not folded into `os.environ` for every
    adapter-loading step.

    `SHL_DEPLOY_TOKEN` was absent from both. `reference/updating.md` tells the
    operator to set it with `gh variable set`, so the documented path puts a
    deploy credential in `vars`, where `SHL_VARS: ${{ toJSON(vars) }}` carries
    it into steps built to hold no deploy credential — and its value is never
    literally redacted out of evidence or issue text either.

    Derived from the workflows rather than listed here: any `SHL_*` name the
    templates read from the `secrets` context is by definition a secret.
    """

    WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"

    def _secret_context_names(self) -> set[str]:
        names = set()
        for path in sorted(self.WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            names |= set(re.findall(r"secrets\.(SHL_[A-Z_]+)", text))
        return names

    def test_the_derivation_finds_something(self):
        # Without this the comparison below passes on an empty set, which is
        # the same silence it exists to break.
        self.assertTrue(self._secret_context_names())

    def test_every_secret_the_workflows_read_is_redacted_by_value(self):
        for name in sorted(self._secret_context_names()):
            with self.subTest(secret=name):
                self.assertIn(
                    name, SECRET_ENV_VARS,
                    f"{name} is read from the secrets context but is not in "
                    "SECRET_ENV_VARS, so its value is neither redacted from "
                    "scrubbed output nor withheld from variable hydration",
                )

class ASecretInJsonIsStillASecret(unittest.TestCase):
    """Agent output quotes env and config as JSON constantly.

    The `key=value` rule could not see a quote between the key and the colon,
    so `{"api_key": "<32 hex>"}` shipped verbatim to a GitHub issue — a bare
    provider token on the scrubber's own documented primary path. Literal-value
    redaction only covers secrets THIS process happens to hold; everything else
    is shape-matched, and the shape was the gap.
    """

    CASES = (
        '{"api_key": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"}',
        '{"token":"ZZ9theQuickBrownFox11"}',
        "{'secret': 'hunter2hunter2hunter2'}",
    )

    def test_a_json_quoted_secret_is_redacted(self):
        for case in self.CASES:
            with self.subTest(case=case):
                out = scrub(case)
                self.assertIn("[REDACTED]", out)
                self.assertNotIn("a1b2c3d4e5f6", out)
                self.assertNotIn("ZZ9theQuickBrownFox11", out)
                self.assertNotIn("hunter2hunter2hunter2", out)

    def test_the_bare_form_still_works(self):
        # The widening must not cost the original shape.
        self.assertIn("[REDACTED]", scrub("api_key = plainvalue12345678"))


if __name__ == "__main__":
    unittest.main()
