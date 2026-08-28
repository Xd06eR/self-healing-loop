"""The one rule for "a path, or `-` for stdin", shared by both entry points.

`loop.py` and `guardrails/cli.py` are separate command-line surfaces that the
workflows drive with the same convention. The framework's most expensive
recurring defect is two callers following one rule separately until they follow
it differently, and the missing-argument case is where these two diverge first,
so the rule is a function to call rather than a convention to remember —
and living in `guardrails/` keeps it importable from `loop.py` without a
guardrail importing the loop entry point back.
"""
import sys
from pathlib import Path


def read_text_arg(value: str | None) -> str:
    """The text an argument names: stdin for `-` or a missing value, else a file.

    `-` is the universal shell convention for "read stdin", and `heal.yml` pipes
    the fix diff in exactly that way. Treating it as a filename raises
    ``FileNotFoundError: '-'`` at the point three agent calls have already been
    spent, which is where a cycle can least afford to die.
    """
    if value is None or value == "-":
        return sys.stdin.read()
    return Path(value).read_text(encoding="utf-8")
