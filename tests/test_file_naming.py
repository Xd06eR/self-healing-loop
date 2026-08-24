"""The tree's file-naming case convention, pinned so it cannot drift.

The rule, which the tree already follows and nothing stated: **uppercase means a
person opens it first; lowercase means the machine loads it on demand.**

`artifacts/` is the proof the rule is real rather than invented. Its templates
are machinery the installer reads, so they are lowercase; the installer writes
them into a target as documents someone opens, so they arrive uppercase
(`setup.md` -> `SETUP.md`). The same content changes case because its reader
changes.

One exception, and it is not a softening: a handful of names are fixed by an
external convention rather than chosen here. A harness auto-loads `CLAUDE.md`
and `AGENTS.md` by those exact names, a skill is `SKILL.md`, and `README.md` is
the one filename that needs no explanation. Casing those by the rule would break
discovery, so they are uppercase wherever they sit.
"""
import unittest
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent.parent

# Fixed by an external convention: a harness, a skill loader, or universal habit
# looks for this exact spelling. Not a list of things exempt from the rule but a
# list of names this project does not get to choose.
EXTERNALLY_FIXED = {"SKILL.md", "README.md", "CLAUDE.md", "AGENTS.md"}

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}


def _tracked_files():
    for path in FRAMEWORK.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() or path.is_symlink():
            yield path


class FileNamesFollowTheCaseConvention(unittest.TestCase):
    def test_only_externally_fixed_names_are_uppercase(self):
        offenders = [
            str(p.relative_to(FRAMEWORK))
            for p in _tracked_files()
            if p.name not in EXTERNALLY_FIXED and p.name != p.name.lower()
        ]
        self.assertEqual(
            offenders,
            [],
            "uppercase means a person opens it first. These are machine-loaded, "
            "so they should be lowercase, or added to EXTERNALLY_FIXED with the "
            "convention that fixes their spelling",
        )

    def test_the_check_actually_inspects_files(self):
        # Without this, an rglob that returned nothing would report the tree
        # clean forever — the shape of failure this project keeps meeting.
        self.assertGreater(len(list(_tracked_files())), 40)

    def test_every_externally_fixed_name_is_present(self):
        # A name kept on the exemption list after its file is gone quietly
        # widens the rule. Each entry has to be earning it.
        names = {p.name for p in _tracked_files()}
        self.assertEqual(EXTERNALLY_FIXED - names, set())


class TemplatesAreMachineryAndArtifactsAreDocuments(unittest.TestCase):
    """The `artifacts/` case flip is the convention's own worked example."""

    def test_artifact_templates_are_lowercase(self):
        templates = sorted(p.name for p in (FRAMEWORK / "artifacts").glob("*.md"))
        self.assertEqual(templates, [n.lower() for n in templates])
        self.assertGreater(len(templates), 0)

    def test_the_skill_writes_them_out_uppercase(self):
        # The flip is what the rule predicts, so it is asserted rather than
        # assumed: a template renamed without its write-out target renamed too
        # leaves the installer copying a file that is not there.
        # Both names on ONE line, because the pairing is the claim. Checked
        # separately, `README.md` is satisfied by any other mention of it in the
        # file — so lowercasing the write-out target passes while the installer
        # copies to a name the rule says is wrong.
        skill = (FRAMEWORK / "SKILL.md").read_text(encoding="utf-8")
        for template, artifact in (
            ("artifacts/setup.md", "SETUP.md"),
            ("artifacts/report.md", "INSTALL-REPORT.md"),
            ("artifacts/readme.md", "README.md"),
        ):
            pairs = [
                line for line in skill.splitlines()
                if template in line and f"/{artifact}" in line
            ]
            self.assertTrue(
                pairs,
                f"no single instruction copies {template} to a target's {artifact}",
            )


if __name__ == "__main__":
    unittest.main()
