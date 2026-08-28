"""Loop entry points — the thin CLI the GitHub Actions workflows call.

Builds a ``ConfiguredAgent`` from the ``SHL_*`` repo vars/secrets, then exposes
one function per loop stage. The workflows (``watch.yml`` / ``heal.yml``) call
these via ``python -B loop.py <subcommand>``; every ``gh`` / ``git`` write stays
a plain workflow step, never inside an agent call.

Env contract for this module (set as repo vars, except the token which is a
secret). Every name it reads, including through what it calls — an entry listed
as optional elsewhere and required here is the one that stops a cycle:

- ``SHL_HARNESS``      "claude-code" | "opencode"  (a REGISTRY key)
- ``SHL_MODEL``        model id
- ``SHL_AUTH_TOKEN``   provider key (SECRET)
- ``SHL_REPRO_PATH``   REQUIRED. Where a reproducing test is written, with a
                       literal ``{}`` for the issue number. No default is
                       correct across languages, so an unset value raises.
- ``SHL_BASE_URL``     endpoint, for non-native providers (optional)
- ``SHL_AUTH_ENV``     provider-native auth env var name, e.g. ANTHROPIC_API_KEY (optional)
- ``SHL_ADAPTER``      import path of the target adapter module (default "adapters.target")
- ``SHL_CYCLE_ID``     the Actions run id, pinning all three roles to one
                       evidence dir (read by ``evidence.cycle_dir``)
- ``SHL_VARS``         the repo's whole variable context as JSON, folded into
                       the environment for the adapter (read by
                       ``adapters.hydrate_repo_vars``)

The full install-facing contract, including what only the workflows read, is in
``SKILL.md`` and ``artifacts/setup.md``.
"""
import json
import os
import sys
from pathlib import Path

from adapters import load_adapter, optional_ids_fn
from agent.base import AgentRole
from agent.harness import ConfiguredAgent, ModelConfig, _subprocess_runner, get_harness
from evidence import cycle_dir, record_artifact, record_json
from gh_state import _gh, count_attempts, find_open_issue, fingerprint_marker
from guardrails.incident_memory import DEFAULT_LOG_PATH, search_similar
from guardrails.stdio import read_text_arg
from log_compact import compact_log, failure_fingerprints, unfingerprintable
from role import run_role


# The loop appends one incident per healed cycle and runs on a cron for months,
# so what reaches the prompt has to stay bounded. Exact-fingerprint matching
# already keeps unrelated incidents out entirely; these cap the pathological
# case where the SAME failure recurs dozens of times.
MAX_RECALLED_INCIDENTS = 3
MAX_ROOT_CAUSE_CHARS = 400


def format_incidents(matches: list) -> str:
    """Render matched incidents for a prompt, newest first, reverted first of all.

    A reverted fix is the highest-value thing memory holds: it says the obvious
    fix was already tried and made things worse. Ranking those ahead of merged
    ones means the cap can never be what drops that warning.

    Repeats of one failure collapse into a single entry carrying how many times
    it happened. The store is append-only, so a bug that keeps coming back
    leaves a row per occurrence, all saying nearly the same thing — spending
    three prompt slots on one fact, and letting twenty repeats of a solved bug
    push a reverted warning about a different one out of the cap. Collapsed, the
    count becomes the signal: a failure seen twelve times whose fix was once
    reverted is the clearest evidence available that the obvious fix is wrong.
    """
    if not matches:
        return ""
    # Group by WHICH failure and HOW it ended. Merged and reverted must never
    # merge into one entry: that would hide that a fix was once reverted.
    groups: dict[tuple, list] = {}
    for position, record in enumerate(matches):
        key = (tuple(sorted(record.fingerprints)), record.outcome)
        groups.setdefault(key, []).append((position, record))

    collapsed = []
    for members in groups.values():
        newest_position, newest = members[-1]
        collapsed.append((newest_position, newest, len(members)))

    ranked = sorted(
        collapsed, key=lambda item: (item[1].outcome != "reverted", -item[0])
    )
    kept = ranked[:MAX_RECALLED_INCIDENTS]
    lines = []
    for _, record, count in kept:
        flag = " [KNOWN-BAD: prior fix was reverted]" if record.outcome == "reverted" else ""
        seen = f", seen {count} times" if count > 1 else ""
        cause = record.root_cause
        if len(cause) > MAX_ROOT_CAUSE_CHARS:
            cause = cause[:MAX_ROOT_CAUSE_CHARS].rstrip() + "…"
        lines.append(f"- issue #{record.issue_id} ({record.outcome}{seen}){flag}: {cause}")
    omitted = len(collapsed) - len(kept)
    if omitted:
        # Say what was dropped; a silent cap reads as "this is everything".
        lines.append(f"({omitted} other matching incident(s) omitted)")
    return "\n".join(lines)


def recall_incidents(raw: str, repo_root: str = "", log_path: Path = DEFAULT_LOG_PATH) -> str:
    """Prior incidents for the failures present in ``raw``, formatted for a prompt.

    ``raw`` is the log as ``read_log`` returned it, never the compacted signal:
    identities are derived from frames compaction is free to drop, and a lookup
    keyed on less than the record was written with matches nothing while
    reporting success.

    Keyed on failure fingerprints, never on the issue title: the title is prose
    a model writes fresh each cycle, so it differs even for an identical repeat.
    The identities come from the same source `record_cycle` writes with, so the
    two halves of memory cannot key a failure differently. Empty string when
    nothing matches, which is the common case and the correct one.
    """
    prints = failure_fingerprints(raw, strip_prefix=repo_root, ids_fn=optional_ids_fn())
    return format_incidents(search_similar(prints, log_path=log_path))


def build_agent_from_env(
    env: dict, runner=_subprocess_runner, evidence_dir=None
) -> ConfiguredAgent:
    harness = get_harness(env["SHL_HARNESS"])
    model = ModelConfig(
        model=env["SHL_MODEL"],
        auth_token=env["SHL_AUTH_TOKEN"],
        base_url=env.get("SHL_BASE_URL", ""),
        auth_env=env.get("SHL_AUTH_ENV", ""),
    )
    return ConfiguredAgent(harness, model, runner=runner, evidence_dir=evidence_dir)


def run_watch(adapter, raw_out: Path | None = None) -> str:
    """Read the target's log, return compacted signal. Empty string = idle.

    ``raw_out`` receives the log exactly as read. Everything that derives a
    failure IDENTITY reads that file rather than the signal, and the split is
    load-bearing: compaction keeps error lines plus their INDENTED
    continuation, while a Go panic puts its trace behind a blank line and
    indents none of it. Identifying from compacted text therefore hands
    ``TargetAdapter.failure_ids`` a message with no frames, on precisely the
    runtimes that method exists to serve — it returns ``[]``, the cycle is
    refused as unfingerprintable, and the loop stalls on every tick forever.

    One ``read_log`` call serves both, because on most targets it is a network
    request against the host's log API.

    The prompt budget belongs to ``compact_log``; restating its default here
    gave two places one number, and changing the real one would have left this
    on the old value.
    """
    raw = adapter.read_log()
    if raw_out is not None:
        raw_out.write_text(raw, encoding="utf-8")
    return compact_log(raw)


def _repo_root(repo_path) -> str:
    """Absolute repo root, stripped from fingerprints so they are machine-portable."""
    return str(Path(repo_path).resolve())


def _agent_cwd(repo_path) -> Path:
    """The cwd the coding agent runs under: the installed `.shl/`
    inside the target repo. Running there lets the headless agent auto-load the
    loop's operating doc (`.shl/CLAUDE.md`) and scopes its default
    file view to the loop folder; the target's own code is one level up (`../`)."""
    return Path(repo_path) / ".shl"


# Where the workflow will write the reproducing test. Diagnose does not pick the
# path, but it must still write correct relative imports, which depend on the
# directory — so it is told the pattern. `{}` matches SHL_TEST_ONE's placeholder
# convention; the issue number does not exist yet at Diagnose time.
#
# There is deliberately NO default. A pytest-shaped default
# (`tests/test_repro_issue_{}.py`) is on every other stack a wrong answer
# indistinguishable from a working one: Diagnose gets told to write a `.py` path
# (a strong steer to emit the wrong language outright) and the file lands where
# the runner never collects, so the red-then-green proof silently never runs.
# Phase 1 discovers this per target; unset means the install is incomplete.
def _repro_path_pattern() -> str:
    pattern = os.environ.get("SHL_REPRO_PATH")
    if not pattern:
        raise RuntimeError(
            "SHL_REPRO_PATH is unset. It has no default because no default is "
            "correct across languages: set it to this target's reproducing-test "
            "path, repo-relative, with a literal {} for the issue number."
        )
    return pattern


def repro_path(issue_number) -> str:
    """Where the reproducing test for ``issue_number`` gets written.

    Every driver calls this rather than substituting the pattern itself. Two
    drivers given the same rule eventually follow it differently, and the copy
    nothing tests is the one that ships — so this is a function to call, not a
    rule to follow. Exposed as ``loop.py repro-path N`` for the workflow,
    which is shell.
    """
    return _repro_path_pattern().replace("{}", str(issue_number))


def run_diagnose(agent, repo_path, log: str, raw_log: str) -> dict:
    """Diagnose the failure in ``log``, with any prior incidents for it recalled.

    Two views of one failure, and they are not interchangeable. ``log`` is the
    compacted signal, which is what the agent reads: bounded, one slot per
    distinct failure. ``raw_log`` is what came off the target, and it is what
    the recall keys on, because a fingerprint is built from frames compaction
    is free to drop.

    The recall is still computed here rather than taken as a parameter.
    Otherwise every caller has to derive the lookup key the same way, and one
    that derives it differently recalls nothing while reporting success: the
    mechanism is dead on the path that ships, with no symptom anywhere. Handing
    in the source text is safe in a way handing in the KEY is not — there is
    still exactly one derivation, and it lives here.
    """
    return run_role(
        AgentRole.DIAGNOSE,
        {
            "log": log,
            "repro_path": _repro_path_pattern(),
            "incident_memory": recall_incidents(raw_log, _repo_root(repo_path)),
        },
        agent,
        _agent_cwd(repo_path),
    )


def run_fix(agent, repo_path, issue: str, repro: str, raw_log: str = "",
            issue_number: str = "") -> dict:
    """``raw_log`` is the cycle's uncompacted log — the same key Diagnose recalled on.

    The frozen path is COMPUTED here from the issue number rather than accepted
    as a parameter, for the reason L9 records: two drivers given the same rule
    drift, and the one under test is not the one that ships. `templates/fix.md`
    and the loop-agent operating doc both promise Fix is told which file is
    frozen, and this is what keeps that promise true — without it Fix sees a red
    test in the tree with no way to know it is untouchable, and editing it burns
    an attempt on a gate rejection whose message never names the file.

    It is injected only when a reproducing test was actually WRITTEN. The
    workflow writes that file solely when Diagnose returned ``reproducible:
    true``, and the prompts say plainly that most runtime failures do not reduce
    to a deterministic test — so on the majority path there is no frozen file,
    and naming one anyway sends Fix looking for something that is not there.
    ``repro`` carries the answer already: Diagnose's ``repro_test`` object,
    empty whenever nothing was specified.

    The same answer gates ``repro`` itself. The caller passes
    ``json.dumps(repro_test)``, which is the two-character string ``{}`` when
    there is none — non-empty, so ``build_prompt`` renders its heading and the
    prompt asserts that Diagnose specified a test while showing an empty
    object. An absent thing has to be absent from the prompt, not present and
    hollow.
    """
    has_repro = _has_repro(repro)
    return run_role(
        AgentRole.FIX,
        {
            "issue": issue,
            "repro": repro if has_repro else "",
            "frozen": repro_path(issue_number) if issue_number and has_repro else "",
            "incident_memory": recall_incidents(raw_log, _repo_root(repo_path)),
        },
        agent,
        _agent_cwd(repo_path),
    )


def frozen_repro(diagnose: dict) -> str:
    """The reproducing test the WORKFLOW acted on, or ``"{}"`` when there is none.

    `heal.yml` writes the repro file, proves it red, freezes it and passes
    `--frozen` to the gate on one condition: `jq -r .reproducible` is the string
    `true`. Anything downstream that decides the same question differently is
    describing a cycle that did not happen.

    Reading `repro_test.code` was such a difference. `role.validate_contract`
    constrains only the forward direction — `reproducible: true` requires a
    usable `repro_test` — so a payload carrying `reproducible: false` beside
    populated code is valid, and on it the workflow froze nothing while Fix was
    handed a FROZEN path over a template promising that file was already proven
    red. Both call sites in `main()` spelled the derivation out separately,
    which is what let it be wrong in one place; it is resolved here instead, the
    same way `failure_ids` is.

    `is not True` rather than a truthiness test: `"false"` is a non-empty string
    and this must not be the one place a quoted flag gets through.
    """
    if diagnose.get("reproducible") is not True:
        return "{}"
    return json.dumps(diagnose.get("repro_test", {}))


def _has_repro(repro: str) -> bool:
    """Whether `frozen_repro` carried reproducing test code through.

    Kept separate because the callers hold the rendered string rather than the
    payload. It answers "is there code here", and `frozen_repro` is what decides
    whether there was supposed to be.
    """
    try:
        return bool(json.loads(repro or "{}").get("code"))
    except (ValueError, AttributeError):
        return False


def run_review(agent, repo_path, diff: str, issue: str = "", repro: str = "") -> dict:
    """Review the diff against the failure it claims to fix.

    The issue and the frozen repro go in alongside the diff. Withhold them and
    Review can judge only whether the code looks sound — not whether it
    addresses the failure that was actually reported. A human reviewer opens the
    linked issue first; this is the same information.

    The repro is gated on actually existing, by the same rule ``run_fix``
    applies to the frozen path: most failures do not reduce to a deterministic
    test, and on those cycles the caller's ``json.dumps({})`` would otherwise
    render a heading claiming Diagnose specified one. Review is told to judge
    the fix against the issue and the repro, so a hollow section is worse here
    than anywhere else — it invites a verdict on a test that was never written.
    """
    return run_role(
        AgentRole.REVIEW,
        {"diff": diff, "issue": issue, "repro": repro if _has_repro(repro) else ""},
        agent,
        _agent_cwd(repo_path),
    )


def under_attempt_cap(issue_number: int, cap: int = 2, gh_runner=_gh) -> bool:
    """True if the issue has had fewer than ``cap`` fix attempts (still allowed)."""
    return count_attempts(issue_number, gh_runner=gh_runner) < cap


def _read_arg_or_stdin(args: list[str]) -> str:
    """The text a subcommand was given, by the shared path-or-stdin rule."""
    return read_text_arg(args[1] if len(args) > 1 else None)


def _raw_log_arg(args: list[str], index: int, cmd: str) -> str | None:
    """The uncompacted log at ``args[index]``, or None after reporting why not.

    Refused rather than defaulted to the compacted signal. That default is
    available, reads as harmless, and silently reinstates the whole defect:
    recall would key on frames the record was never written with, match
    nothing, and report an empty recall — which is what a project that has
    never failed twice looks like. A missing argument is a wiring error and
    says so.
    """
    if len(args) > index:
        return read_text_arg(args[index])
    sys.stderr.write(
        f"{cmd} needs the RAW log path as argument {index}. Incident recall keys "
        "on failure identities derived from frames the compacted signal does not "
        "carry, so recalling from the signal matches nothing and says nothing.\n"
    )
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: loop.py {watch|diagnose|fix|review} ...", file=sys.stderr)
        return 2
    cmd = args[0]
    repo = Path(os.environ.get("GITHUB_WORKSPACE", "."))

    # watch runs no agent, so it needs no provider token in env — keeping the
    # agent lazy lets the workflow withhold SHL_AUTH_TOKEN from the watch step.
    if cmd == "watch":
        raw_out = Path(args[1]) if len(args) > 1 else None
        signal = run_watch(load_adapter(), raw_out)
        print(signal if signal else "IDLE")
        return 0

    # Also token-free, and deliberately so: the step that uses it runs
    # agent-authored code and therefore holds no secret.
    if cmd == "repro-path":
        print(repro_path(args[1]))
        return 0

    # Issue identity, for the workflow's find-or-file step. Derived here rather
    # than in shell so there is ONE implementation of "which failure is this".
    # A shell version keying on the model-written title matches inconsistently,
    # and every miss files a duplicate issue, resetting the attempt cap to zero.
    if cmd in ("fingerprint-marker", "find-issue"):
        signal = Path(args[1]).read_text(encoding="utf-8")
        ids_fn = optional_ids_fn()
        # Refuse a failure whose stack this compactor cannot read, rather than
        # proceeding with no dedup key. Without one the workflow finds no
        # existing issue, opens a new one, and counts zero prior attempts, so
        # the same failure is healed again on every tick and the cap that exists
        # to stop that never accumulates. Stopping here costs one cycle and says
        # why; continuing costs every cycle and says nothing.
        if unfingerprintable(signal, ids_fn=ids_fn):
            sys.stderr.write(
                "this log carries a failure but no fingerprint could be derived "
                "from it: frame parsing covers Python and V8 stacks, and issue "
                "dedup, incident recall and the attempt cap all key on the "
                "fingerprint. Refusing rather than filing an unkeyed issue.\n"
            )
            return 1
        prints = failure_fingerprints(signal, strip_prefix=_repo_root(repo), ids_fn=ids_fn)
        if cmd == "fingerprint-marker":
            print(fingerprint_marker(prints))
        else:
            found = find_open_issue(prints)
            print(found if found else "")
        return 0

    # Diagnose opens a cycle; Fix and Review are separate processes that join it.
    evidence = cycle_dir(repo, new=(cmd == "diagnose"))
    agent = build_agent_from_env(os.environ, evidence_dir=evidence)
    if cmd == "diagnose":
        log = _read_arg_or_stdin(args)
        raw_log = _raw_log_arg(args, 2, cmd)
        if raw_log is None:
            return 2
        record_artifact(evidence, "signal.txt", log)
        payload = run_diagnose(agent, repo, log, raw_log)
        record_json(evidence, "diagnose.json", payload)
        print(json.dumps(payload))
        return 0
    if cmd == "fix":
        issue_n = int(args[1])
        raw_log = _raw_log_arg(args, 2, cmd)
        if raw_log is None:
            return 2
        if not under_attempt_cap(issue_n):
            payload = {"escalate": True, "reason": "attempt cap reached"}
            record_json(evidence, "fix.json", payload)
            print(json.dumps(payload))
            return 0
        # Fix reads Diagnose's output directly: the issue body + the frozen
        # reproducing test CODE it must make pass without touching the file.
        diagnose = json.loads(Path("diagnose.json").read_text(encoding="utf-8"))
        issue = diagnose.get("issue_body", "")
        repro = frozen_repro(diagnose)
        payload = run_fix(agent, repo, issue, repro, raw_log=raw_log, issue_number=args[1])
        record_json(evidence, "fix.json", payload)
        print(json.dumps(payload))
        return 0
    if cmd == "review":
        diff = _read_arg_or_stdin(args)
        record_artifact(evidence, "fix.diff", diff)
        # Same source Fix reads, so the reviewer sees the failure the fix claims
        # to address rather than judging the diff in isolation.
        diagnose = json.loads(Path("diagnose.json").read_text(encoding="utf-8"))
        payload = run_review(
            agent,
            repo,
            diff,
            issue=diagnose.get("issue_body", ""),
            repro=frozen_repro(diagnose),
        )
        record_json(evidence, "review.json", payload)
        print(json.dumps(payload))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
