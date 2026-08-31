"""The deterministic merge gate: did this fix weaken an existing test?

The real gate (LLM review is a backstop). The agent is told to make tests pass by
changing source, never by weakening tests, so a test-file diff that removes an
assertion or adds a skip/xfail/expectedFailure marker is a fix gaming its way to
green. Additive new tests are fine.

File-aware: a hunk is policed if EITHER the old or new path is a test file (by
naming convention rather than by language — see ``_DEFAULT_TEST_GLOBS``), so
renaming a test out of one convention to evade the check does not work, and a
JS target needs no extra globs for its filenames.

Heuristic, line-based — known ceilings (rewiring
a test's call site to a safe code path) are closed elsewhere: the frozen test
is tamper-proof via `is_frozen_test_touched`, and red-then-green is enforced by
the workflow's Red/Green steps plus `--repro-rc`. The residual ceiling is a
semantic rewrite of a NON-frozen test; AST-diff is the upgrade. Residual risks
are listed in the self-healing-loop repo's `docs/risks.md`.
"""
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Optional, Sequence

# IGNORECASE as a flag rather than an inline `(?i)`: these get alternated with a
# target-supplied pattern, and a global flag is only legal at the start of an
# expression, so the inline form makes the combined regex fail to compile.
_ASSERT_RE = re.compile(
    r"\b(assert|self\.assert|expect|assertequal|assert_raises|raises|should_)",
    re.IGNORECASE,
)
_SKIP_RE = re.compile(
    r"(skip|xfail|expectedfailure|expected_failure|pytest\.mark\.skip|unittest\.skip)",
    re.IGNORECASE,
)
_DIFF_HEADER_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')

# What a test file is called, per convention rather than per language. A single
# ecosystem's rule is not enough: match only pytest's `test_` prefix and on a JS
# target, where every test is `foo.test.ts`, nothing matches and this whole
# check silently polices nothing. Only conventions covered by a test are listed;
# add a glob when a target needs one, not in anticipation of one.
_DEFAULT_TEST_GLOBS = (
    "test_*",    # pytest, unittest
    "*_test.*",  # go
    "*.test.*",  # jest, vitest
    "*.spec.*",  # jasmine, vitest, angular
)

# Editing the runner's config silences a test as effectively as deleting its
# assertions, so these are frozen too. Listing Python's files alone leaves a JS
# fix free to add an `exclude` entry to vitest.config.ts and walk straight past
# the frozen test.
_DEFAULT_TEST_CONFIG_GLOBS = (
    # Python
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "noxfile.py",
    # JS/TS. `package.json` carries the `test` script and often the jest block,
    # which is why a dependency manifest belongs in a list about test config —
    # and the precedent for the manifests below.
    "vitest.config.*",
    "vite.config.*",  # vitest reads its `test` block when there is no vitest.config
    "jest.config.*",
    "jest.setup.*",
    ".mocharc.*",
    "playwright.config.*",
    "cypress.config.*",
    "karma.conf.*",
    "package.json",
    # Ruby, Rust, JVM. Go is deliberately absent: `go test` is configured by the
    # test files themselves rather than by a manifest, so there is no equivalent
    # file to freeze — naming Go here would claim a coverage that has no target.
    # Both agent-facing prompts tell the agent the gate refuses these, and the
    # framework's own suite pins this list and that prose together.
    ".rspec",
    "Rakefile",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)


def _lines(diff_text: str) -> list[str]:
    """Split a diff the way git wrote it: on newlines, and nothing else.

    ``str.splitlines()`` also breaks on ``\\v \\f \\x1c \\x1d \\x1e \\x85
    \\u2028 \\u2029``, none of which git treats as a line ending. A source line
    carrying one of them is therefore ONE line to git and TWO here — and the
    second half is attacker-chosen text arriving where this module expects a
    header.

    That is enough to forge a file attribution: `+# tidy\\x0cdiff --git a/src/app.py
    b/src/app.py` reads as a comment to git and as a new file header to a
    `splitlines()` reader, so every line after it is policed as belonging to
    `src/app.py`, the test globs stop matching, and an assertion removed from a
    real test file passes unseen. The same trick forges `--- /dev/null`, which
    makes a modified helper look created and exempt from the helper freeze.

    Every reader of a diff in this module goes through here for the same reason
    they all go through `_header_pair`: one parser, one set of rules.

    A single trailing ``\\r`` is dropped, and only that one. `splitlines()`
    consumed it; `split("\\n")` leaves it attached, where `_DIFF_HEADER_RE`
    swallows it into the captured path and a CRLF diff stops matching its own
    filenames. Stripping the character git itself treats as part of a line
    ending is not the same as splitting on it, so the forgery defence above is
    untouched.
    """
    return [
        line[:-1] if line.endswith("\r") else line
        for line in diff_text.split("\n")
    ]


def _unquote(path: str) -> str:
    """Decode git's C-quoting of a path, or return it unchanged.

    git wraps a path in quotes and octal-escapes its bytes whenever it holds
    anything outside printable ASCII, a quote or a backslash — `core.quotePath`
    is on by default, so this is the ordinary form for any non-English filename.
    Decoding matters for basename globbing; a prefix check survives without it,
    since ASCII characters are never escaped.

    Falls back to the raw text rather than to nothing. Every caller treats an
    empty path list as "this line names no file", so failing to decode by
    returning nothing would turn an unreadable name into a silent pass, which
    is the failure this parser exists to prevent.
    """
    if "\\" not in path:
        return path
    try:
        return (
            path.encode("latin-1", "backslashreplace")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8")
        )
    except (UnicodeDecodeError, UnicodeEncodeError):
        return path


def _header_pair(line: str) -> Optional[tuple[str, str]]:
    """The ``(old, new)`` paths a diff header line names, or None for content.

    An empty side means the line names only the other one: ``--- a/x`` fixes the
    old path and says nothing about the new. Every reader of a diff header goes
    through here: two of them restating these patterns separately drift, one
    decoding git's quoting and the other not, and an accented filename is then
    policed by half the checks.
    """
    match = _DIFF_HEADER_RE.match(line)
    if match:
        return (_unquote(match.group(1)), _unquote(match.group(2)))
    for prefix, is_new in (('--- a/', False), ('--- "a/', False),
                           ('+++ b/', True), ('+++ "b/', True)):
        if line.startswith(prefix):
            path = _unquote(line[len(prefix):].strip().rstrip('"'))
            return ("", path) if is_new else (path, "")
    return None


def _header_paths(line: str) -> list[str]:
    """Both paths named by a diff header line, or empty for a content line."""
    pair = _header_pair(line)
    return [path for path in pair if path] if pair else []


def _matches(path: str, globs: Sequence[str]) -> bool:
    """Glob the basename and the full path, case-sensitively.

    Basename covers filename conventions (`foo.test.ts`); full path covers
    directory ones (jest's `__tests__/`), which no filename rule can express.
    `fnmatchcase` rather than `fnmatch` because the latter folds case on
    Windows, where `*_test.*` would then also match `latest.py`.
    """
    if not path:
        return False
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(name, g) or fnmatchcase(path, g) for g in globs)


def is_test_weakened(
    diff_text: str,
    test_globs: Sequence[str] = _DEFAULT_TEST_GLOBS,
    assert_re: re.Pattern = _ASSERT_RE,
    skip_re: re.Pattern = _SKIP_RE,
) -> Optional[str]:
    """Describe how a test file in the diff was weakened, or None if none was.

    The two patterns are parameters because what an assertion looks like is a
    property of the target, not of this check. The built-ins describe Python and
    JS; a target whose runtime says `t.Errorf` or `#[ignore]` supplies its own,
    and an unrecognised form makes this check pass everything silently.

    Returns the reason rather than printing it: this is a pure function with
    several call sites, and a blocked cycle has to be able to say what it caught
    or the block is unreadable in the evidence bundle. None stays falsy, so a
    truthiness call site reads the same.
    """
    old_file = ""
    new_file = ""
    for line in _lines(diff_text):
        pair = _header_pair(line)
        if pair is not None:
            old, new = pair
            old_file = old or old_file
            new_file = new or new_file
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        # Police if either path is a test path — catches rename-out-of-test_.
        if not (
            _matches(old_file, test_globs)
            or _matches(new_file, test_globs)
        ):
            continue
        path = new_file or old_file
        if line.startswith("-") and not line.startswith("---"):
            if assert_re.search(line[1:]):
                return f"{path}: assertion removed: {line[1:].strip()}"
        elif line.startswith("+") and not line.startswith("+++"):
            if skip_re.search(line[1:]):
                return f"{path}: skip marker added: {line[1:].strip()}"
    return None


def count_test_files(diff_text: str, test_globs: Sequence[str] = _DEFAULT_TEST_GLOBS) -> int:
    """How many distinct files in the diff the weakening check can see.

    Reported alongside a pass so the evidence bundle distinguishes "no test was
    weakened" from "no file in this diff looked like a test to me". The globs
    describe Python and JS by default, so on a stack whose convention nobody
    supplied this returns 0 while every other check reports clean, and that zero
    is the only thing in the record that says the check was blind.
    """
    seen = set()
    for line in _lines(diff_text):
        for path in _header_paths(line):
            if _matches(path, test_globs):
                seen.add(path)
    return len(seen)


def is_frozen_test_touched(diff_text: str, frozen_path: str) -> Optional[str]:
    """Describe a diff touching ``frozen_path`` (either side), or None if clean.

    The repro test Diagnose specified is written by the workflow and proven red
    on the broken code BEFORE Fix runs. Fix must make it pass by changing
    source, never by editing it — so any change to that file in Fix's diff is a
    freeze violation. This is what closes the call-site-swap bypass: Fix cannot
    rewire a test it is forbidden to touch.

    Parsed through ``_header_paths`` rather than matched as raw substrings.
    Comparing the frozen path against the unparsed header text misses every
    C-quoted name, so a frozen test with any non-ASCII byte in it — the ordinary
    case wherever contributors do not write in English — could be edited freely
    while the pass line attested that it had not been.
    """
    needle = frozen_path.rsplit("/", 1)[-1]
    violation = (
        f"{frozen_path}: the frozen reproducing test was modified; "
        f"a fix must make it pass by changing source, never by editing it"
    )
    for line in _lines(diff_text):
        for path in _header_paths(line):
            normalised = path.removeprefix("./")
            if normalised == frozen_path or normalised.endswith(f"/{frozen_path}"):
                return violation
            # Basename alone, deliberately WIDER than the path match above —
            # which already covers the absolute-header case. This refuses a
            # same-named file in another directory too, and that over-refusal is
            # the point: the cost is one wasted cycle on a fix that renamed
            # around the freeze, against a merged fix that edited the test it
            # was proven against. A gate errs toward refusing.
            if normalised.rsplit("/", 1)[-1] == needle:
                return violation
    return None


# Directory names that mean "tests live here and nothing else does". Naming
# rather than sniffing the contents: a test tree legitimately holds helpers and
# fixtures, so "every file here looks like a test" is false for exactly the
# directories worth freezing. The installer picks the repro path knowing the
# layout, so this reads a decision already made rather than guessing at one.
_DEDICATED_TEST_DIRS = frozenset({"tests", "test", "spec", "specs", "__tests__"})


def helper_freeze_applies(
    frozen_path: str, test_globs: Sequence[str] = (), root: str = "."
) -> bool:
    """Whether the frozen test's directory holds tests and nothing else.

    Freezing one file does not freeze what it imports, so a helper beside the
    reproducing test is an open route to a green gate: neuter the helper and the
    frozen test passes without the bug being touched.

    Refusing every modification in that directory closes it — but only where the
    directory is a dedicated test tree. Go puts ``foo.go`` and ``foo_test.go``
    side by side, and a blanket rule there would refuse every legitimate source
    fix, so the check reads the directory and declines when it finds source.

    Declining is reported rather than silent (``cli`` names it in the pass
    line). A guard that quietly does not apply reads exactly like one that
    passed, which is the defect this module exists to prevent.
    """
    if not frozen_path or "/" not in frozen_path:
        return False
    directory = frozen_path.rsplit("/", 1)[0]
    if directory.rsplit("/", 1)[-1].lower() not in _DEDICATED_TEST_DIRS:
        return False
    try:
        # Confirm it exists and holds something. A path that does not resolve is
        # unknown, and unknown must not read as a directory worth freezing.
        return any((Path(root) / directory).iterdir())
    except OSError:
        return False


def is_test_helper_touched(
    diff_text: str,
    frozen_path: str,
    test_globs: Sequence[str] = (),
    root: str = ".",
) -> Optional[str]:
    """Name a pre-existing file in the frozen test's directory that the diff edits.

    Additions are fine — Fix legitimately writes regression tests there. What is
    refused is MODIFYING something that was already present, because that is the
    shape of the bypass: the frozen test is untouchable, so the thing it imports
    becomes the lever.

    Returns None when the layout mixes tests and source, per
    ``helper_freeze_applies``.
    """
    if not helper_freeze_applies(frozen_path, test_globs, root):
        return None
    directory = frozen_path.rsplit("/", 1)[0] if "/" in frozen_path else ""
    prefix = f"{directory}/" if directory else ""
    for path, created in _diff_files(diff_text):
        if created or path == frozen_path:
            continue
        if path.startswith(prefix) and "/" not in path[len(prefix):]:
            return (
                f"{path}: the frozen test's directory is frozen with it — a fix "
                f"may add files here but not change one that was already present, "
                f"because what the frozen test imports decides whether it passes"
            )
    return None


def _diff_files(diff_text: str) -> list[tuple[str, bool]]:
    """Every path the diff touches, paired with whether the diff CREATES it.

    Creation is read from git's ``--- /dev/null``, which is the only reliable
    marker: a ``new file mode`` line is absent for some diff formats, and the
    ``b/`` path looks identical either way.
    """
    files: list[tuple[str, bool]] = []
    current: Optional[str] = None
    created = False
    for line in _lines(diff_text):
        if line.startswith("diff --git"):
            if current is not None:
                files.append((current, created))
            paths = _header_paths(line)
            current = paths[-1].removeprefix("./") if paths else None
            created = False
        elif line.startswith("--- ") and line.rstrip().endswith("/dev/null"):
            created = True
    if current is not None:
        files.append((current, created))
    return files


def is_test_config_touched(
    diff_text: str, config_globs: Sequence[str] = _DEFAULT_TEST_CONFIG_GLOBS
) -> Optional[str]:
    """Name the test-runner config the diff touches, or None if it touches none.

    A frozen test is useless if Fix instead edits a test-config file to skip
    it (conftest.py collect_ignore, vitest.config.ts exclude, a rewritten
    `test` script in package.json). Any diff touching one of these is a freeze
    violation, whether or not a frozen test is currently active.
    """
    for line in _lines(diff_text):
        for path in _header_paths(line):
            if _matches(path, config_globs):
                return (
                    f"{path}: test-runner config edited, which can exclude or skip "
                    f"the frozen reproducing test without touching the test itself"
                )
    return None


# The installed loop's own directory. Nothing inside it is writable by a fix,
# incident memory included: the Record step writes that file and runs after this
# gate, so no legitimate cycle has it in the diff. Exempting it would let a fix
# append a record whose `root_cause` is injected verbatim into every later
# Diagnose and Fix prompt — reverted records ranked first, so the prompt cap
# cannot drop it — which turns one untrusted log line into permanent influence
# over every future cycle.
LOOP_DIR = ".shl/"

# The pipeline that runs the gate, as opposed to the gate itself. Kept separate
# from LOOP_DIR because it is a different claim with a different remedy: the
# loop tree is off-limits because a fix could rewrite its own judge, and this is
# off-limits because a fix could delete the step that calls the judge at all.
WORKFLOW_DIR = ".github/workflows/"


def is_loop_tree_touched(diff_text: str) -> Optional[str]:
    """Name the loop-tree path the diff edits, or None if it edits none.

    Fix runs with cwd ``.shl/`` (so the harness auto-loads the
    loop's operating doc), and an edit-permitting mode auto-approves writes in
    the working directory. That directory holds this module, the CLI that
    invokes it, the confidentiality filter and ``loop.py`` — so without this
    check the fix agent can rewrite the gate that is about to judge it, and the
    later gate/scrub/review steps then execute the tampered code with the
    provider token and GH_TOKEN in their environment. Nothing needs a shell,
    and the vendored ``.gitignore`` does not cover ``*.py``, so the tampered
    loop is committed and every later cycle runs it.

    Deliberately blunt rather than clever: Fix has no legitimate reason to edit
    the loop at all, so any hit is a violation. A prompt-injected agent gets no
    say in what this function considers acceptable, which is the point — the
    other guardrails all police the agent's OUTPUT; this one polices its reach.
    """
    for line in _lines(diff_text):
        for path in _header_paths(line):
            # removeprefix, not lstrip: lstrip strips CHARACTERS, so
            # `".shl/x".lstrip("./")` eats the leading dot and the
            # directory stops matching its own name.
            normalised = path.removeprefix("./")
            if normalised.startswith(LOOP_DIR):
                return (
                    f"{normalised}: the fix agent may not edit the machinery "
                    f"that judges it"
                )
    return None


def is_workflow_touched(diff_text: str) -> Optional[str]:
    """Name the workflow file the diff edits, or None if it edits none.

    ``is_loop_tree_touched`` stops a fix rewriting the judge; this stops it
    removing the step that calls the judge. Without it, a diff deleting the Gate
    step from ``heal.yml`` passes every check in this module, because none of
    them looks outside the loop tree.

    GitHub refuses a ``GITHUB_TOKEN`` push that touches ``.github/workflows/``,
    so such a cycle dies at the push rather than merging a disarmed pipeline.
    That is a backstop and not a substitute: it lives on a server this code
    cannot inspect, an install that hands ``checkout`` a personal token defeats
    it, and it fires in the worst available way — after the agent calls are
    spent, with no attempt recorded, so the cap never advances and the same
    failure is re-diagnosed on every tick forever. Refusing here converts a
    silent forever-loop into a block that states its cause.

    A fix legitimately needing a workflow change is a fix that needs a human.
    """
    for line in _lines(diff_text):
        for path in _header_paths(line):
            normalised = path.removeprefix("./")
            if normalised.startswith(WORKFLOW_DIR):
                return (
                    f"{normalised}: the fix agent may not edit the workflow "
                    f"that runs the gate"
                )
    return None


# What git consults to decide whether it emits a diff's CONTENT at all. A
# per-directory file, so it is matched by basename anywhere in the tree.
DIFF_CONFIG = ".gitattributes"


def is_diff_config_touched(diff_text: str) -> Optional[str]:
    """Name the ``.gitattributes`` the diff touches, or None if it touches none.

    One line — ``* -diff`` — makes git print ``Binary files a/x and b/x differ``
    in place of every ``+``/``-`` line, for every path it covers. The
    no-weakening check reads content lines, so it then inspects a diff that has
    none and reports clean, and the Review agent is handed the same empty diff
    and approves it. Nothing else in this module notices, because every other
    check asks about PATHS and the paths are still all there.

    Third member of a family, and the reason it gets its own check rather than
    joining the test-config globs: ``is_loop_tree_touched`` stops a fix
    rewriting its judge, ``is_workflow_touched`` stops it deleting the step that
    calls the judge, and this stops it blinding the judge while leaving both in
    place. A fix has no legitimate reason to change how git renders diffs.
    """
    for line in _lines(diff_text):
        for path in _header_paths(line):
            normalised = path.removeprefix("./")
            if normalised.rsplit("/", 1)[-1] == DIFF_CONFIG:
                return (
                    f"{normalised}: a fix may not change how git renders diffs — "
                    f"`-diff` empties the content this gate and the reviewer read"
                )
    return None


# How git says "I am not printing this file's content". Both spellings, because
# `--binary` emits the second and the plain form emits the first.
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")


def is_test_content_unreadable(
    diff_text: str, test_globs: Sequence[str] = _DEFAULT_TEST_GLOBS
) -> Optional[str]:
    """Name a test file whose content the diff does not carry, or None.

    `is_test_weakened` decides by reading `+`/`-` lines, so a test file that
    arrives with none of them is reported clean — the check ran, found no
    removed assertion, and said so. `is_diff_config_touched` closes one way to
    produce that, a `.gitattributes` committed into the tree. It is not the
    only way, and the others leave no trace in any diff:

    - `.git/info/attributes` and `core.attributesFile` are untracked by
      construction, and the workflow's tamper hash covers `.git/config` and
      `.git/hooks/` only.
    - one NUL byte makes git call the file binary with no configuration at all,
      which needs no `.git/` write and no attributes file anywhere.

    Enumerating the routes is the losing move — they converge on one shape by
    the time this module sees it, so the shape is what is refused. Verified
    against real git: the same assertion removal BLOCKS as text and PASSES with
    one NUL byte added, while the pass line attests `no test weakened`.

    Scoped to paths matching the test globs. A fix adding a binary fixture
    elsewhere is ordinary, and refusing that would block real work to defend a
    check that never reads those files.
    """
    old_file = ""
    new_file = ""
    for line in _lines(diff_text):
        pair = _header_pair(line)
        if pair is not None:
            old, new = pair
            old_file = old or old_file
            new_file = new or new_file
            continue
        if not line.startswith(_BINARY_MARKERS):
            continue
        if _matches(old_file, test_globs) or _matches(new_file, test_globs):
            path = new_file or old_file
            return (
                f"{path}: git emitted no content for this test file, so the "
                f"no-weakening check has nothing to read — a NUL byte or a "
                f"`-diff` attribute outside the tree both produce this"
            )
    return None


def new_failures(before: Iterable[str], after: Iterable[str]) -> set[str]:
    """Tests that were passing before the fix and are failing after it.

    The regression signal, and the reason the gate does not simply demand a
    fully green suite: most real repos carry pre-existing failures, and vetoing
    every fix until someone cleans those up makes the loop unusable. Proof that
    the bug itself is fixed comes from the frozen reproducing test flipping red
    to green — a separate check the caller must still enforce.

    Compares SETS, not counts: a fix that repairs one test while breaking
    another leaves the count unchanged and must still be caught.
    """
    return set(after) - set(before)