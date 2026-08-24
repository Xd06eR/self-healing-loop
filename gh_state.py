"""GitHub-derived loop state — durable across ephemeral runners.

Actions runners are wiped after each job, so a local-file attempt counter
resets every cron tick and the cap never fires. State must come from GitHub's
own durable record instead:

- attempt count  = the highest "fix attempt N" marker a bot comment has posted
                   on the issue (the heal workflow posts one per failed Fix).
- issue identity = the fingerprint marker embedded in an issue body, so a
                   recurring failure lands on the issue already counting it.

Both shell to ``gh`` and parse its ``--json`` output. A ``gh_runner`` is injected
so tests feed canned JSON without the CLI or network.

Failures propagate (JSONDecodeError, nonzero gh) and the step dies, because
Actions runs each ``run:`` body under ``bash -e``. That is the intended
direction: a cycle that cannot read its own attempt count must not proceed as
though the count were zero, which would let one failure be re-attempted forever.

State lives in GitHub rather than on the runner because the runner is
disposable — a file-backed counter resets to zero on every cycle, so the attempt
cap it exists to enforce could never be reached.
"""
import base64
import json
import re
import subprocess
from typing import Callable, Sequence

GhRunner = Callable[[Sequence[str]], str]

_ATTEMPT_RE = re.compile(r"fix attempt\s+(\d+)", re.IGNORECASE)

# An HTML comment, so it renders invisibly in the issue body while staying
# greppable. The issue TITLE cannot serve as the key: it is prose a model wrote
# and it differs every cycle for an identical failure. Incident memory keys on
# the same fingerprints, for the same reason.
_MARKER_RE = re.compile(r"<!-- shl-fingerprint: (.*?) -->")


def fingerprint_marker(fingerprints) -> str:
    """The line embedded in an issue body so a later cycle can recognise it.

    The payload is JSON in urlsafe base64, not a comma join. A fingerprint is
    ``Type@path:line`` and its path comes from the log, so a comma in a path
    split one identity into two wrong halves and dedup missed forever, and a
    ``-->`` in a path closed the HTML comment early and injected chosen text
    into the issue body — the one GitHub surface this reaches without the
    scrubber. Base64's alphabet contains neither character, so neither needs
    escaping. Empty keeps the bare shape: the workflow greps it when refusing
    an unfingerprintable log.
    """
    fps = sorted(set(fingerprints))
    token = (
        base64.urlsafe_b64encode(json.dumps(fps, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
        if fps
        else ""
    )
    return f"<!-- shl-fingerprint: {token} -->"


def _marked_fingerprints(body: str) -> set:
    """The LAST marker in the body, never the first.

    The workflow appends its marker after the scrubbed issue body, so the
    workflow's is always the final one. The body itself is agent-written from
    untrusted logs, and a first-match search let a decoy marker in the prose
    hijack dedup: the wrong fingerprints match nothing, the loop files a
    duplicate issue, and the attempt cap on the real one counts from zero.
    """
    matches = list(_MARKER_RE.finditer(body or ""))
    if not matches:
        return set()
    token = matches[-1].group(1)
    try:
        padded = token + "=" * (-len(token) % 4)
        return set(json.loads(base64.urlsafe_b64decode(padded)))
    except (ValueError, json.JSONDecodeError):
        # The comma-joined form, from issues filed before the encoding. A
        # target updating mid-flight still needs to recognise them or it
        # re-files a duplicate for every failure it has already healed.
        return set(filter(None, token.split(",")))


def _gh(argv: Sequence[str]) -> str:
    result = subprocess.run(
        ["gh", *argv], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(argv)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def count_attempts(issue_number: int, gh_runner: GhRunner = _gh) -> int:
    """Highest fix-attempt marker posted on the issue; 0 if none."""
    out = gh_runner(["issue", "view", str(issue_number), "--json", "comments"])
    data = json.loads(out)
    highest = 0
    for comment in data.get("comments", []):
        m = _ATTEMPT_RE.search(comment.get("body", ""))
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


def find_open_issue(fingerprints, gh_runner: GhRunner = _gh):
    """Number of the open loop-filed issue for this failure, or ``None``.

    Matching is set INTERSECTION, the same rule ``incident_memory.search_similar``
    uses, because a signal routinely carries several failures and the one being
    worked survives while its neighbours come and go.

    Getting this wrong is not cosmetic. ``count_attempts`` counts markers on an
    issue, so filing a fresh issue for a recurring failure resets the counter to
    zero and the attempt cap can never fire — the loop would retry an unfixable
    bug on every cron tick indefinitely. Issues carrying no marker belong to
    other people and are never touched.
    """
    wanted = set(fingerprints)
    if not wanted:
        return None
    # The newest 100 open issues. A bound, deliberately: the loop's own issues
    # close as they heal, so a window past 100 means 100+ unhealed failures
    # are open at once — a state where a duplicate issue is the least of the
    # operator's problems, and where unbounded listing spends the API budget
    # on every tick of a cron.
    out = gh_runner(
        ["issue", "list", "--state", "open", "--json", "number,body", "--limit", "100"]
    )
    for issue in json.loads(out):
        if wanted & _marked_fingerprints(issue.get("body", "")):
            return issue["number"]
    return None