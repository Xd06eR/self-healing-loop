"""Structural invariants of the shipped installer text.

`SKILL.md` is prose an agent executes, so it drifts the way prose drifts: a
manifest is listed in one place and copied from another, a template gains a
placeholder nothing fills, a deleted concept survives in one clause. None of
that breaks a parse, and none of it is visible until an install goes wrong in
someone else's repo. F12 is exactly this shape — `SKILL.md` states twice that
the framework's own `tests/` are not installed, and nothing in the prose stops
an installer copying all 17 of them anyway.

Same approach as `test_workflows.py`: read the shipped file, assert structural
properties, stdlib only.
"""
import inspect
import re
import shutil
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import loop

FRAMEWORK = Path(__file__).resolve().parents[1]
SKILL = FRAMEWORK / "SKILL.md"
# Templates for what the install PRODUCES. Kept out of templates/, which
# holds the three role prompts the loop uses at runtime and is vendored
# whole — mixing them puts a half-filled record in the target.
ARTIFACTS = FRAMEWORK / "artifacts"
SETUP_TEMPLATE = ARTIFACTS / "setup.md"
REPORT_TEMPLATE = ARTIFACTS / "report.md"
REFERENCE = FRAMEWORK / "reference"

# Loaded by the installing agent on the branch it is on, never vendored.
REFERENCE_FILES = (
    "platforms.md",
    "closing-gaps.md",
    "harnesses.md",
    "verifying-the-install.md",
)

# Every asked-tier decision needs somewhere in the record to land. Keyed on a
# distinctive word so the test survives rewording of the surrounding heading.
DECISION_KEYS = (
    "notion of correct",
    "failures surface",
    "make them visible",
    "retention",
    "deploys this",
    "merged commit is live",
    "off-limits",
    "reviews the generated tests",
    "escalation",
    "harness",
)


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def setup_text() -> str:
    return SETUP_TEMPLATE.read_text(encoding="utf-8")


class ProvisionalIsGone(unittest.TestCase):
    """No headless install path exists, and no file may mention one.

    Without an operator you cannot install correctly, so you do not install.
    A surviving mention re-authorises the path for an agent reading the prose.
    """

    def test_skill_never_mentions_provisional(self):
        self.assertNotIn("PROVISIONAL", skill_text().upper())

    def test_setup_template_never_mentions_provisional(self):
        self.assertNotIn("PROVISIONAL", setup_text().upper())


class ReferenceFilesAreReachable(unittest.TestCase):
    """Progressive disclosure only works if the spine points at the branches."""

    def test_reference_files_exist(self):
        for name in REFERENCE_FILES:
            with self.subTest(name=name):
                self.assertTrue(
                    (REFERENCE / name).is_file(), f"missing reference/{name}"
                )

    def test_skill_links_every_reference_file(self):
        text = skill_text()
        for name in REFERENCE_FILES:
            with self.subTest(name=name):
                self.assertIn(
                    f"reference/{name}",
                    text,
                    f"SKILL.md never points at reference/{name}, so it is dead weight",
                )

    def test_no_orphan_reference_files(self):
        # A reference file nobody links is one the installer will never read.
        text = skill_text()
        for path in sorted(REFERENCE.glob("*.md")):
            with self.subTest(name=path.name):
                self.assertIn(f"reference/{path.name}", text)

    def test_no_orphan_artifact_templates(self):
        # An artifact template is only ever produced because a phase says to
        # copy it. One nothing names is a file that ships and is never written,
        # which reads as a deliberate omission to whoever finds it later.
        text = skill_text()
        for path in sorted(ARTIFACTS.glob("*.md")):
            with self.subTest(name=path.name):
                self.assertIn(f"artifacts/{path.name}", text)


class VendoringManifestMatchesReality(unittest.TestCase):
    """The manifest the installer checks against must equal what ships.

    `SKILL.md` embeds a `want = {...}` set that the install verifies the copied
    tree against. That set is written by hand next to a directory that changes,
    so it drifts, and drift is a real failure in both directions: a missing
    entry means a truncated install that dies on import, an extra entry means
    files copied into a target nobody meant to install (F12, the framework's
    own `tests/`).
    """

    # Framework top-level entries that are NOT part of the portable core, with
    # why each stays behind.
    NOT_VENDORED = {
        "SKILL.md",  # the installer itself
        "CLAUDE.md",  # framework dev guide; the target gets loop_context's copy
        "AGENTS.md",  # symlink to that dev guide
        "README.md",  # human overview
        "reference",  # installer-only progressive disclosure
        "artifacts",  # templates for install output, filled then copied
        "loop_context",  # copied as CLAUDE.md/AGENTS.md, not as a directory
        "tests",  # the framework's own tests — F12
        "workflows",  # copied to .github/workflows, not into the loop dir
    }

    # Present in an installed loop but not copied from the framework top level:
    # Phase 4 writes the gitignore and renames loop_context/CLAUDE.md twice.
    GENERATED_AT_INSTALL = {".gitignore", "CLAUDE.md", "AGENTS.md"}

    def shipped(self) -> set[str]:
        return {
            p.name
            for p in FRAMEWORK.iterdir()
            if not p.name.startswith(".")
            and p.name != "__pycache__"
            and p.name not in self.NOT_VENDORED
        }

    def declared(self) -> set[str]:
        """The `want = {...}` literal out of SKILL.md's manifest check."""
        match = re.search(r"want\s*=\s*\{(.*?)\}", skill_text(), re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md has no `want = {...}` manifest check")
        return set(re.findall(r"'([^']+)'", match.group(1)))

    def test_manifest_equals_what_the_framework_ships(self):
        self.assertEqual(
            self.declared(),
            self.shipped() | self.GENERATED_AT_INSTALL,
            "SKILL.md's manifest has drifted from the framework's real contents",
        )

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_nothing_the_install_needs_is_gitignored(self):
        """A file present on disk but ignored by git never reaches a target.

        Every other check here reads the filesystem, where an ignored file
        looks identical to a tracked one. The install copies from a CLONE, so
        the difference only appears in the target — as a file that is simply
        absent, with whatever depends on it failing there instead of here.

        The framework's `.gitignore` blanket-ignores the extensions agents
        write into the loop directory (`*.json`, `*.txt`, `*.diff`), which is
        deliberate and must stay: those carry raw agent output. Shipped files
        sharing one of those extensions therefore need an explicit negation.
        """
        names = sorted(self.shipped())
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-v"],
            cwd=FRAMEWORK,
            input="\n".join(names),
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):  # 128 = not a repo
            self.skipTest(f"git check-ignore unavailable: {result.stderr.strip()}")
        # Output is `<file>:<line>:<pattern>\t<path>`. A path whose LAST matching
        # pattern is a negation is not ignored; anything else printed is.
        ignored = [
            line.rsplit("\t", 1)[1]
            for line in result.stdout.splitlines()
            if line and not line.rsplit("\t", 1)[0].rsplit(":", 1)[1].startswith("!")
        ]
        self.assertEqual(
            ignored, [], f"shipped but gitignored, so absent from every install: {ignored}"
        )

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_the_gitignore_the_install_writes_keeps_every_shipped_file(self):
        """The target's own `.gitignore` must not hide the files just copied in.

        Phase 4 writes a second gitignore into `.shl/`, and it
        blanket-ignores the extensions agents write there. Phase 8 then commits
        the install, so anything that gitignore matches is dropped from the
        commit, absent from the runner's checkout, and missing at the point the
        loop needs it — while every local check still passes, because the file
        is right there on disk.

        Simulated against real git rather than by reading the patterns:
        gitignore precedence has enough corners that a hand-rolled matcher
        would be the thing under test.
        """
        block = re.search(r"```gitignore\n(.*?)```", skill_text(), re.DOTALL)
        self.assertIsNotNone(block, "SKILL.md no longer writes a target .gitignore")
        rules = textwrap.dedent(block.group(1))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitignore").write_text(rules, encoding="utf-8")
            names = sorted(self.shipped() - {".gitignore"})
            for name in names:
                target = root / name
                if (FRAMEWORK / name).is_dir():
                    target.mkdir()
                    (target / "__init__.py").touch()
                else:
                    target.touch()
            result = subprocess.run(
                ["git", "check-ignore", "--stdin", "-v"],
                cwd=root,
                input="\n".join(names),
                capture_output=True,
                text=True,
            )
        ignored = [
            line.rsplit("\t", 1)[1]
            for line in result.stdout.splitlines()
            if line and not line.rsplit("\t", 1)[0].rsplit(":", 1)[1].startswith("!")
        ]
        self.assertEqual(
            ignored,
            [],
            f"the installed .gitignore hides files the install just wrote: {ignored}",
        )


class TemplatePlaceholdersAreFilled(unittest.TestCase):
    """Every {{PLACEHOLDER}} needs an installer instruction that fills it.

    An unfilled placeholder ships to the operator as literal `{{TEST_CMD}}`,
    which reads as a broken install rather than a missing step.
    """

    def placeholders(self) -> set[str]:
        return set(re.findall(r"\{\{([A-Z_]+)\}\}", setup_text()))

    def test_template_has_placeholders(self):
        # Guards the test itself: a template that lost its placeholders would
        # make the assertion below vacuously true.
        self.assertTrue(self.placeholders())

    def test_skill_names_every_placeholder(self):
        text = skill_text()
        for name in sorted(self.placeholders()):
            with self.subTest(placeholder=name):
                self.assertIn(
                    f"{{{{{name}}}}}",
                    text,
                    f"setup.md has {{{{{name}}}}} but SKILL.md never says to fill it",
                )


class InstallWritesADurableReport(unittest.TestCase):
    """A chat-only report dies with the session.

    The moment it matters is months later, when someone asks why this repo
    serves an error endpoint the install added — and by then the conversation
    that installed it is gone.
    """

    def test_report_template_exists(self):
        self.assertTrue(REPORT_TEMPLATE.is_file())

    def test_skill_tells_the_installer_to_write_it(self):
        self.assertIn("artifacts/report.md", skill_text())

    def test_report_separates_project_files_from_loop_files(self):
        # Product code the operator now owns must not be buried in one
        # combined file list.
        text = REPORT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("{{PROJECT_FILES}}", text)
        self.assertIn("{{LOOP_FILES}}", text)

    def test_report_placeholders_are_named_in_the_skill_or_the_template(self):
        """Every `{{…}}` must be fillable, or the install ships a literal one.

        The name promised a cross-check and the body only guarded against
        vacuity — so the check its title describes was never performed. A
        placeholder added to the template with nothing anywhere saying where its
        value comes from reached a target unfilled, and a report reading
        `Owner: {{OWNER}}` is how an install announces itself as broken.

        Fillable means one of two things: `SKILL.md` says to fill it, or the
        template explains it inline right there. `report.md` does the second for
        several, which is why this is not simply the `setup.md` rule again.

        Granularity is the SECTION, and that is a real limit rather than an
        oversight. A placeholder dropped into a section that already carries
        guidance passes here. Tightening it further would mean either naming
        every placeholder in `SKILL.md`, which the spine has no room for, or
        matching prose against placeholder names, which is guesswork. What this
        catches is the case that actually shipped: a heading, a placeholder, and
        nothing telling the installer what belongs between them.
        """
        text = REPORT_TEMPLATE.read_text(encoding="utf-8")
        names = set(re.findall(r"\{\{([A-Z_]+)\}\}", text))
        self.assertTrue(names, "no placeholders; this test would be vacuous")

        skill = skill_text()
        # Sections, not lines. Most of these placeholders sit alone under a
        # heading whose prose is the explanation — `{{LOOP_FILES}}` follows a
        # paragraph saying exactly what belongs there. Demanding the text share
        # a LINE with the placeholder fails every one of those while catching
        # nothing real; what matters is that an installer reading the file knows
        # what to put there.
        sections = re.split(r"^#+ ", text, flags=re.M)
        unexplained = []
        for name in sorted(names):
            token = f"{{{{{name}}}}}"
            if token in skill:
                continue
            owning = next((s for s in sections if token in s), "")
            prose = re.sub(r"\{\{[A-Z_]+\}\}", "", owning).strip()
            if len(prose) < 40:
                unexplained.append(name)
        self.assertEqual(
            unexplained,
            [],
            "report.md has placeholders that neither SKILL.md tells the "
            "installer to fill nor the template explains inline; each would "
            "ship into a target as a literal",
        )

    def test_every_section_the_report_sends_the_reader_to_exists(self):
        """The report cites SETUP.md by section, and both files ship as
        templates that are edited separately.

        A citation is the operator's only route from "this was not verified" to
        the procedure that would verify it, so a stale one strands them in
        their own decision record. Renaming a heading in setup.md is the drift
        this catches; it does NOT catch an installer omitting the section while
        filling, which no check here can see.
        """
        headings = {
            line.lstrip("#").strip().lower()
            for line in setup_text().splitlines()
            if line.startswith("#")
        }
        cited = re.findall(r"`SETUP\.md` § \*([^*]+)\*", REPORT_TEMPLATE.read_text(encoding="utf-8"))
        self.assertTrue(cited, "the citation form changed; this check now reads nothing")
        for section in cited:
            with self.subTest(section=section):
                self.assertIn(
                    section.lower(), headings,
                    f"report.md sends the reader to SETUP.md § {section!r}, "
                    "which is not a heading in artifacts/setup.md",
                )


class RecordHasAHomeForEveryDecision(unittest.TestCase):
    """The interview asks; the record has to be able to hold the answers.

    The decision list is read out of `SKILL.md` itself, not from a constant in
    this file. Checking a hand-maintained list against the template only proves
    the template matches the list: add an eleventh decision to the interview
    with nowhere to record it and a constant-driven test stays green, which is
    precisely the drift this is supposed to catch.
    """

    def interview_decisions(self) -> list[str]:
        """The numbered decisions in SKILL.md's Phase 2, as their question text."""
        phase = skill_text().split("## Phase 2")[1].split("## Phase 3")[0]
        return [
            re.sub(r"[*_`]", "", q).strip().lower()
            for q in re.findall(r"^\d+\.\s+(.+?\?)", phase, flags=re.MULTILINE)
        ]

    def test_skill_actually_lists_decisions(self):
        # Without this the two tests below pass on an empty list.
        found = self.interview_decisions()
        self.assertGreaterEqual(len(found), 8, f"parsed only {found}")

    def test_every_numbered_decision_is_one_the_parse_can_see(self):
        """The parse keys on a trailing `?`; a decision may be worded without one.

        Reword one to a statement and it silently leaves the set — so its
        record-slot check below stops running while the count above still
        clears its floor. The decision keeps being asked in the interview and
        has nowhere to be written down, which is the exact drift these tests
        exist to catch, one level up.
        """
        phase = skill_text().split("## Phase 2")[1].split("## Phase 3")[0]
        numbered = re.findall(r"^\d+\.\s+(.+)$", phase, flags=re.MULTILINE)
        self.assertEqual(
            len(numbered), len(self.interview_decisions()),
            "a numbered decision in Phase 2 is not phrased as a question, so "
            f"nothing checks that it has a slot in the record: {numbered}",
        )

    def test_every_interview_decision_has_a_slot_in_the_record(self):
        record = setup_text().lower()
        for question in self.interview_decisions():
            # Match on the question's distinctive tail rather than the whole
            # sentence, so rewording the lead-in does not break the link.
            key = next((k for k in DECISION_KEYS if k in question), None)
            with self.subTest(decision=question[:48]):
                self.assertIsNotNone(
                    key,
                    f"interview asks {question!r} but no DECISION_KEYS entry tracks it; "
                    "add one here and a heading in artifacts/setup.md",
                )
                self.assertIn(
                    key,
                    record,
                    f"the interview asks about {key!r} with nowhere to record it",
                )

    def test_record_carries_provenance(self):
        # A record that cannot say whether a value was looked up, asked or
        # defaulted cannot be audited on re-install.
        text = setup_text().lower()
        for word in ("looked up", "asked", "defaulted"):
            with self.subTest(provenance=word):
                self.assertIn(word, text)


class SpineStaysThin(unittest.TestCase):
    """Branch-specific content belongs in reference/, never in the spine.

    Asserted on content rather than only on bytes. A byte cap is a proxy, and
    the thing it stands in for is checkable directly: the spine is what loads
    on every install, so it must not carry detail that only one branch needs.
    Platform recipes grow with every platform; gap-closing instructions only
    matter to a target that has the gap.
    """

    # Naming a platform means a recipe is creeping back into the spine.
    PLATFORM_NAMES = (
        "vercel",
        "netlify",
        "fly.toml",
        "render.yaml",
        "heroku",
        "railway",
        "sentry",
        "cloudwatch",
    )

    # Instruction-level markers from closing-gaps.md. Naming the *category* of
    # a gap is spine content ("sourcemap and build-identifier config" belongs
    # in the install manifest); telling the agent how to close it is not.
    GAP_INSTRUCTIONS = (
        "characterization",
        "rate-limit",
        "same-origin",
        "unhandledrejection",
        "productionbrowsersourcemaps",
    )

    # Backstop against general bloat, not a design target. Sized just above the
    # spine so it catches drift rather than forcing cuts to load-bearing text.
    #
    # The byte cap is a PROXY. The real invariant is the two assertions above:
    # no platform recipe, no gap-closing instructions in the spine.
    #
    # **Restructure instead of raising.** When a phase outgrows the spine, move
    # its branch-specific detail into reference/ and leave a pointer, the way 5a
    # points at closing-gaps.md and 5b at adapter.md. Raising the number keeps
    # every install paying to load detail one branch needs, which is the cost
    # the reference/ split exists to avoid.
    LIMIT_BYTES = 27 * 1024

    def test_no_platform_recipe_in_the_spine(self):
        text = skill_text().lower()
        for name in self.PLATFORM_NAMES:
            with self.subTest(platform=name):
                self.assertNotIn(
                    name, text, f"{name} detail belongs in reference/platforms.md"
                )

    def test_no_gap_closing_instructions_in_the_spine(self):
        text = skill_text().lower()
        for marker in self.GAP_INSTRUCTIONS:
            with self.subTest(marker=marker):
                self.assertNotIn(
                    marker,
                    text,
                    f"{marker} detail belongs in reference/closing-gaps.md",
                )

    def test_every_optional_variable_command_says_when_to_skip_it(self):
        """A `gh variable set` line whose value can render empty needs a guard.

        `gh` reads `--body ""` as "no value given" and opens an interactive
        prompt, which then swallows however many lines the operator pasted
        after it. Observed live, costing a debug cycle — and the fix was applied
        to three of the lines and missed on three more, twice, which is why this
        is a check rather than a habit.

        Fail closed: every command whose body is a placeholder needs a guard
        UNLESS it names a variable listed below as always set. Deriving
        "optional" from the table's wording was tried and is too weak — the four
        additive variables describe themselves as *extra* globs and patterns
        rather than as omittable, so a word-matching rule missed exactly the
        lines that had already shipped the defect. A new variable added with no
        guard now fails this test until someone decides which kind it is.
        """
        # Settled by the interview or by Phase 1, so they always have a value.
        # A variable belongs here only if leaving it unset is a broken install.
        ALWAYS_SET = {
            "SHL_HARNESS", "SHL_MODEL",
            "SHL_TEST_CMD", "SHL_TEST_ONE", "SHL_REPRO_PATH",
        }
        setup = (ARTIFACTS / "setup.md").read_text(encoding="utf-8")
        block = re.search(r"```bash\n(.*?)```", setup, re.S)
        self.assertIsNotNone(block, "setup.md has no gh command block")
        lines = block.group(1).splitlines()

        unguarded = []
        for i, line in enumerate(lines):
            m = re.match(r"gh variable set\s+(SHL_[A-Z_]+)\s+--body\s+['\"]\{\{", line.strip())
            if not m or m.group(1) in ALWAYS_SET:
                continue
            name = m.group(1)
            # A guard is inline on the command, or a preceding comment that
            # NAMES this variable. Accepting any nearby skip/omit comment lets
            # one variable's guard vouch for its neighbours — which is how the
            # original fix appeared complete while three lines were still bare.
            inline = re.search(r"#.*\b(skip|omit)\b", line, re.I)
            above = [
                ln for ln in lines[max(0, i - 3): i]
                if ln.lstrip().startswith("#") and name in ln
            ]
            if not inline and not above:
                unguarded.append(name)

        self.assertEqual(
            unguarded,
            [],
            "these commands render `--body ''` on a real target and open a "
            "prompt that eats the rest of a pasted block; each needs a comment "
            "saying when to skip the line",
        )

    def test_nothing_is_dispatched_before_the_merge(self):
        """`workflow_dispatch` sees a workflow only on the DEFAULT branch.

        The install lands on a branch and is reviewed there, so on a first
        install neither workflow exists anywhere dispatchable until the PR
        merges. A phase order that puts the dispatch first makes the one runner
        check the installer is *required* to perform impossible to perform —
        and the installer, unable to satisfy a required step, either invents a
        reason to skip it or reports a failure the operator cannot act on.

        Asserted as document order rather than by phase number, because the
        numbers are labels and the dependency is what matters.
        """
        text = skill_text()
        dispatch = text.find("gh workflow run watch.yml")
        self.assertNotEqual(dispatch, -1, "no dispatch instruction found")

        # Compare the SECTIONS, not the first mention of merging anywhere in
        # the file: earlier phases refer forward to the review, so a naive index
        # comparison is satisfied by that forward reference and holds whatever
        # order the phases are actually in. And comparing heading TEXT cannot
        # see a reorder either, because a heading travels with its block.
        headings = [
            (m.start(), text[m.start() + 1 : text.index("\n", m.start() + 1)])
            for m in re.finditer(r"\n## ", text)
        ]
        owning = max(pos for pos, _ in headings if pos < dispatch)
        gate = [pos for pos, title in headings if "artifact gate" in title.lower()]
        self.assertTrue(gate, "no artifact-gate section found")
        self.assertGreater(
            owning,
            gate[0],
            "the section that dispatches the watch comes before the PR review, "
            "which cannot work: workflow_dispatch requires the workflow on the "
            "default branch, and the install lives on a branch until that merge",
        )

    def test_the_spine_states_why_the_dispatch_waits(self):
        # The ordering above is only followed if its reason is legible; an
        # unexplained order gets 'corrected' by the next person to read it.
        text = skill_text().lower()
        self.assertIn("default branch", text)
        self.assertRegex(
            text,
            r"workflow_dispatch[^.]{0,120}default branch",
            "the spine orders the phases on this rule without stating it",
        )

    def test_skill_is_under_the_size_backstop(self):
        size = SKILL.stat().st_size
        self.assertLessEqual(
            size,
            self.LIMIT_BYTES,
            f"SKILL.md is {size} bytes, over the {self.LIMIT_BYTES} backstop — "
            "move a branch into reference/",
        )


class InstallingNeverSeedsADefect(unittest.TestCase):
    """A default install must not write a deliberate bug into someone's repo.

    Mandating a seeded bug in Phase 9 conflates two separate testing needs:
    verifying the FRAMEWORK (development, ours) and verifying an INSTALL (the
    operator's). Only the second is theirs to run, and it is theirs to choose —
    never silent, never forced.

    What stays mandatory is everything that seeds nothing. The honest ceiling on
    that is recorded in the report: `loop.py watch` constructs no agent, so a
    no-seed run cannot prove the harness executes on a runner.
    """

    OPT_IN_DOC = "verifying-the-install.md"

    def phase_9(self) -> str:
        return skill_text().split("## Phase 9")[1].split("## Phase 10")[0]

    def test_phase_9_does_not_tell_the_installer_to_seed(self):
        self.assertNotIn(
            "seed",
            self.phase_9().lower(),
            "Phase 9 still instructs seeding; that belongs in the opt-in doc",
        )

    def test_the_opt_in_doc_exists_and_says_it_is_optional(self):
        path = REFERENCE / self.OPT_IN_DOC
        self.assertTrue(path.is_file(), f"missing reference/{self.OPT_IN_DOC}")
        head = path.read_text(encoding="utf-8")[:600].lower()
        self.assertIn("optional", head, "the opt-in doc does not say so up front")

    def test_the_opt_in_doc_states_its_blast_radius_before_the_steps(self):
        # Someone deciding whether to run this needs to know what it touches
        # BEFORE the checklist, not in a footnote after it.
        text = REFERENCE / self.OPT_IN_DOC
        head = text.read_text(encoding="utf-8").split("- [ ]")[0].lower()
        self.assertIn("branch", head)

    def test_the_record_marks_the_seeded_steps_optional(self):
        # setup.md is what survives in the target; if it presents seeding as
        # part of going live, the installer's restraint bought nothing.
        text = setup_text().lower()
        seeded = text.find("seed")
        self.assertNotEqual(seeded, -1, "the record dropped the self-test entirely")
        heading = text.rfind("\n## ", 0, seeded)
        section = text[heading:seeded]
        self.assertIn("optional", section, "seeding is not under an optional heading")


class RepoSettingsThatKillEveryCycle(unittest.TestCase):
    """Phase 0 must check the repo settings that let a cycle run, then die.

    Both branch protection and the Actions workflow permissions fail the same
    way: the cycle reads the log, diagnoses, writes the repro, fixes, passes the
    gate — burning two agent calls — and then dies at `gh pr create` or at the
    merge. Every cycle, forever, and loudly, so nothing bad merges; the loop
    simply never works.

    `can_approve_pull_request_reviews` is the one a real install dies on. It is
    off by default on repos created since 2023 and is a policy toggle, so
    heal.yml's own `permissions: pull-requests: write` block cannot override it.
    """

    ENDPOINT = "actions/permissions/workflow"
    FIELDS = ("default_workflow_permissions", "can_approve_pull_request_reviews")

    def phase_0(self) -> str:
        return skill_text().split("## Phase 0")[1].split("## Phase 1")[0]

    def test_phase_0_checks_the_workflow_permissions_endpoint(self):
        self.assertIn(
            self.ENDPOINT,
            self.phase_0(),
            "Phase 0 never reads the Actions workflow permissions, so an install "
            "proceeds on a repo where heal.yml's `gh pr create` cannot run",
        )

    def test_phase_0_names_both_fields(self):
        phase = self.phase_0()
        for field in self.FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, phase)

    def test_the_record_carries_the_settings_too(self):
        # SKILL.md is never installed into the target, so months later the
        # record is the only copy the operator still has.
        text = setup_text()
        for field in self.FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, text)

    def test_the_record_carries_the_ui_path_as_well_as_the_cli(self):
        # `gh` may be absent or unauthenticated on the machine that needs to
        # change this, and the setting is not one the loop can set for itself.
        text = setup_text().lower()
        for label in ("read and write permissions", "create and approve pull requests"):
            with self.subTest(label=label):
                self.assertIn(label, text)

    def put_command(self, text: str) -> str | None:
        """The `gh api -X PUT` invocation, line-continuations collapsed."""
        match = re.search(
            r"gh api -X PUT [^\n]*?" + re.escape(self.ENDPOINT) + r".*?"
            r"can_approve_pull_request_reviews=\w+",
            text,
            re.DOTALL,
        )
        return re.sub(r"\s+", " ", match.group(0).replace("\\\n", " ")) if match else None

    def test_both_files_carry_the_same_fix_command(self):
        """Phase 0 needs it standalone; the record is what survives the install.

        Two copies, so pin them equal rather than trusting nobody edits one.
        """
        in_skill, in_record = self.put_command(skill_text()), self.put_command(setup_text())
        self.assertIsNotNone(in_skill, "SKILL.md gives no way to FIX what Phase 0 checks")
        self.assertIsNotNone(in_record, "the record gives no way to fix it either")
        self.assertEqual(in_skill, in_record, "the two copies of the fix have drifted")



class TheInstallerNamesNoHarnessSpecificTool(unittest.TestCase):
    """The installer must read correctly under any harness that can run it.

    Two harnesses ship, and the framework's own principle is that a harness is a
    config entry rather than framework code. Naming one harness's tool in the
    spine breaks that in the one file every install reads: an agent running
    elsewhere is told to call something it does not have, and the honest
    reading — "this instruction is not for me" — silently skips the rule.

    The fix is always to state the INTENT rather than the mechanism. A capable
    model works out how to put four choices to a person on its own platform.
    """

    # Tool names, not English words. `Read`/`Write`/`Edit` are excluded
    # deliberately: they collide with ordinary prose about reading and writing.
    HARNESS_TOOLS = ("AskUserQuestion", "TodoWrite", "WebFetch", "WebSearch",
                     "NotebookEdit", "ExitPlanMode", "SlashCommand")

    def _installer_docs(self):
        yield SKILL
        yield from sorted((SKILL.parent / "reference").glob("*.md"))
        yield from sorted((SKILL.parent / "artifacts").glob("*.md"))
        yield SKILL.parent / "loop_context" / "CLAUDE.md"

    def test_no_installer_facing_doc_names_one(self):
        checked = 0
        for path in self._installer_docs():
            if not path.is_file():
                continue
            checked += 1
            text = path.read_text(encoding="utf-8")
            for tool in self.HARNESS_TOOLS:
                with self.subTest(doc=path.name, tool=tool):
                    self.assertNotIn(
                        tool, text,
                        f"{path.name} names the harness-specific tool `{tool}`. "
                        f"State the intent instead, so the rule survives a "
                        f"different harness.",
                    )
        self.assertGreater(checked, 5, "no installer docs scanned; the check is vacuous")


class TheDeferredPlaceholdersAllNameTheirHome(unittest.TestCase):
    """Phase 3 established the pattern: a placeholder left unset is backfilled,
    and the backfill names WHERE and WHEN.

    `{{VERIFIED}}` is deferred by design — its value cannot exist until Phase
    9a runs — so Phase 9a must name the file it lands in. The first version
    said "record both as {{VERIFIED}}" with no file, which sends the installer
    to fill a value into a prompt and leaves `SETUP.md` shipping the literal.
    """

    def test_phase_9a_backfills_the_verified_placeholder_into_setup(self):
        m = re.search(
            r"backfill `\{\{VERIFIED\}\}` in `([A-Za-z./-]+)`", skill_text()
        )
        self.assertIsNotNone(
            m, "Phase 9a does not say where the {{VERIFIED}} backfill goes"
        )
        self.assertEqual(m.group(1), "SETUP.md")


class NoTemplateAssertsAResultItCannotHave(unittest.TestCase):
    """`INSTALL-REPORT.md` is written before the operator merges, and the
    dispatched watch is reachable only after the merge — so the template may
    not state the watch's result as obtained. The first version asserted it
    "reached IDLE" unconditionally: on every first install that is a result
    from a run that could not have happened."""

    def test_the_report_template_never_states_the_watch_result(self):
        text = (ARTIFACTS / "report.md").read_text(encoding="utf-8")
        self.assertNotIn(
            "reached IDLE", text,
            "report.md asserts a dispatched-watch result; on a first install "
            "the dispatch is not reachable until after the report is written",
        )


class NoDocCarriesATimeAnchor(unittest.TestCase):
    """The state docs' own convention: current fact only, nothing phrased
    relative to an earlier version. "A recent audit" is unresolvable to any
    later reader and goes stale at the next audit — and the README is the copy
    most likely to be read years from now."""

    DOCS = ("README.md", "SKILL.md")

    def test_no_shipped_doc_says_recent_or_lately(self):
        for name in self.DOCS:
            text = (FRAMEWORK / name).read_text(encoding="utf-8")
            hit = re.search(r"(?i)\b(recent|recently|lately|last week)\b", text)
            self.assertIsNone(
                hit, f"{name} carries a time anchor at {hit and hit.group(0)!r}"
            )


class TheGapCheckMatchesItsOwnExclusions(unittest.TestCase):
    """`updating.md`'s variable-gap command filters five names out. A paragraph
    that then says two of them "survive the strip" describes a command the file
    no longer contains — the drift this catches is the exclusion list and the
    prose moving apart."""

    def test_nothing_is_said_to_survive_a_strip_that_filters_it(self):
        """The gap command filters five names out, and a "survives the strip"
        claim is stale by construction — the names it described are excluded,
        and the claim describes a command the file no longer contains.

        Matched on the PHRASE, not the variable names: the stale prose said
        "the cycle id and the bulk-variables blob", which a name-matching pin
        never sees. Any surviving-claim is about the filtered set, because
        nothing else was ever stripped.
        """
        text = (REFERENCE / "updating.md").read_text(encoding="utf-8")
        self.assertNotIn(
            "survive the strip", text,
            "updating.md describes names surviving a strip its own command "
            "filters out",
        )


class TheManifestGeneratorRecordsOnlyWhatTheFrameworkOwns(unittest.TestCase):
    """`updating.md` says what the manifest covers; its generator must agree.

    The prose is explicit — the vendored core and the two workflows, "not
    `adapters/target.py` and not `SETUP.md`, which the framework does not own
    and never overwrites". The generator `rglob`-ed all of `.shl/` and recorded
    them anyway, and the state table sends an unmodified framework-owned file to
    OVERWRITE. So following the documented update replaced the operator's own
    adapter, and the install's own record of itself, with framework content.

    EXECUTED against a simulated install rather than read: the exclusion and the
    prose were each independently sensible, and only running them together shows
    they disagree. Same reason the sibling check below executes its snippet.
    """

    TARGET_OWNED = ("adapters/target.py", "SETUP.md", "README.md", "INSTALL-REPORT.md")
    FRAMEWORK_OWNED = ("loop.py", "guardrails/gate.py", "opencode.json")

    def snippet(self) -> str:
        text = (REFERENCE / "updating.md").read_text(encoding="utf-8")
        match = re.search(r'python3 -B -c "\n(.*?)\n\s*"', text, re.DOTALL)
        self.assertIsNotNone(match, "updating.md has no manifest generator snippet")
        return textwrap.dedent(match.group(1))

    def _generate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in self.TARGET_OWNED + self.FRAMEWORK_OWNED:
                path = root / ".shl" / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            flows = root / ".github" / "workflows"
            flows.mkdir(parents=True)
            for name in ("watch.yml", "heal.yml"):
                (flows / name).write_text("on: {}\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-B", "-c", self.snippet()],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads((root / ".shl" / "manifest.json").read_text(encoding="utf-8"))

    def test_target_owned_files_are_not_recorded(self):
        recorded = self._generate()
        wrongly = [rel for rel in self.TARGET_OWNED if f".shl/{rel}" in recorded]
        self.assertEqual(
            wrongly, [],
            f"the generator files these as framework-owned, so the next update "
            f"overwrites work the framework did not write: {wrongly}",
        )

    def test_framework_owned_files_still_are(self):
        # The exclusion must not be so broad that the manifest stops covering
        # what it exists for; an empty manifest passes the check above.
        recorded = self._generate()
        for rel in self.FRAMEWORK_OWNED:
            with self.subTest(path=rel):
                self.assertIn(f".shl/{rel}", recorded)
        self.assertIn(".github/workflows/heal.yml", recorded)

    def test_the_prose_and_the_generator_name_the_same_exclusions(self):
        text = (REFERENCE / "updating.md").read_text(encoding="utf-8")
        prose = text.partition("## Read the manifest")[2].partition("|")[0]
        for rel in ("adapters/target.py", "SETUP.md"):
            with self.subTest(path=rel):
                self.assertIn(rel, prose)


class TheManifestCheckCanActuallyPass(unittest.TestCase):
    """A correct install must satisfy the check the installer runs on it.

    The manifest snippet filters scratch by extension, and `opencode.json` is a
    shipped file whose extension is on that list. So a perfectly correct install
    reports it missing, and the phase that proves the copy is complete fails on
    every run. Asserting on the snippet's TEXT cannot catch this: the `want` set
    and the filter are both individually right, and only running them together
    against a real directory shows they contradict each other.

    The directory name is read out of the snippet rather than written here, so
    this stays honest if the installed tree is ever renamed.
    """

    def snippet(self) -> str:
        match = re.search(r'python3 -c "\n(.*?)\n\s*"', skill_text(), re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md has no manifest-check snippet")
        return textwrap.dedent(match.group(1))

    def loop_dir(self) -> str:
        match = re.search(r"os\.listdir\('([^']+)'\)", self.snippet())
        self.assertIsNotNone(match, "the snippet does not list a directory")
        return match.group(1)

    def wanted(self) -> set[str]:
        match = re.search(r"want\s*=\s*\{(.*?)\}", self.snippet(), re.DOTALL)
        return set(re.findall(r"'([^']+)'", match.group(1)))

    def test_a_tree_holding_exactly_the_manifest_is_reported_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / self.loop_dir()
            root.mkdir(parents=True)
            for name in self.wanted():
                # Directories vs files does not matter to os.listdir; every
                # manifest entry just has to exist under that name.
                (root / name).write_text("", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-c", self.snippet()],
                cwd=tmp, text=True, capture_output=True,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"a correct install fails its own manifest check: {proc.stderr.strip()}",
            )
            self.assertIn("manifest clean", proc.stdout)

    def test_the_manifest_phase_4_writes_does_not_fail_phase_4s_own_check(self):
        # Phase 4 writes `manifest.json` and then runs this check over the same
        # directory, so the check has to tolerate a file the install itself just
        # created. It survives on the scratch rule rather than on a name: it
        # ends in `.json` and is absent from `want`, so it is filtered out of
        # `have`. That is the opposite arrangement to `opencode.json`, which is
        # IN `want` and needs the explicit scratch exemption — and getting the
        # two the wrong way round is what made this check unpassable before.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / self.loop_dir()
            root.mkdir(parents=True)
            for name in self.wanted():
                (root / name).write_text("", encoding="utf-8")
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-c", self.snippet()],
                cwd=tmp, text=True, capture_output=True,
            )
            self.assertEqual(
                proc.returncode, 0,
                "Phase 4 writes manifest.json and its own check then rejects "
                f"the tree: {proc.stderr.strip()}",
            )

    def test_a_missing_core_file_is_still_caught(self):
        # The check must not have been loosened into uselessness.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / self.loop_dir()
            root.mkdir(parents=True)
            for name in self.wanted() - {"loop.py"}:
                (root / name).write_text("", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-c", self.snippet()],
                cwd=tmp, text=True, capture_output=True,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("loop.py", proc.stderr)

    def test_cycle_scratch_is_still_ignored(self):
        # A re-run on an installed loop must tolerate what cycles leave behind.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / self.loop_dir()
            root.mkdir(parents=True)
            for name in self.wanted():
                (root / name).write_text("", encoding="utf-8")
            for scratch in ("signal.txt", "diagnose.json", "fix.diff", "suite_raw.raw"):
                (root / scratch).write_text("", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-c", self.snippet()],
                cwd=tmp, text=True, capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.strip())


class TheFrontmatterTheHarnessParses(unittest.TestCase):
    """The `---` block decides whether this skill can be found or run at all.

    Everything else in this file is prose an agent reads once it is already
    running. The frontmatter is the part a harness parses: `name` is what the
    operator types and what the loader keys on, `description` is what a model
    matches a request against. Nothing checked either, so a name with a space,
    a dropped key or a broken block would ship as a skill that silently never
    resolves — and this file is the one place a defect cannot be caught by the
    loop's own tests, because the loop is not running yet.
    """

    def frontmatter(self) -> dict:
        text = skill_text()
        match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md does not open with a frontmatter block")
        pairs = {}
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            self.assertTrue(_, f"frontmatter line is not `key: value`: {line!r}")
            pairs[key.strip()] = value.strip()
        return pairs

    def test_it_carries_both_keys_a_loader_needs(self):
        missing = {"name", "description"} - set(self.frontmatter())
        self.assertFalse(missing, f"frontmatter is missing {sorted(missing)}")

    def test_the_name_is_a_usable_skill_identifier(self):
        name = self.frontmatter()["name"]
        # Lowercase, hyphen-separated, no spaces: the form every harness accepts
        # and the form an operator can type. Pinned as a shape, not as a literal,
        # so renaming the product does not require editing a test to match.
        self.assertRegex(
            name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
            f"{name!r} is not a usable skill identifier",
        )

    def test_the_description_says_when_to_use_it(self):
        # A description that only says what the skill IS gives a model nothing
        # to match a request against, so the skill exists and never fires.
        self.assertRegex(self.frontmatter()["description"], r"(?i)\buse when\b")

    def test_the_title_and_the_name_describe_the_same_thing(self):
        # A rename that touches one and not the other leaves the operator typing
        # a name the document never mentions.
        name = self.frontmatter()["name"]
        title = next(l for l in skill_text().splitlines() if l.startswith("# "))
        words = [w for w in name.split("-") if len(w) > 3]
        for word in words:
            with self.subTest(word=word):
                self.assertIn(word, title.lower(), f"the H1 {title!r} never says {word!r}")


class TheInstallRecordsWhatItVendored(unittest.TestCase):
    """Update mode compares content against a record, so the install must write one.

    Without `manifest.json` an update can see that a vendored file differs from
    the current framework and cannot tell WHY: stale, or deliberately patched by
    the operator. Overwriting on that guess destroys a local change silently;
    skipping on it leaves a stale copy silently. The record is the only thing
    separating the two, and it is written at install or never.
    """

    def test_phase_4_writes_the_manifest(self):
        self.assertRegex(
            skill_text(),
            r"(?m)^- \*\*Write `\.shl/manifest\.json`\*\*",
            "Phase 4 no longer records what it vendored, so every later update "
            "has to guess whether a difference is staleness or a local patch",
        )

    def test_an_existing_install_is_routed_to_the_update_branch(self):
        # The reference is inert unless something loads it, and Phase 2 is the
        # only place that decides install-versus-update.
        phase2 = skill_text().split("## Phase 2")[1].split("## Phase 3")[0]
        self.assertIn(
            "reference/updating.md",
            phase2,
            "Phase 2 detects an existing install but never loads the branch "
            "that says what may be overwritten",
        )

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_the_manifest_survives_the_gitignore_the_install_writes(self):
        """The sibling check above only covers files the framework ships.

        `manifest.json` is written at install, so it is not in that set and was
        not covered by anything. The vendored gitignore drops `*.json` to keep
        raw agent output out of commits, which swallows the manifest too — and
        an ignored manifest is absent from the next clone, read as "no install",
        and answered with a fresh install over a live loop.

        Run against real git: gitignore negation precedence is the thing under
        test, so a hand-rolled matcher would be testing itself.
        """
        block = re.search(r"```gitignore\n(.*?)```", skill_text(), re.DOTALL)
        self.assertIsNotNone(block, "SKILL.md no longer writes a target .gitignore")
        rules = textwrap.dedent(block.group(1))

        survive = ["manifest.json", "opencode.json", "incident_memory/log.jsonl"]
        # Without this the three assertions below all pass on a gitignore that
        # dropped `*.json` entirely — which would commit every agent payload.
        must_be_ignored = "diagnose.json"
        probes = survive + [must_be_ignored]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitignore").write_text(rules, encoding="utf-8")
            (root / "incident_memory").mkdir()
            for name in probes:
                (root / name).touch()
            result = subprocess.run(
                ["git", "check-ignore", "--stdin", "-v"],
                cwd=root, input="\n".join(probes), capture_output=True, text=True,
            )
        ignored = {
            line.rsplit("\t", 1)[1]
            for line in result.stdout.splitlines()
            if line and not line.rsplit("\t", 1)[0].rsplit(":", 1)[1].startswith("!")
        }
        for name in survive:
            with self.subTest(path=name):
                self.assertNotIn(
                    name, ignored,
                    f"the installed .gitignore hides {name}, so it never reaches "
                    "the commit and the next clone behaves as though it does not exist",
                )
        self.assertIn(
            must_be_ignored, ignored,
            "the scratch rule stopped matching, so raw agent output would be committed",
        )


class NoDocClaimsAPatternTheGateDoesNotHave(unittest.TestCase):
    """Three files restate the gate's built-in assertion and skip forms.

    The direction that costs something is a doc naming a form the gate cannot
    actually see: the installer reads "the built-in covers `should_`", omits
    `SHL_ASSERT_PATTERN`, and the no-weakening check then polices nothing on
    that target — silently, on the one guard standing in front of the default
    branch. Under-listing is safe by comparison, so only over-claiming is
    asserted here.

    The lists had already drifted apart across the three files, which is why
    this reads them all rather than the spine alone.
    """

    # Backtick-slash runs, which is how all three sites write these lists. An
    # earlier form anchored on "built-in" and matched non-greedily, so on the
    # one sentence carrying four lists it captured only the first and the
    # assertion and skip forms — the ones the finding was about — went
    # unchecked. Collect every run, then filter.
    _RUN = re.compile(r"(?:`[^`]+`/)+`[^`]+`")

    def _claimed(self, text: str) -> set[str]:
        """Bare word forms named in a slash list.

        A token carrying `.` or `*` is a filename or a glob, not an assertion
        or skip form: that drops the test-file globs and the runner-config
        names sharing the sentence, and it drops Go's `t.Error`/`t.Fatal`,
        which the docs name precisely to say the gate does NOT see them.
        Structural rather than a list of exempt names, so a doc cannot quietly
        add a claim the filter happens to spell.
        """
        return {
            tok.strip("`")
            for run in self._RUN.findall(text)
            for tok in run.split("/")
            if "." not in tok.strip("`") and "*" not in tok
        }

    def test_every_named_form_is_in_the_compiled_pattern(self):
        from guardrails.gate import _ASSERT_RE, _SKIP_RE

        seen = 0
        for path in (SKILL, SETUP_TEMPLATE):
            for form in sorted(self._claimed(path.read_text(encoding="utf-8"))):
                seen += 1
                with self.subTest(doc=path.name, form=form):
                    self.assertTrue(
                        _ASSERT_RE.search(form) or _SKIP_RE.search(form),
                        f"{path.name} tells the installer the gate recognises "
                        f"{form!r}, and neither compiled pattern matches it — "
                        "so the variable that would have covered it gets skipped",
                    )
        # Pinned rather than a floor. A floor of one list's worth still passes
        # when a doc drops a whole list, which is the drift worth catching —
        # both files currently name the same seven forms. Changing a list is
        # allowed; changing this number in the same commit is the point.
        self.assertEqual(
            seen, 14,
            f"parsed {seen} claimed forms, expected 14 (7 per doc). A doc "
            "gained or lost a built-in list; confirm it matches gate.py and "
            "update this count deliberately.",
        )


class TheReadmeStatesTheRealAttemptCap(unittest.TestCase):
    """`.shl/README.md` names the cap as a number, so the number has to be right.

    It used to be a `{{ATTEMPT_CAP}}` placeholder, which invited the installer
    to invent a value: nothing configures the cap, `loop.under_attempt_cap`
    hard-codes it. Writing the literal removes that invitation and creates the
    opposite risk — the constant moves and the sentence the operator reads goes
    stale, telling them the loop gives up later than it does. This is the only
    thing tying the two together.
    """

    def test_the_number_matches_the_code(self):
        cap = inspect.signature(loop.under_attempt_cap).parameters["cap"].default
        text = (ARTIFACTS / "readme.md").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            rf"escalates after {cap}\b",
            f"under_attempt_cap defaults to {cap}; artifacts/readme.md says "
            "something else, and the operator reads the readme",
        )

    def test_no_placeholder_offers_to_configure_it(self):
        # A placeholder here is not a formatting slip: it reads as a knob, and
        # there is no SHL_ATTEMPT_CAP for it to be filled from.
        self.assertNotIn(
            "ATTEMPT_CAP", (ARTIFACTS / "readme.md").read_text(encoding="utf-8")
        )


class TheReadmeCreditsThePrWithOnlyWhatItHasPassed(unittest.TestCase):
    """`heal.yml` opens the PR first and reviews it after — in that order.

    The readme's whole job is telling a person what a bot-authored PR already
    survived, and the review is the one item on that list that has NOT
    happened when the PR appears. Listing it there is not a wording slip: an
    open PR carrying a review the loop's own reviewer BLOCKED looks identical
    to one it approved, since the block step comments its reason and exits,
    leaving the PR open. A reader told the review is a precondition reads that
    PR as endorsed.

    `test_workflows.WorkflowStepOrder.test_the_pr_exists_before_the_review_that_comments_on_it`
    pins the ordering this rests on.
    """

    def _pre_pr_bullets(self) -> list[str]:
        """Every line making the pre-PR claim: lead-in, bullets, sub-bullets.

        Collecting only column-0 bullets left two ways to keep the false claim
        and pass: put it in the lead-in sentence, or indent it as a sub-bullet —
        which did not merely go unchecked, it ENDED the scan, so everything
        after it was unexamined too. Both were proven to pass by mutation.
        """
        text = (ARTIFACTS / "readme.md").read_text(encoding="utf-8")
        after = text.partition("Before any PR opens")[2]
        self.assertTrue(after, "readme.md no longer says what precedes a PR")
        lines = after.splitlines()
        # The remainder of the lead-in line is part of the claim.
        block = [lines[0]] if lines and lines[0].strip() else []
        for line in lines[1:]:
            if not line.strip():
                continue
            # A bullet at any indent, or a continuation of one, stays in the
            # claim; the first paragraph at column 0 ends it.
            if line.startswith(("-", " ", "\t")):
                block.append(line)
                continue
            break
        self.assertTrue(block, "found no pre-PR claim to check")
        self.assertTrue(
            any(ln.lstrip().startswith("-") for ln in block),
            "the pre-PR claim has no bullets at all; the list this checks is gone",
        )
        return block

    def test_the_pre_pr_list_claims_no_review(self):
        for bullet in self._pre_pr_bullets():
            self.assertNotRegex(
                bullet, r"(?i)review",
                "the readme lists the review among what a PR has already "
                "passed, and heal.yml runs it after the PR is open",
            )

    def test_the_readme_still_explains_where_the_review_fits(self):
        # Deleting the sentence would satisfy the check above while leaving the
        # reader with no account of when the review happens — a worse answer
        # than the wrong one, since the review is what decides the merge. A bare
        # search for "review" cannot hold this: the opening paragraph already
        # says the loop "reviews it", so the claim has to be the ORDERING.
        text = (ARTIFACTS / "readme.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?i)after(?:\*\*)? the PR is open")


if __name__ == "__main__":
    unittest.main()
