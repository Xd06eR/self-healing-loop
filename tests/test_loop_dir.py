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



def _loop_tree_half() -> str:
    """The loop-tree half of the FIRST guard step, verbatim from the workflow.

    Lifted rather than rebuilt. A reconstruction proves only that the lines the
    test writes behave as expected: redirect the shipped `ls-files` call into
    `/dev/null` and `$created` is empty on every cycle, yet a rebuilt copy still
    catches the created file and a string search still finds both commands. Same
    wrong-seam defect this project has shipped three times — testing the
    predicate instead of the caller.
    """
    text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
    # Bounded to the FIRST guard step, not searched for across the file. The
    # command text is not a safe anchor: there are two guards now, so blinding
    # this one made `index` skip to the second, both sides of the identity
    # check below then matched, and a mutation that removed the creation query
    # from the shipped guard passed. The step name is what identifies the step.
    opener = "- name: Loop tree untouched — checked by git"
    step_start = text.index(opener)
    step_end = text.index("\n      - name: ", step_start + len(opener))
    step = text[step_start:step_end]
    assert "Loop tree untouched again" not in step, "the two guard steps were confused"

    start = step.index(f'created="$(git ls-files --others --exclude-standard -- {LOOP_DIR}')
    end = step.index("\n          fi\n", start) + len("\n          fi\n")
    block = textwrap.dedent(step[step.rindex("\n", 0, start) + 1:end])
    assert "exit 1" in block, "the lifted block carries no refusal"
    return block


def _installed_repo(tmp: str) -> Path:
    """A throwaway repo shaped like an install: committed loop dir + its gitignore."""
    repo = Path(tmp)
    loop = repo / LOOP_DIR
    loop.mkdir(parents=True)
    (loop / ".gitignore").write_text(
        "evidence/\n*.json\n!opencode.json\n*.txt\n*.diff\n*.raw\n__pycache__/\n*.pyc\n"
    )
    (loop / "loop.py").write_text("# vendored\n")
    (repo / "src.py").write_text("# target source\n")
    for cmd in (["git", "init", "-q", "."],
                ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "install"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


class TheGuardSeesCreationAndNotOnlyModification(unittest.TestCase):
    """`git diff` reports modified TRACKED files, never untracked ones.

    So a fix that CREATES a file under the loop dir walks past a guard built
    only on `git diff --quiet`, and creation is the cheaper tamper: an interpreter
    started with the loop dir on PYTHONPATH imports `sitecustomize.py` from it at
    startup, before `-m guardrails.cli` loads, and `-B` disables bytecode rather
    than `site`. The vendored .gitignore covers scratch, not `*.py`, so the file
    would be committed and run on every later cycle.

    Executed against a real repository rather than asserted against the YAML,
    because the whole point is what git does, not what the file says. The script
    under test is lifted from `heal.yml` by `_loop_tree_half`, not rebuilt here.
    """

    def _guard(self, repo: Path) -> int:
        return subprocess.run(
            ["bash", "-e"], input=_loop_tree_half(), text=True,
            capture_output=True, cwd=repo,
        ).returncode

    def test_ordinary_cycle_scratch_does_not_trip_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _installed_repo(tmp)
            loop = repo / LOOP_DIR
            (loop / "evidence").mkdir()
            for scratch in ("signal.txt", "diagnose.json", "fix.diff", "suite_raw.raw"):
                (loop / scratch).write_text("x")
            self.assertEqual(self._guard(repo), 0, "the guard tripped on gitignored scratch")

    def test_a_created_source_file_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _installed_repo(tmp)
            (repo / LOOP_DIR / "sitecustomize.py").write_text("# executes at interpreter startup\n")
            self.assertEqual(self._guard(repo), 1, "a created .py under the loop dir went unreported")

    def test_a_modified_tracked_file_is_still_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _installed_repo(tmp)
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


class TheGuardRunsAgainAfterTheSuite(unittest.TestCase):
    """The guard above runs before the last agent-authored code does.

    `Green` and `Run suite` execute the repro test and the target's suite with a
    full shell, and both come AFTER `Loop tree untouched` has already passed.
    Every step below them starts an interpreter with `PYTHONPATH=.shl`, which
    imports `.shl/sitecustomize.py` at startup before `-m guardrails.cli` loads
    — `-B` disables bytecode, not `site`, and the vendored .gitignore covers
    scratch, not `*.py`. So a repro test that plants that file at Green or `Run
    suite` executes inside the gate that judges the fix, inside the commit and
    the verdict comment that carry the write credential, and inside the review
    that carries the provider credential. Nothing between it and them looks.

    ONLY the git-only half repeats, and that is what makes a second instance
    safe. `git diff --quiet HEAD` and `git ls-files --others` are both fresh
    queries against the working tree with no baseline, so a second instance
    cannot disagree with an earlier measurement. The execution-surface hash
    compares a value across a step boundary; that shape is what once made this
    guard refuse every cycle on every repo, so it stays where it is.
    """

    # The em dash discriminates: the first guard is "Loop tree untouched —",
    # this one "Loop tree untouched again", so neither prefix matches both.
    PREFIX = "Loop tree untouched again"
    FIRST = "Loop tree untouched —"

    def _step(self, prefix: str | None = None) -> dict:
        prefix = prefix or self.PREFIX
        text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        for chunk in re.split(r"^ {6}- (?=name:)", text, flags=re.MULTILINE)[1:]:
            name = re.match(r"name: (.+)", chunk)
            if not name or not name.group(1).strip().startswith(prefix):
                continue
            body = re.search(r"^ {8}run: \|\s*\n(.*?)(?=^ {6,8}\S|\Z)", chunk, re.M | re.S)
            cond = re.search(r"^ {8}if: (.+)$", chunk, flags=re.MULTILINE)
            self.assertIsNotNone(body, f"{name.group(1).strip()!r} has no run body")
            return {
                "name": name.group(1).strip(),
                "run": textwrap.dedent(body.group(1)),
                "cond": cond.group(1) if cond else "",
            }
        raise AssertionError(
            f"heal.yml has no step named {prefix!r}: nothing re-checks the loop "
            "tree between the suite and the steps that hold a credential"
        )

    def _names(self) -> list:
        text = (WORKFLOWS / "heal.yml").read_text(encoding="utf-8")
        return re.findall(r"^ {6}- (?:name|uses): (.+?)\s*$", text, re.MULTILINE)

    def test_it_sits_after_the_last_agent_code_and_before_the_gate(self):
        # Position is the whole control. Before `Run suite` it is the step
        # already there; after `Gate` the interpreter that judges the fix has
        # already imported whatever was planted.
        names = self._names()
        here = names.index(self._step()["name"])
        self.assertGreater(here, names.index("Run suite"))
        self.assertLess(here, next(i for i, n in enumerate(names) if n.startswith("Gate")))

    def test_it_runs_on_every_cycle_the_first_guard_does(self):
        # An `if:` can retire a step in place while every other check here still
        # passes on a body it no longer runs. Pinned to the first guard's
        # condition, which `test_workflows.py` in turn pins to the step that
        # commits — so the two instances cannot diverge on reachability.
        self.assertEqual(self._step()["cond"], self._step(self.FIRST)["cond"])

    def test_it_does_not_repeat_the_cross_step_comparison(self):
        """The reason this step is safe to add, asserted rather than described.

        Hashing git's execution surface here would compare a value against an
        output published by an earlier step. That is the shape that once made
        this guard refuse every cycle on every repo, unconditionally, with a
        green suite — so a second instance must carry no Actions expression and
        no snapshot at all, only the two self-contained git queries.
        """
        run = self._step()["run"]
        for forbidden in ("git-exec-surface", "git_surface", "sha256sum", "${{"):
            self.assertNotIn(
                forbidden, run,
                f"the second guard carries {forbidden!r}, so its verdict depends on "
                "something measured in another step",
            )

    def test_its_commands_are_the_shipped_ones(self):
        # Two copies of a check that drift are one check and one decoration.
        # The `::error::` line is exempt: it names which agent-authored code is
        # under suspicion, and at this position that is the suite, not the fix.
        def commands(block: str) -> list:
            return [ln for ln in block.strip().splitlines() if "::error::" not in ln]

        self.assertEqual(commands(self._step()["run"]), commands(_loop_tree_half()))

    def _run(self, repo: Path) -> int:
        return subprocess.run(
            ["bash", "-e"], input=self._step()["run"], text=True,
            capture_output=True, cwd=repo,
        ).returncode

    def _after_a_cycle(self, tmp: str) -> Path:
        """A repo as `Run suite` leaves it: everything a real cycle has by then.

        Fix's own output is part of that and is the reason both commands carry a
        `.shl/` pathspec: an edited source file and a newly written regression
        test are present on every healthy cycle, so a guard that looked at the
        whole tree would refuse all of them.
        """
        repo = _installed_repo(tmp)
        (repo / "src.py").write_text("# fixed by the loop\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_repro.py").write_text("def test_x():\n    assert True\n")
        loop = repo / LOOP_DIR
        (loop / "frozen_path.txt").write_text("tests/test_repro.py")
        (loop / "suite_raw.txt").write_text("1 failed, 40 passed\n")
        (loop / "current_raw.txt").write_text("tests/test_repro.py::test_x\n")
        evidence = loop / "evidence" / "c1"
        evidence.mkdir(parents=True)
        (evidence / "suite.txt").write_text("scrubbed\n")
        (evidence / "current_failures.txt").write_text("scrubbed\n")
        return repo

    def test_ordinary_cycle_scratch_does_not_trip_it(self):
        """The assertion that decides whether this step is a fix or a regression.

        A guard that refuses on the files `Run suite` legitimately leaves would
        block every cycle on every repo while looking like a working check —
        which is exactly what the last change to this guard did.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._after_a_cycle(tmp)
            self.assertEqual(self._run(repo), 0, "the second guard refuses a normal cycle")

    def test_a_test_that_plants_sitecustomize_at_green_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._after_a_cycle(tmp)
            (repo / LOOP_DIR / "sitecustomize.py").write_text(
                "# imported by every PYTHONPATH=.shl step below this one\n"
            )
            self.assertEqual(
                self._run(repo), 1,
                "a file planted after the first guard reaches the gate and both "
                "credentialed steps unreported",
            )


if __name__ == "__main__":
    unittest.main()
