"""How the loop talks to a coding agent — config-as-data, universal across harnesses.

A harness maps the model id, the auth key, the optional base URL and the
per-role restrictions onto the three primitives every headless CLI agent shares:
an argv list, an environment dict and a working directory. ``REGISTRY`` is what
ships, and naming an agent here the registry does not carry promises support it
cannot keep.

The structured-output contract lives in the role prompt (templates/*.md tell the
agent to end with a fenced json block), not a CLI flag, so one shared parser
(``agent.base.extract_structured``) works across harnesses. OpenCode's
``--format json`` is a streaming event stream rather than a final-result shape;
do not enable it.

Harness flags churn, so the installer skill doc-verifies the chosen harness's
current flags at install time.
"""
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from agent.base import AgentAdapter, AgentRole


@dataclass(frozen=True)
class AgentRun:
    """Everything one headless agent invocation produced.

    The loop only needs ``stdout`` to parse the structured answer, but evidence
    needs the rest: without ``stderr`` and ``returncode`` a crashed agent (bad
    auth, missing binary) is indistinguishable from one that merely emitted no
    json block — both surface as the same parse error.
    """

    stdout: str
    stderr: str = ""
    returncode: int = 0
    duration_s: float = 0.0


# Injected so command construction + env are testable without the CLI installed.
Runner = Callable[[Sequence[str], Mapping[str, str], Path], AgentRun]

_PROMPT = "{prompt}"
_MODEL = "{model}"


def _subprocess_runner(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> AgentRun:
    started = time.monotonic()
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
    return AgentRun(
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        duration_s=round(time.monotonic() - started, 3),
    )


@dataclass(frozen=True)
class ModelConfig:
    """Operator-supplied, harness-agnostic. Forwarded verbatim by render."""

    model: str
    auth_token: str
    base_url: str = ""
    # Overrides HarnessConfig.auth_env when set. OpenCode needs this: its auth
    # env var is the provider's native one (auto-detected from provider/model),
    # so the harness ships no fixed default and the operator names it here.
    auth_env: str = ""

    def __post_init__(self) -> None:
        # An unset GitHub repo variable expands to the EMPTY STRING in a
        # workflow's env block, not to nothing, so a missing value arrives here
        # as "" rather than as a KeyError. Forwarded, it renders `--model ""` or
        # an empty auth header and fails at the provider several steps later,
        # with nothing in the message naming the variable that was never set.
        # base_url and auth_env are legitimately empty on a native provider.
        for value, variable in ((self.model, "SHL_MODEL"),
                                (self.auth_token, "SHL_AUTH_TOKEN")):
            if not value.strip():
                raise ValueError(f"{variable} is empty; set it as a repo variable")


@dataclass(frozen=True)
class HarnessConfig:
    """One harness = how model/auth/role map onto argv + env. Pure data."""

    name: str
    install: tuple[str, ...]
    argv: tuple[str, ...]  # contains "{prompt}"; "{model}" if model rides argv
    role_argv: dict[AgentRole, tuple[str, ...]] = field(default_factory=dict)
    model_env: tuple[str, ...] = ()  # env vars set to the model id (Claude tiers)
    auth_env: str = ""  # default; ModelConfig.auth_env overrides
    base_url_env: str = ""  # env var for a non-native base URL
    # What this harness denies for every role, as (flag, tools). Checked against
    # the rendered argv, so trimming a tool from role_argv fails the guard.
    # Argv-shaped; a harness restricted by config file uses preflight instead.
    required_denial: tuple[str, tuple[str, ...]] = ()
    # What a SPECIFIC role must deny on top of `required_denial`, because roles
    # do not all need the same restriction and one shared list can only carry
    # what every role has. The read-only roles are the case: `--add-dir ..`
    # widens their READS to the whole target, so what keeps them read-only is
    # denying the file-writing tools outright — `Edit(./**)` is cwd-relative and
    # covers the loop tree alone.
    role_denial: dict[AgentRole, tuple[str, ...]] = field(default_factory=dict)
    # Per-role command that must exit 0 before the role runs. For a harness that
    # selects its restriction by name, where an unresolved name falls back to an
    # unrestricted default instead of failing, this is what makes it fail.
    preflight_argv: dict[AgentRole, tuple[str, ...]] = field(default_factory=dict)


def render(
    h: HarnessConfig, m: ModelConfig, role: AgentRole
) -> tuple[list[str], dict[str, str]]:
    """Substitute model + role into argv, and auth + model + base_url into env.

    Returns (argv, env). ``{prompt}`` is left in argv for ``ConfiguredAgent.run``
    to fill — render does not know the prompt. A model-level auth_env wins over
    the harness default so one harness covers many providers (OpenCode).
    """
    auth_env = m.auth_env or h.auth_env
    env: dict[str, str] = {}
    if auth_env:
        env[auth_env] = m.auth_token
    if h.base_url_env and m.base_url:
        env[h.base_url_env] = m.base_url
    for var in h.model_env:
        env[var] = m.model
    argv = [m.model if tok == _MODEL else tok for tok in h.argv]
    argv += list(h.role_argv.get(role, ()))
    return argv, env


class ConfiguredAgent(AgentAdapter):
    """An AgentAdapter bound to one harness + model. Built once, run per role."""

    def __init__(
        self,
        harness: HarnessConfig,
        model: ModelConfig,
        runner: Runner = _subprocess_runner,
        evidence_dir: Optional[Path] = None,
    ):
        self._harness = harness
        self._model = model
        self._runner = runner
        # When set, every invocation leaves its prompt/output/metadata on disk.
        # Imported lazily in run() so the agent seam stays usable standalone.
        self._evidence_dir = evidence_dir

    def _assert_denies_what_it_claims(self, role: AgentRole, argv: list[str]) -> None:
        """Verify the rendered argv carries the denial the harness declares.

        A non-empty config proves someone filled it in, not that the values
        restrict anything: a flag the CLI silently ignores reads identically to
        a working one. This checks what the values do.
        """
        flag, tools = self._harness.required_denial
        try:
            value = argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            raise RuntimeError(
                f"harness '{self._harness.name}' declares denial via {flag}, but "
                f"role {role.value} renders no such flag: {argv}"
            ) from None
        # Split into entries and compare whole names. `tool in value` is a
        # substring test, and every way of weakening this flag stays a superstring
        # of the tool it names: `Bash(rm:*)` scopes it, `BashOutput` is a
        # different tool, and prose describing the deny satisfies it outright. The
        # guard would then pass while the role holds a live shell and both tokens.
        denied = {entry.strip() for entry in value.split(",")}
        required = tuple(tools) + tuple(self._harness.role_denial.get(role, ()))
        missing = [tool for tool in required if tool not in denied]
        if missing:
            raise RuntimeError(
                f"harness '{self._harness.name}' role {role.value} does not deny "
                f"{', '.join(missing)} (its {flag} carries '{value}'); refusing to "
                f"run it while the loop's credentials are in the environment"
            )

    def _assert_preflight_passes(
        self, role: AgentRole, env: dict[str, str], cwd: Path
    ) -> None:
        """Run the harness's per-role preflight and refuse on a non-zero exit.

        Selecting a restriction by name is only safe if an unresolved name
        stops the cycle. Where the harness itself treats that as a warning and
        continues under an unrestricted default, this is the check that turns a
        missing or misspelled config into a refusal.
        """
        command = self._harness.preflight_argv[role]
        result = self._runner(command, env, cwd)
        if result.returncode != 0:
            raise RuntimeError(
                f"harness '{self._harness.name}' preflight failed for role "
                f"{role.value} (exit {result.returncode}): its restriction does "
                f"not resolve, and running anyway would use an unrestricted "
                f"default. Command: {' '.join(command)}. {result.stderr.strip()}"
            )

    def run(self, prompt: str, role: AgentRole, cwd: Path) -> str:
        # No role runs unrestricted, including Fix. Every role's prompt carries
        # text derived from an untrusted log, and the Fix step holds both the
        # provider token and GH_TOKEN, so "may edit source" is not "may do
        # anything". An unfilled config raises here rather than running open.
        if not self._harness.role_argv.get(role):
            raise RuntimeError(
                f"harness '{self._harness.name}' has no restriction for role "
                f"{role.value}; refusing to run it unrestricted while the loop's "
                f"credentials are in the environment. Fill role_argv."
            )
        argv, env = render(self._harness, self._model, role)
        # A restriction the loop cannot verify is not a restriction. Argv-level
        # denial and a preflight are the two forms that verification takes; a
        # harness offering neither is refused.
        if not self._harness.required_denial and role not in self._harness.preflight_argv:
            raise RuntimeError(
                f"harness '{self._harness.name}' offers no way to verify role "
                f"{role.value} is restricted: it declares neither required_denial "
                f"nor a preflight. A filled-in config is not a restricting one."
            )
        if self._harness.required_denial:
            self._assert_denies_what_it_claims(role, argv)
        if role in self._harness.preflight_argv:
            self._assert_preflight_passes(role, env, cwd)
        argv = [prompt if tok == _PROMPT else tok for tok in argv]
        result = self._runner(argv, env, cwd)
        if self._evidence_dir is not None:
            from evidence import record_agent_run

            record_agent_run(
                self._evidence_dir, role, prompt, argv, env, cwd, result
            )
        return result.stdout


# Flag sources: code.claude.com/docs/en/headless + settings; opencode.ai/docs/cli.

# Denied for EVERY role. `Edit(./**)` is cwd-relative on purpose: it covers the
# loop's own tree — the gate that judges the diff, the CLI that runs it, the
# scrubber, loop.py — while the target's own source one level up stays editable.
# `Edit(...)` is the binding form; it covers every file-editing tool, and a
# `Write(...)` path rule is ignored by file permission checks entirely.
_DENIED = ("--disallowedTools", "Bash,WebFetch,WebSearch,Agent,Edit(./**)")

# The read-only roles are handed the target's source to read, one level up from
# their cwd, so they additionally deny every file-writing tool outright rather
# than relying on the mode. `Edit(./**)` stays in the string because that is what
# the harness-level guard checks for; the bare names widen the denial from the
# loop tree to everywhere.
_DENIED_READ_ONLY = (
    "--disallowedTools",
    "Bash,WebFetch,WebSearch,Agent,Edit(./**),Edit,Write,NotebookEdit",
)

# Diagnose and Review must READ the target's code — the failure they judge lives
# in `../`, not in the loop tree. Without this the roles are confined to their
# own cwd: Diagnose can then only infer a root cause from the log text, which it
# reports honestly but which degrades to guesswork on any non-obvious bug.
_TARGET_SOURCE = ("--add-dir", "..")

CLAUDE_CODE = HarnessConfig(
    name="claude-code",
    # Pinned: the runner installs this fresh every cycle, and what churns across
    # releases is exactly what this framework rests on — argv flags and
    # permission semantics. The npm tag to read when bumping is `stable`;
    # `latest` here tracks the same build as `next`.
    install=("npm", "i", "-g", "@anthropic-ai/claude-code@2.1.236"),
    argv=("claude", "-p", "{prompt}"),
    role_argv={
        # dontAsk is the documented locked-down-CI mode, and it is NOT
        # deny-by-default in the absolute sense: a built-in read-only Bash set
        # (echo, cat, grep, find, ...) runs unprompted in EVERY mode and cannot
        # be configured off except by an explicit deny. Without the denials
        # below, an injected log line could have a read-only role run
        # `echo $ANTHROPIC_AUTH_TOKEN` — the agent inherits the runner's whole
        # environment — and put the value in issue_body, which the workflow
        # then posts to a GitHub issue.
        AgentRole.DIAGNOSE: (
            "--permission-mode",
            "dontAsk",
            *_TARGET_SOURCE,
            *_DENIED_READ_ONLY,
        ),
        AgentRole.REVIEW: (
            "--permission-mode",
            "dontAsk",
            *_TARGET_SOURCE,
            *_DENIED_READ_ONLY,
        ),
        # acceptEdits lets Fix edit without prompting, but it ALSO auto-approves
        # common filesystem commands (rm, mv, cp, sed, mkdir, touch, rmdir), so
        # an allow-list alone does not make it shell-free.
        #
        # Note --allowedTools is an allow-RULE list, not a restriction on which
        # tools exist: every other tool stays in context and is governed by the
        # mode. That is why the deny list, not the allow list, is what bounds
        # this role.
        AgentRole.FIX: (
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Read,Edit,Write",
            *_DENIED,
        ),
    },
    # EVERY alias, not just the one the operator names. A third-party endpoint
    # serves one catalogue, while the harness still resolves its own aliases for
    # background small-model calls, subagents and any tier a prompt mentions —
    # each unset one keeping a default id that provider has never heard of, so
    # the cycle dies part-way rather than at startup. Setting a variable this
    # CLI ignores costs nothing; omitting one it reads costs a cycle.
    #
    # The `_NAME` variants only label the model picker, which is unreachable
    # headless; they are set so a display string cannot name a different model
    # than the one running.
    model_env=(
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ),
    # Default only. The provider decides which variable carries its key, and
    # third-party endpoints differ: some read ANTHROPIC_AUTH_TOKEN, others
    # ANTHROPIC_API_KEY. The operator names it through ModelConfig.auth_env,
    # which wins over this.
    auth_env="ANTHROPIC_AUTH_TOKEN",
    base_url_env="ANTHROPIC_BASE_URL",
    required_denial=(
        "--disallowedTools",
        ("Bash", "WebFetch", "WebSearch", "Agent", "Edit(./**)"),
    ),
    # The read-only pair only: Fix legitimately renders none of these, so they
    # cannot live in `required_denial`. Kept in step with `_DENIED_READ_ONLY` by
    # a test that derives the membership from which roles carry `--add-dir`.
    role_denial={
        AgentRole.DIAGNOSE: ("Edit", "Write", "NotebookEdit"),
        AgentRole.REVIEW: ("Edit", "Write", "NotebookEdit"),
    },
)

OPENCODE = HarnessConfig(
    name="opencode",
    # Pinned: this CLI's flags and its restriction mechanism both vary across
    # releases, so an unpinned global install can land a build the config below
    # does not describe.
    install=("npm", "i", "-g", "opencode-ai@1.18.15"),
    # --pure blocks project plugins, which a target repo could otherwise use to
    # widen what an agent may do.
    argv=("opencode", "run", "--pure", "--model", "{model}", "{prompt}"),
    # Restriction is per-agent and lives in `opencode.json`; argv only selects
    # which agent runs. That file sits at the loop root because this CLI
    # discovers it from the working directory and offers no path override, so it
    # is the one harness-specific file in an otherwise harness-agnostic tree,
    # inert on a target using any other harness. Denying a tool there removes it
    # from the agent's context rather than prompting for it.
    #
    # `--agent <name>` MUST be paired with the preflight below: `run` treats an
    # unresolved name as a warning and continues under the unrestricted default
    # agent, while `debug agent` exits non-zero on the same input. That preflight
    # is also how this harness satisfies the restriction guard, since its denial
    # lives in a file rather than in argv.
    role_argv={
        AgentRole.DIAGNOSE: ("--agent", "shl-diagnose"),
        AgentRole.FIX: ("--agent", "shl-fix"),
        AgentRole.REVIEW: ("--agent", "shl-review"),
    },
    preflight_argv={
        AgentRole.DIAGNOSE: ("opencode", "debug", "agent", "shl-diagnose"),
        AgentRole.FIX: ("opencode", "debug", "agent", "shl-fix"),
        AgentRole.REVIEW: ("opencode", "debug", "agent", "shl-review"),
    },
    # Model rides in --model argv and must name the provider PLAN, not just the
    # vendor: `zai-coding-plan/glm-5.2` on the international plan,
    # `zhipuai-coding-plan/glm-5.2` on the China one, never `zhipuai/glm-5.2`. A
    # model id pointing at an endpoint the credential does not cover retries
    # rather than erroring, so the symptom is a hung cycle. Auth uses the
    # provider's native env var, named by the operator via ModelConfig.auth_env,
    # with no fixed default.
    model_env=(),
    auth_env="",
    base_url_env="",
)

# The workflow picks one by name from the SHL_HARNESS env var.
REGISTRY: dict[str, HarnessConfig] = {
    "claude-code": CLAUDE_CODE,
    "opencode": OPENCODE,
}


def get_harness(name: str) -> HarnessConfig:
    """Look up a shipped harness by name; KeyError (with the known list) if unknown."""
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown harness '{name}'; known: {sorted(REGISTRY)}") from None