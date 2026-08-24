"""The installed directory's name, wherever enforcing something depends on it.

Three places decide whether a fix can rewrite the machinery that judges it, and
each one FAILS OPEN when its copy of the name is wrong:

  * `opencode.json`'s edit deny — a stale pattern matches nothing, so the Fix
    agent may edit the loop's own tree under OpenCode.
  * `gate.LOOP_DIR` — a stale prefix matches no path, so `is_loop_tree_touched`
    reports every tamper as clean.
  * `heal.yml`'s `git diff --quiet HEAD -- <dir>` — the worst of the three,
    because git exits 0 for a path that does not exist. A wrong name here does
    not error; it silently reports success on every cycle.

They cannot share a constant at runtime: one is Python, one JSON, one YAML.
So the constant is `gate.LOOP_DIR` and these tests are the enforcement, which
is why they assert the name rather than merely that the files agree with each
other — three files agreeing on a directory nobody installs is still a no-op.
"""
import json
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from guardrails.gate import LOOP_DIR, is_loop_tree_touched

FRAMEWORK = Path(__file__).resolve().parents[1]
WORKFLOWS = FRAMEWORK / "workflows"
SKILL = FRAMEWORK / "SKILL.md"
OPENCODE = FRAMEWORK / "opencode.json"

EXPECTED = ".shl/"


class TheLoopDirectoryHasOneCanonicalName(unittest.TestCase):
    def test_the_constant_is_the_installed_name(self):
        self.assertEqual(LOOP_DIR, EXPECTED)

    def test_a_similarly_named_project_path_is_not_the_loop(self):
        # At the ROOT, so the prefix genuinely overlaps. Under `docs/` it
        # shares nothing with `.shl/` and passes whether the guard compares
        # the name with its separator or without.
        diff = "diff --git a/.shldocs/x.md b/.shldocs/x.md\n+text\n"
        self.assertFalse(is_loop_tree_touched(diff))


class EveryEnforcementSiteUsesThatName(unittest.TestCase):
    def test_opencode_denies_edits_under_it(self):
        config = json.loads(OPENCODE.read_text(encoding="utf-8"))
        denies = [
            pattern
            for agent in config.get("agent", {}).values()
            for pattern, action in (agent.get("permission", {}).get("edit") or {}).items()
            if action == "deny"
        ]
        self.assertTrue(denies, "no edit deny rule at all; the Fix agent is unrestricted")
        self.assertTrue(
            any(LOOP_DIR.rstrip("/") in pattern for pattern in denies),
            f"OpenCode's edit deny does not scope to {LOOP_DIR}: {denies}",
        )

    def test_the_workflows_git_guard_names_it(self):
        text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        self.assertIn(
            f"git diff --quiet HEAD -- {LOOP_DIR}", text,
            "the workflow's independent loop-tree check does not name the installed directory",
        )

    def test_no_stale_directory_name_survives_anywhere(self):
        # A half-finished rename is the failure mode this whole file exists for.
        stale = ".self-healing-loop"
        scanned = 0
        for path in sorted(FRAMEWORK.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or ".git" in path.parts:
                continue
            if path.suffix not in {".py", ".yml", ".json", ".md", ".gitignore"} and path.name != ".gitignore":
                continue
            if path.name == "test_loop_dir.py":  # names it deliberately, above
                continue
            scanned += 1
            with self.subTest(path=str(path.relative_to(FRAMEWORK))):
                self.assertNotIn(stale, path.read_text(encoding="utf-8", errors="replace"))
        # A filter that stops matching reports the tree clean forever, which is
        # indistinguishable from a rename that finished. Its sibling in
        # `test_file_naming.py` carries the same guard for the same reason.
        self.assertGreater(scanned, 40, f"the sweep inspected only {scanned} files")


class TheGuardedPathIsOneThatActuallyExists(unittest.TestCase):
    """`git diff --quiet HEAD -- <path>` exits 0 for a path that is not there.

    So the workflow's independent check is only a check while the directory it
    names is the directory the installer creates. If the two ever diverge the
    step keeps passing, reports success, and guards nothing — which is exactly
    how a rename turns a control into decoration.
    """

    def test_the_installer_writes_to_the_directory_the_guard_watches(self):
        # The COPY instructions, not any mention. A backticked path anywhere in
        # the file satisfies a bare containment check, including a sentence
        # telling an installer not to write there.
        skill = SKILL.read_text(encoding="utf-8")
        name = re.escape(LOOP_DIR.rstrip("/"))
        copies = re.findall(rf"(?im)^[^\n]*\bcopy\b[^\n]*`{name}/", skill)
        self.assertTrue(
            copies,
            f"SKILL.md never instructs a copy INTO {LOOP_DIR}, so the git check "
            "in heal.yml runs against a path that does not exist and passes",
        )
        # The three `artifacts/*.md` copies also land in the loop dir, so a bare
        # count is satisfied by them alone — and none of them puts a line of the
        # framework's own code there. The guard exists to police the VENDORED
        # CORE; these are the two instructions that put it in place.
        for required in ("manifest", "loop_context/CLAUDE.md"):
            self.assertTrue(
                [line for line in copies if required in line],
                f"no copy instruction vendors {required} into {LOOP_DIR}, so the "
                "directory the guard watches holds documents and no loop",
            )

    def test_the_manifest_check_lists_that_same_directory(self):
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn(f"os.listdir('{LOOP_DIR.rstrip('/')}')", skill)



class TheGuardSeesCreationAndNotOnlyModification(unittest.TestCase):
    """`git diff` reports modified TRACKED files, never untracked ones.

    So a fix that CREATES a file under the loop dir walks past a guard built
    only on `git diff --quiet`, and creation is the cheaper tamper: an interpreter
    started with the loop dir on PYTHONPATH imports `sitecustomize.py` from it at
    startup, before `-m guardrails.cli` loads, and `-B` disables bytecode rather
    than `site`. The vendored .gitignore covers scratch, not `*.py`, so the file
    would be committed and run on every later cycle.

    Executed against a real repository rather than asserted against the YAML,
    because the whole point is what git does, not what the file says.

    The script under test is LIFTED FROM `heal.yml`, not rebuilt here. A
    reconstruction proves only that the two lines this file writes behave as
    expected: redirect the shipped `ls-files` call into `/dev/null` and
    `$created` is empty on every cycle, yet a rebuilt copy still catches the
    created file and the string search below still finds both commands. Same
    wrong-seam defect this project has shipped three times — testing the
    predicate instead of the caller.
    """

    def _shipped_script(self) -> str:
        """The loop-tree half of the guard step, verbatim from the workflow."""
        text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        start = text.index(f'created="$(git ls-files --others --exclude-standard -- {LOOP_DIR}')
        end = text.index("\n          fi\n", start) + len("\n          fi\n")
        block = textwrap.dedent(text[text.rindex("\n", 0, start) + 1:end])
        assert "exit 1" in block, "the lifted block carries no refusal"
        return block

    def _guard(self, repo: Path) -> int:
        return subprocess.run(
            ["bash", "-e"], input=self._shipped_script(), text=True,
            capture_output=True, cwd=repo,
        ).returncode

    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        loop = repo / LOOP_DIR
        loop.mkdir(parents=True)
        (loop / ".gitignore").write_text(
            "evidence/\n*.json\n!opencode.json\n*.txt\n*.diff\n*.raw\n__pycache__/\n*.pyc\n"
        )
        (loop / "loop.py").write_text("# vendored\n")
        for cmd in (["git", "init", "-q", "."],
                    ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "install"]):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
        return repo

    def test_ordinary_cycle_scratch_does_not_trip_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            loop = repo / LOOP_DIR
            (loop / "evidence").mkdir()
            for scratch in ("signal.txt", "diagnose.json", "fix.diff", "suite_raw.raw"):
                (loop / scratch).write_text("x")
            self.assertEqual(self._guard(repo), 0, "the guard tripped on gitignored scratch")

    def test_a_created_source_file_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            (repo / LOOP_DIR / "sitecustomize.py").write_text("# executes at interpreter startup\n")
            self.assertEqual(self._guard(repo), 1, "a created .py under the loop dir went unreported")

    def test_a_modified_tracked_file_is_still_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            (repo / LOOP_DIR / "loop.py").write_text("# tampered\n")
            self.assertEqual(self._guard(repo), 1)

    def test_the_shipped_workflow_uses_both_halves(self):
        text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        self.assertIn(f"git ls-files --others --exclude-standard -- {LOOP_DIR}", text)
        self.assertIn(f"git diff --quiet HEAD -- {LOOP_DIR}", text)
        # Computing the untracked list is not the same as ACTING on it: the
        # refusal must branch on both, or the creation half is dead code that
        # reads as coverage.
        refusals = [
            ln for ln in text.splitlines()
            if f"git diff --quiet HEAD -- {LOOP_DIR}" in ln and ln.strip().startswith("if")
        ]
        self.assertTrue(refusals, "no `if` refuses on the git diff check")
        for line in refusals:
            self.assertIn(
                "$created", line,
                f"the refusal ignores created files, so a new .py walks past it: {line.strip()}",
            )

if __name__ == "__main__":
    unittest.main()
