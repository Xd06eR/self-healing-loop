"""Log compactor — raw log to the error signal an agent needs to diagnose.

The watch step pulls a target's raw log (via the TargetAdapter) and runs it
through here before handing it to the Diagnose agent. Goal: keep the signal
(error lines + traceback frames + their indented continuation), drop the noise
(repeated heartbeats, debug/HTTP chatter), and bound the size so a token-burning
multi-MB log never reaches the model.

Reads as a string, returns a string. The watch step treats an empty result as
"idle" — nothing to heal this cycle.
"""
import re

_ERROR_RE = re.compile(
    r"(?i)(error|exception|traceback|failed|failure|fatal|critical|panic|"
    r"refused|denied|timeout|timed out|errno|raised|cannot|undefined|"
    r"nil pointer|segmentation|core dumped)"
)


# The final "SomeError: message" line of a traceback identifies the failure far
# better than its first line, which is usually a generic "Exception in ASGI
# application" shared by every unrelated bug.
# The qualifier is optional because `Error:` and `Exception:` are themselves the
# commonest shapes there are — `throw new Error(...)` is the JS/TS default and
# `raise Exception(...)` the Python one. Requiring a prefix silently excludes
# both, which leaves no fingerprint on the two runtimes this module reads
# natively, and the cycle then refuses every such failure.
# Case-SENSITIVE on purpose. Matching `ERROR:` too would turn a generic log
# level into an identity: every unrelated failure sharing a frame collapses to
# one fingerprint, which is the same defect as keying on a message. An
# exception TYPE is CamelCase in every language this parses; a log level is not.
_EXC_LINE_RE = re.compile(r"^(?:[A-Za-z_][\w.]*)?(?:Error|Exception)\b")
_TB_START_RE = re.compile(r"^Traceback \(most recent call last\)")
# Python's two chain markers. The exception line ABOVE one of these is not the
# failure — it is an intermediate step on the way to it, and it is routinely
# pure library code (starlette turns a KeyError into the AttributeError the app
# actually sees). Splitting there gives that fragment its own identity, which
# every unrelated bug reaching the same library line then shares.
_TB_CHAIN_RE = re.compile(
    r"^(?:During handling of the above exception"
    r"|The above exception was the direct cause)"
)
_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+)')

# The same frame in V8 form: `at fn (path:LINE:COL)` or, for a top-level frame,
# `at path:LINE:COL`. Frames carrying no location at all (`at Array.map
# (<anonymous>)`) appear in real traces between two that do, and simply do not
# match. Column is captured but dropped: it is far more volatile than the line
# across builds, and two occurrences of one bug must fingerprint identically.
_JS_FRAME_RE = re.compile(r"^\s*at (?:[^()]*\()?([^()\s]+?):(\d+):\d+\)?\s*$")

# Third-party code. The DEEPEST frame of a traceback is usually here — the
# library that finally raised — and it is shared by every unrelated bug that
# reaches it: two different missing attributes both bottom out at the same line
# of starlette's datastructures.py. Keying on that makes distinct failures
# identical, which collapses them into one compaction slot (dropping the rarer
# one) and makes incident memory recall the wrong prior fix. The deepest frame
# in code the project OWNS is what actually identifies the bug.
_VENDOR_RE = re.compile(
    r"(?:^|/)(?:\.venv|venv|site-packages|dist-packages|node_modules|vendor|\.tox|\.nox)(?:/|$)"
    r"|^/usr/lib/python"
    r"|^node:"  # Node builtins: node:internal/modules/cjs/loader, node:fs, ...
)

# Volatile bits that differ between two occurrences of the SAME failure and
# that survive `_PAYLOAD`, which runs first. Addresses and timestamps are not
# listed: `_PAYLOAD`'s `\b\d+\b` rule has already replaced every digit run they
# are made of, so a rule for either would never match. A pointer survives
# because a hex run is one word and that rule does not reach it.
_VOLATILE = (
    (re.compile(r"0x[0-9a-fA-F]{6,}"), "<ptr>"),
)

# Payload inside an error MESSAGE — a key, an id, a count. Two occurrences of one
# bug routinely differ here ("KeyError: 'job-1'" vs "'job-2'"), and if that
# defeats deduplication the size bound evicts rarer bugs, which is the failure
# this module exists to prevent. Applied only when building a signature; the
# displayed line keeps its real values.
_PAYLOAD = (
    (re.compile(r"'[^']*'"), "'<v>'"),
    (re.compile(r'"[^"]*"'), '"<v>"'),
    (re.compile(r"\b\d+\b"), "<n>"),
)


def _is_continuation(line: str) -> bool:
    # Traceback frames and indented context start with whitespace.
    return bool(line) and line[0].isspace()


def _normalize(text: str, patterns=_VOLATILE) -> str:
    for pattern, placeholder in patterns:
        text = pattern.sub(placeholder, text)
    return text.strip()


def _frames_raise_site_first(block: list[str]) -> list[tuple[str, str]]:
    """`(path, "path:line")` for each frame, raise site FIRST.

    The two built-in formats print in opposite directions: Python lists the
    outermost caller first and the raise site last, V8 lists the throw site
    first. Normalising here is what stops a JS fingerprint keying on whichever
    module happened to be at the bottom of the call stack — a location that is
    identical for every unrelated bug in the process and therefore worse than
    no fingerprint at all.

    """
    python_frames: list[tuple[str, str]] = []
    js_frames: list[tuple[str, str]] = []
    for line in block:
        match = _FRAME_RE.match(line)
        if match:
            python_frames.append((match.group(1), f"{match.group(1)}:{match.group(2)}"))
            continue
        match = _JS_FRAME_RE.match(line)
        if match:
            js_frames.append((match.group(1), f"{match.group(1)}:{match.group(2)}"))
    return js_frames or python_frames[::-1]


def _exception_signature(block: list[str]) -> str | None:
    """Identify a block by WHICH failure it is: exception type + where it was raised.

    Keyed on the type plus the deepest stack frame rather than the whole message.
    Two different bugs commonly share a message ("'NoneType' object has no
    attribute 'get'") and would otherwise collapse into one slot, silently
    deleting one of them; the same bug commonly varies its message by an id and
    would otherwise never collapse at all.

    ``None`` when the block carries no exception at all, and also when it
    carries one this module cannot locate. An HTTP access line reading "500
    Internal Server Error" is a symptom, not an identity: every unrelated 500 on
    every endpoint normalises to the same string, so anything keyed on it would
    match everything.

    A type plus its MESSAGE is not a usable identity either, which is why an
    exception with no readable frame returns ``None`` rather than falling back
    to one. Messages fail in both directions at once: the same bug carries a
    different unquoted detail on each occurrence, so dedup misses and the
    attempt cap counts from zero forever, while two unrelated bugs sharing a
    common message ("value is null") collapse into one issue and recall each
    other's fix. Returning ``None`` routes the log to ``unfingerprintable``,
    which stops the cycle and tells the operator to implement
    ``TargetAdapter.failure_ids`` — the seam that exists for exactly this.
    """
    frames = _frames_raise_site_first(block)
    owned = [f for path, f in frames if not _VENDOR_RE.search(path)]
    frame = next(iter(owned or [f for _, f in frames]), "")
    if not frame:
        return None
    for line in reversed(block):
        stripped = line.strip()
        if _EXC_LINE_RE.match(stripped):
            return f"{stripped.split(':', 1)[0]}@{frame}"
    return None


def _signature(block: list[str]) -> str:
    """Dedup key for compaction. Every block needs one, exception or not."""
    signature = _exception_signature(block)
    if signature is not None:
        return signature
    # `_blocks` appends `[line]` or extends an existing list, so a block always
    # holds at least one line. `_PAYLOAD` first, then `_VOLATILE` on what is
    # left; the comment on `_VOLATILE` records why that order matters.
    return _normalize(_normalize(block[0], _PAYLOAD))


# CPython renders `asyncio.TaskGroup` tracebacks (3.11+) with a `|`/`+` gutter
# down the left of every frame: `  |   File "x", line 1`. The gutter sits where
# the frame regex expects whitespace, so every ExceptionGroup failure
# fingerprinted to nothing and the whole async-by-TaskGroup class was refused
# as unfingerprintable on any 3.11+ target. The strip is gated on the marker
# CPython itself emits, so an ordinary log line that happens to begin with a
# pipe is untouched.
_GROUP_GUTTER = re.compile(r"^(\s*)[|+]")


def _ungutter(raw: str) -> str:
    if "Exception Group" not in raw and "BaseExceptionGroup" not in raw and "ExceptionGroup" not in raw:
        return raw
    return "\n".join(_GROUP_GUTTER.sub(r"\1", line) for line in raw.split("\n"))


def _keep_error_lines(raw: str) -> list[str]:
    raw = _ungutter(raw)
    kept: list[str] = []
    in_tb = False  # inside a traceback / error continuation block
    for line in raw.splitlines():
        if _ERROR_RE.search(line):
            kept.append(line)
            in_tb = True
        elif in_tb and _is_continuation(line):
            kept.append(line)
            # stays in the block until a non-indented non-error line breaks it
        else:
            in_tb = False
    return kept


def unfingerprintable(raw: str, ids_fn=None) -> bool:
    """True when a log carries failure text no fingerprint could be derived from.

    Frame parsing recognises Python and V8 stacks. A runtime outside that set —
    Go, Ruby, Rust, Java — yields error lines but no `Type@path:line`, and every
    consumer of a fingerprint then degrades silently at once: incident memory
    recalls nothing, issue dedup matches nothing, and the attempt cap counts
    from zero forever.

    The distinction that matters is between a log with NO failure, which is an
    ordinary idle result, and a log whose failure could not be read, which is a
    coverage gap wearing the same clothes. Only the second is a fault.
    """
    return bool(_keep_error_lines(raw)) and not failure_fingerprints(raw, ids_fn=ids_fn)


def failure_fingerprints(raw: str, strip_prefix: str = "", ids_fn=None) -> list[str]:
    """Stable identities of the distinct failures in a log, in order of appearance.

    This is the key incident memory matches on, so it has to survive the things
    that differ between two occurrences of one bug: client port, object id,
    timestamp, and the message payload. It also has to survive a different
    CHECKOUT PATH, or an incident recorded on a developer's machine can never
    match the same failure on a CI runner — pass the repo root as
    ``strip_prefix`` to make it relative.
    """
    if not raw:
        return []
    prefix = strip_prefix.rstrip("/") + "/" if strip_prefix else ""
    # The target's own identities, for a runtime this module cannot read. The
    # seam sits here rather than at frame parsing because extraction is itself
    # format-specific: a Go panic puts its trace behind a blank line and indents
    # none of it, so it never survives to the point where frames are counted.
    if ids_fn is not None:
        supplied = ids_fn(raw)
        if supplied is not None:
            return list(dict.fromkeys(s.replace(prefix, "") if prefix else s for s in supplied))
    seen: set[str] = set()
    fingerprints: list[str] = []
    for block in _blocks(_keep_error_lines(raw)):
        signature = _exception_signature(block)
        if signature is None:
            continue
        if prefix:
            signature = signature.replace(prefix, "")
        if signature not in seen:
            seen.add(signature)
            fingerprints.append(signature)
    return fingerprints


def _chain_continues(lines: list[str], index: int) -> bool:
    """True when the exception at ``index`` is followed by another in the chain."""
    for line in lines[index + 1 :]:
        if not line.strip():
            continue
        return bool(_TB_CHAIN_RE.match(line.strip()))
    return False


def _blocks(lines: list[str]) -> list[list[str]]:
    """Group kept lines into blocks, keeping a whole traceback as ONE block.

    Both `Traceback (most recent call last):` and the terminal `SomeError: msg`
    are non-indented, so splitting on indentation alone tears a single traceback
    into three blocks — and the frames block then carries the same signature for
    every traceback in the log, so all frames collapse into one slot and get
    reattached to whichever error came last. That fabricates a plausible-looking
    traceback with the wrong file:line, which the agent then goes and "fixes".
    """
    grouped: list[list[str]] = []
    in_traceback = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if in_traceback:
            grouped[-1].append(line)
            # A chained traceback ("During handling ...") is not indented either,
            # but only the terminal exception line closes the block.
            if (
                not _is_continuation(line)
                and _EXC_LINE_RE.match(stripped)
                and not _chain_continues(lines, index)
            ):
                in_traceback = False
            continue
        if _TB_START_RE.match(stripped):
            # Keep the "ERROR: Exception in ASGI application" header with its own
            # traceback instead of orphaning it.
            if grouped and len(grouped[-1]) == 1 and not _EXC_LINE_RE.match(grouped[-1][0].strip()):
                grouped[-1].append(line)
            else:
                grouped.append([line])
            in_traceback = True
            continue
        if grouped and _is_continuation(line):
            grouped[-1].append(line)
        else:
            grouped.append([line])
    return grouped


def compact_log(raw: str, max_chars: int = 8000) -> str:
    if not raw:
        return ""
    kept = _keep_error_lines(raw)

    # One slot per DISTINCT failure, keeping its most recent occurrence and the
    # number of times it happened. Without this, a health check that 500s on
    # every page render buries a rarer bug under dozens of identical tracebacks,
    # and the tail cut below drops the rare one entirely — so the loop can never
    # see it and never heals it.
    # Keyed by signature and ordered by LAST occurrence: re-inserting on every
    # repeat moves a failure to the end of the dict. First-appearance order
    # would make the eviction below drop whichever failure started earliest,
    # including one still firing at the end of the window, which is the inverse
    # of what "errors surface at the end of a log" is supposed to mean.
    latest: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for block in _blocks(kept):
        sig = _signature(block)
        counts[sig] = counts.get(sig, 0) + 1
        latest.pop(sig, None)
        latest[sig] = block  # most recent wins: it carries the freshest context

    collapsed: list[list[str]] = []
    for sig, newest in latest.items():
        block = list(newest)
        if counts[sig] >= 2:
            block[0] = f"{block[0]}  [repeated {counts[sig]} times]"
        collapsed.append(block)

    # Tail-biased: drop the failure that has been quiet longest, which is now
    # the front of the list.
    # Whole blocks, never part of one — slicing mid-traceback orphans the frames
    # from their exception line, which hands the model a headless stack AND
    # costs the failure its raise site, so it can only be identified by its
    # message and collides with every other failure phrased the same way.
    while len(collapsed) > 1 and sum(len("\n".join(b)) + 1 for b in collapsed) > max_chars:
        collapsed.pop(0)

    text = "\n".join("\n".join(block) for block in collapsed)
    if len(text) > max_chars:
        # One block alone exceeds the budget; it has to be cut somewhere. Cut at
        # a newline so we at least don't hand the model a half-line.
        text = text[-max_chars:]
        nl = text.find("\n")
        if 0 <= nl < len(text) - 1:
            text = text[nl + 1 :]
    return text