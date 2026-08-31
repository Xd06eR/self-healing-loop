import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters import hydrate_repo_vars, optional_ids_fn


class AnAdapterSeesEveryVariableTheOperatorSet(unittest.TestCase):
    """A repo variable is invisible to a step that does not name it in `env:`.

    An adapter may read any `SHL_*` name it likes, but the workflow templates
    can only name the ones the framework knows about. So an adapter that needs
    a project id, a region or an org reads `None` at runtime while the variable
    sits correctly set in the repo, and the failure is quiet: `read_log` raises
    on every tick, or `health_check` returns None and the loop verifies nothing.

    Re-vendoring makes it worse. Hand-added `env:` lines are wiped by a fresh
    copy of the template, so an install that worked goes silent with no error
    and no diff anyone reads. Passing the whole `vars` context as one JSON blob
    removes both failures instead of detecting them: the template never needs
    editing, so there is nothing for a re-vendor to drop.
    """

    def test_a_variable_the_template_never_names_still_arrives(self):
        blob = json.dumps({"SHL_PLATFORM_PROJECT": "example-project"})
        with mock.patch.dict(os.environ, {"SHL_VARS": blob}, clear=True):
            hydrate_repo_vars()
            self.assertEqual(os.environ["SHL_PLATFORM_PROJECT"], "example-project")

    def test_an_explicit_env_entry_wins(self):
        # A step that names a variable explicitly is the more specific
        # statement, and workflow-level overrides must not be undone by the
        # bulk context.
        blob = json.dumps({"SHL_MODEL": "from-vars"})
        with mock.patch.dict(os.environ, {"SHL_VARS": blob, "SHL_MODEL": "from-env"}, clear=True):
            hydrate_repo_vars()
            self.assertEqual(os.environ["SHL_MODEL"], "from-env")

    def test_values_are_stringified(self):
        # toJSON does not promise strings, and os.environ rejects anything else
        # with a TypeError that would abort the step.
        blob = json.dumps({"SHL_RETRIES": 3, "SHL_ENABLED": True})
        with mock.patch.dict(os.environ, {"SHL_VARS": blob}, clear=True):
            hydrate_repo_vars()
            self.assertEqual(os.environ["SHL_RETRIES"], "3")
            self.assertEqual(os.environ["SHL_ENABLED"], "True")

    def test_no_blob_is_not_an_error(self):
        # Local runs and older installs have no SHL_VARS. Hydration is an
        # addition to the existing env, never a precondition for it.
        with mock.patch.dict(os.environ, {"SHL_MODEL": "local"}, clear=True):
            hydrate_repo_vars()
            self.assertEqual(os.environ["SHL_MODEL"], "local")

    def test_an_unreadable_blob_is_loud(self):
        # This function exists to delete a "set correctly, absent at runtime"
        # failure. Swallowing a malformed blob reinstates exactly that, one
        # layer further from the cause: read_log raises every tick and nothing
        # anywhere names SHL_VARS. An empty value stays silent because that is
        # the ordinary absent case, covered separately.
        for blob in ("not json", "[]", "null", "3"):
            with self.subTest(blob=blob):
                with mock.patch.dict(os.environ, {"SHL_VARS": blob}, clear=True):
                    with self.assertRaises(ValueError):
                        hydrate_repo_vars()

    def test_a_name_outside_the_framework_namespace_is_not_hydrated(self):
        # `vars` carries organization-level variables, inherited by every repo
        # in the org. An unfiltered fold would let an unset NODE_OPTIONS or
        # GIT_SSH_COMMAND set anywhere in that org change how this loop's
        # subprocesses run, with nothing recording that it happened.
        blob = json.dumps({"NODE_OPTIONS": "--require /tmp/evil", "GIT_DIR": "/tmp/evil"})
        with mock.patch.dict(os.environ, {"SHL_VARS": blob}, clear=True):
            hydrate_repo_vars()
            self.assertNotIn("NODE_OPTIONS", os.environ)
            self.assertNotIn("GIT_DIR", os.environ)

    def test_a_withheld_credential_name_is_never_re_materialised(self):
        # watch.yml withholds the provider token from the step running read_log
        # by not naming it in `env:`. An operator who stored that token as a
        # variable rather than a secret would otherwise have it delivered into
        # the one step built to run without it.
        blob = json.dumps({"SHL_AUTH_TOKEN": "leaked", "SHL_LOG_TOKEN": "leaked"})
        with mock.patch.dict(os.environ, {"SHL_VARS": blob}, clear=True):
            hydrate_repo_vars()
            self.assertNotIn("SHL_AUTH_TOKEN", os.environ)
            self.assertNotIn("SHL_LOG_TOKEN", os.environ)

    def test_loading_the_adapter_hydrates_first(self):
        # The seam that matters: every consumer reaches the adapter through
        # load_adapter, so hydrating anywhere else leaves some caller reading an
        # environment the others do not see.
        from adapters import load_adapter

        blob = json.dumps({"SHL_PROBE_MARKER": "arrived"})
        with mock.patch.dict(os.environ, {"SHL_VARS": blob}, clear=True):
            with self.assertRaises(ImportError):
                load_adapter()
            self.assertEqual(os.environ["SHL_PROBE_MARKER"], "arrived")


class NoDocClaimsHydrationDeliversASecret(unittest.TestCase):
    """`hydrate_repo_vars` skips `SECRET_ENV_VARS` — docs must not say otherwise.

    The reason a secret is a secret invites the wrong
    mechanism: a doc that claims `SHL_VARS` folds a stored-as-variable credential
    into the adapter's environment, when hydration is exactly the path that
    refuses those names. The true route is the raw job-level blob sitting in
    every step's environment. A wrong mechanism in a security rationale is
    worse than none: an operator auditing the claim finds it refuted by the
    code and stops trusting the paragraph that carries the real instruction.

    Paragraph-scoped, because the rationale names the credential in one
    sentence and the mechanism in the next.
    """

    def test_no_secret_name_is_said_to_be_hydrated_or_folded(self):
        import re
        from guardrails.confidentiality_filter import SECRET_ENV_VARS

        framework = Path(__file__).resolve().parents[1]
        for path in framework.glob("**/*.md"):
            if "tests" in path.parts:
                continue
            for para in path.read_text(encoding="utf-8").split("\n\n"):
                if not re.search(r"(?i)hydrat|fold", para):
                    continue
                named = [n for n in SECRET_ENV_VARS if n in para]
                if not named:
                    continue
                with self.subTest(file=path.name, para=para[:60]):
                    self.assertRegex(
                        para, r"(?i)skip|refus|never re-materialis",
                        f"{path.name} explains a secret with a hydration claim, and "
                        f"hydration skips {named}; the open route is the raw "
                        f"job-level blob, not hydration",
                    )


class OnlyTheAdapterItselfIsOptional(unittest.TestCase):
    """"No adapter here" and "the adapter is broken" must not read the same.

    `optional_ids_fn` returns None so a target with no adapter falls back to
    the built-in Python/V8 parsing. A bare `except ImportError` also swallows
    an import raised INSIDE the adapter's own module body — a missing SDK, a
    typo, a circular import — and every consumer then reads "this stack has no
    custom identities". `record_cycle` stores an empty fingerprint list, which
    matches nothing forever — a mechanism entirely dead while every test over
    it stays green, which is the defect this seam exists to close.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sys.path.insert(0, self.tmp.name)
        self.addCleanup(sys.path.remove, self.tmp.name)
        self.addCleanup(sys.modules.pop, "brokenadapter", None)

    def _write(self, body: str) -> None:
        Path(self.tmp.name, "brokenadapter.py").write_text(body, encoding="utf-8")

    def test_a_missing_adapter_module_still_falls_back(self):
        with mock.patch.dict(os.environ, {"SHL_ADAPTER": "notinstalled"}, clear=True):
            self.assertIsNone(optional_ids_fn())

    def test_an_import_failing_inside_the_adapter_surfaces(self):
        self._write("import a_dependency_nobody_installed\nadapter = None\n")
        with mock.patch.dict(os.environ, {"SHL_ADAPTER": "brokenadapter"}, clear=True):
            with self.assertRaises(ImportError):
                optional_ids_fn()

    def test_a_working_adapter_supplies_its_identities(self):
        self._write(
            "class A:\n"
            "    def failure_ids(self, raw):\n"
            "        return ['X@a:1']\n"
            "adapter = A()\n"
        )
        with mock.patch.dict(os.environ, {"SHL_ADAPTER": "brokenadapter"}, clear=True):
            self.assertEqual(optional_ids_fn()("anything"), ["X@a:1"])


if __name__ == "__main__":
    unittest.main()
