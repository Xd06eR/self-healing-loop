"""Resolution of the target's adapter, for every part of the loop that needs it.

This lives beside the seam rather than in `loop.py` because incident memory
needs it too, and a guardrail importing the loop entry point would be a cycle.
`log_compact` and `gate` do not import it, so they stay readable with no target
on the path.
"""
import importlib
import json
import os

# Imported rather than restated: the withheld set is defined once, beside the
# scrubber that redacts those same values, so the two cannot drift apart.
from guardrails.confidentiality_filter import SECRET_ENV_VARS


def hydrate_repo_vars() -> None:
    """Fold the repo's whole variable set, passed as `SHL_VARS`, into os.environ.

    An adapter may read any `SHL_*` name, but a repo variable reaches a step
    only if that step's `env:` block names it — and the workflow templates can
    only name variables the framework knows about, so a variable an adapter
    invents reads None while sitting correctly set in the repo settings.

    Explicit `env:` entries win, so a step can still override one value without
    the bulk context undoing it. `secrets.*` is a separate Actions context and
    is not part of `toJSON(vars)`, so no secret travels this path.
    """
    raw = os.environ.get("SHL_VARS")
    if not raw:
        # Absent is ordinary: a local run has no blob, and explicit env entries
        # are complete on their own.
        return
    try:
        supplied = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Degrading quietly here reinstates the silent "set correctly, absent at
        # runtime" failure this function exists to remove, one layer further
        # from the cause: read_log raises every tick with nothing naming
        # SHL_VARS.
        raise ValueError(f"SHL_VARS is set but is not valid JSON: {exc}") from exc
    if not isinstance(supplied, dict):
        raise ValueError(f"SHL_VARS must be a JSON object, got {type(supplied).__name__}")
    for key, value in supplied.items():
        # Only the framework's own namespace. The blob carries organization-level
        # variables too, inherited by every repo in the org, so an unfiltered
        # fold would let a NODE_OPTIONS, PYTHONPATH or GIT_SSH_COMMAND set
        # anywhere in the org change how this loop's subprocesses run.
        if not key.startswith("SHL_"):
            continue
        # Never re-materialise a name a step deliberately withheld. A provider
        # token stored as a variable rather than a secret would otherwise arrive
        # in the one step built to run without it.
        if key in SECRET_ENV_VARS:
            continue
        # setdefault, so an explicitly-set variable stays authoritative.
        os.environ.setdefault(key, str(value))


def load_adapter():
    """The instance exported as `adapter` by the module `SHL_ADAPTER` names."""
    # Every consumer reaches the target through here, so this is the one place
    # where hydrating covers all of them. Doing it in a caller would leave the
    # others reading a different environment.
    hydrate_repo_vars()
    # `or`, not a default argument: an unset GitHub repo variable expands to the
    # EMPTY STRING in the workflow's env block, not to nothing, so `.get(...,
    # default)` returns "" and import_module("") raises ValueError. An operator
    # who never sets SHL_ADAPTER should land on the default, not on a crash.
    module_path = os.environ.get("SHL_ADAPTER") or "adapters.target"
    return importlib.import_module(module_path).adapter


def optional_ids_fn():
    """The target's own failure identities, when its adapter can be imported.

    Every consumer of a fingerprint resolves it through here rather than
    accepting one as a parameter, so issue dedup, the attempt cap, incident
    recall and the recorded incident all key a failure the same way.

    Only the ADAPTER's own absence is optional. A bare `except ImportError`
    draws the line somewhere else: it also swallows an import raised inside the
    adapter module's body — a missing SDK, a typo, a circular import — and
    returns the value every consumer reads as "this stack has no custom
    identities". `record_cycle` then stores an empty fingerprint list, a record
    that matches nothing forever, which is the defect this seam exists to close.

    So the name that could not be found is compared against the module asked
    for. Where the adapter is genuinely unavailable the built-in Python and V8
    parsing still applies, and `unfingerprintable` refuses anything it cannot
    read rather than proceeding without a key.
    """
    module_path = os.environ.get("SHL_ADAPTER") or "adapters.target"
    try:
        return load_adapter().failure_ids
    except ModuleNotFoundError as exc:
        # `exc.name` is the module Python could not find. It matches the one
        # asked for, or a package on the way to it; anything else came from the
        # adapter's own imports and is its bug, not a missing adapter.
        if exc.name == module_path or module_path.startswith(f"{exc.name}."):
            return None
        raise
