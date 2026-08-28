import json
import re
import unittest
from pathlib import Path

README_TEMPLATE = Path(__file__).resolve().parents[1] / "artifacts" / "readme.md"

from gh_state import (
    _marked_fingerprints,
    count_attempts,
    find_open_issue,
    fingerprint_marker,
)


def _gh_factory(responses):
    """Build a fake gh runner: matches on a tuple of args, returns canned text."""
    calls = []

    def runner(argv):
        calls.append(list(argv))
        key = tuple(argv)
        for prefix, out in responses:
            if all(a in argv for a in prefix):
                return out
        raise AssertionError(f"unexpected gh argv: {argv}")

    return calls, runner


class TestCountAttempts(unittest.TestCase):
    def test_max_attempt_marker_wins(self):
        comments = [
            {"body": "🤖 fix attempt 1 failed: suite red"},
            {"body": "🤖 fix attempt 2 failed: gate blocked weakened test"},
            {"body": "human comment, no marker"},
        ]
        out = json.dumps({"comments": comments})
        _, runner = _gh_factory([(["issue", "view"], out)])
        self.assertEqual(count_attempts(42, gh_runner=runner), 2)

    def test_no_markers_is_zero(self):
        out = json.dumps({"comments": [{"body": "nothing here"}]})
        _, runner = _gh_factory([(["issue", "view"], out)])
        self.assertEqual(count_attempts(1, gh_runner=runner), 0)

    def test_empty_comments_is_zero(self):
        _, runner = _gh_factory([(["issue", "view"], json.dumps({"comments": []}))])
        self.assertEqual(count_attempts(7, gh_runner=runner), 0)


class TestFindOpenIssue(unittest.TestCase):
    """Dedup must key on the fingerprint, never on the issue title.

    This is the defect incident memory keys around, in a second place: the title
    is prose a model writes fresh every cycle, so `gh issue list --search
    "$title in:title"` matches inconsistently. A miss files a SECOND issue for a
    failure already being worked, which resets `count_attempts` to zero — so the
    attempt cap can never fire and the loop retries an unfixable bug forever.
    """

    def _issues(self, *pairs):
        return json.dumps(
            [
                {"number": n, "body": f"Some prose.\n{fingerprint_marker(fps)}\n"}
                for n, fps in pairs
            ]
        )

    def test_same_failure_reuses_the_issue_despite_a_different_title(self):
        out = self._issues((7, ["TypeError@lib/parser.ts:47"]))
        _, runner = _gh_factory([(["issue", "list"], out)])
        found = find_open_issue(["TypeError@lib/parser.ts:47"], gh_runner=runner)
        self.assertEqual(found, 7)

    def test_unrelated_failure_files_a_new_issue(self):
        out = self._issues((7, ["TypeError@lib/parser.ts:47"]))
        _, runner = _gh_factory([(["issue", "list"], out)])
        self.assertIsNone(find_open_issue(["KeyError@lib/other.ts:9"], gh_runner=runner))

    def test_partial_overlap_counts_as_the_same_incident(self):
        # A signal routinely carries several failures; the one being worked is
        # still present next tick even when its neighbours change.
        out = self._issues((7, ["A@a.ts:1", "B@b.ts:2"]))
        _, runner = _gh_factory([(["issue", "list"], out)])
        self.assertEqual(find_open_issue(["B@b.ts:2", "C@c.ts:3"], gh_runner=runner), 7)

    def test_issue_without_a_marker_is_ignored(self):
        # Human-filed issues live in the same list and must never be hijacked.
        out = json.dumps([{"number": 3, "body": "the site looks wrong on mobile"}])
        _, runner = _gh_factory([(["issue", "list"], out)])
        self.assertIsNone(find_open_issue(["A@a.ts:1"], gh_runner=runner))

    def test_no_fingerprints_matches_nothing(self):
        _, runner = _gh_factory([(["issue", "list"], self._issues((7, ["A@a.ts:1"])))])
        self.assertIsNone(find_open_issue([], gh_runner=runner))

    def test_marker_round_trips_and_is_order_independent(self):
        self.assertEqual(fingerprint_marker(["b", "a"]), fingerprint_marker(["a", "b"]))


class TheMarkerSurvivesCommasAndHostilePaths(unittest.TestCase):
    """The marker is the dedup key, and it is built from LOG-derived strings.

    Two defects shared one root: the marker joined fingerprints with a comma,
    and a fingerprint is `Type@path:line` where the path comes from the log —
    so a path carrying a comma split into two wrong fingerprints and dedup
    missed forever, and a path carrying `-->` closed the HTML comment early and
    injected attacker-chosen text into the issue body, the one GitHub surface
    the marker reaches without passing the scrubber.

    Both are closed by encoding rather than by escaping: the payload is JSON in
    urlsafe base64, whose alphabet contains neither a comma nor a `-` followed
    by `>`. Round-trip through the same function the workflow greps with.
    """

    def test_a_comma_in_a_path_does_not_split_the_fingerprint(self):
        from gh_state import _marked_fingerprints

        fps = ["KeyError@data, rough/x.py:12", "ValueError@app/y.py:3"]
        decoded = _marked_fingerprints("prose\n" + fingerprint_marker(fps) + "\n")
        self.assertEqual(decoded, set(fps))

    def test_a_path_cannot_close_the_comment_and_inject(self):
        from gh_state import _marked_fingerprints

        hostile = 'KeyError@x --> <script>alert(1)</script>:1'
        marker = fingerprint_marker([hostile])
        # The comment closes exactly once, where the function put it: a second
        # `-->` anywhere earlier would end the comment and render the rest as
        # body text, which is the injection.
        self.assertEqual(marker.count("-->"), 1)
        self.assertTrue(marker.endswith("-->"))
        self.assertEqual(_marked_fingerprints(marker), {hostile})

    def test_an_order_change_does_not_change_the_marker(self):
        self.assertEqual(
            fingerprint_marker(["b@x:1", "a@y:2"]),
            fingerprint_marker(["a@y:2", "b@x:1"]),
        )

    def test_a_legacy_comma_joined_marker_still_parses(self):
        # Issues filed before the encoding carry the old form; a target that
        # updates mid-flight must still recognise them or it re-files a duplicate
        # for every failure it has already seen.
        from gh_state import _marked_fingerprints

        self.assertEqual(
            _marked_fingerprints("<!-- shl-fingerprint: A@x:1,B@y:2 -->"),
            {"A@x:1", "B@y:2"},
        )

    def test_the_empty_marker_keeps_its_bare_shape(self):
        # The workflow greps this shape when refusing an unfingerprintable log;
        # changing the empty form silently breaks that path.
        self.assertEqual(fingerprint_marker([]), "<!-- shl-fingerprint:  -->")


class TheDedupWindowAndCapDefaultsArePinned(unittest.TestCase):
    """Two bounds nothing behavioural pinned: the listing window and the cap.

    `--limit 100` was a string in the source with a comment; changing it to 30
    or 1000 passed the suite either way, and only a doc-sync test noticed a cap
    change. Both are behaviour an operator depends on, so both get a test that
    drives the real function with a stub `gh`.
    """

    def _runner(self, bodies):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            return json.dumps(bodies)

        return calls, runner

    def test_the_listing_window_is_one_hundred(self):
        calls, runner = self._runner([])
        find_open_issue({"A@x:1"}, gh_runner=runner)
        joined = [" ".join(c) for c in calls]
        self.assertTrue(
            any("--limit 100" in j for j in joined),
            f"the dedup window changed: {joined}",
        )

class TheMarkerSurvivesCommasAndHostilePaths(unittest.TestCase):
    """The marker is the dedup key, and it is built from LOG-derived strings.

    Two defects shared one root: the marker joined fingerprints with a comma,
    and a fingerprint is `Type@path:line` where the path comes from the log —
    so a path carrying a comma split into two wrong fingerprints and dedup
    missed forever, and a path carrying `-->` closed the HTML comment early and
    injected attacker-chosen text into the issue body, the one GitHub surface
    the marker reaches without passing the scrubber.

    Both are closed by encoding rather than by escaping: the payload is JSON in
    urlsafe base64, whose alphabet contains neither a comma nor a `-` followed
    by `>`. Round-trip through the same function the workflow greps with.
    """

    def test_a_comma_in_a_path_does_not_split_the_fingerprint(self):
        from gh_state import _marked_fingerprints

        fps = ["KeyError@data, rough/x.py:12", "ValueError@app/y.py:3"]
        decoded = _marked_fingerprints("prose\n" + fingerprint_marker(fps) + "\n")
        self.assertEqual(decoded, set(fps))

    def test_a_path_cannot_close_the_comment_and_inject(self):
        from gh_state import _marked_fingerprints

        hostile = 'KeyError@x --> <script>alert(1)</script>:1'
        marker = fingerprint_marker([hostile])
        # The comment closes exactly once, where the function put it: a second
        # `-->` anywhere earlier would end the comment and render the rest as
        # body text, which is the injection.
        self.assertEqual(marker.count("-->"), 1)
        self.assertTrue(marker.endswith("-->"))
        self.assertEqual(_marked_fingerprints(marker), {hostile})

    def test_an_order_change_does_not_change_the_marker(self):
        self.assertEqual(
            fingerprint_marker(["b@x:1", "a@y:2"]),
            fingerprint_marker(["a@y:2", "b@x:1"]),
        )

    def test_a_legacy_comma_joined_marker_still_parses(self):
        # Issues filed before the encoding carry the old form; a target that
        # updates mid-flight must still recognise them or it re-files a duplicate
        # for every failure it has already seen.
        from gh_state import _marked_fingerprints

        self.assertEqual(
            _marked_fingerprints("<!-- shl-fingerprint: A@x:1,B@y:2 -->"),
            {"A@x:1", "B@y:2"},
        )

    def test_the_empty_marker_keeps_its_bare_shape(self):
        # The workflow greps this shape when refusing an unfingerprintable log;
        # changing the empty form silently breaks that path.
        self.assertEqual(fingerprint_marker([]), "<!-- shl-fingerprint:  -->")


class AgentWrittenProseCannotHijackTheDedupKey(unittest.TestCase):
    """The issue body is written by Diagnose from untrusted logs; the workflow
    appends its marker AFTER it. So the workflow's marker is always the LAST,
    and a first-match read let a decoy planted in the prose win — the wrong
    fingerprints match nothing, a duplicate issue is filed, and the attempt cap
    on the real one counts from zero forever.
    """

    def test_the_last_marker_wins_over_a_decoy_in_the_body(self):
        real = fingerprint_marker(["KeyError@app/x.py:55"])
        body = (
            "Analysis of the failure.\n"
            "<!-- shl-fingerprint: PALE-DO-NOT-USE -->\n"
            "More prose quoted from the log.\n" + real
        )
        self.assertEqual(_marked_fingerprints(body), {"KeyError@app/x.py:55"})

    def test_a_body_with_only_a_decoy_still_reads_it(self):
        # Not a silent empty: an issue carrying exactly one marker is the
        # ordinary case, and this must not start ignoring single markers.
        self.assertEqual(
            _marked_fingerprints("<!-- shl-fingerprint: A@x:1,B@y:2 -->"),
            {"A@x:1", "B@y:2"},
        )


class TheOperatorsAttemptMarkerActuallyCounts(unittest.TestCase):
    """The comment `artifacts/readme.md` tells an operator to post must work.

    Two ways it silently does not, and both leave the operator believing they
    armed the cap while the loop retries. `count_attempts` needs a DIGIT, so a
    literal `N` matches the regex not at all and counts zero. And it takes the
    MAX rather than a count, so a number at or below one the loop has already
    posted for itself changes nothing.

    Pinned against the real parser rather than restated, because the two drift
    apart silently: nothing about a wrong instruction fails until an operator
    follows it during an incident.
    """

    CAP = 2  # loop.under_attempt_cap's default; the loop escalates at this count.

    def _instruction(self):
        """The marker the readme tells the operator to post, taken from its code span."""
        text = README_TEMPLATE.read_text(encoding="utf-8")
        markers = re.findall(r"`(fix attempt [^`]*)`", text)
        self.assertEqual(len(markers), 1, f"expected one instructed marker, got {markers}")
        return markers[0]

    def test_the_instructed_marker_parses_as_an_attempt(self):
        body = f"{self._instruction()}"
        self.assertEqual(count_attempts(1, gh_runner=lambda a: json.dumps(
            {"comments": [{"body": body}]})), self.CAP)

    def test_it_still_reaches_the_cap_beside_the_loops_own_comment(self):
        # The loop posts `🤖 fix attempt 1 failed: ...` for itself. An operator
        # marker at or below that leaves the max where it was, so the cap never
        # fires and the next tick tries again.
        comments = [
            {"body": "🤖 fix attempt 1 failed: gate or suite"},
            {"body": self._instruction()},
        ]
        reached = count_attempts(1, gh_runner=lambda a: json.dumps({"comments": comments}))
        self.assertGreaterEqual(reached, self.CAP, "the operator's marker did not arm the cap")


if __name__ == "__main__":
    unittest.main()