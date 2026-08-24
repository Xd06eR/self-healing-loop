import json
import tempfile
import unittest
from pathlib import Path

from guardrails.confidentiality_filter import scrub
from guardrails.incident_memory import (
    IncidentRecord,
    record_cycle,
    record_incident,
    search_similar,
)


class TestIncidentMemory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "log.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    # Matching is on FINGERPRINTS, not on the title. Querying with a short
    # hand-written string passes here while the real path — a raw log line
    # matched against a model-written title — can never hit anything.
    FP = "ZeroDivisionError@app/stats.py:12"

    def test_search_returns_empty_when_no_log_yet(self):
        self.assertEqual(search_similar([self.FP], log_path=self.log_path), [])

    def test_record_then_search_finds_match(self):
        entry = IncidentRecord(
            issue_id="1",
            signature="ZeroDivisionError in average()",
            root_cause="empty list not guarded",
            fix_commit="abc123",
            outcome="merged",
            fingerprints=[self.FP],
        )
        record_incident(entry, log_path=self.log_path)
        results = search_similar([self.FP], log_path=self.log_path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].fix_commit, "abc123")

    def test_search_excludes_unrelated_records(self):
        entry = IncidentRecord(
            issue_id="1",
            signature="ZeroDivisionError",
            root_cause="x",
            fix_commit="abc123",
            outcome="merged",
            fingerprints=[self.FP],
        )
        record_incident(entry, log_path=self.log_path)
        self.assertEqual(search_similar(["KeyError@app/other.py:3"], log_path=self.log_path), [])

    def test_the_title_is_not_the_key(self):
        # Two records with the SAME human-written title and different
        # fingerprints. `search_similar` takes no title at all, so the property
        # can only be shown this way: identical prose, no match.
        for issue, fingerprint in (("1", "KeyError@a.py:1"), ("2", "TypeError@b.py:9")):
            record_incident(
                IncidentRecord(issue, "500 error", "x", "c", "merged", [fingerprint]),
                log_path=self.log_path,
            )
        found = search_similar(["KeyError@a.py:1"], log_path=self.log_path)
        self.assertEqual([r.issue_id for r in found], ["1"])

    def test_multiple_records_append_not_overwrite(self):
        for i in range(3):
            record_incident(
                IncidentRecord(
                    issue_id=str(i),
                    signature="ZeroDivisionError",
                    root_cause="x",
                    fix_commit=f"commit{i}",
                    outcome="merged",
                    fingerprints=[self.FP],
                ),
                log_path=self.log_path,
            )
        self.assertEqual(len(search_similar([self.FP], log_path=self.log_path)), 3)


class RetentionKeepsTheLogBounded(unittest.TestCase):
    """The log is git-committed and appended once per healed cycle, forever.

    Unbounded, a 3-hourly cron writes roughly 5 MB a year into the target's
    repo. It never reaches the prompt — matching is exact-fingerprint and
    capped — so this is weight, not a correctness problem, right up until a
    target refuses the push.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "log.jsonl"
        self.addCleanup(self._tmp.cleanup)

    def _write(self, n, outcome="merged", start=0):
        for i in range(start, start + n):
            record_incident(
                IncidentRecord(
                    issue_id=str(i),
                    signature="t",
                    root_cause="c",
                    fix_commit="abc",
                    outcome=outcome,
                    fingerprints=[f"E@f.py:{i}"],
                ),
                log_path=self.log_path,
                max_records=10,
            )

    def _ids(self):
        return [
            json.loads(line)["issue_id"]
            for line in self.log_path.read_text().splitlines()
            if line.strip()
        ]

    def test_below_the_cap_nothing_is_dropped(self):
        self._write(10)
        self.assertEqual(len(self._ids()), 10)

    def test_above_the_cap_the_log_stops_growing(self):
        self._write(25)
        self.assertLessEqual(len(self._ids()), 10)

    def test_a_reverted_incident_survives_however_old(self):
        """The one record type that must never be pruned.

        A reverted outcome says the obvious fix was already tried and made
        things worse. `format_incidents` ranks those first precisely so the
        prompt cap cannot drop the warning; retention silently deleting it
        would defeat that from underneath.
        """
        self._write(1, outcome="reverted")
        self._write(30, start=1)
        self.assertIn("0", self._ids(), "the oldest reverted record was pruned")

    def test_pruning_preserves_chronological_order(self):
        self._write(25)
        kept = [int(i) for i in self._ids()]
        self.assertEqual(kept, sorted(kept), "recency ranking reads file order")


class ACorruptRowDoesNotTakeMemoryDownWithIt(unittest.TestCase):
    """This store is append-only and committed by a workflow, so a truncated
    write or a hand-edit is a realistic state. Crashing on one row loses recall
    for every later cycle, and crashing in `_prune` fails the Record step after
    the fix has already merged and deployed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "log.jsonl"
        self.addCleanup(self._tmp.cleanup)

    GOOD = {
        "issue_id": "1", "signature": "t", "root_cause": "c",
        "fix_commit": "a", "outcome": "merged", "fingerprints": ["E@f.py:1"],
    }

    def _write(self, *lines):
        self.log_path.write_text("".join(line + "\n" for line in lines))

    def test_search_skips_unreadable_rows_and_still_matches(self):
        for label, bad in (("malformed", "{oops"), ("not-an-object", "null"),
                           ("unknown-key", '{"nope": 1}')):
            with self.subTest(row=label):
                self._write(bad, json.dumps(self.GOOD))
                found = search_similar(["E@f.py:1"], log_path=self.log_path)
                self.assertEqual([r.issue_id for r in found], ["1"])

    def test_prune_survives_a_row_that_is_valid_json_but_not_a_record(self):
        rows = ["null"] + [json.dumps(dict(self.GOOD, issue_id=str(i))) for i in range(6)]
        self._write(*rows)
        record_incident(IncidentRecord(**dict(self.GOOD, issue_id="new")),
                        log_path=self.log_path, max_records=3)
        kept = [line for line in self.log_path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(kept), 3)

    def test_a_cap_of_zero_empties_the_store_rather_than_keeping_it_whole(self):
        # `reverted[-0:]` is the WHOLE list, so the exempt group used to absorb
        # every record and the cap kept exactly what it was told to drop.
        self._write(json.dumps(dict(self.GOOD, outcome="reverted")))
        record_incident(IncidentRecord(**dict(self.GOOD, issue_id="new")),
                        log_path=self.log_path, max_records=0)
        kept = [line for line in self.log_path.read_text().splitlines() if line.strip()]
        self.assertEqual(kept, [])

    def test_one_string_is_refused_rather_than_matching_nothing(self):
        # The L8 defect's exact shape: a raw log line handed in as an identity.
        # Both silent readings of it — one identity, or a bag of characters —
        # return no matches, which reads as "this failure is new".
        self._write(json.dumps(self.GOOD))
        with self.assertRaises(TypeError):
            search_similar("E@f.py:1", log_path=self.log_path)


class WhatIsCommittedToTheRepoIsScrubbed(unittest.TestCase):
    """`log.jsonl` is the one GitHub write path that skipped the scrubber.

    `heal.yml` scrubs the issue title, the issue body, the PR summary and the
    review reason before any of them reaches GitHub — then hands `record_cycle`
    the RAW `issue_body` and commits the result. The record is the worst place
    to skip it: an issue can be edited, but this is committed to the default
    branch, append-only, exempt from pruning when the outcome is `reverted`,
    and rendered verbatim into every future matching prompt.

    Diagnose writes `issue_body` while quoting an untrusted log, so a provider
    token appearing in one log line is redacted on its way to the issue and
    committed verbatim here.

    Scrubbed inside `record_cycle` rather than at the workflow call site,
    because the workflow is one caller of several and a rule every caller must
    remember is the drift this project keeps paying for (L9).
    """

    SECRET = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKK"

    def _record(self, root_cause, signature="TypeError in parse"):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log.jsonl"
            record_cycle(
                issue_id="7",
                signature=signature,
                root_cause=root_cause,
                fix_commit="abc123",
                outcome="merged",
                signal='TypeError: bad\n  File "/repo/app.py", line 3, in parse\n',
                log_path=log,
            )
            return log.read_text(encoding="utf-8")

    def test_the_scrubber_recognises_this_shape_at_all(self):
        # Guards the premise: a token shape the scrubber ignores would make the
        # test below pass without the fix being present.
        self.assertNotIn(self.SECRET, scrub(self.SECRET))

    def test_a_secret_in_the_root_cause_never_reaches_the_log(self):
        written = self._record(f"The handler logged its key: {self.SECRET}")
        self.assertNotIn(self.SECRET, written)

    def test_a_secret_in_the_title_never_reaches_the_log_either(self):
        # `signature` is `issue_title` — the same raw agent output, written
        # while quoting the same untrusted log, stored in the same committed
        # record. Covering only `root_cause` left this half unpinned, and the
        # mutation proving it went NOT CAUGHT until this test existed.
        written = self._record("parse() drops attachments", signature=self.SECRET)
        self.assertNotIn(self.SECRET, written)

    def test_the_surrounding_prose_survives(self):
        # Scrubbing that ate the record would trade a leak for a dead
        # mechanism: the root cause is what a later cycle reads to learn the
        # obvious fix was already tried.
        written = self._record("parse() assumes 'parts' is present")
        self.assertIn("parse() assumes", written)
