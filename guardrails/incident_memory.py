"""Append-only postmortem log, read before every Diagnose stage so a repeat failure signature is recognized instead of re-derived from scratch - the mechanism that makes the loop faster over time, not just safe.

Records are matched on FINGERPRINTS (exception type + raise site, from
``log_compact.failure_fingerprints``), never on the human-written title. The
title is prose: it varies every cycle because a model wrote it, so matching on
it recognizes nothing. The fingerprint is derived from the same log on both
sides, so an identical failure keys identically.
"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from adapters import optional_ids_fn
from guardrails.confidentiality_filter import scrub
from log_compact import failure_fingerprints

DEFAULT_LOG_PATH = Path(__file__).parent.parent / "incident_memory" / "log.jsonl"


@dataclass
class IncidentRecord:
    issue_id: str
    signature: str  # human-readable title, for display; NOT the match key
    root_cause: str
    fix_commit: str
    outcome: str  # "merged" | "reverted" | "escalated"
    # Defaulted so a record carrying no fingerprints still parses rather than
    # breaking every later read of an append-only log. It matches nothing, which
    # is the correct outcome for a failure that was never identified.
    fingerprints: list[str] = field(default_factory=list)


# A cap on RECORDS, not on bytes: `root_cause` is the agent-written issue body
# and nothing truncates it on the way in, so a run of verbose incidents makes
# the file larger than any size estimate would suggest. The store is
# append-only and lives in the target's repo, growing once per healed cycle for
# as long as the cron runs.
MAX_RECORDS = 500


def record_incident(
    entry: IncidentRecord,
    log_path: Path = DEFAULT_LOG_PATH,
    max_records: int = MAX_RECORDS,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")
    _prune(log_path, max_records)


def _prune(log_path: Path, max_records: int) -> None:
    """Trim the oldest records, never a reverted one.

    The file is rewritten only once it is over the cap, so below the cap an
    incident commit stays a one-line append. Above it, each cycle drops one row
    and adds one, so those commits do carry a whole-file diff — the cost of a
    bounded append-only store, paid only after 500 healed cycles.

    A `reverted` record is exempt because it is the highest-value thing memory
    holds — it says the obvious fix was already tried and made things worse.
    `format_incidents` ranks those ahead of everything else so the PROMPT cap
    can never drop the warning; pruning them here would defeat that from
    underneath, and silently, since a missing incident looks like a bug nobody
    has hit yet.
    """
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    if len(lines) <= max_records:
        return
    reverted, others = [], []
    for index, line in enumerate(lines):
        # A row that will not parse cannot be judged, so it is treated as
        # non-exempt and ages out with the ordinary ones. Anything at all can
        # sit in an append-only file a workflow writes, so this catches the
        # shape errors too, not just malformed JSON: a bare `null` parses fine
        # and then has no `.get`.
        try:
            record = json.loads(line)
            outcome = record.get("outcome") if isinstance(record, dict) else None
        except json.JSONDecodeError:
            outcome = None
        (reverted if outcome == "reverted" else others).append(index)
    # Newest-first within each group, so the cap keeps recent history; the
    # exempt group is filled first and can itself be trimmed in the
    # pathological case where nearly every fix has regressed. The empty-cap
    # case is spelled out because `reverted[-0:]` is the WHOLE list, so a cap
    # of zero would keep everything it was asked to drop.
    keep = set(reverted[-max_records:] if max_records else [])
    keep.update(others[-(max_records - len(keep)) :] if len(keep) < max_records else [])
    # Written back in file order: `search_similar` returns matches in that
    # order and `format_incidents` reads position as recency.
    log_path.write_text("".join(lines[i] + "\n" for i in sorted(keep)))


def record_cycle(
    issue_id: str,
    signature: str,
    root_cause: str,
    fix_commit: str,
    outcome: str,
    raw_log: str,
    repo_root: str = "",
    log_path: Path = DEFAULT_LOG_PATH,
) -> IncidentRecord:
    """Record a completed cycle, fingerprinting its log.

    ``raw_log`` is the log as the adapter returned it, not the compacted
    signal: what is stored has to be derived from the same text a later
    `recall_incidents` will look up with, and identities are built from frames
    compaction is free to drop. Recording from compacted text stores fewer
    identities than the lookup derives, so the repeat this record exists to
    catch never matches it.

    One implementation, called rather than reimplemented: a caller that builds
    the record inline has to derive the fingerprint rule itself, and a rule two
    places follow separately is one they eventually follow differently.

    The identities come from `optional_ids_fn`, the same source `recall_incidents`
    reads, so what is stored is what a later cycle will look up. On a runtime the
    built-in parsing cannot read, deriving them here instead would store an empty
    list — a record that matches nothing, indistinguishable from a project that
    has never failed.

    `signature` and `root_cause` are scrubbed HERE rather than by the caller.
    Both come straight from the agent's `diagnose.json`, written while quoting
    an untrusted log, and this record is committed to the default branch,
    append-only, exempt from pruning once an outcome is `reverted`, and
    re-rendered into every later matching prompt — so it is the longest-lived
    copy of that text anywhere. `heal.yml` scrubs the issue title, the issue
    body, the PR summary and the review reason on their way to GitHub and
    handed this one through raw, which is exactly what a rule every caller must
    remember produces (L9). Scrubbing at the seam means no caller can forget.
    """
    entry = IncidentRecord(
        issue_id=issue_id,
        signature=scrub(signature),
        root_cause=scrub(root_cause),
        fix_commit=fix_commit,
        outcome=outcome,
        fingerprints=failure_fingerprints(
            raw_log, strip_prefix=repo_root, ids_fn=optional_ids_fn()
        ),
    )
    record_incident(entry, log_path=log_path)
    return entry


def search_similar(
    fingerprints: list[str], log_path: Path = DEFAULT_LOG_PATH
) -> list[IncidentRecord]:
    """Records sharing at least one fingerprint with the failure being diagnosed.

    Exact-set intersection, deliberately: this is what keeps an unrelated
    incident out of the agent's prompt no matter how many accumulate over
    months. Precision here is the primary defence against context pollution;
    the caps in ``loop.format_incidents`` are only the second layer.

    A list, never a bare string, and the refusal is explicit because neither
    wrong reading of a string is loud: as one identity it matches nothing, and
    to ``set()`` it is a bag of characters that matches nothing either. Both
    read as "this failure is new", which is the shape of the defect that left
    this mechanism dead behind four passing tests (L8).
    """
    if isinstance(fingerprints, str):
        raise TypeError(
            "search_similar takes a list of fingerprints, not one string: a raw "
            "log line used as an identity matches nothing, silently and forever"
        )
    wanted = set(fingerprints)
    if not wanted or not log_path.exists():
        return []
    matches = []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # One unreadable row must not end recall for every later cycle. This
            # store is append-only and committed by a workflow, so a truncated
            # write or a hand-edit is survivable in a way it would not be for
            # config: the row is skipped, the rest still match, and the loop
            # degrades by one incident rather than losing its whole memory.
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                if wanted & set(record.get("fingerprints") or []):
                    matches.append(IncidentRecord(**record))
            except (json.JSONDecodeError, TypeError):
                continue
    return matches
