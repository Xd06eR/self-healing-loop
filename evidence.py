"""Per-cycle evidence bundle — what each agent was asked, and what it answered.

Every role (Diagnose / Fix / Review) is a one-shot headless subprocess whose
prompt, reasoning, stderr and exit code would otherwise vanish: the workflow
keeps only the parsed json, and the runner is ephemeral. That makes a cycle
impossible to audit or reproduce after the fact. This module writes each step to
disk instead, so a human can review what happened and a failed cycle can be
debugged from its artifacts.

Everything written here is scrubbed (``guardrails.confidentiality_filter``),
local and cloud alike — a failure log can carry a leaked key straight into a
prompt, and the bundle is uploadable as an Actions artifact. Two invariants hold
by construction: env VALUES are never written (that dict holds the provider
token, only key names go in), and the prompt is stored once rather than
duplicated into the metadata.

Scrubbing is applied to values BEFORE json serialization, never to serialized
json text: the ``key=value`` rule is deliberately greedy and would eat a closing
quote, corrupting the file it was meant to protect.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.base import AgentRole
from agent.harness import AgentRun
from guardrails.confidentiality_filter import scrub

_EVIDENCE_DIRNAME = "evidence"


def cycle_dir(repo_path: Path, new: bool = False) -> Path:
    """Create and return this cycle's evidence dir under the installed loop.

    The workflow exports ``SHL_CYCLE_ID`` (the Actions run id), which pins every
    step of one cloud cycle to one dir and always wins. Without it the dir is
    named for the UTC time the cycle opened (``YYYYMMDD-HHMMSS``), so a human
    browsing past cycles can tell when each ran — this is runtime data, not
    authored content, which is why a timestamp is right here. The format sorts
    lexicographically in chronological order, which is what "join the latest"
    below relies on.

    ``new=True`` opens a new dir and is passed by the role that STARTS a cycle
    (Diagnose); Fix and Review run as separate processes and join the most
    recent dir instead, or the three steps of one heal would scatter across
    three.
    """
    base = Path(repo_path) / ".shl" / _EVIDENCE_DIRNAME
    cid = os.environ.get("SHL_CYCLE_ID", "").strip()
    if not cid:
        existing = sorted(p.name for p in base.glob("*") if p.is_dir())
        if new or not existing:
            cid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        else:
            cid = existing[-1]
    target = base / cid
    target.mkdir(parents=True, exist_ok=True)
    return target


def record_agent_run(
    directory: Path,
    role: AgentRole,
    prompt: str,
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: Path,
    run: AgentRun,
) -> None:
    """Write one role's prompt, raw output, stderr and metadata."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = role.value

    (directory / f"{stem}.prompt.txt").write_text(scrub(prompt), encoding="utf-8")
    (directory / f"{stem}.raw.txt").write_text(scrub(run.stdout), encoding="utf-8")
    if run.stderr.strip():
        (directory / f"{stem}.stderr.txt").write_text(scrub(run.stderr), encoding="utf-8")

    meta = {
        "role": stem,
        # The prompt already has its own file; repeating it here would double
        # the disclosure surface for no gain.
        "argv": [
            f"<prompt: {len(prompt)} chars>" if a == prompt else scrub(str(a))
            for a in argv
        ],
        "env_keys": sorted(env),
        "cwd": scrub(str(cwd)),
        "returncode": run.returncode,
        "duration_s": run.duration_s,
        "stdout_chars": len(run.stdout),
        "stderr_chars": len(run.stderr),
    }
    (directory / f"{stem}.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def record_artifact(directory: Path, name: str, text: str) -> None:
    """Write a scrubbed plain-text cycle artifact (signal, diff, gate, suite)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(scrub(text), encoding="utf-8")


def _scrub_deep(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_deep(v) for v in value]
    return value


def record_json(directory: Path, name: str, payload: Any) -> None:
    """Write a scrubbed json artifact, scrubbing values not serialized text."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps(_scrub_deep(payload), indent=2), encoding="utf-8"
    )
