"""The role prompts and the validator must describe the same output.

`validate_contract` rejects a payload missing a required field, and a rejection
costs the whole cycle: the agent call is already paid for, the issue is already
filed, and the run dies parsing the answer. So a field the validator requires
and no prompt asks for is not a documentation defect, it is a cycle that cannot
complete — and it is invisible until a model happens to omit the field.

These read the shipped templates and `loop_context/CLAUDE.md`, which is the
operating doc every role auto-loads, and check both directions against
`role._CONTRACT`.
"""
import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from agent.base import AgentRole
from role import _CONTRACT

FRAMEWORK = Path(__file__).resolve().parents[1]
TEMPLATES = FRAMEWORK / "templates"
LOOP_CONTEXT = FRAMEWORK / "loop_context" / "CLAUDE.md"


def _template(role: AgentRole) -> str:
    return (TEMPLATES / f"{role.value}.md").read_text(encoding="utf-8")


class TheContractItselfIsPinned(unittest.TestCase):
    """Both loops below iterate `_CONTRACT`, so shrinking it shrinks the checks.

    That is the direction that actually costs something. Drop `repro_test` and
    `validate_contract` stops requiring it; the Red step then runs
    `jq -r .repro_test.code` on a missing key, writes jq's literal `null` as the
    frozen reproducing test, and that file fails for reasons unrelated to the
    bug — so `red=true` is unconditional and the escalation that catches a bad
    repro spec goes dead. Pinning the field set is what makes the iteration
    honest.
    """

    def test_the_required_field_sets_are_exactly_these(self):
        self.assertEqual(
            {role.value: set(fields) for role, fields in _CONTRACT.items()},
            {
                "diagnose": {"issue_title", "issue_body", "reproducible", "confidence"},
                "fix": {"summary", "files_changed"},
                "review": {"approved", "reason"},
            },
        )


class EveryRequiredFieldIsAskedFor(unittest.TestCase):
    def test_the_contract_is_not_empty(self):
        # Without this, every loop below passes vacuously on an empty contract.
        self.assertTrue(_CONTRACT)
        for role, fields in _CONTRACT.items():
            with self.subTest(role=role.value):
                self.assertTrue(fields)

    def test_each_role_template_names_all_its_required_fields(self):
        for role, fields in _CONTRACT.items():
            text = _template(role)
            for field in sorted(fields):
                with self.subTest(role=role.value, field=field):
                    # As a DECLARED field (`name:`), not as a loose substring:
                    # the word "reason" occurs in ordinary prose, so a bare
                    # containment check passes a template that renamed the
                    # field it actually asks for.
                    self.assertRegex(
                        text, rf"(?m)^\s*[-*]?\s*`?{field}`?\s*:",
                        f"{role.value}.md never asks for {field!r}, which "
                        "validate_contract requires; the cycle dies after the "
                        "agent call is already spent",
                    )

    def test_the_operating_doc_names_them_too(self):
        # Every role auto-loads this file, and it restates the contract. A field
        # listed in one place and not the other is the drift this catches.
        text = LOOP_CONTEXT.read_text(encoding="utf-8")
        for role, fields in _CONTRACT.items():
            for field in sorted(fields):
                with self.subTest(role=role.value, field=field):
                    self.assertIn(f"`{field}`", text)


class AFieldTheValidatorNeverRequiresSaysSo(unittest.TestCase):
    """The other direction: a prompt asking for a field nothing enforces.

    The checks above run contract -> prompt, which catches a required field no
    prompt asks for. Nothing ran the reverse, and the reverse is what shipped:
    `tests_added` sat in the same flat bullet list as `summary` and
    `files_changed`, reading as contractual while `validate_contract` never
    looked for it. Presented identically, the agent cannot tell which omission
    kills the cycle and which one nobody notices — so a field outside the
    contract has to say that it is outside it.
    """

    # `- name:` at the start of a line. The role templates declare output fields
    # this way and nothing else in them does: every other bullet opens with a
    # capital or with `**`, so the lowercase-and-colon shape is the field list.
    _FIELD = re.compile(r"(?m)^[-*]\s+`?([a-z_]+)`?\s*:(.*)$")

    def test_every_declared_output_field_is_required_or_marked(self):
        for role, fields in _CONTRACT.items():
            declared = self._FIELD.findall(_template(role))
            # Without this the loop below passes on a template whose field list
            # stopped matching, which is the same silence it exists to break.
            self.assertTrue(declared, f"{role.value}.md declares no output fields")
            for field, rest in declared:
                if field in fields:
                    continue
                with self.subTest(role=role.value, field=field):
                    self.assertRegex(
                        rest,
                        r"(?i)\b(?:omit|not required)\b",
                        f"{role.value}.md asks for {field!r}, which "
                        "validate_contract never requires, without saying so — "
                        "beside the enforced fields it reads as contractual",
                    )


class ARequiredFieldNoCodeReadsNamesItsDestination(unittest.TestCase):
    """A field the driver demands and no step consumes still has to earn it.

    `validate_contract` rejects a payload without one of these, so omitting it
    costs the whole cycle — but nothing downstream branches on the value, which
    leaves the prompt as the only thing that can tell the agent what a
    considered answer is even for. Unexplained, such a field collects whatever
    is cheapest to type, and the operator opening the evidence bundle finds a
    filled slot with no information in it.

    The membership is asked of the code rather than listed here, which is what
    found `files_changed`: an audit named `confidence` and `tests_added` and
    missed the third field in the same position.
    """

    def _consumers(self, field: str) -> list[str]:
        """Shipped modules mentioning the field, excluding the contract itself."""
        hits = []
        for pattern in ("*.py", "*.yml"):
            for path in FRAMEWORK.rglob(pattern):
                rel = path.relative_to(FRAMEWORK)
                # tests/ asserts on the names, and role.py is where the
                # requirement is declared; neither is a consumer of the value.
                if rel.parts[0] == "tests" or rel.name == "role.py":
                    continue
                if field in path.read_text(encoding="utf-8"):
                    hits.append(str(rel))
        return hits

    def test_an_unconsumed_field_says_where_its_value_goes(self):
        checked = 0
        for role, fields in _CONTRACT.items():
            for field in sorted(fields):
                if self._consumers(field):
                    continue
                checked += 1
                bullet = next(
                    (line for line in _template(role).splitlines()
                     if re.match(rf"^[-*]\s+`?{field}`?\s*:", line)),
                    None,
                )
                with self.subTest(role=role.value, field=field):
                    self.assertIsNotNone(
                        bullet, f"{role.value}.md never declares {field!r}"
                    )
                    self.assertIn(
                        "evidence bundle",
                        bullet,
                        f"no shipped code reads {field!r}, so {role.value}.md is "
                        "the only thing that can tell the agent where the value "
                        "ends up and who acts on it",
                    )
        # Guards the loop: if every field acquired a consumer the check would
        # pass while asserting nothing, and that is worth noticing rather than
        # inheriting silently.
        self.assertTrue(checked, "no unconsumed required fields; delete this test")


class ReproducibleIsDescribedAsABoolean(unittest.TestCase):
    """A quoted "true" is rejected, and the rejection wastes the whole cycle.

    The workflow branches on `jq -r .reproducible` against the string "true",
    which a JSON string also satisfies, so the validator refuses non-booleans
    outright rather than letting a stringly-typed answer reach the Red step and
    freeze jq's literal `null` as the reproducing test. The prompts have to say
    which type they mean, or the strictness only shows up as a dead cycle.
    """

    def test_the_operating_doc_says_bool(self):
        text = LOOP_CONTEXT.read_text(encoding="utf-8")
        self.assertRegex(text, r"`reproducible`\s*\(bool\)")

    def test_the_template_offers_the_two_literals(self):
        text = _template(AgentRole.DIAGNOSE)
        self.assertRegex(text, r"`?reproducible`?:\s*true or false")
        self.assertNotIn('reproducible: "true"', text)


class NoPromptContradictsTheGate(unittest.TestCase):
    """A prompt telling an agent to do what the gate refuses burns an attempt.

    The gate runs before Review and blocks deterministically, so an instruction
    at odds with it cannot be argued with: it just costs the cycle.
    """

    def test_fix_is_told_the_frozen_test_is_untouchable(self):
        self.assertRegex(
            _template(AgentRole.FIX),
            r"(?i)do not edit.*frozen|never.*touch.*frozen",
        )

    def test_fix_is_told_pre_existing_failures_are_not_its_job(self):
        # The gate compares failure sets, so an unrelated pre-existing failure
        # does not block. A prompt that told Fix to fix everything red would
        # widen every diff and trip the scope check in Review.
        #
        # As a BULLET, not a loose substring. A prose test cannot exclude a
        # contradiction — appending "ignore the above, fix every failing test"
        # leaves the original words intact — so the claim is narrowed to what a
        # pattern can actually hold: the rule is stated as an instruction, in
        # the list of instructions.
        self.assertRegex(_template(AgentRole.FIX), r"(?m)^[-*] .*\bwas passing\b")

    def test_fix_is_not_promised_a_shell(self):
        # Asserted per file, not over both concatenated: either one alone
        # satisfying the check lets the other drop the claim silently, and the
        # template is what the Fix role is handed for that specific call.
        # The SENTENCE, not the fragment: "no shell" survives inside "the old
        # rule of no shell no longer applies", which is the one rewrite that
        # would matter. The enforceable half of this claim lives in
        # test_harness.RestrictionsMatchTheDocumentedCIMode.test_fix_cannot_reach_a_shell;
        # this is a doc-drift check on top of it.
        for name, text in (
            ("fix.md", _template(AgentRole.FIX)),
            ("loop_context/CLAUDE.md", LOOP_CONTEXT.read_text(encoding="utf-8")),
        ):
            with self.subTest(source=name):
                self.assertRegex(text, r"(?i)\b(?:you have|fix has) no shell\b")

    def test_the_operating_doc_scopes_the_failure_log_to_diagnose(self):
        """Only Diagnose is given the log, and the doc all three roles load said otherwise.

        `loop_context/CLAUDE.md` auto-loads for every role, so an unscoped claim
        there tells Fix and Review to consult a section their prompt does not
        contain. Asked of the code rather than assumed: `run_fix` supplies
        `issue`, `repro`, `frozen` and `incident_memory`; `run_review` supplies
        `diff`, `issue` and `repro`. Neither is given `log`.
        """
        import inspect
        import re as _re

        import loop
        from role import _CONTEXT_LABELS

        for fn in ("run_fix", "run_review"):
            keys = _re.findall(r'"(\w+)":', inspect.getsource(getattr(loop, fn)))
            self.assertNotIn("log", keys, f"{fn} now supplies a log; rewrite this test")

        label = _CONTEXT_LABELS["log"]
        text = LOOP_CONTEXT.read_text(encoding="utf-8")
        line = next(
            (ln for ln in text.splitlines() if label.split(" (")[0] in ln), None
        )
        self.assertIsNotNone(line, f"the operating doc no longer names {label!r}")
        # The scoping has to come BEFORE the label, not merely somewhere on the
        # line: the sentence goes on to explain what Fix and Review get instead,
        # so it names Diagnose either way and a whole-line search passes an
        # unscoped claim. That mutation went NOT CAUGHT until this narrowed.
        before = line.partition(label.split(" (")[0])[0]
        self.assertRegex(
            before, r"(?i)diagnose",
            "the operating doc announces the failure log without first saying "
            "it reaches Diagnose alone, so two of three roles are sent looking "
            "for a section they were never given",
        )

    def test_the_optional_repro_is_described_as_optional(self):
        # `loop.run_fix` and `loop.run_review` inject the repro only when
        # Diagnose supplied runnable code, and both prompts say most runtime
        # failures do not reduce to a deterministic test — so the absence is
        # the ordinary case. A prompt announcing the repro unconditionally
        # sends a cold agent looking for context it was never given, or worse,
        # tells it to judge the fix against a test that does not exist.
        for role in (AgentRole.FIX, AgentRole.REVIEW):
            with self.subTest(role=role.value):
                self.assertRegex(_template(role), r"(?i)only when Diagnose")

    def test_diagnose_is_told_the_repro_must_carry_a_visible_assertion(self):
        """A test that is red by RAISING satisfies the Red step and kills the gate.

        `guardrails.cli` reads the frozen test and exits 2 when its assertion
        pattern matches nothing in it, on the stated grounds that a test proven
        red then green contains an assertion by construction. That is false: a
        reproducing test can be red purely because the call under test throws,
        and such a file contains no assertion in any language. The cycle then
        dies after Diagnose and Fix are both spent, with a message telling the
        operator to set `--assert-pattern` when their pattern was never the
        problem. The premise is only true if Diagnose is told to make it true.
        """
        from guardrails import gate

        red_by_raising = "def test_parse_config():\n    parse_config({})\n"
        # The gate's own view of that file, so this is not an assumption about
        # the pattern: it is the pattern's answer.
        self.assertIsNone(
            gate._ASSERT_RE.search(red_by_raising),
            "the built-in pattern matches this, so the instruction below "
            "guards nothing and this test needs a different example",
        )
        # The INSTRUCTION, not the topic. `diagnose.md` uses the word twice in
        # one sentence — once to require an assertion and once to explain why —
        # so a bare `(?i)assertion` search survives deleting the imperative and
        # keeping the explanation, which is the reversal that matters. Scoped to
        # the sentence for the same reason the refusal check above is: the
        # requirement has to sit beside the thing it requires.
        sentences = [
            s for s in re.split(r"(?<=\.)\s+", _template(AgentRole.DIAGNOSE))
            if re.search(r"(?i)assertion", s)
        ]
        self.assertTrue(
            sentences,
            "diagnose.md never mentions an assertion, so a red-by-raising test "
            "reaches the gate and exits 2 with a misdirecting message",
        )
        self.assertTrue(
            any(re.search(r"(?i)\b(must|has to|required)\b", s) for s in sentences),
            f"diagnose.md discusses assertions without requiring one; the gate's "
            f"premise that a frozen test contains an assertion is only true if "
            f"this instruction makes it true. Sentences found: {sentences}",
        )

    def test_no_role_is_told_to_run_git(self):
        for role in AgentRole:
            with self.subTest(role=role.value):
                # Any phrasing, anywhere in the line. The old form required
                # the line to BEGIN with "run git", so "when you are done you
                # should run `git add -A`" passed it. The templates say only
                # what the workflow does with git, never an imperative, so
                # naming the verbs is enough and needs no context exemption.
                # Read verbs sit in the list for the same reason as the write
                # verbs, not a weaker one: every role denies `Bash`, so `git log`
                # is exactly as impossible as `git push` and merely looks
                # harmless. A role told to run one spends its call finding out.
                self.assertNotRegex(
                    _template(role),
                    r"(?i)\bgit\s+(?:add|commit|push|revert|merge|checkout"
                    r"|log|diff|show|status)\b",
                )


class NoPromptNamesAConfigFileTheGateIgnores(unittest.TestCase):
    """Telling Fix a file is refused, when it is not, is worse than silence.

    Both agent-facing documents list the files that carry test configuration —
    `pyproject.toml`, `package.json`, `Cargo.toml`, `pom.xml`, `.rspec` — and
    say the gate rejects any diff touching one. The built-in globs described
    Python and JS only, so on a Ruby, Rust or JVM target the named file was not
    policed at all and the sentence was false.

    Two ways to make it true, and the direction matters. Narrowing the prose to
    whatever the globs happen to cover leaves the gate blind on those stacks and
    merely stops advertising it. Widening the globs to cover what the prose
    already promises closes the hole — and `package.json` was in the list all
    along, which is the same category of file for the same reason, so this
    follows a decision already taken rather than making a new one.

    The direction of this check is deliberate and matches
    `NoDocClaimsAPatternTheGateDoesNotHave`: a doc naming a file the gate
    ignores fails. A file the gate polices but no doc mentions is fine — that
    errs safe.
    """

    # Backticked filenames from the sentences that promise enforcement. Derived
    # from the docs rather than restated, so adding an example to a prompt puts
    # it under this check automatically.
    _NAMED = re.compile(r"`([A-Za-z_.][\w.]*(?:\.\*)?)`")

    def _claimed(self, text: str) -> set[str]:
        named = set()
        for line in text.splitlines():
            if "test-runner config" not in line and "test runner" not in line:
                continue
            for token in self._NAMED.findall(line):
                if "/" in token:
                    continue  # a path or a glob over a directory, not a filename
                # Dotted (`pyproject.toml`, `.rspec`) OR capitalised-and-dotless
                # (`Makefile`, `Rakefile`, `Justfile`). Requiring a dot was the
                # obvious filter and it is wrong: the dotless build files are
                # exactly the ones a prompt names, and `Rakefile` is in the
                # gate's own glob list — so the check could not see a filename
                # it exists to police. Proven by a mutation that added
                # `Makefile` and went NOT CAUGHT.
                if "." in token or token[:1].isupper():
                    named.add(token)
        return named

    def test_the_derivation_finds_the_documented_examples(self):
        # Without this the loop below passes vacuously if the sentences are
        # reworded out of its reach.
        found = self._claimed(_template(AgentRole.FIX)) | self._claimed(
            LOOP_CONTEXT.read_text(encoding="utf-8")
        )
        self.assertIn("pyproject.toml", found)
        self.assertGreaterEqual(len(found), 6, f"only found {sorted(found)}")

    def test_every_config_file_a_prompt_names_is_actually_policed(self):
        from guardrails.gate import _DEFAULT_TEST_CONFIG_GLOBS, _matches

        sources = {
            "fix.md": _template(AgentRole.FIX),
            "loop_context/CLAUDE.md": LOOP_CONTEXT.read_text(encoding="utf-8"),
        }
        for name, text in sources.items():
            for filename in sorted(self._claimed(text)):
                # A `*` in the doc stands for a real extension.
                probe = filename.replace(".*", ".ts")
                with self.subTest(source=name, config=filename):
                    self.assertTrue(
                        _matches(probe, _DEFAULT_TEST_CONFIG_GLOBS),
                        f"{name} tells the agent the gate rejects `{filename}`, "
                        f"and the built-in globs do not match it — so on that "
                        f"stack the file is unpoliced and the promise is false",
                    )


class NoDocCallsAFileUnpolicedThatTheGatePolices(unittest.TestCase):
    """The over-claim direction was covered; this is the under-claim direction.

    An existing check refuses a doc that names a config file the compiled globs
    ignore, because an installer reading "the built-in covers this" then skips
    the variable and the check polices nothing. The opposite drift is quieter
    and shipped anyway: `_DEFAULT_TEST_CONFIG_GLOBS` gained the Ruby, Rust and
    JVM manifests, `templates/` was updated, and four reader-facing documents
    went on describing those files as unprotected.

    It costs real work rather than safety — an operator sets a variable that was
    already redundant, and a reader forms a worse opinion of the gate than it
    deserves — but a state doc claiming coverage it does not have and one
    denying coverage it does have are the same defect, and this project audits
    its docs against the code precisely to stop both.
    """

    DOCS = ("SKILL.md", "README.md", "artifacts/setup.md", "artifacts/report.md",
            "reference/verifying-the-install.md", "loop_context/CLAUDE.md",
            "templates/fix.md")
    UNPOLICED = re.compile(r"(?i)\bunpoliced\b|\bwalks? past the gate\b|covers [^.]*only\b")

    def test_no_covered_manifest_is_described_as_unprotected(self):
        from guardrails.gate import _DEFAULT_TEST_CONFIG_GLOBS

        covered = [g for g in _DEFAULT_TEST_CONFIG_GLOBS if "*" not in g]
        self.assertTrue(covered, "no literal config filenames to check against")
        for name in self.DOCS:
            text = (FRAMEWORK / name).read_text(encoding="utf-8")
            # By CLAUSE, not by line. A correct sentence names the covered
            # manifests and then says anything else is unpoliced, so a
            # line-wide search flags the very wording that fixed this.
            for line in re.split(r"[.;]\s+", text.replace("\n", " ")):
                if not self.UNPOLICED.search(line):
                    continue
                named = [f for f in covered if f"`{f}`" in line]
                with self.subTest(doc=name, line=line[:60]):
                    self.assertEqual(
                        named, [],
                        f"{name} calls {named} unpoliced, and the gate's built-in "
                        f"config globs have matched them since the list was "
                        f"widened; the doc is describing an older gate",
                    )


class EveryGateRefusalIsStatedInWhatFixReads(unittest.TestCase):
    """A deterministic refusal nobody warned Fix about costs the whole cycle.

    The gate runs after Fix returns, so a rule the prompt never stated is
    discovered by breaking it: the agent call is spent, the attempt is
    recorded, and the block's reason reaches the PR rather than the agent that
    could have avoided it. Adding a check to `guardrails.cli` is therefore also
    a prompt change, and nothing made that true before this test.

    The label set is harvested from the gate's own pass line rather than listed
    here, so a new check with no matching instruction fails this instead of
    shipping quietly. `fix.md` and the operating doc are read together because
    both are handed to the same agent — the template as the prompt, the
    operating doc by auto-load from the cwd — and each rule needs one home, not
    two copies to keep in step.
    """

    # Every label the gate can print, mapped to the wording that has to be
    # somewhere Fix reads. The MAPPING is authored; the MEMBERSHIP is not, and
    # the equality assertion below is what keeps the two honest.
    _STATED_AS = {
        "loop tree untouched": r"(?i)write anything into `?\.shl/",
        "workflow untouched": r"\.github/workflows/",
        "diff rendering untouched": r"\.gitattributes",
        "test content readable": r"(?i)readable as text",
        "no test weakened": r"(?i)\bweaken",
        "no test config touched": r"(?i)test-runner config",
        "frozen test untouched": r"(?i)frozen reproducing test",
        "frozen test's helpers untouched": r"(?i)dedicated test directory",
    }

    # Naming the subject is not stating the rule. `(?i)\bweaken` is satisfied by
    # "You may weaken an existing test when the fix requires it", which is the
    # instruction reversed — so the keyword above only locates the claim, and the
    # line carrying it must also forbid something. The docs put one claim per
    # line, which is what makes the line the right unit to test.
    # Directive forms only, and scoped to the SENTENCE naming the subject rather
    # than to the line. Two failures of a looser version are the reason for both
    # halves. A bare `not` matched "you may leave it readable as text or not,
    # whichever suits the fix", which reverses the rule. And a line carrying two
    # claims — "Never touch the frozen test. Never weaken a test: …" — let the
    # first directive vouch for the second clause, so flipping only the second
    # to "You may weaken a test" passed. A rule has to be forbidden in the same
    # breath as it is named, which is also how it reads to the agent.
    _PROHIBITION = re.compile(
        r"(?i)(\bnever\b|\bdo not\b|\bdon't\b|\bmay not\b|\bmust not\b|\bcannot\b"
        r"|\brefus\w*|\breject\w*|\boff-limits\b|\bforbidden\b)"
    )

    # The refusals that exit 1 WITHOUT a label, mapped to what Fix must be told.
    # The harvest above reads the pass line, which only knows the labelled
    # checks — so these three were outside every assertion in this class while
    # its docstring claimed a new check "fails this instead of shipping
    # quietly". One of them was stated nowhere at all.
    _UNLABELLED = {
        "regression: ": r"(?i)was passing",
        "suite exited ": r"(?i)suite could not run",
        "frozen reproducing test did not pass": r"(?i)frozen reproducing test",
    }

    def test_every_exit_one_path_is_accounted_for(self):
        """Fail closed on COUNT, so a new refusal cannot be added silently.

        The labelled checks share one `return 1` inside their loop; each
        unlabelled refusal has its own. Pinning the total is what makes a fourth
        one fail here rather than reach an agent that was never warned about it.
        """
        source = (FRAMEWORK / "guardrails" / "cli.py").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("return 1"), 1 + len(self._UNLABELLED),
            "guardrails/cli.py has a refusal this class does not know about; add "
            "it to _UNLABELLED with the wording Fix is given, or to the labelled "
            "checks list",
        )
        for fragment in self._UNLABELLED:
            with self.subTest(refusal=fragment):
                self.assertIn(fragment, source)

    def test_each_unlabelled_refusal_is_stated_too(self):
        sources = {
            "fix.md": _template(AgentRole.FIX),
            "loop_context/CLAUDE.md": LOOP_CONTEXT.read_text(encoding="utf-8"),
        }
        for fragment, pattern in self._UNLABELLED.items():
            with self.subTest(refusal=fragment):
                self.assertTrue(
                    any(re.search(pattern, text) for text in sources.values()),
                    f"the gate exits 1 with {fragment!r} and neither "
                    f"{' nor '.join(sources)} prepares the agent for it",
                )

    @staticmethod
    def _pass_line() -> str:
        """The gate's attestation for a clean diff with every check in play."""
        from guardrails import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A dedicated test tree, so the helper-freeze check applies rather
            # than declining — declining would drop its label from the line.
            (root / "tests").mkdir()
            frozen = root / "tests" / "test_repro_issue_7.py"
            frozen.write_text("def test_x():\n    assert parse({}) == 'd'\n", encoding="utf-8")
            diff = root / "fix.diff"
            diff.write_text(
                "diff --git a/app/parse.py b/app/parse.py\n"
                "--- a/app/parse.py\n+++ b/app/parse.py\n"
                '-    return cfg["mode"]\n+    return cfg.get("mode", "d")\n',
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(
                    ["gate", "--diff", str(diff), "--frozen", str(frozen), "--repro-rc", "0"]
                )
        assert rc == 0, f"gate did not pass on a clean diff (exit {rc})"
        return out.getvalue()

    def _labels(self):
        line = self._pass_line().partition("PASSED: ")[2].strip()
        segments = [s.strip() for s in line.split(";") if s.strip()]
        # The line carries refusal labels plus attestations that are not checks:
        # the frozen test's own result (carries a path in parentheses), the
        # matched-file count (opens with a digit), and the two notes for a check
        # that declined (carry "NOT"). Everything else is a check that ran.
        labels = [
            s for s in segments
            if "(" not in s and "NOT" not in s and not s[0].isdigit()
        ]
        return segments, labels

    def test_the_filter_actually_drops_something(self):
        # Without this the label list could be the whole line, and the mapping
        # below would be asserting against attestations rather than checks.
        segments, labels = self._labels()
        self.assertLess(len(labels), len(segments))

    def test_the_gates_checks_are_exactly_the_ones_mapped_here(self):
        _, labels = self._labels()
        self.assertEqual(
            set(labels), set(self._STATED_AS),
            "the gate's refusal set and this mapping disagree: a check was "
            "added or removed without deciding what Fix is told about it",
        )

    def test_each_one_is_stated_where_fix_will_read_it(self):
        sources = {
            "fix.md": _template(AgentRole.FIX),
            "loop_context/CLAUDE.md": LOOP_CONTEXT.read_text(encoding="utf-8"),
        }
        _, labels = self._labels()
        for label in sorted(labels):
            pattern = re.compile(self._STATED_AS[label])
            with self.subTest(check=label):
                lines = [
                    sentence
                    for text in sources.values()
                    for line in text.splitlines()
                    for sentence in re.split(r"(?<=\.)\s+", line)
                    if pattern.search(sentence)
                ]
                self.assertTrue(
                    lines,
                    f"the gate can block a fix with {label!r} and neither "
                    f"{' nor '.join(sources)} says so, so the agent finds out "
                    f"by losing a cycle to it",
                )
                self.assertTrue(
                    any(self._PROHIBITION.search(line) for line in lines),
                    f"{label!r} is mentioned in a prompt but never forbidden — "
                    f"the agent is told the subject exists, not that it is "
                    f"refused. Lines found: {lines}",
                )


class TheInjectionReportHasSomewhereToGo(unittest.TestCase):
    """The operating doc names one output field per role to report injection in.

    Those three names are the whole mechanism: a role that finds an injected
    instruction has no other channel. If a contract field is renamed and this
    sentence is not, every role is told to report into a field that no longer
    exists — the instruction still reads correctly and reaches nobody, which is
    the same defect the approve-path `reason` already was.

    What this cannot check is whether the report is USEFUL, or whether a role
    stays silent when there is nothing to report. Both are judgements about
    prose, and a regex over this file would only pin its vocabulary.
    """

    def test_each_named_field_exists_in_that_roles_contract(self):
        doc = LOOP_CONTEXT.read_text(encoding="utf-8")
        named = dict(re.findall(r"`(\w+)` for (\w+)", doc))
        self.assertEqual(
            {role.lower() for role in named.values()},
            {role.value for role in AgentRole},
            f"the operating doc names {sorted(named.values())}; every role needs one",
        )
        for field, role_name in named.items():
            role = AgentRole(role_name.lower())
            with self.subTest(role=role_name):
                self.assertIn(
                    field, _CONTRACT[role],
                    f"the doc tells {role_name} to report in `{field}`, which is "
                    f"not in its contract {sorted(_CONTRACT[role])}",
                )


class NothingVendoredCitesALessonNumber(unittest.TestCase):
    """`(L8)`, `(L9)` and friends name a numbered lesson no document defines.

    Nothing in this repo carries such a list, and `loop_context/CLAUDE.md`, the
    operating doc a target actually receives, defines none either. So a citation
    in vendored code points an operator — or an agent reading its own tree — at
    nothing at all. Every site that carries one states the rule in the
    surrounding sentence regardless, which is what makes the number pure dangle.

    Mechanical because whoever writes a citation cannot see it dangle: the
    number reads as a reference to its author, and only a check that tries to
    resolve it can tell that nothing is on the other end.
    """

    # What the install copies into `.shl/`. Kept as globs rather than the
    # SKILL.md manifest because this is about REACHABILITY, not vendoring
    # fidelity: a file that ships is a file whose references must resolve there.
    VENDORED = (
        "*.py", "adapters/**/*.py", "agent/**/*.py", "guardrails/**/*.py",
        "templates/*.md", "loop_context/*.md",
        # Both of these also land in a target: `artifacts/` is written into the
        # repo root as SETUP.md / INSTALL-REPORT.md / README.md, and the
        # workflows into .github/workflows/. A sweep that skips them reports a
        # clean bill over two of the four things the operator actually opens.
        "artifacts/*.md", "workflows/*.yml",
    )
    # Not `\(L\d+\)`. Parentheses are how these citations were mostly written,
    # not what makes them dangle — a bare `for the reason L9 records` resolves
    # exactly as poorly and slipped straight through a sweep that demanded them.
    CITATION = re.compile(r"\bL\d+\b")

    def test_no_vendored_file_cites_one(self):
        checked, offenders = 0, []
        for pattern in self.VENDORED:
            for path in sorted(FRAMEWORK.glob(pattern)):
                if "tests" in path.parts:
                    continue
                checked += 1
                for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if self.CITATION.search(line):
                        offenders.append(f"{path.relative_to(FRAMEWORK)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))
        # Refuse to pass on an empty sweep: a renamed directory would otherwise
        # report a clean bill over nothing at all.
        self.assertGreater(checked, 15, f"only {checked} vendored files swept")


if __name__ == "__main__":
    unittest.main()
