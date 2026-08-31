"""Thin CLI so a GitHub Actions step can call the guardrails without Python glue.

Subcommands:

- ``gate --diff <path|-> [--frozen PATH --repro-rc N]``  exit 0 if the diff
  leaves the loop's own tree alone, weakens no test, touches no test-runner
  config and no frozen reproducing test, and — when ``--baseline``/``--current``
  are supplied — introduces no new failure. Exit 1 on a violation, 2 on input
  the gate cannot judge on (a pattern that does not compile, half a baseline
  pair, ``--frozen`` with no exit code, an assertion form it cannot recognise).
  Without a baseline pair, exit 0 says nothing about regressions.
- ``scrub --text <path|->``  print the scrubbed text to stdout.

Reads ``-`` as stdin. Designed for workflow steps like::

    PYTHONPATH=.shl python -B -m guardrails.cli gate --diff fix.diff \
        --frozen tests/test_repro.py --repro-rc 0
    PYTHONPATH=.shl python -B -m guardrails.cli scrub --text issue_body.txt

Always ``-m guardrails.cli`` with PYTHONPATH, never ``python .../guardrails/cli.py``:
run as a script, Python puts THIS file's directory on sys.path instead of the
package root, so the imports below raise ModuleNotFoundError and every cycle
dies at its first scrub.
"""
import argparse
import re
import sys
from pathlib import Path

from guardrails.confidentiality_filter import scrub as _scrub
from guardrails.stdio import read_text_arg as _read
from guardrails.gate import (
    _ASSERT_RE,
    _DEFAULT_TEST_CONFIG_GLOBS,
    _DEFAULT_TEST_GLOBS,
    _SKIP_RE,
    count_test_files,
    is_diff_config_touched,
    is_frozen_test_touched,
    helper_freeze_applies,
    is_loop_tree_touched,
    is_test_config_touched,
    is_test_helper_touched,
    is_test_content_unreadable,
    is_test_weakened,
    is_workflow_touched,
    new_failures,
)


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _alternate(built_in: re.Pattern, extra: str | None) -> re.Pattern:
    """``built_in`` widened with the target's own form, never replaced by it.

    Raises ``re.error`` on an unusable pattern, which is the right direction:
    an installer-supplied regex that does not compile must stop the gate rather
    than leave it quietly matching only the built-in half.
    """
    if not extra or not extra.strip():
        return built_in
    return re.compile(f"(?:{built_in.pattern})|(?:{extra.strip()})", built_in.flags)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="guardrails.cli", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gate", help="fail if the diff weakened or unfroze a test")
    g.add_argument("--diff", required=True, help="diff file path, or - for stdin")
    g.add_argument(
        "--frozen",
        help="path of the frozen reproducing test; gate fails if the diff touches it",
    )
    g.add_argument(
        "--baseline",
        help="file of test IDs failing BEFORE the fix, one per line (from "
        "adapter.failing_tests()). With --current, the gate blocks on tests that "
        "were passing and now fail, instead of demanding a fully green suite.",
    )
    g.add_argument(
        "--current",
        help="file of test IDs failing AFTER the fix, one per line",
    )
    g.add_argument(
        "--suite-rc",
        type=int,
        help="exit code of the full suite after the fix. Cross-checks the parsed "
        "failure list: a non-zero code with nothing parsed means the suite could "
        "not run, which must not be read as 'everything passed'.",
    )
    g.add_argument(
        "--repro-rc",
        type=int,
        help="exit code of running ONLY the frozen reproducing test after the fix. "
        "Positive proof that the bug is fixed; preferred over inferring it from "
        "the test's absence in the failure list.",
    )
    g.add_argument(
        "--test-globs",
        help="comma-separated extra globs naming this target's test files, ADDED "
        "to the built-in filename conventions. Needed for directory conventions "
        "(jest's '*__tests__/*'), which no filename rule can express: without it "
        "those files are invisible to the no-weakening check.",
    )
    g.add_argument(
        "--test-config-globs",
        help="comma-separated extra globs naming this target's test-runner config, "
        "ADDED to the built-ins. Editing one silences a test as effectively as "
        "deleting its assertions, and the built-ins name Python and JS files "
        "only — so '.rspec', 'Cargo.toml', 'pom.xml' and 'build.gradle' are "
        "invisible until named here.",
    )
    g.add_argument(
        "--assert-pattern",
        help="regex for what an assertion looks like in this target's tests, "
        "ALTERNATED with the built-in one. The built-in knows assert/expect/"
        "raises; a runtime that reports failure as 't.Errorf' has no assertion "
        "the check can see, so removing every one of them reads as no change.",
    )
    g.add_argument(
        "--skip-pattern",
        help="regex for what switching a test off looks like here, ALTERNATED "
        "with the built-in one. The built-in knows skip/xfail; 'xit(', "
        "'pending', '#[ignore]' and '@Disabled' are invisible until named.",
    )

    s = sub.add_parser("scrub", help="print scrubbed text")
    s.add_argument("--text", required=True, help="text file path, or - for stdin")

    args = parser.parse_args(argv)

    if args.cmd == "gate":
        # Half a baseline pair silently disables the regression check and the
        # --suite-rc cross-check, so refuse it rather than passing a hollow
        # gate. The fix proof is --repro-rc and is guarded separately below.
        if bool(args.baseline) != bool(args.current):
            sys.stderr.write("--baseline and --current must be supplied together\n")
            return 2

        # A frozen test is only evidence once it has been RUN. Without the exit
        # code the gate can say the file was not edited and nothing more, so
        # letting the run continue would attest a fix proof that never happened.
        if args.frozen and args.repro_rc is None:
            sys.stderr.write(
                "--frozen needs --repro-rc: the frozen reproducing test proves the "
                "fix only by passing, and its absence from a failure list is also "
                "what a test that errored at collection looks like\n"
            )
            return 2

        # Extend, never replace: an operator adding their directory convention
        # must not silently switch off the built-in `test_*` / `*.test.*` rules.
        test_globs = tuple(_DEFAULT_TEST_GLOBS) + _split(args.test_globs)
        config_globs = tuple(_DEFAULT_TEST_CONFIG_GLOBS) + _split(args.test_config_globs)
        try:
            assert_re = _alternate(_ASSERT_RE, args.assert_pattern)
            skip_re = _alternate(_SKIP_RE, args.skip_pattern)
        except re.error as exc:
            sys.stderr.write(f"invalid pattern: {exc}\n")
            return 2

        # Prove the assertion pattern describes THIS target before trusting it
        # to police anything: a pattern that recognises nothing lets every check
        # below report a clean diff for a fix that emptied the suite.
        #
        # The frozen reproducing test is the sample, because it was run red and
        # then green. That does NOT make it contain an assertion — a test is
        # equally red when the code under test simply throws — so the premise is
        # held up by `templates/diagnose.md`, which requires an explicit
        # assertion in the repro, and the message below names both causes rather
        # than sending the operator to fix a pattern that was never wrong.
        # `--frozen` carries two independent jobs: the path is matched against
        # the diff (no file needed), and the file is read to prove the assertion
        # pattern describes this target. Only the second needs it on disk, so a
        # missing file degrades that proof rather than refusing the run — but it
        # says so here and again in the pass line, because a proof that quietly
        # did not run is the failure this whole check exists to prevent.
        pattern_proved = False
        if args.frozen:
            frozen_file = Path(args.frozen)
            if not frozen_file.is_file():
                sys.stderr.write(
                    f"WARNING: frozen test not readable at {args.frozen} (cwd {Path.cwd()}); "
                    "cannot prove the assertion pattern describes this target\n"
                )
            else:
                body = frozen_file.read_text(encoding="utf-8", errors="replace")
                if not assert_re.search(body):
                    sys.stderr.write(
                        f"no assertion recognised in the frozen test {args.frozen}: "
                        f"the no-weakening check cannot police this target's tests. "
                        f"Either set --assert-pattern to this runtime's assertion "
                        f"form, or the reproducing test asserts nothing and is red "
                        f"only because the code under test throws — read the file "
                        f"before changing the pattern.\n"
                    )
                    return 2
                pattern_proved = True

        try:
            diff = _read(args.diff)
        except OSError as exc:
            # A gate that dies on a missing diff file exits 1 with a traceback
            # and no BLOCKED line, which reads in the evidence bundle exactly
            # like a refusal that forgot to state its grounds.
            sys.stderr.write(f"cannot read diff {args.diff}: {exc}\n")
            return 2
        # One list, used BOTH to refuse and to attest. Each check returns the
        # violation it found rather than a bare flag, so a block names its
        # cause; and the pass line below is built from this same list, so it
        # cannot claim a check that is not in it. A hardcoded attestation states
        # what the code was written to do rather than what it did, which turns a
        # deleted check into a signed approval naming the guarantee it dropped.
        checks = [
            # Every entry is evaluated when this list is built, so position
            # decides which reason is REPORTED, not whether a check runs. The
            # first four police the agent's REACH and the rest its OUTPUT, so
            # a blocked cycle names the reach violation. The workflow's
            # `git`-only step is what covers the case where this module is
            # itself the thing that was tampered with.
            ("loop tree untouched", is_loop_tree_touched(diff)),
            ("workflow untouched", is_workflow_touched(diff)),
            # Must precede every content-based check below: `.gitattributes`
            # decides whether those checks get any content to read at all.
            ("diff rendering untouched", is_diff_config_touched(diff)),
            # The one that does not depend on the agent having touched a tracked
            # file: a NUL byte or an attributes file outside the tree blinds the
            # content checks while leaving no diff at all.
            ("test content readable", is_test_content_unreadable(diff, test_globs=test_globs)),
            ("no test weakened", is_test_weakened(
                diff, test_globs=test_globs, assert_re=assert_re, skip_re=skip_re
            )),
            # Editing test-runner config silences tests whether or not a frozen
            # test exists, so this is not conditional on one.
            ("no test config touched", is_test_config_touched(diff, config_globs=config_globs)),
        ]
        helper_freeze = args.frozen and helper_freeze_applies(args.frozen, test_globs)
        if args.frozen:
            checks.append(
                ("frozen test untouched", is_frozen_test_touched(diff, args.frozen))
            )
        if helper_freeze:
            # Conditional because a layout that puts tests beside source would
            # refuse every legitimate fix. The pass line below says which way
            # it went.
            checks.append(
                ("frozen test's helpers untouched", is_test_helper_touched(
                    diff, args.frozen, test_globs=test_globs
                ))
            )
        for label, reason in checks:
            if reason:
                sys.stderr.write(f"BLOCKED [{label}]: {reason}\n")
                return 1

        after = (
            {l.strip() for l in _read(args.current).splitlines() if l.strip()}
            if args.current
            else set()
        )
        if args.baseline and args.current:
            before = {l.strip() for l in _read(args.baseline).splitlines() if l.strip()}
            regressions = new_failures(before, after)
            if regressions:
                for test_id in sorted(regressions):
                    sys.stderr.write(f"regression: {test_id}\n")
                return 1

        # An empty failure list means either "everything passed" or "the suite
        # never ran and nothing could be parsed". Only the exit code separates
        # them, and reading the second as the first would merge a commit that
        # breaks the entire suite. Outside the baseline block, because an absent
        # `--current` is the emptiest that list ever gets — inside it, a hand
        # invocation got exit 0 whatever the suite had done.
        if args.suite_rc is not None and args.suite_rc != 0 and not after:
            sys.stderr.write(
                f"suite exited {args.suite_rc} but no failing tests were parsed; "
                "the suite likely could not run\n"
            )
            return 1

        # The fix is PROVEN only by the frozen reproducing test going green, and
        # only its own exit code proves that: absence from a failure list is
        # also what a test that errored at collection, or was deselected, looks
        # like.
        if args.frozen and args.repro_rc != 0:
            sys.stderr.write(
                f"frozen reproducing test did not pass (exit {args.repro_rc}): {args.frozen}\n"
            )
            return 1

        # State what passed, because an empty gate.txt is indistinguishable from
        # a gate that never ran. Built from `checks` above, so it can only name
        # checks that actually executed.
        attested = [label for label, _ in checks]
        if args.frozen:
            attested.append(f"frozen test passing ({args.frozen})")
        if args.baseline and args.current:
            attested.append("no test that was passing now fails")
        # Name how much of the suite the weakening check could actually see. A
        # zero here is the one signal that says the check was blind rather than
        # satisfied: the globs describe Python and JS, so a diff touching only
        # `spec/parser_spec.rb` matches nothing and passes silently.
        attested.append(f"{count_test_files(diff, test_globs)} test file(s) matched the test globs")
        if args.frozen and not helper_freeze:
            # Named, not omitted. A check that quietly did not apply reads
            # exactly like one that ran and passed.
            attested.append(
                "helpers beside the frozen test NOT frozen — its directory is "
                "not a dedicated test tree, so a fix may edit what it imports"
            )
        if not pattern_proved:
            # Said on EVERY unproved cycle, not only when a frozen test was
            # supplied and unreadable. The proof hangs off `--frozen`, and the
            # workflow passes that only when Diagnose could reproduce — which
            # the prompts describe as the minority case. So on most cycles the
            # proof never runs, and silence would let a pass read as though the
            # pattern had been checked against something.
            attested.append(
                "assertion pattern NOT proved against the frozen test"
                if args.frozen
                else "assertion pattern NOT proved — no frozen test this cycle, so "
                "nothing here confirms the no-weakening check can read this "
                "target's tests"
            )
        sys.stdout.write("PASSED: " + "; ".join(attested) + "\n")
        return 0
    # The only other subcommand; argparse rejects anything else before here.
    sys.stdout.write(_scrub(_read(args.text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())