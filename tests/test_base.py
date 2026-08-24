import unittest

from adapters.base import TargetAdapter


class _MinimalAdapter(TargetAdapter):
    """Everything a target is REQUIRED to provide."""

    def read_log(self) -> str:
        return "ok"


class TestTargetAdapter(unittest.TestCase):
    def test_cannot_instantiate_abstract_base(self):
        with self.assertRaises(TypeError):
            TargetAdapter()

    def test_read_log_alone_satisfies_the_contract(self):
        # The contract deliberately requires ONE method. deploy() and rollback()
        # have no callers anywhere in the framework, so making them abstract
        # forces every installer to write two dead methods to satisfy an ABC.
        # Instantiating is the assertion; the return value is the stub's own.
        self.assertIsInstance(_MinimalAdapter(), TargetAdapter)

    def test_optional_methods_default_to_not_supported(self):
        # None means "this target cannot answer", and each caller handles it:
        # the gate falls back to demanding a green suite, verify skips the
        # health probe. Neither may read None as a negative answer.
        adapter = _MinimalAdapter()
        self.assertIsNone(adapter.failing_tests())
        self.assertIsNone(adapter.health_check())


if __name__ == "__main__":
    unittest.main()
