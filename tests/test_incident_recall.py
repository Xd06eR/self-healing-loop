"""End-to-end contract for incident memory: does a repeat failure get recognized?

Unit tests around `search_similar` go green over a dead mechanism whenever every
one of them queries with a short hand-written string. The real loop's query is a
raw log line — a traceback frame, or a uvicorn INFO line carrying an ephemeral
port — and matching that by substring against a human-written issue title can
never hit, not even for an identical repeat.

So these tests drive the whole path with REAL log text, which is the only shape
that catches it.
"""
import contextlib
import importlib
import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from gh_state import _marked_fingerprints
from pathlib import Path

from guardrails.incident_memory import (
    IncidentRecord,
    record_cycle,
    record_incident,
    search_similar,
)
from adapters import optional_ids_fn
from log_compact import compact_log, failure_fingerprints
from loop import MAX_RECALLED_INCIDENTS, format_incidents, recall_incidents

# A real traceback, as uvicorn writes it. The address and the object id differ
# between two occurrences of the SAME bug, so any key that keeps them recognises
# no repeat.
LOG_A = '''INFO:     127.0.0.1:35402 - "POST /generate-image HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "/srv/app/.venv/lib/python3.12/site-packages/starlette/routing.py", line 74, in app
    response = await func(request)
  File "/srv/app/app/main.py", line 55, in get_comfyui
    return app.state.comfyui()
AttributeError: 'State' object has no attribute 'comfyui'
'''

# The same failure, later: different client port, different frame above it.
LOG_A_AGAIN = '''INFO:     127.0.0.1:41999 - "POST /generate-image HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "/srv/app/.venv/lib/python3.12/site-packages/anyio/_backends/_asyncio.py", line 1033, in run
    result = context.run(func, *args)
  File "/srv/app/app/main.py", line 55, in get_comfyui
    return app.state.comfyui()
AttributeError: 'State' object has no attribute 'comfyui'
'''

# A genuinely different bug, elsewhere in the same file.
LOG_B = '''ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "/srv/app/app/main.py", line 127, in health_check
    return {"status": STATIS}
NameError: name 'STATIS' is not defined
'''


def _record(log_path, fingerprints, outcome="merged", issue_id="1", root_cause="x"):
    record_incident(
        IncidentRecord(
            issue_id=issue_id,
            signature="title",
            root_cause=root_cause,
            fix_commit="abc123",
            outcome=outcome,
            fingerprints=fingerprints,
        ),
        log_path=log_path,
    )


class Fingerprints(unittest.TestCase):
    def test_same_failure_fingerprints_identically_despite_volatile_detail(self):
        self.assertEqual(failure_fingerprints(LOG_A), failure_fingerprints(LOG_A_AGAIN))

    def test_fingerprint_names_the_exception_and_its_raise_site(self):
        (fingerprint,) = failure_fingerprints(LOG_A, strip_prefix="/srv/app")
        self.assertEqual(fingerprint, "AttributeError@app/main.py:55")

    def test_different_failures_fingerprint_differently(self):
        self.assertNotEqual(failure_fingerprints(LOG_A), failure_fingerprints(LOG_B))

    def test_a_log_with_two_failures_yields_both(self):
        both = failure_fingerprints(LOG_A + LOG_B, strip_prefix="/srv/app")
        self.assertEqual(
            sorted(both),
            ["AttributeError@app/main.py:55", "NameError@app/main.py:127"],
        )

    def test_two_bugs_raising_in_the_same_library_line_stay_distinct(self):
        # Both of these bottom out at the identical starlette frame. Keying on
        # the deepest frame makes them one fingerprint, so memory recalls the
        # wrong prior fix and compaction drops one of the two failures.
        def tb(app_line, attr):
            return (
                "ERROR:    Exception in ASGI application\n"
                "Traceback (most recent call last):\n"
                f'  File "/srv/app/app/main.py", line {app_line}, in get_thing\n'
                f"    return app.state.{attr}\n"
                '  File "/srv/app/.venv/lib/python3.12/site-packages/starlette/datastructures.py", line 686, in __getattr__\n'
                "    raise AttributeError(message.format(self.__class__.__name__, key))\n"
                f"AttributeError: 'State' object has no attribute '{attr}'\n"
            )

        first = failure_fingerprints(tb(51, "olama"), strip_prefix="/srv/app")
        second = failure_fingerprints(tb(55, "comfyui"), strip_prefix="/srv/app")
        self.assertNotEqual(first, second)
        self.assertEqual(first, ["AttributeError@app/main.py:51"])
        self.assertEqual(second, ["AttributeError@app/main.py:55"])

    def test_a_traceback_entirely_inside_a_library_still_fingerprints(self):
        # No project frame at all: fall back to the deepest frame rather than
        # producing nothing.
        only_vendor = (
            "Traceback (most recent call last):\n"
            '  File "/srv/app/.venv/lib/python3.12/site-packages/httpx/_client.py", line 900, in send\n'
            "    raise ConnectError(msg)\n"
            "ConnectError: connection refused\n"
        )
        self.assertEqual(
            failure_fingerprints(only_vendor, strip_prefix="/srv/app"),
            ["ConnectError@.venv/lib/python3.12/site-packages/httpx/_client.py:900"],
        )

    def test_strip_prefix_makes_a_fingerprint_portable_between_machines(self):
        # Same bug, checked out at two different paths (local vs a CI runner).
        local = failure_fingerprints(LOG_A, strip_prefix="/srv/app")
        runner = failure_fingerprints(
            LOG_A.replace("/srv/app", "/home/runner/work/proj/proj"),
            strip_prefix="/home/runner/work/proj/proj",
        )
        self.assertEqual(local, runner)


class Recall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "log.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_repeat_of_the_same_failure_is_recognized(self):
        _record(self.log_path, failure_fingerprints(LOG_A))
        hits = search_similar(failure_fingerprints(LOG_A_AGAIN), log_path=self.log_path)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].fix_commit, "abc123")

    def test_an_unrelated_failure_recalls_nothing(self):
        _record(self.log_path, failure_fingerprints(LOG_A))
        self.assertEqual(search_similar(failure_fingerprints(LOG_B), log_path=self.log_path), [])

    def test_a_legacy_record_without_fingerprints_still_parses(self):
        # A legacy record carries no `fingerprints` field at all, so the read
        # path must skip it rather than crash on the missing key.
        self.log_path.write_text(
            '{"issue_id": "1", "signature": "s", "root_cause": "r", '
            '"fix_commit": "c", "outcome": "merged"}\n',
            encoding="utf-8",
        )
        self.assertEqual(search_similar(failure_fingerprints(LOG_A), log_path=self.log_path), [])


class ContextBudget(unittest.TestCase):
    """Memory must not grow into the prompt as the loop runs for months."""

    # The cap bounds DISTINCT failures; repeats of one failure collapse first
    # (see RepeatsCollapse), so these use a separate fingerprint per record.
    def test_only_the_most_recent_matches_are_injected(self):
        records = [
            IncidentRecord("1", "s", f"cause {i}", "c", "merged", [f"fp-{i}"])
            for i in range(10)
        ]
        rendered = format_incidents(records)
        self.assertEqual(rendered.count("- issue #"), MAX_RECALLED_INCIDENTS)
        # Most recent kept, oldest dropped.
        self.assertIn("cause 9", rendered)
        self.assertNotIn("cause 0", rendered)

    def test_the_omitted_count_is_stated_rather_than_hidden(self):
        records = [
            IncidentRecord("1", "s", "c", "c", "merged", [f"fp-{i}"]) for i in range(10)
        ]
        self.assertIn(f"{10 - MAX_RECALLED_INCIDENTS} other", format_incidents(records))

    def test_a_long_root_cause_is_truncated(self):
        rendered = format_incidents([IncidentRecord("1", "s", "y" * 5000, "c", "merged", ["fp"])])
        self.assertLess(len(rendered), 1000)
        self.assertIn("…", rendered)

    def test_a_reverted_fix_is_flagged_and_ranked_first(self):
        records = [
            IncidentRecord("1", "s", "newer merged fix", "c", "merged", ["fp"]),
            IncidentRecord("2", "s", "older reverted fix", "c", "reverted", ["fp"]),
        ]
        rendered = format_incidents(records)
        self.assertIn("KNOWN-BAD", rendered)
        # A prior fix that got reverted is the highest-value warning; it must not
        # be the entry that a cap silently drops.
        self.assertLess(rendered.index("older reverted fix"), rendered.index("newer merged fix"))

    def test_nothing_matched_renders_empty_not_a_stub_heading(self):
        self.assertEqual(format_incidents([]), "")


class RepeatsCollapse(unittest.TestCase):
    """One recurring failure must not fill the prompt with near-identical prose.

    The store is append-only, so a bug that comes back twelve times leaves twelve
    rows saying almost the same thing. Three of them reaching the agent is three
    slots spent on one fact. Collapsed, the repeat count is itself the signal:
    "seen 12 times, a prior fix was reverted" is the strongest hint available
    that the obvious fix is wrong.
    """

    def test_identical_repeats_render_as_one_entry_with_a_count(self):
        records = [
            IncidentRecord(str(i), "s", "the same root cause", "c", "merged", ["fp"])
            for i in range(4)
        ]
        rendered = format_incidents(records)
        self.assertEqual(rendered.count("- issue #"), 1)
        self.assertIn("seen 4 times", rendered)

    def test_a_single_occurrence_is_not_labelled_with_a_count(self):
        rendered = format_incidents([IncidentRecord("1", "s", "c", "c", "merged", ["fp"])])
        self.assertNotIn("seen", rendered)

    def test_merged_and_reverted_for_one_failure_stay_separate(self):
        # Collapsing these together would hide that a fix was once reverted.
        records = [
            IncidentRecord("1", "s", "the fix that held", "c", "merged", ["fp"]),
            IncidentRecord("2", "s", "the fix that broke things", "c", "reverted", ["fp"]),
        ]
        rendered = format_incidents(records)
        self.assertEqual(rendered.count("- issue #"), 2)
        self.assertIn("KNOWN-BAD", rendered)

    def test_the_cap_counts_distinct_failures_not_raw_rows(self):
        # 12 rows, 2 distinct failures: both fit, nothing is reported omitted.
        records = [
            IncidentRecord(str(i), "s", "a", "c", "merged", ["fp-a"]) for i in range(6)
        ] + [IncidentRecord(str(i), "s", "b", "c", "merged", ["fp-b"]) for i in range(6)]
        rendered = format_incidents(records)
        self.assertEqual(rendered.count("- issue #"), 2)
        self.assertNotIn("omitted", rendered)

    def test_repeats_do_not_crowd_out_a_reverted_warning(self):
        # 20 merged repeats of one failure plus one reverted incident: the
        # reverted one must survive, which raw-row ranking would not guarantee.
        records = [
            IncidentRecord(str(i), "s", "noise", "c", "merged", ["fp-a"]) for i in range(20)
        ] + [IncidentRecord("99", "s", "the reverted one", "c", "reverted", ["fp-b"])]
        rendered = format_incidents(records)
        self.assertIn("the reverted one", rendered)
        self.assertIn("KNOWN-BAD", rendered)


class ReachesTheAgent(unittest.TestCase):
    """The recall must land in the PROMPT, not merely work as a function.

    `recall_incidents` can be correct in isolation while the loop recalls
    nothing, because a driver that derives the lookup key itself derives it
    wrong. Asserting on the payload run_diagnose/run_fix actually build is what
    closes that gap: they own the recall, so a caller cannot supply it.
    """

    def setUp(self):
        import loop

        self.loop = loop
        self._original = loop.search_similar
        loop.search_similar = lambda fingerprints, log_path=None: (
            [IncidentRecord("42", "t", "the prior root cause", "c", "reverted", ["fp"])]
            if fingerprints
            else []
        )
        # run_diagnose injects the repro-path pattern into the prompt, and that
        # pattern has no default — no path is correct across languages, so an
        # unset one raises rather than steering Diagnose at pytest. heal.yml
        # exports it at job level; a test exercising run_diagnose needs it too.
        self._prior_repro = os.environ.get("SHL_REPRO_PATH")
        os.environ["SHL_REPRO_PATH"] = "tests/test_repro_issue_{}.py"

    def tearDown(self):
        self.loop.search_similar = self._original
        if self._prior_repro is None:
            os.environ.pop("SHL_REPRO_PATH", None)
        else:
            os.environ["SHL_REPRO_PATH"] = self._prior_repro

    def _payload_for(self, call):
        captured = {}

        def fake_run_role(role, context, agent, cwd):
            captured.update(context)
            return {}

        original = self.loop.run_role
        self.loop.run_role = fake_run_role
        try:
            call()
        finally:
            self.loop.run_role = original
        return captured

    def test_diagnose_prompt_carries_the_recalled_incident(self):
        context = self._payload_for(
            lambda: self.loop.run_diagnose(object(), Path("/repo"), LOG_A, LOG_A)
        )
        # Non-empty is the property here: the rendering is ContextBudget's,
        # and asserting the stub's literals through format_incidents would make
        # this class fail for reasons that have nothing to do with the wiring.
        self.assertTrue(context["incident_memory"])

    def test_fix_prompt_carries_it_too(self):
        context = self._payload_for(
            lambda: self.loop.run_fix(object(), Path("/repo"), "issue", "repro", raw_log=LOG_A)
        )
        self.assertTrue(context["incident_memory"])

    def test_no_signal_means_no_recall_rather_than_a_crash(self):
        context = self._payload_for(
            lambda: self.loop.run_fix(object(), Path("/repo"), "issue", "repro", raw_log="")
        )
        self.assertEqual(context["incident_memory"], "")


# A real Go panic. Its trace sits behind a blank line and none of it is indented,
# so the built-in line filter keeps the panic line alone and no frame is ever
# reached — which is why this runtime needs the adapter's own identities.
GO_PANIC = """panic: runtime error: index out of range [3] with length 2

goroutine 1 [running]:
main.process(0xc000010030, 0x3)
\t/srv/app/handler.go:42 +0x1d
main.main()
\t/srv/app/main.go:12 +0x65
exit status 2
"""

# The same bug on a later run: different goroutine address, different caller.
GO_PANIC_AGAIN = """panic: runtime error: index out of range [5] with length 2

goroutine 7 [running]:
main.process(0xc0000b4180, 0x5)
\t/srv/app/handler.go:42 +0x1d
main.serve.func1()
\t/srv/app/server.go:88 +0x12c
exit status 2
"""

_STUB_ADAPTER = '''
import os
import re


class _GoAdapter:
    """Stands in for an installed adapters/target.py on a Go project."""

    def read_log(self):
        # Whatever the test put there, so `loop.py watch` can be driven for
        # real instead of having its output hand-written by the caller.
        return os.environ.get("SHL_STUB_LOG", "")

    def failure_ids(self, raw_log):
        kind = re.search(r"^panic: (.+?)(?: \\[| with |$)", raw_log, re.MULTILINE)
        frame = re.search(r"^\\t(\\S+\\.go):(\\d+)", raw_log, re.MULTILINE)
        if not (kind and frame):
            return []
        return [f"{kind.group(1)}@{frame.group(1)}:{frame.group(2)}"]


adapter = _GoAdapter()
'''


@contextlib.contextmanager
def go_adapter_installed():
    """Put a Go adapter on the import path the way a real install does.

    Driving this through `SHL_ADAPTER` and a real import, rather than passing a
    function in, is the point: the loop resolves the adapter itself, and a test
    that hands one over proves nothing about whether the resolution happens.
    """
    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / "shl_stub_adapters"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "target.py").write_text(textwrap.dedent(_STUB_ADAPTER))
        sys.path.insert(0, tmp)
        prior = os.environ.get("SHL_ADAPTER")
        os.environ["SHL_ADAPTER"] = "shl_stub_adapters.target"
        importlib.invalidate_caches()
        try:
            yield
        finally:
            if prior is None:
                os.environ.pop("SHL_ADAPTER", None)
            else:
                os.environ["SHL_ADAPTER"] = prior
            sys.path.remove(tmp)
            for name in [n for n in sys.modules if n.startswith("shl_stub_adapters")]:
                del sys.modules[name]


class MemoryUsesTheTargetsOwnIdentities(unittest.TestCase):
    """Record and recall must key a Go failure the same way dedup already does.

    `fingerprint-marker` asks the adapter for failure identities, so issue dedup
    and the attempt cap work on a Go target. A consumer that does not ask stores
    an empty fingerprint list and matches nothing — the guardrail switched off on
    exactly the targets the seam exists to serve, and an empty list looks
    identical to a project that has simply never failed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "log.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_a_recorded_go_failure_carries_the_adapters_identity(self):
        with go_adapter_installed():
            entry = record_cycle(
                issue_id="7",
                signature="index out of range in process()",
                root_cause="slice indexed past its length",
                fix_commit="abc123",
                outcome="merged",
                raw_log=GO_PANIC,
                repo_root="/srv/app",
                log_path=self.log_path,
            )
        self.assertEqual(entry.fingerprints, ["runtime error: index out of range@handler.go:42"])

    def test_a_go_repeat_is_recalled(self):
        """The round trip, which is the only thing that proves memory works.

        Both halves derive the key from a raw log, so both must ask the same
        source. Either one falling back to built-in parsing yields an empty set
        and the recall silently returns nothing.
        """
        with go_adapter_installed():
            record_cycle(
                issue_id="7",
                signature="index out of range in process()",
                root_cause="slice indexed past its length",
                fix_commit="abc123",
                outcome="reverted",
                raw_log=GO_PANIC,
                repo_root="/srv/app",
                log_path=self.log_path,
            )
            recalled = recall_incidents(
                GO_PANIC_AGAIN, repo_root="/srv/app", log_path=self.log_path
            )
        self.assertIn("#7", recalled)
        self.assertIn("KNOWN-BAD", recalled)

    def test_an_unrelated_go_failure_recalls_nothing(self):
        other = GO_PANIC.replace("handler.go:42", "billing.go:9")
        with go_adapter_installed():
            record_cycle(
                issue_id="7",
                signature="index out of range in process()",
                root_cause="slice indexed past its length",
                fix_commit="abc123",
                outcome="merged",
                raw_log=GO_PANIC,
                repo_root="/srv/app",
                log_path=self.log_path,
            )
            self.assertEqual(
                recall_incidents(other, repo_root="/srv/app", log_path=self.log_path), ""
            )

    def test_the_dedup_marker_derives_the_same_identity(self):
        """Run the command heal.yml runs, not the function underneath it.

        The marker keys issue dedup and the attempt cap; the record keys memory.
        Both must resolve the adapter the same way or a Go failure is deduped
        under one identity and remembered under another, and each half looks
        correct on its own.
        """
        import io

        import loop

        with go_adapter_installed(), tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "signal.txt"
            signal.write_text(GO_PANIC)
            captured, sys.stdout = sys.stdout, io.StringIO()
            try:
                rc = loop.main(["fingerprint-marker", str(signal)])
                out = sys.stdout.getvalue()
            finally:
                sys.stdout = captured
        self.assertEqual(rc, 0, "a stack the adapter can identify was refused")
        self.assertTrue(
            any("handler.go:42" in f for f in _marked_fingerprints(out)), out
        )

    def test_without_an_adapter_the_built_in_parsing_still_applies(self):
        """A Python target must keep working with no `failure_ids` in sight."""
        record_cycle(
            issue_id="3",
            signature="missing attribute",
            root_cause="state never set",
            fix_commit="def456",
            outcome="merged",
            raw_log=LOG_A,
            repo_root="/srv/app",
            log_path=self.log_path,
        )
        self.assertIn(
            "#3", recall_incidents(LOG_A_AGAIN, repo_root="/srv/app", log_path=self.log_path)
        )


class TheIdentityIsDerivedFromTheRawLog(unittest.TestCase):
    """Drive the commands `watch.yml` runs, in the order it runs them.

    Every other test in this file hands the identity path a raw panic directly.
    The workflow does not: it runs `loop.py watch`, which compacts. Were the
    marker given that output, it would see only what compaction kept. Compaction keeps error lines and
    their INDENTED continuation, and a Go panic puts its trace behind a blank
    line, which ends the collected block — so the frames the adapter keys on are gone
    before the marker runs. It returns `[]`, `unfingerprintable` reports the
    failure unreadable, and the cycle is refused. Every tick, forever, on the
    runtimes this seam exists to serve.

    So the raw log travels to every consumer of an identity, and compaction
    serves the prompt alone. These pin both halves of that: the raw log
    arrives intact, and the prompt is still compacted.
    """

    @contextlib.contextmanager
    def _go_target(self):
        prior = os.environ.get("SHL_STUB_LOG")
        os.environ["SHL_STUB_LOG"] = GO_PANIC
        try:
            with go_adapter_installed(), tempfile.TemporaryDirectory() as tmp:
                yield Path(tmp)
        finally:
            if prior is None:
                os.environ.pop("SHL_STUB_LOG", None)
            else:
                os.environ["SHL_STUB_LOG"] = prior

    def _watch(self, raw_path):
        """`loop.py watch <raw>`: compacted signal returned, raw log written."""
        import io

        import loop

        captured, sys.stdout = sys.stdout, io.StringIO()
        try:
            rc = loop.main(["watch", str(raw_path)])
            return rc, sys.stdout.getvalue()
        finally:
            sys.stdout = captured

    def test_watch_writes_the_uncompacted_log_for_the_identity_path(self):
        with self._go_target() as tmp:
            raw = tmp / "signal.raw"
            rc, _ = self._watch(raw)
            written = raw.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn("/srv/app/handler.go:42", written)

    def test_the_marker_reads_that_file_and_keeps_the_adapters_identity(self):
        import io

        import loop

        with self._go_target() as tmp:
            raw = tmp / "signal.raw"
            self._watch(raw)
            captured, sys.stdout = sys.stdout, io.StringIO()
            try:
                rc = loop.main(["fingerprint-marker", str(raw)])
                out = sys.stdout.getvalue()
            finally:
                sys.stdout = captured
        self.assertEqual(rc, 0, "a stack the adapter can identify was refused")
        self.assertTrue(any("handler.go:42" in f for f in _marked_fingerprints(out)), out)

    def test_diagnose_recalls_from_the_raw_log_and_not_from_the_signal(self):
        """The mutation no other test in this suite can catch.

        Every other recall test passes one string as BOTH the signal and the
        raw log, so swapping `raw_log` for `log` inside `run_diagnose` changes
        no result anywhere and the whole suite stays green over the defect.
        Separating them needs a failure whose identity does not survive
        compaction — which is exactly the case this seam exists for.
        """
        import loop

        captured = {}
        with self._go_target():
            # The premise, asserted rather than assumed: compaction destroys
            # this identity and the raw log carries it.
            self.assertEqual(
                failure_fingerprints(compact_log(GO_PANIC), ids_fn=optional_ids_fn()), []
            )
            self.assertTrue(failure_fingerprints(GO_PANIC, ids_fn=optional_ids_fn()))

            with mock.patch.object(
                loop, "search_similar",
                lambda fingerprints, log_path=None: (
                    [IncidentRecord("42", "t", "prior root cause", "c", "reverted", ["fp"])]
                    if fingerprints else []
                ),
            ), mock.patch.object(
                loop, "run_role",
                lambda role, context, agent, cwd: captured.update(context) or {},
            ), mock.patch.dict(
                os.environ, {"SHL_REPRO_PATH": "tests/test_repro_issue_{}.py"}
            ):
                loop.run_diagnose(object(), Path("/repo"), compact_log(GO_PANIC), GO_PANIC)

        self.assertTrue(
            captured["incident_memory"],
            "recall keyed on the compacted signal, which carries no Go identity",
        )

    def test_what_reaches_the_prompt_is_still_compacted(self):
        # The other half of the fix. Carrying raw to the identity path must not
        # send raw to the agent: the size bound is why compaction exists.
        with self._go_target() as tmp:
            _, signal = self._watch(tmp / "signal.raw")
        self.assertIn("panic: runtime error", signal)
        self.assertNotIn("goroutine 1 [running]:", signal)


class RecallStripsTheCheckoutPathWithoutBeingTold(unittest.TestCase):
    """`run_diagnose` must pass the repo root; recall must use it.

    A fingerprint is `Type@path:line`. Recorded on one checkout and looked up
    on another — a laptop and a runner, or two runner jobs with different
    workspace paths — the absolute prefixes differ and nothing matches.

    Two levels, because the defect can live at either and each test alone
    leaves the other free. Every pre-existing test passed `strip_prefix`
    itself, which proved the derivation and let `run_diagnose` pass `""`
    with the whole suite green — recall returning "" forever, indistinguishable
    from a project that has simply never failed twice.
    """

    def test_the_derivation_makes_a_recorded_fingerprint_portable(self):
        recorded = failure_fingerprints(
            'Traceback (most recent call last):\n'
            '  File "/build/aaa/app/handler.py", line 42, in handle\n'
            "KeyError: 'id'\n",
            strip_prefix="/build/aaa",
        )
        looked_up = failure_fingerprints(
            'Traceback (most recent call last):\n'
            '  File "/runner/work/zzz/app/handler.py", line 42, in handle\n'
            "KeyError: 'id'\n",
            strip_prefix="/runner/work/zzz",
        )
        self.assertEqual(recorded, looked_up)
        self.assertTrue(recorded, "no fingerprint at all")

    def test_run_diagnose_hands_recall_the_repo_root(self):
        # A spy, because the store path is absolute at import and the wiring —
        # not the store — is what the mutation removes.
        import loop as _loop

        seen = {}

        def spy(log, repo_root="", **kw):
            seen["repo_root"] = repo_root
            return ""

        class Stub:
            def run(self, prompt, role, cwd):
                return (
                    '```json\n{"issue_title": "t", "issue_body": "b",'
                    ' "reproducible": false, "confidence": "low"}\n```'
                )

        with mock.patch.object(_loop, "recall_incidents", spy), \
             mock.patch.dict(os.environ, {"SHL_REPRO_PATH": "tests/t_{}.py"}):
            _loop.run_diagnose(Stub(), "/some/checkout", log="boom", raw_log="boom")
        self.assertTrue(
            seen.get("repo_root"),
            "run_diagnose passed no repo root, so a fingerprint recorded under "
            "a different workspace path can never match and recall is dead",
        )
        self.assertIn("/some/checkout", seen["repo_root"])


if __name__ == "__main__":
    unittest.main()
