"""Adapter contract every automation target must implement.

Deliberately small: `read_log` is the only required method. Deploying and
rolling back are NOT the adapter's job — the workflow runs the operator's
`SHL_DEPLOY_CMD` (which may carry deploy credentials an agent-adjacent module
should never hold) and reverts with `git revert`. They are absent rather than
abstract-with-no-caller: nothing in the framework would ever call them, so
declaring them here only forces every installer to write two dead methods to
satisfy the ABC, and any guidance about mocking them is guidance about code
that cannot run.
"""
from abc import ABC, abstractmethod
from typing import Optional


class TargetAdapter(ABC):
    @abstractmethod
    def read_log(self) -> str:
        """Return the target's current failure/status signal as text."""

    def failing_tests(self) -> Optional[set[str]]:
        """Test IDs currently failing, or None if this target cannot list them.

        Optional, and the only per-target knowledge the gate needs beyond the
        suite's exit code: every runner reports failures in its own shape
        (``FAILED path::name``, ``✕ name``, ``--- FAIL: Name``,
        ``rspec ./spec/x_spec.rb:12``, and so on), so the framework cannot parse
        them generically without carrying a list of runners that is stale the
        moment someone uses a new one. The adapter already knows this project's
        tooling, so it supplies them. Read what the suite actually prints.

        With this implemented, the gate blocks on tests that were PASSING and
        now fail, instead of demanding a fully green suite; a repo carrying any
        pre-existing failure would otherwise veto every fix forever. Returning
        None keeps the strict all-green rule, so an adapter that cannot list
        failing tests still works.
        """
        return None

    def failure_ids(self, raw_log: str) -> Optional[list[str]]:
        """Stable identity of each distinct failure in ``raw_log``, in order.

        **The text is the raw log `read_log` returned, never the compacted
        signal the agent is prompted with.** That guarantee is the method's
        whole basis: compaction keeps error lines and their *indented*
        continuation, which drops a Go panic's trace entirely — it sits behind
        a blank line and is not indented. Deriving identity from compacted text
        would hand this method a message with no frames, on precisely the
        runtimes it exists to serve, so the workflow carries the raw log to
        every consumer of an identity and compacts only for the prompt.

        Optional, and needed by any runtime the framework cannot read: it
        understands Python tracebacks and V8 stacks, and nothing else. A Go
        panic separates its trace from its message with a blank line and
        indents none of it; a Ruby backtrace uses its own frame syntax. Neither
        survives the built-in extraction, so neither produces an identity — and
        an identity is what issue dedup, incident recall and the attempt cap all
        key on. Without one the loop refuses those failures rather than healing
        the same one on every tick, forever.

        Return one string per distinct failure, shaped ``Type@path:line``.
        Three properties make it usable, and all three are this method's job:

        - **Stable.** Two occurrences of one bug must produce byte-identical
          strings, so strip anything that varies between runs — addresses,
          object ids, timestamps, ports.
        - **Specific.** Key on the frame that RAISED, not the outermost caller
          or the deepest library frame. A shared entry point is identical for
          every unrelated failure in the process, which is worse than no
          identity because it looks like one.
        - **Owned.** Prefer a frame in this project's own code over one in a
          dependency, for the same reason.

        **What you return is published.** It goes into the dedup marker on a
        GitHub issue, and into the incident log this loop commits to the
        default branch. It is deliberately NOT passed through the
        confidentiality scrubber: an identity of the shape ``panic@handler.go:42``
        is character-for-character an email address, so redaction would rewrite
        it and collapse every distinct failure onto one key. So the bound is
        yours — key on the type and the frame, never on the message payload.
        That is the same rule as **Stable** above, arrived at from the other
        side: a value that varies between two occurrences of one bug is also a
        value that carries whatever the log put there.

        Return None to use the built-in parsing, which is correct on a Python or
        JS target. Return ``[]`` only when the log genuinely holds no failure.
        """
        return None

    def health_check(self) -> Optional[bool]:
        """Is the DEPLOYED service healthy? None when the target cannot say.

        Optional, and worth implementing whenever the target actually deploys
        somewhere. The post-deploy suite runs on the runner against the merged
        source, which says nothing about whether the thing that got deployed is
        answering — a green suite next to a service returning 502 is exactly the
        state that should trigger a rollback and would otherwise pass verify.

        Keep it cheap and side-effect free: one request to a health endpoint.
        Return None (the default) rather than guessing when there is no
        deployment to probe; the workflow then relies on the suite alone.
        """
        return None
