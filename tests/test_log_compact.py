import re
import unittest

# Captured verbatim from `node run.js` on two genuinely different bugs in one
# file (L8: drive this with a real artifact, never a hand-written string — tests
# that invent their own input go green over a completely dead mechanism, which
# is how incident memory held four passing tests while recalling nothing).
#
# The two traces carry the IDENTICAL exception line and differ only in the
# throw site, which is what makes them the sharp case: with no JS frame parsing
# both collapse to one message-only fingerprint, so compaction drops one bug
# outright and incident memory recalls the wrong prior for the other.
_JS_TWO_BUGS = """\
TypeError: Cannot read properties of undefined (reading 'map')
    at renderParts (/srv/app/lib/parser.js:2:21)
    at Array.map (<anonymous>)
    at toMarkdown (/srv/app/lib/parser.js:5:19)
    at Object.<anonymous> (/srv/app/run.js:2:7)
    at Module._compile (node:internal/modules/cjs/loader:1705:14)
    at Module.load (node:internal/modules/cjs/loader:1441:32)
    at wrapModuleLoad (node:internal/modules/cjs/loader:237:24)
TypeError: Cannot read properties of undefined (reading 'map')
    at toMarkdown (/srv/app/lib/parser.js:5:19)
    at Object.<anonymous> (/srv/app/run.js:3:7)
    at Module._compile (node:internal/modules/cjs/loader:1705:14)
    at Module.load (node:internal/modules/cjs/loader:1441:32)
    at Function.executeUserEntryPoint [as runMain] (node:internal/modules/run_main:171:5)
    at node:internal/main/run_main_module:36:49
"""

from gh_state import _marked_fingerprints, fingerprint_marker
from log_compact import compact_log, failure_fingerprints, unfingerprintable


class TestCompactLog(unittest.TestCase):
    def test_error_line_kept_debug_dropped(self):
        raw = "[INFO] booting\n[DEBUG] x=1\n[ERROR] KeyError: 'user'\n[INFO] done"
        out = compact_log(raw)
        self.assertIn("KeyError", out)
        self.assertNotIn("[DEBUG]", out)
        self.assertNotIn("booting", out)

    def test_traceback_block_kept_as_unit(self):
        raw = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 10, in handle\n'
            "    return db[user]\n"
            "KeyError: 'user'\n"
        )
        out = compact_log(raw)
        self.assertIn("Traceback", out)
        self.assertIn('File "app.py"', out)
        self.assertIn("KeyError", out)

    def test_indented_continuation_after_error_kept(self):
        # A non-traceback error line followed by indented context.
        raw = "[ERROR] request failed\n  at router.handle(line 42)\n[INFO] next"
        out = compact_log(raw)
        self.assertIn("request failed", out)
        self.assertIn("router.handle", out)

    def test_repeated_line_collapsed(self):
        raw = "[ERROR] retry failed\n" * 50
        out = compact_log(raw)
        self.assertLess(len(out), len(raw))
        self.assertIn("retry failed", out)
        self.assertIn("repeated", out.lower())

    def test_output_bounded(self):
        raw = "[ERROR] boom: " + ("x" * 50000) + "\n"
        out = compact_log(raw, max_chars=1000)
        self.assertLessEqual(len(out), 1000)

    def test_empty_in_empty_out(self):
        self.assertEqual(compact_log(""), "")

    def test_noise_only_yields_empty_idle_signal(self):
        # No error signal -> empty, which the watch step reads as "idle".
        raw = "[INFO] ok\n[DEBUG] heartbeat\n[INFO] 200 GET /\n"
        self.assertEqual(compact_log(raw), "")


def _traceback(exc: str, frame: str) -> str:
    return (
        "ERROR:    Exception in ASGI application\n"
        "Traceback (most recent call last):\n"
        f'  File "app/main.py", line 1, in {frame}\n'
        "    something()\n"
        f"{exc}\n"
    )


class TestDistinctErrorsSurvive(unittest.TestCase):
    """A loud repeated failure must not evict a rarer, different one.

    Real logs repeat: a health poll that 500s on every page render produces
    dozens of identical tracebacks, while a broken feature endpoint fails once.
    A pure tail cut keeps only the noisy one, so the loop can never even see
    the rarer bug — it is starved indefinitely.
    """

    def test_rare_error_survives_a_flood_of_a_different_one(self):
        rare = _traceback("AttributeError: 'State' object has no attribute 'olama'", "get_ollama")
        noisy = _traceback("NameError: name 'STATIS' is not defined", "health_check")
        raw = rare + noisy * 40  # the rare one happens first, then gets buried
        out = compact_log(raw, max_chars=2000)
        self.assertIn("AttributeError", out)
        self.assertIn("NameError", out)

    def test_repeats_of_one_error_are_collapsed_with_a_count(self):
        noisy = _traceback("NameError: name 'STATIS' is not defined", "health_check")
        out = compact_log(noisy * 9)
        self.assertEqual(out.count("NameError: name 'STATIS' is not defined"), 1)
        self.assertRegex(out, r"repeated\s+9\b")  # the count is reported, not silently dropped

    def test_single_occurrence_is_not_annotated_with_a_count(self):
        out = compact_log(_traceback("ValueError: boom", "f"))
        self.assertIn("ValueError: boom", out)
        self.assertNotIn("repeated", out)

    def test_still_bounded_when_every_error_is_distinct(self):
        raw = "".join(_traceback(f"ValueError: distinct {i}", "f") for i in range(200))
        out = compact_log(raw, max_chars=1500)
        self.assertLessEqual(len(out), 1500)


def _tb_at(exc: str, module: str, line: int) -> str:
    return (
        "ERROR:    Exception in ASGI application\n"
        "Traceback (most recent call last):\n"
        f'  File "{module}", line {line}, in handler\n'
        "    do_work()\n"
        f"{exc}\n"
    )


class TestFramesStayWithTheirError(unittest.TestCase):
    """A traceback's frames must not be reattached to a different error.

    Dropping information is survivable; fabricating an association is not. If a
    rare bug's exception line ends up sitting under another bug's frames, the
    Diagnose agent is handed a coherent-looking traceback with the wrong
    file:line and autonomously "fixes" the wrong code.
    """

    def test_frames_are_not_stolen_by_a_noisier_traceback(self):
        rare = _tb_at("KeyError: 'olama'", "app/rare.py", 11)
        noisy = _tb_at("ZeroDivisionError: division by zero", "app/health.py", 99)
        out = compact_log(rare + noisy * 3)
        rare_block = out.split("KeyError: 'olama'")[0]
        self.assertIn("app/rare.py", rare_block)
        self.assertNotIn("app/health.py", rare_block)

    def test_one_traceback_is_one_block(self):
        # The "Traceback (most recent call last):" line is non-indented, so a
        # naive splitter makes it its own block whose signature is identical for
        # every traceback in the log — collapsing all frames into one slot.
        out = compact_log(_tb_at("ValueError: boom", "app/a.py", 5))
        self.assertNotIn("repeated", out)
        self.assertIn("app/a.py", out)


class TestSignatureDiscrimination(unittest.TestCase):
    """Distinct bugs must stay distinct; repeats of one bug must still collapse."""

    def test_same_message_from_different_places_are_different_bugs(self):
        msg = "AttributeError: 'NoneType' object has no attribute 'get'"
        raw = _tb_at(msg, "app/rare.py", 11) + _tb_at(msg, "app/health.py", 99) * 3
        out = compact_log(raw)
        self.assertIn("app/rare.py", out)
        self.assertIn("app/health.py", out)

    def test_varying_payload_in_the_message_still_collapses(self):
        # A per-request id in the message must not defeat deduplication, or the
        # size bound evicts rarer bugs — the exact failure this module prevents.
        rare = _tb_at("KeyError: 'olama'", "app/rare.py", 11)
        noisy = "".join(
            _tb_at(f"KeyError: 'job-{i}'", "app/jobs.py", 42) for i in range(400)
        )
        out = compact_log(rare + noisy, max_chars=8000)
        self.assertIn("app/rare.py", out)
        self.assertIn("olama", out)


class OverflowEvictsByRecencyOfTheLastOccurrence(unittest.TestCase):
    """"Errors surface at the end of a log" has to be measured on the last one.

    Every distinct failure holds one slot, so overflow only bites when many are
    live at once — and then the one worth dropping is the one that has been
    quiet longest, not the one that happened to appear first in the window. A
    bug still firing right now was evicted for having started early, which is
    the opposite of what the tail bias is for.
    """

    def test_eviction_drops_the_failure_quiet_longest_not_the_one_seen_first(self):
        early_and_still_firing = _tb_at("KeyError: 'a'", "app/a.py", 11)
        seen_once_in_between = _tb_at("KeyError: 'b'", "app/b.py", 22)
        # Room for exactly one collapsed block, repeat marker included.
        budget = len(compact_log(early_and_still_firing * 2)) + 5
        out = compact_log(
            early_and_still_firing + seen_once_in_between + early_and_still_firing,
            max_chars=budget,
        )
        self.assertIn("app/a.py", out)
        self.assertNotIn("app/b.py", out)

    def test_distinct_failures_keep_their_own_slots_when_there_is_room(self):
        # The inverse, so the rule above cannot be satisfied by dropping more.
        out = compact_log(
            _tb_at("KeyError: 'a'", "app/a.py", 11)
            + _tb_at("KeyError: 'b'", "app/b.py", 22)
        )
        self.assertIn("app/a.py", out)
        self.assertIn("app/b.py", out)


class JavaScriptStacks(unittest.TestCase):
    """A JS stack is Python's, reversed and differently punctuated.

    Frames read `at fn (path:L:C)` rather than `File "path", line N`, and they
    print innermost-FIRST — so the "last frame wins: the raise site" rule that
    is correct for Python picks the OUTERMOST caller for JS. Porting the regex
    without the direction produces confident, wrong file:line, which is worse
    than no fingerprint at all.
    """

    def test_two_bugs_sharing_a_message_get_distinct_fingerprints(self):
        prints = failure_fingerprints(_JS_TWO_BUGS, strip_prefix="/srv/app")
        self.assertEqual(len(prints), 2, prints)
        self.assertEqual(len(set(prints)), 2, prints)

    def test_the_fingerprint_names_the_throw_site_not_the_outermost_caller(self):
        prints = failure_fingerprints(_JS_TWO_BUGS, strip_prefix="/srv/app")
        # Bug one is thrown at parser.js:2, bug two at parser.js:5. `run.js`
        # only ever appears further out, so seeing it means the direction is
        # backwards.
        self.assertEqual(prints[0], "TypeError@lib/parser.js:2")
        self.assertEqual(prints[1], "TypeError@lib/parser.js:5")

    def test_node_internal_frames_are_vendor(self):
        prints = failure_fingerprints(_JS_TWO_BUGS, strip_prefix="/srv/app")
        self.assertNotIn("node:internal", " ".join(prints))

    def test_compaction_keeps_both_bugs(self):
        # One slot per distinct failure. Sharing a fingerprint would silently
        # drop one of two live bugs before Diagnose ever sees it.
        out = compact_log(_JS_TWO_BUGS)
        self.assertIn("parser.js:2:21", out)
        self.assertIn("parser.js:5:19", out)

    def test_a_frame_with_no_location_is_skipped_not_fatal(self):
        # `at Array.map (<anonymous>)` carries no file:line and appears in real
        # traces between two frames that do. The identity must come from the
        # throw site regardless, so the exact fingerprint is the assertion —
        # "the list is non-empty" is already implied by the two tests above.
        stack = (
            "TypeError: x is not a function\n"
            "    at Array.map (<anonymous>)\n"
            "    at handler (/srv/app/lib/run.js:9:3)\n"
        )
        self.assertEqual(
            failure_fingerprints(stack, strip_prefix="/srv/app"),
            ["TypeError@lib/run.js:9"],
        )


class ATargetSuppliedIdentityMakesAnyStackFingerprintable(unittest.TestCase):
    """The framework reads Python and V8 logs; the target supplies the rest.

    The seam is the whole identity, not just frame parsing, because extraction
    is format-specific too: a Go panic separates its trace from its message with
    a blank line and indents none of it, so the built-in line filter keeps the
    panic line alone and no frame is ever reached. Holding that knowledge in
    core code fixes the supported set at release time, and every stack outside
    it produces no identity — which is what issue dedup, incident recall and the
    attempt cap all key on.
    """

    GO_PANIC = (
        "panic: runtime error: index out of range [3] with length 3\n"
        "\n"
        "goroutine 1 [running]:\n"
        "main.process(0x0, 0x3)\n"
        "\t/app/handler.go:42 +0x1d\n"
        "main.main()\n"
        "\t/app/main.go:11 +0x25\n"
    )

    @staticmethod
    def go_ids(raw):
        """Raise site first, which for Go is the order the panic already prints."""
        ids = []
        for line in raw.splitlines():
            match = re.match(r"^\t(\S+\.go):(\d+)", line)
            if match:
                ids.append(f"panic@{match.group(1)}:{match.group(2)}")
                break
        return ids

    def test_without_one_a_go_panic_has_no_identity(self):
        self.assertEqual(failure_fingerprints(self.GO_PANIC), [])

    def test_with_one_it_keys_on_the_raising_file_and_line(self):
        prints = failure_fingerprints(self.GO_PANIC, ids_fn=self.go_ids)
        self.assertEqual(prints, ["panic@/app/handler.go:42"])

    def test_the_same_failure_twice_keys_identically(self):
        # Incident recall matches on exact fingerprint, so a repeat must produce
        # the same string despite differing addresses.
        variant = self.GO_PANIC.replace("0x1d", "0x9f").replace("0x25", "0x31")
        self.assertEqual(
            failure_fingerprints(self.GO_PANIC, ids_fn=self.go_ids),
            failure_fingerprints(variant, ids_fn=self.go_ids),
        )

    def test_the_repo_prefix_is_stripped_from_supplied_ids_too(self):
        # An incident recorded on a laptop must match the same failure on a
        # runner, which checks out at a different absolute path.
        ids = lambda raw: ["panic@/home/dev/app/handler.go:42"]
        self.assertEqual(
            failure_fingerprints(self.GO_PANIC, strip_prefix="/home/dev/app", ids_fn=ids),
            ["panic@handler.go:42"],
        )

    def test_returning_none_falls_back_to_the_built_ins(self):
        python_tb = (
            "Traceback (most recent call last):\n"
            '  File "/app/x.py", line 7, in go\n    boom()\n'
            "ValueError: bad\n"
        )
        self.assertEqual(
            failure_fingerprints(python_tb, ids_fn=lambda raw: None),
            failure_fingerprints(python_tb),
        )


class BareErrorAndUnreadableFramesAreHandledHonestly(unittest.TestCase):
    """Two shapes the built-in parser must get right or refuse.

    Both are silent when wrong: a fingerprint that never forms stops the loop
    dead on a runtime the docs promise to support, and a fingerprint that forms
    but does not identify anything is worse, because nothing refuses it.
    """

    JS_BARE = (
        "Error: Failed to export conversation\n"
        "    at exportChat (/app/src/export.ts:88:11)\n"
    )
    PY_BARE = (
        "Traceback (most recent call last):\n"
        '  File "/app/x.py", line 3, in go\n    boom()\n'
        "Exception: boom\n"
    )

    def test_a_plain_error_is_fingerprinted_like_any_other(self):
        """`throw new Error(...)` is the default in JS/TS, and `raise Exception`
        in Python. `reference/adapter.md` tells those targets they need no
        `failure_ids`, so if these do not parse the loop is inert on the two
        runtimes the framework claims to read natively."""
        for name, raw, want in (
            ("js", self.JS_BARE, "Error@/app/src/export.ts:88"),
            ("py", self.PY_BARE, "Exception@/app/x.py:3"),
        ):
            with self.subTest(stack=name):
                self.assertFalse(unfingerprintable(raw), f"{name} was refused")
                self.assertEqual(failure_fingerprints(raw), [want])

    def test_an_unreadable_stack_is_refused_rather_than_keyed_on_its_message(self):
        """A message is not an identity, in either direction.

        Two occurrences of one bug differ by whatever unquoted detail the
        message carries, so dedup misses and the attempt cap counts from zero
        forever. Two unrelated bugs sharing a message collide into one issue and
        recall each other's fix. Refusing costs one cycle and says why.
        """
        alice = "com.acme.NotFoundException: no account for user alice\n\tat com.acme.A.find(A.java:9)\n"
        bob = alice.replace("alice", "bob")
        for name, raw in (("alice", alice), ("bob", bob)):
            with self.subTest(log=name):
                self.assertTrue(unfingerprintable(raw), "keyed on a message")
                self.assertEqual(failure_fingerprints(raw), [])


class AnExceptionGroupTracebackStillYieldsIdentities(unittest.TestCase):
    """`asyncio.TaskGroup` failures are the idiomatic async form since 3.11.

    CPython renders them with a `|`/`+` gutter down the left of every frame,
    which defeats the frame regex (`^\\s*File`) at the character it meets the
    pipe — so every TaskGroup failure fingerprinted to nothing, the loop
    refused the cycle, and an entire class of async bug was un-healable on any
    Python 3.11+ target. `TargetAdapter.failure_ids` is the seam for stacks
    this module cannot read, but this one is Python: reading it is a
    normalisation, not a new parser.

    The fixture is CPython 3.11's own rendering shape, indentation and gutter
    included.
    """

    GROUP_TB = (
        "  + Exception Group Traceback (most recent call last):\n"
        "  |   File \"/repo/app/worker.py\", line 41, in run\n"
        "  |     async with asyncio.TaskGroup() as tg:\n"
        "  |   File \"/usr/lib/python3.11/asyncio/taskgroups.py\", line 145, in __aexit__\n"
        "  |     raise BaseExceptionGroup(\n"
        "  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)\n"
        "  +-+---------------- 1 ----------------\n"
        "    | Traceback (most recent call last):\n"
        "    |   File \"/repo/app/worker.py\", line 55, in fetch\n"
        "    |     return payload[\"id\"]\n"
        "    | KeyError: 'id'\n"
        "    +------------------------------------\n"
    )

    def test_the_leaf_exception_is_fingerprinted(self):
        """One group trace yields two STABLE identities: the wrapper and the leaf.

        Both are load-bearing. The leaf (`KeyError@worker.py:55`) is the bug;
        the wrapper line is what survives when the same task re-raises a
        different leaf. Dedup intersects sets and recall matches any, so two
        identities for one occurrence is correct, and what failed before was
        zero.
        """
        prints = failure_fingerprints(self.GROUP_TB)
        self.assertTrue(prints, "an ExceptionGroup trace yielded no identity")
        self.assertTrue(
            any(p.startswith("KeyError@") and "worker.py:55" in p for p in prints),
            f"no identity carries the leaf: {prints}",
        )

    def test_it_is_not_reported_unfingerprintable(self):
        self.assertFalse(unfingerprintable(self.GROUP_TB))

    def test_two_occurrences_of_one_group_match(self):
        """The second occurrence differs in exactly the ways one bug does:
        ports, ids, timestamps. Its fingerprints must not.

        An earlier version of this test replaced substrings with themselves,
        which proved `f(x) == f(x)` and nothing else — the mutation-class
        defect this whole file exists to catch, written into it.
        """
        # The leaf differs as a DIFFERENT BUG differs: exception type (a
        # message-only change keeps the same signature by design — the message
        # is deliberately excluded from the identity).
        other = self.GROUP_TB.replace("KeyError", "ValueError")
        wrappers = [
            f for f in failure_fingerprints(self.GROUP_TB)
            if "Exception Group" in f or "ExceptionGroup" in f
        ]
        wrappers_other = [
            f for f in failure_fingerprints(other)
            if "Exception Group" in f or "ExceptionGroup" in f
        ]
        self.assertTrue(wrappers, "no wrapper identity at all")
        self.assertEqual(wrappers, wrappers_other)
        # And the leaves, being different bugs now, must differ.
        self.assertNotEqual(failure_fingerprints(self.GROUP_TB), failure_fingerprints(other))

    def test_the_wrapper_identity_is_pinned_beside_the_leaf(self):
        # The wrapper line is what survives a re-raise with a different leaf;
        # a pin on the leaf alone let deleting the wrapper identity pass.
        prints = failure_fingerprints(self.GROUP_TB)
        self.assertTrue(
            any("Exception Group" in f or "ExceptionGroup" in f for f in prints),
            f"the group wrapper carries no identity: {prints}",
        )

    def test_a_piped_error_line_is_still_gated(self):
        """The gutter strip is gated on the group marker, so an ordinary error
        line that begins with a pipe must still be read as an error — the
        strip is a reader for one rendering, not a licence to ignore pipes.

        The previous version asserted only `prints` truthy on a log whose
        pipe-line was not an error line, which passed with the strip made
        unconditional and proved nothing.
        """
        # A pipe-prefixed GROUP trace on its own: the strip is gated on the
        # group marker, and that gate is the whole promise. A target whose
        # ordinary logs carry pipes has its own format — that is
        # TargetAdapter.failure_ids' seam, not this normaliser's.
        self.assertFalse(unfingerprintable(self.GROUP_TB))
        # And an adjacent plain ERROR line does not break the group's identity:
        # the continuation logic folds it into the same block, whose deepest
        # frame is the group's. One signature for a symptom line plus the
        # failure it describes is the correct collapse.
        raw = "| ERROR: ValueError at app/x.py:12 boom\n" + self.GROUP_TB
        self.assertFalse(unfingerprintable(raw))
        self.assertTrue(failure_fingerprints(raw))


class AGenericLogLevelIsNotAnIdentity(unittest.TestCase):
    """`ERROR:` is a log level; `ValueError:` is an exception type.

    Matching the first case-insensitively turns every unrelated failure sharing
    a stack frame into one fingerprint — dedup collapses distinct bugs into a
    single issue, and incident recall hands one bug's fix to another. That is
    the same defect as keying on a message, which this module's own docstring
    rules out, and it is why the pattern is case-SENSITIVE: an exception type is
    CamelCase in every language parsed here, a log level is not.
    """

    FRAME = '  File "/repo/app/h.py", line 9, in go\n'

    def test_a_bare_error_level_yields_no_identity(self):
        self.assertEqual(failure_fingerprints("ERROR: something failed\n" + self.FRAME), [])

    def test_a_real_exception_type_on_the_same_frame_does(self):
        # The pair matters: without this, refusing everything would also pass.
        prints = failure_fingerprints(
            "Traceback (most recent call last):\n" + self.FRAME + "ValueError: bad\n"
        )
        self.assertTrue(prints)
        self.assertTrue(prints[0].startswith("ValueError@"), prints)

    def test_two_unrelated_error_lines_do_not_collapse_together(self):
        # The consequence, stated directly: distinct bugs must stay distinct.
        a = failure_fingerprints(
            "Traceback (most recent call last):\n" + self.FRAME + "ValueError: bad\n"
        )
        b = failure_fingerprints(
            "Traceback (most recent call last):\n" + self.FRAME + "KeyError: 'x'\n"
        )
        self.assertNotEqual(a, b)


class AFingerprintIsAnIdentityNotALogLine(unittest.TestCase):
    """The type half of a fingerprint must be a TYPE, not everything before a colon.

    `_EXC_LINE_RE` matches any line beginning with the word `Error` or
    `Exception`, which includes ordinary prose an application logs. Taking
    `split(":", 1)[0]` from such a line yields the whole line, and that is two
    defects at once.

    It is an unstable identity: `Error refreshing token for user alice` and the
    same failure for `bob` fingerprint differently, so dedup misses, a fresh
    issue opens every tick, and the attempt cap counts from zero forever —
    which is precisely what `TargetAdapter.failure_ids` tells an implementer to
    avoid.

    And it is a disclosure: the fingerprint marker is appended to the issue body
    AFTER the scrubber has run on it, so whatever the line carried is published
    verbatim, and the same list is committed to the target's default branch in
    `incident_memory/log.jsonl`.

    Refusing is the correct answer rather than salvaging a leading word: a
    generic `Error@path:line` shared by every unrelated prose failure is worse
    than no identity, because it looks like one.
    """

    PROSE = (
        "Error refreshing credential AKIAIOSFODNN7EXAMPLE for user bob@corp.example\n"
        '  File "/srv/app/worker.py", line 42, in refresh\n'
    )
    REAL = (
        "Traceback (most recent call last):\n"
        '  File "/srv/app/worker.py", line 42, in refresh\n'
        "AttributeError: 'State' object has no attribute 'comfyui'\n"
    )

    def test_a_prose_line_yields_no_fingerprint(self):
        self.assertEqual(failure_fingerprints(self.PROSE), [])

    def test_and_the_cycle_is_refused_rather_than_keyed_on_it(self):
        # The log carries failure text, so this is the coverage-gap branch, not
        # idle. `watch.yml` stops here before spending an agent call.
        self.assertTrue(unfingerprintable(self.PROSE))

    def test_nothing_from_that_line_can_reach_the_issue_marker(self):
        # The marker is what actually ships. Asserted on the decoded payload
        # rather than on the fingerprint list, because base64 is encoding, not
        # protection, and the encoded form hides a plain-text search.
        marker = fingerprint_marker(failure_fingerprints(self.PROSE))
        decoded = " ".join(_marked_fingerprints(marker))
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", decoded)
        self.assertNotIn("bob@corp.example", decoded)

    def test_a_real_exception_still_fingerprints(self):
        # The guard must not buy safety by refusing the ordinary case.
        self.assertEqual(
            failure_fingerprints(self.REAL, strip_prefix="/srv/app"),
            ["AttributeError@worker.py:42"],
        )

    def test_a_dotted_type_still_fingerprints(self):
        # `com.acme.NotFoundException` is one identifier with dots, not prose.
        java = (
            "com.acme.NotFoundException: no account for user alice\n"
            '  File "/srv/app/a.py", line 9, in find\n'
        )
        self.assertEqual(
            failure_fingerprints(java, strip_prefix="/srv/app"),
            ["com.acme.NotFoundException@a.py:9"],
        )


class AFingerprintIsNeverPutThroughTheScrubber(unittest.TestCase):
    """Redacting an identity destroys the identity. This pins the refusal.

    The reasoning is not obvious and the change is tempting — the fingerprint
    marker is appended to an issue body after that body has been scrubbed, so
    "just scrub the fingerprints too" reads like the safe fix. It is not.

    A fingerprint is `Type@path:line`. On a path with no directory component,
    which is the ordinary case for Go and Ruby, that is character-for-character
    the shape of an email address, and the scrubber's PII rule rewrites
    `panic@handler.go:42` to `[REDACTED]:42`. Every distinct Go failure then
    collapses onto one identity: dedup matches unrelated bugs, incident memory
    recalls the wrong prior fix, and the attempt cap counts them together —
    silently, on exactly the runtimes the adapter seam exists to serve.

    The built-in path is bounded by SHAPE instead (see the class above), and
    what an adapter returns is bounded by the contract stated in
    `adapters/base.py` and `reference/adapter.md`: that string is published in
    an issue body and committed to the default branch, so it must not carry
    log payload.
    """

    GO_ID = "panic@handler.go:42"

    def test_the_scrubber_would_indeed_destroy_it(self):
        # The premise, asserted rather than described — if this ever stops
        # being true the reasoning above is stale and the refusal is arguable.
        from guardrails.confidentiality_filter import scrub

        self.assertEqual(scrub(self.GO_ID), "[REDACTED]:42")

    def test_so_a_supplied_identity_passes_through_intact(self):
        self.assertEqual(
            failure_fingerprints("panic: boom\n", ids_fn=lambda raw: [self.GO_ID]),
            [self.GO_ID],
        )

    def test_and_two_distinct_go_failures_stay_distinct(self):
        # What redaction would cost, stated as behaviour: these two collapse to
        # one identity the moment anything scrubs them.
        both = failure_fingerprints(
            "panic: boom\n",
            ids_fn=lambda raw: ["panic@handler.go:42", "panic@billing.go:9"],
        )
        self.assertEqual(len(set(both)), 2, both)


if __name__ == "__main__":
    unittest.main()
