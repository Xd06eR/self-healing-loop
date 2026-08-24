"""Per-role runner — assembles the prompt, calls the agent, parses + validates output.

This is the unit the loop calls once per role (Diagnose / Fix / Review). It does
not run git/gh and does not enforce the attempt cap; the workflow (plain Actions
steps) does the GitHub writes, and ``loop.py fix`` enforces the cap before it
calls the agent. Keeping this module to prompt -> agent -> structured-and-
validated dict makes it unit-testable without a real harness or network.
"""
from pathlib import Path

from agent.base import AgentAdapter, AgentRole, extract_structured

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Context keys this runner knows how to label. Each role gets the keys relevant
# to it; absent keys are simply not injected.
_CONTEXT_LABELS = {
    "log": "Failure log (compacted)",
    "repro_path": "Reproducing test will be written to (pattern; {} is the issue number)",
    "incident_memory": "Incident memory (prior fixes for this signature)",
    "issue": "Issue filed (root cause from Diagnose)",
    "repro": "Reproducing test Diagnose specified",
    "frozen": "FROZEN reproducing test — do not edit, rename or move this file",
    "diff": "Diff to review",
}

# Required structured-output fields per role. Mirrors templates/*.md.
_CONTRACT = {
    AgentRole.DIAGNOSE: {"issue_title", "issue_body", "reproducible", "confidence"},
    AgentRole.FIX: {"summary", "files_changed"},
    AgentRole.REVIEW: {"approved", "reason"},
}


def _load_template(role: AgentRole) -> str:
    return (_TEMPLATE_DIR / f"{role.value}.md").read_text(encoding="utf-8")


def build_prompt(role: AgentRole, context: dict) -> str:
    """Role template followed by a Context section of the supplied keys.

    Raises on a key no label describes. The loop iterates the LABEL table, so
    an unlabelled key is dropped rather than rendered — and the result is a
    prompt quietly missing context the caller believed it had supplied, with
    the agent reasoning without it and returning an answer that still
    validates. Context is the whole of what a cold role can know.
    """
    unlabelled = sorted(set(context) - set(_CONTEXT_LABELS))
    if unlabelled:
        raise KeyError(
            f"no context label for {unlabelled}; add one to _CONTEXT_LABELS or "
            f"the key is silently dropped from the prompt"
        )
    parts = [_load_template(role), "", "## Context", ""]
    for key, label in _CONTEXT_LABELS.items():
        value = context.get(key)
        if value:
            parts.append(f"### {label}")
            parts.append(str(value))
            parts.append("")
    return "\n".join(parts)


def validate_contract(role: AgentRole, payload: dict) -> None:
    missing = set(_CONTRACT[role]) - payload.keys()
    # A Diagnose that claims reproducible MUST ship runnable reproducing test
    # CODE. The workflow composes the path itself, writes that code, proves it
    # red, and freezes
    # the file before Fix runs — so a missing or shapeless repro_test voids the
    # red-then-green safety net.
    if role is AgentRole.DIAGNOSE and "reproducible" in payload:
        # Booleans only. The workflow branches on `jq -r .reproducible` compared
        # against the STRING "true", which the JSON string `"true"` also
        # satisfies while `is True` here does not — so a string slips past this
        # check with no repro_test, and the Red step then writes jq's literal
        # `null` as the reproducing test, watches it fail, and records red=true
        # for a reason unrelated to the bug. The escalation that exists to catch
        # a bad repro spec never fires.
        if not isinstance(payload["reproducible"], bool):
            missing.add("reproducible{boolean}")
        elif payload["reproducible"]:
            repro = payload.get("repro_test")
            if not isinstance(repro, dict) or not repro.get("code"):
                missing.add("repro_test{code}")
        # No path check, because there is no agent-supplied path to check. The
        # workflow composes it from SHL_REPRO_PATH and the issue number, so an
        # injected path in the payload is inert rather than filtered. A regex
        # here could only encode one language's convention — a pytest-shaped
        # `^tests/test_[A-Za-z0-9_]+\.py$` fails validation on every non-Python
        # target, so no cycle completes. Guarded by tests/test_workflows.py.
    # `approved` decides whether agent-written code reaches the default branch,
    # and the workflow reads it with `jq -e`, which exits 0 for everything that
    # is not JSON `false` or `null` — so `"false"`, `"no"`, `0`, `""` and `[]`
    # all merge. Same trap as `reproducible` above, on the field with the larger
    # blast radius, so it gets the same guard rather than trusting the model to
    # emit an unquoted boolean.
    if role is AgentRole.REVIEW and not isinstance(payload.get("approved"), bool):
        missing.discard("approved")
        missing.add("approved{boolean}")
    if missing:
        raise ValueError(
            f"{role.value} output missing required fields: {sorted(missing)}"
        )


def run_role(
    role: AgentRole, context: dict, agent: AgentAdapter, repo_path: Path
) -> dict:
    """Prompt the agent for one role, return its validated structured output.

    Raises ValueError on an unparseable response or a contract violation; the
    caller decides whether that consumes a Fix attempt or escalates.
    """
    prompt = build_prompt(role, context)
    stdout = agent.run(prompt, role, repo_path)
    payload = extract_structured(stdout)
    validate_contract(role, payload)
    return payload