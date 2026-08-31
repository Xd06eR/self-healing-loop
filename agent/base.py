"""Agent invocation contract — which agent does the loop's reasoning.

Parallel to TargetAdapter (which project to heal), this is "which agent". Which
agents actually ship is ``agent.harness.REGISTRY``, and nowhere else — a name
listed here that the registry does not carry reads as support that does not
exist.

The structured-output contract lives in the role prompt (templates/*.md tell
the agent to end with a fenced json block), NOT in a CLI flag. That is what
makes this agent-agnostic: every agent can emit a json block, so extraction is
shared here rather than depending on one agent's --output-format.
"""
import json
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path


class AgentRole(Enum):
    DIAGNOSE = "diagnose"  # read-only: diagnose + specify a reproducing test
    FIX = "fix"            # edits source only: no shell, and the frozen repro test is off-limits
    REVIEW = "review"      # read-only: judge a diff


class AgentAdapter(ABC):
    @abstractmethod
    def run(self, prompt: str, role: AgentRole, cwd: Path) -> str:
        """Invoke the agent headlessly on `prompt` with role-appropriate
        least-privilege permissions, in `cwd`, returning raw stdout."""


def _fenced_blocks(stdout: str) -> list[str]:
    """Fenced blocks, found by LINE rather than by character.

    A closing fence is a line whose only content is the backticks. A fence
    carried inside the answer is not: JSON forbids a literal newline inside a
    string, so ```` ``` ```` appearing in a value is always mid-line, alongside
    the rest of that value.

    That distinction is the whole point. A non-greedy `(.*?)``` ` stops at the
    first fence anywhere, including one inside `repro_test.code` — which is
    SOURCE, and plausibly contains a fence for any repro test touching markdown,
    docs or prompts. Every candidate block then fails to parse and a valid
    Diagnose answer raises, upstream of issue filing: no issue, no fingerprint
    marker, no attempt recorded, and the next tick spends another agent call on
    the same failure.
    """
    blocks: list[str] = []
    current: list[str] | None = None
    for line in stdout.split("\n"):
        stripped = line.strip()
        if current is None:
            if stripped.startswith("```"):
                current = []
            continue
        if stripped == "```":
            blocks.append("\n".join(current))
            current = None
            continue
        current.append(line)
    return blocks


def extract_structured(stdout: str) -> dict:
    """Pull the agent's structured answer out of raw stdout.

    Scans fenced blocks last-first and returns the first that parses as JSON,
    so trailing chatter after the answer is tolerated and an untagged ``` fence
    still works. Raises ValueError if none parse — a garbled agent response
    fails loud rather than silently returning {}.
    """
    for block in reversed(_fenced_blocks(stdout)):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable json block in agent output")