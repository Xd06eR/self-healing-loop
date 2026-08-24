"""The suite must not silently drop tests.

A `if __name__ == "__main__": unittest.main()` block placed mid-file is legal
Python and completely invisible: discovery imports the module and collects every
class, while running the file directly stops collecting at the guard. So the
count differs by how you invoke it, both numbers look plausible, and the classes
that vanish are whichever happen to sit below the guard.

This has already cost this project real coverage — the loop-tree tamper guard
and the manifest execution check were among the classes being dropped. The fix
was applied to the files someone noticed and not to the rest, which is why the
check now lives here instead of in a habit.
"""
import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _guard_line(tree: ast.Module) -> int | None:
    """Line of the `if __name__ == "__main__":` block, or None if absent."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            return node.lineno
    return None


class NoTestIsHiddenBehindTheMainGuard(unittest.TestCase):
    def test_every_guard_is_the_last_statement_in_its_file(self):
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            guard = _guard_line(tree)
            if guard is None:
                continue
            hidden = [
                n.name
                for n in tree.body
                if isinstance(n, ast.ClassDef) and n.lineno > guard
            ]
            if hidden:
                offenders.append(f"{path.name}: {', '.join(hidden)}")
        self.assertEqual(
            offenders,
            [],
            "these classes sit below the __main__ guard, so running the file "
            "directly collects none of them; move the guard to end of file",
        )

    def test_the_check_actually_parsed_files(self):
        # An empty glob would report every file clean. That failure mode is the
        # whole reason this module exists, so it does not get to have it too.
        parsed = [p for p in TESTS.glob("test_*.py")]
        self.assertGreater(len(parsed), 15)

    def test_at_least_one_file_carries_a_guard(self):
        # If the convention changed to "no guards anywhere", the check above
        # passes vacuously forever. This fails when that happens, so the
        # convention gets revisited rather than silently lapsing.
        guards = [
            p.name
            for p in TESTS.glob("test_*.py")
            if _guard_line(ast.parse(p.read_text(encoding="utf-8"))) is not None
        ]
        self.assertGreater(len(guards), 0)


if __name__ == "__main__":
    unittest.main()
