"""Confidentiality scrub — redact secrets and PII before text leaves approved tools.

Applied to issue bodies, PR text, and any externally-written log line. Default
patterns catch the common leaks (API keys, bearer tokens, key=value secrets,
email). Client-specific data (real client names) is operator-supplied through
the ``SHL_EXTRA_SCRUB_PATTERNS`` repo variable — the framework cannot know a
project's client list. Read here rather than passed in by each caller, because
the callers are nine ``guardrails.cli scrub`` invocations across two workflows
plus ``evidence.py``, and a parameter only the last of those could reach is a
control the install has no way to set.

Errs toward over-redaction: a false positive hides a harmless token, a false
negative leaks confidential data. The ``key=value`` rule is deliberately greedy.
"""
import os
import re
from typing import Sequence

_REDACTED = "[REDACTED]"

# (label, compiled regex). Order doesn't matter; all are applied.
_DEFAULT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("api key sk-...", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("bearer token", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}")),
    ("github token", re.compile(r"gh[opsur]_[A-Za-z0-9]{36,}")),
    ("aws key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    (
        "key=value secret",
        # `key=value` and JSON's `"key": "value"` — the quote between key and
        # colon used to hide the whole value from this rule, leaking a bare
        # provider token verbatim on the scrubber's own documented primary
        # path: any agent output quoting env or config as JSON.
        re.compile(
            r'(?i)(api[_-]?key|token|secret|password|passwd|pwd|auth)["\']?\s*[=:]\s*["\']?\S+'
        ),
    ),
    ("email (PII)", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
]


# Env vars holding a secret this process itself is carrying. Their VALUES are
# redacted literally, which is the only rule no format can evade — the patterns
# above recognise shapes, and this framework's documented primary path is
# third-party providers whose keys may be bare hex, base64, or carry a prefix
# nobody enumerated. The scrubber is the last thing between an agent's text and
# a GitHub issue body, and between raw suite output and an evidence artifact
# anyone with repo read access can download.
# Every name the workflows read from the `secrets` context, plus the two GitHub
# supplies. Membership does two jobs: literal-value redaction here, and
# exclusion from `adapters.hydrate_repo_vars`, so a credential an operator
# stored as a plaintext repo variable is not folded into the environment of
# every adapter-loading step. A name missing from this tuple fails both, and
# `tests/test_confidentiality_filter.py` derives the expected set from the
# workflow templates rather than restating it.
SECRET_ENV_VARS = (
    "SHL_AUTH_TOKEN",
    "SHL_LOG_TOKEN",
    "SHL_DEPLOY_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)

# Below this length a "secret" is more likely a placeholder than a credential,
# and redacting a 3-character value would corrupt ordinary text everywhere it
# happened to appear. Empty values are skipped for the same reason: an empty
# needle matches at every position and would erase the whole document.
_MIN_SECRET_LEN = 8


# Operator-supplied patterns, one regex per line. A variable rather than a
# flag: every scrub in the loop then picks them up, including the ones that
# write an issue body and a PR comment — surfaces that turning the evidence
# upload off does nothing about.
_EXTRA_PATTERNS_VAR = "SHL_EXTRA_SCRUB_PATTERNS"


def _operator_patterns() -> list[tuple[str, re.Pattern]]:
    """Compile ``SHL_EXTRA_SCRUB_PATTERNS``; raise on one that does not compile.

    Skipping a bad pattern would leave the operator believing their client
    names are being removed while the text ships unredacted, which is the worst
    available outcome for a confidentiality control.
    """
    raw = os.environ.get(_EXTRA_PATTERNS_VAR, "")
    return [
        (f"{_EXTRA_PATTERNS_VAR} line {n}", re.compile(line.strip()))
        for n, line in enumerate(raw.splitlines(), start=1)
        if line.strip()
    ]


def _environment_secrets() -> list[str]:
    """Literal secret values this process holds, longest first.

    Longest first so a token that contains another as a substring redacts whole
    rather than leaving a fragment behind.
    """
    values = {
        value
        for var in SECRET_ENV_VARS
        if (value := os.environ.get(var, "").strip()) and len(value) >= _MIN_SECRET_LEN
    }
    return sorted(values, key=len, reverse=True)


def scrub(
    text: str,
    extra_patterns: Sequence[tuple[str, re.Pattern]] = (),
) -> str:
    """Return ``text`` with every known secret redacted.

    Three sources, because each alone has a gap: literal values for secrets
    this process holds (format-proof, but blind to anything outside its own
    environment), shape patterns for the common credential formats, and the
    operator's own patterns for the names only they know. ``extra_patterns``
    stays for in-process callers; the variable is what an install can set.
    """
    out = text
    for value in _environment_secrets():
        out = out.replace(value, _REDACTED)
    for _label, rx in (
        list(_DEFAULT_PATTERNS) + _operator_patterns() + list(extra_patterns)
    ):
        out = rx.sub(_REDACTED, out)
    return out