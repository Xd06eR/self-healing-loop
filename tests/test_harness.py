import json
import unittest
from dataclasses import replace
from pathlib import Path

from agent.base import AgentRole
from agent.harness import (
    CLAUDE_CODE,
    OPENCODE,
    REGISTRY,
    AgentRun,
    ConfiguredAgent,
    HarnessConfig,
    ModelConfig,
    get_harness,
    render,
)

# The framework is model-agnostic: model id, endpoint, key, and even the auth
# env var name are operator config, forwarded verbatim. Placeholders stand for
# ANY provider/model. These assertions prove passthrough, not a pin to one.


def _capture():
    """A runner that records (argv, env, cwd) and returns a valid json block."""
    calls = []

    def runner(argv, env, cwd):
        calls.append((list(argv), dict(env), cwd))
        return AgentRun(stdout='```json\n{"ok": true}\n```')

    return calls, runner


class TestRenderEnv(unittest.TestCase):
    def test_claude_code_third_party_sets_every_tier(self):
        # Claude Code pointed at any non-Anthropic model: id / endpoint / key
        # forwarded as-is; all tier vars overridden or the harness's background
        # small-model calls hit a missing model.
        m = ModelConfig(
            model="example-model-v1",
            auth_token="sk-test-key",
            base_url="https://api.example-provider.test/v1",
        )
        _, env = render(CLAUDE_CODE, m, AgentRole.DIAGNOSE)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-test-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.example-provider.test/v1")
        self.assertEqual(env["ANTHROPIC_MODEL"], "example-model-v1")
        for tier in CLAUDE_CODE.model_env:
            self.assertEqual(env[tier], "example-model-v1", tier)

    def test_claude_code_native_omits_base_url(self):
        m = ModelConfig(model="native-model-id", auth_token="sk-test-native")
        _, env = render(CLAUDE_CODE, m, AgentRole.FIX)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-test-native")

    def test_opencode_auth_env_comes_from_model_not_harness(self):
        # OpenCode uses the provider's native env var, auto-detected from
        # provider/model. The operator states which var; the harness ships no
        # fixed default. ModelConfig.auth_env overrides the harness default.
        m = ModelConfig(
            model="exampleprovider/example-model",
            auth_token="test-provider-key",
            auth_env="EXAMPLEPROVIDER_API_KEY",
        )
        argv, env = render(OPENCODE, m, AgentRole.FIX)
        self.assertEqual(env, {"EXAMPLEPROVIDER_API_KEY": "test-provider-key"})
        # Model rides in argv --model, never env.
        self.assertNotIn("ANTHROPIC_MODEL", env)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "exampleprovider/example-model")


class TestRenderArgv(unittest.TestCase):
    def test_model_substituted_in_argv(self):
        m = ModelConfig(model="exampleprovider/example-model", auth_token="k", auth_env="K")
        argv, _ = render(OPENCODE, m, AgentRole.FIX)
        self.assertNotIn("{model}", argv)
        self.assertIn("exampleprovider/example-model", argv)

    def test_prompt_placeholder_preserved_for_run(self):
        # render does not know the prompt; {prompt} stays for run() to fill.
        m = ModelConfig(model="m", auth_token="k")
        argv, _ = render(CLAUDE_CODE, m, AgentRole.DIAGNOSE)
        self.assertIn("{prompt}", argv)


class RestrictionsMatchTheDocumentedCIMode(unittest.TestCase):
    """The two safety claims this framework makes about roles must be true.

    Neither holds by accident. `--permission-mode plan` reads like the read-only
    choice, but the docs name `dontAsk` as the locked-down-CI mode and plan has
    an edge case where it does not block edits at all — so under plan, Diagnose
    and Review, the two roles that read untrusted logs, are not reliably
    read-only. And `acceptEdits` auto-approves common filesystem COMMANDS (rm,
    mv, cp, sed, mkdir, touch, rmdir), so "Fix has no shell" — asserted in
    loop_context/CLAUDE.md and templates/fix.md — is true only while Bash is
    denied explicitly.
    """

    READ_ONLY = (AgentRole.DIAGNOSE, AgentRole.REVIEW)

    def test_read_only_roles_use_the_locked_down_ci_mode(self):
        m = ModelConfig(model="m", auth_token="k")
        for role in self.READ_ONLY:
            with self.subTest(role=role.value):
                argv, _ = render(CLAUDE_CODE, m, role)
                self.assertIn("dontAsk", argv)
                self.assertNotIn("plan", argv)

    def test_fix_cannot_reach_a_shell(self):
        # acceptEdits alone is not shell-free; Bash must be denied explicitly.
        argv, _ = render(CLAUDE_CODE, ModelConfig(model="m", auth_token="k"), AgentRole.FIX)
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Bash", argv[argv.index("--disallowedTools") + 1])

    def test_every_role_denies_the_egress_and_delegation_tools(self):
        """A deny rule is the only thing that closes these; a mode is not.

        `dontAsk` is not deny-by-default in the absolute sense: a built-in
        read-only Bash set (`echo`, `cat`, `grep`, `find`, ...) runs unprompted
        in EVERY mode and is not configurable except by an explicit deny. So a
        prompt-injected read-only role could run `echo $ANTHROPIC_AUTH_TOKEN` —
        the runner's whole environment is inherited — and put the value in
        `issue_body`, which the workflow posts to a GitHub issue.

        WebFetch/WebSearch are direct egress. Agent spawns a subagent whose own
        frontmatter may carry a different permission mode, which is an
        unverified escalation path rather than a demonstrated one — denied here
        because no role needs it, so the question never has to be answered.
        """
        must_deny = ("Bash", "WebFetch", "WebSearch", "Agent")
        for role in AgentRole:
            argv, _ = render(CLAUDE_CODE, ModelConfig(model="m", auth_token="k"), role)
            self.assertIn("--disallowedTools", argv, f"{role.value} denies nothing")
            denied = argv[argv.index("--disallowedTools") + 1]
            for tool in must_deny:
                with self.subTest(role=role.value, tool=tool):
                    self.assertIn(tool, denied)

    def test_a_role_that_can_read_the_target_denies_every_write_tool(self):
        """`--add-dir ..` widens READS to the whole repo; writes must not follow.

        `Edit(./**)` is cwd-relative, so it denies the loop tree and nothing
        else. What confines Diagnose and Review to reading is the BARE
        `Edit,Write,NotebookEdit` beside it — and those three sit outside
        `required_denial`, so the guard that exists to prove a role is
        restricted never looked at them. Deleting the whole read-only write
        denial passed all 521 tests.

        The membership is computed from `--add-dir`, not listed: a fourth role
        granted the target's source later inherits the requirement instead of
        being forgotten.
        """
        m = ModelConfig(model="m", auth_token="k")
        widened = []
        for role in AgentRole:
            argv, _ = render(CLAUDE_CODE, m, role)
            if "--add-dir" in argv:
                widened.append(role)
                denied = {e.strip() for e in argv[argv.index("--disallowedTools") + 1].split(",")}
                for tool in ("Edit", "Write", "NotebookEdit"):
                    with self.subTest(role=role.value, tool=tool):
                        self.assertIn(
                            tool, denied,
                            f"{role.value} can read the whole repo and does not deny "
                            f"{tool} outright; Edit(./**) covers the loop tree only",
                        )
        self.assertEqual(
            set(widened), set(self.READ_ONLY),
            "the set of roles reading the target's source changed; decide "
            "deliberately whether the new one may also write",
        )

    def test_the_restriction_guard_enforces_that_denial(self):
        """A denial nothing asserts is a denial that can be dropped silently.

        This is the project's own L9 inside the file that exists to enforce L9:
        the argv was right and the guard did not read it. Asserted by driving
        the guard with the denial removed.

        Every widened role, not one of them. Driving DIAGNOSE alone let a
        mutation that dropped REVIEW from `role_denial` pass — the argv still
        carried the names, so the argv test above was satisfied, and the only
        guard test never asked about REVIEW.
        """
        m = ModelConfig(model="m", auth_token="k")
        checked = 0
        for role in AgentRole:
            argv, _ = render(CLAUDE_CODE, m, role)
            if "--add-dir" not in argv:
                continue
            checked += 1
            stripped = replace(
                CLAUDE_CODE,
                role_argv={
                    **CLAUDE_CODE.role_argv,
                    role: tuple(
                        "Bash,WebFetch,WebSearch,Agent,Edit(./**)"
                        if a.startswith("Bash,")
                        else a
                        for a in CLAUDE_CODE.role_argv[role]
                    ),
                },
            )
            agent = ConfiguredAgent(stripped, m)
            weakened, _ = render(stripped, m, role)
            with self.subTest(role=role.value):
                with self.assertRaises(RuntimeError) as caught:
                    agent._assert_denies_what_it_claims(role, list(weakened))
                self.assertIn("NotebookEdit", str(caught.exception))
        self.assertEqual(checked, len(self.READ_ONLY))


class EveryRoleMustBeRestrictedOrRefused(unittest.TestCase):
    """No role runs unrestricted, on any harness — not just the read-only two.

    Covering DIAGNOSE and REVIEW alone is the tempting narrowing, on the
    reasoning that Fix is *supposed* to edit. But "may edit source" is not "may
    do anything": Fix holds BOTH the provider token and GH_TOKEN (heal.yml's Fix
    step), and its prompt carries `issue_body`, which Diagnose wrote from an
    untrusted log. A harness that ships Fix with no restriction hands that
    prompt a shell with two credentials in its environment — and that Fix is
    unreachable only while the read-only roles still refuse, so restricting
    those is what makes it the first role that actually runs.
    """

    def test_no_role_runs_without_a_restriction(self):
        unrestricted = HarnessConfig(
            name="unrestricted",
            install=("pkg", "i", "unrestricted"),
            argv=("unrestricted", "{prompt}"),
        )
        _, runner = _capture()
        agent = ConfiguredAgent(
            unrestricted, ModelConfig(model="m", auth_token="k"), runner=runner
        )
        for role in AgentRole:
            with self.subTest(role=role.value):
                with self.assertRaises(RuntimeError):
                    agent.run("go", role, Path("/t"))

    def test_opencode_selects_a_named_agent_for_every_role(self):
        """Restriction rides `--agent`, so an unnamed role runs unrestricted."""
        for role in AgentRole:
            with self.subTest(role=role.value):
                argv = OPENCODE.role_argv.get(role, ())
                self.assertIn("--agent", argv, f"role {role.value} names no agent")

    def test_opencode_runs_a_role_whose_preflight_passes(self):
        calls = []

        def runner(argv, env, cwd):
            calls.append(list(argv))
            return AgentRun(stdout='```json\n{"ok": true}\n```', returncode=0)

        agent = ConfiguredAgent(
            OPENCODE, ModelConfig(model="p/m", auth_token="k", auth_env="K"), runner=runner
        )
        agent.run("go", AgentRole.FIX, Path("/t"))
        self.assertEqual(len(calls), 2, "expected a preflight call then the run")
        self.assertIn("debug", calls[0])
        self.assertIn("run", calls[1])

    def test_opencode_preflights_the_same_agent_it_runs(self):
        """Drift between the two names would check one agent and run another."""
        self.assertTrue(OPENCODE.role_argv, "role_argv is empty; this test would be vacuous")
        for role, argv in OPENCODE.role_argv.items():
            with self.subTest(role=role.value):
                selected = argv[argv.index("--agent") + 1]
                self.assertEqual(OPENCODE.preflight_argv[role][-1], selected)


class TheAgentCannotEditTheLoopThatJudgesIt(unittest.TestCase):
    """Every role is denied edits inside its own working directory.

    The agent's cwd is the installed loop: the gate that will judge its diff,
    the CLI that runs it, the scrubber that redacts its output, and `loop.py`
    itself. An edit-permitting mode auto-approves writes there, and the prompt
    carries text derived from an untrusted log — so without this rule the
    reachable outcome is a fix that rewrites the gate, which then passes it.

    The rule is cwd-relative, which is what makes it precise: `./**` is the
    loop tree and nothing else, while the target's own source sits one level up
    and stays editable. `Edit(...)` is the form that binds — it covers every
    file-editing tool, and a `Write(...)` path rule is ignored by file
    permission checks entirely.

    `gate.is_loop_tree_touched` still inspects the diff. Two layers on purpose:
    this one bounds what the agent can reach, that one catches anything that
    arrives in the diff regardless of how.
    """

    LOOP_TREE_RULE = "Edit(./**)"

    def test_every_role_denies_edits_to_its_working_directory(self):
        for role in AgentRole:
            with self.subTest(role=role.value):
                argv, _ = render(
                    CLAUDE_CODE, ModelConfig(model="m", auth_token="k"), role
                )
                denied = argv[argv.index("--disallowedTools") + 1]
                self.assertIn(self.LOOP_TREE_RULE, denied)

    def test_the_rule_is_verified_not_merely_present(self):
        # In required_denial, so trimming it from the deny set fails the guard
        # rather than silently widening every role.
        _, tools = CLAUDE_CODE.required_denial
        self.assertIn(self.LOOP_TREE_RULE, tools)


class EveryModelAliasPointsAtTheChosenModel(unittest.TestCase):
    """A tier the operator does not override resolves to a model the endpoint
    does not serve.

    Pointing Claude Code at a third-party endpoint replaces the whole model
    catalogue with one provider's, but the harness still resolves its internal
    aliases — the background small-model call, a subagent's model, any tier a
    prompt names. Each unset alias keeps a default id the provider has never
    heard of, so the cycle fails part-way through rather than at startup, which
    reads as a flaky agent instead of a missing variable.

    The asymmetry decides the rule: setting a variable this CLI ignores costs
    nothing, and omitting one it reads costs a cycle. So every alias is set.
    """

    ALIASES = (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    )

    def test_every_alias_is_set_to_the_chosen_model(self):
        _, env = render(
            CLAUDE_CODE,
            ModelConfig(model="provider/some-model", auth_token="k", base_url="https://x"),
            AgentRole.FIX,
        )
        for var in self.ALIASES:
            with self.subTest(var=var):
                self.assertEqual(env.get(var), "provider/some-model")


class RestrictionIsCheckedByContentNotByPresence(unittest.TestCase):
    """A filled-in config is not a restricting one, and the difference is real.

    Accepting any non-empty `role_argv` checks whether someone
    TOUCHED the config, not what the values do. The concrete case: restricting
    OpenCode through `OPENCODE_PERMISSION`, exactly as its own documentation
    describes, while the pinned CLI ignores that variable entirely. A presence
    check passes that config happily, and every role runs unrestricted holding
    the provider token and GH_TOKEN.

    So a harness DECLARES what it must deny, and the guard verifies the
    rendered argv actually carries it. A harness that declares nothing cannot
    pass at all — fail-closed by construction, rather than by whoever fills it
    next remembering to be careful.
    """

    def _agent(self, harness):
        _, runner = _capture()
        return ConfiguredAgent(
            harness, ModelConfig(model="m", auth_token="k", auth_env="K"), runner=runner
        )

    def test_a_filled_config_that_declares_no_denial_is_refused(self):
        # The dangerous shape: someone fills role_argv with something that
        # looks like a restriction and the guard waves it through.
        looks_restricted = HarnessConfig(
            name="looks-restricted",
            install=("pkg", "i", "x"),
            argv=("x", "{prompt}"),
            role_argv={role: ("--mode", "careful") for role in AgentRole},
        )
        agent = self._agent(looks_restricted)
        for role in AgentRole:
            with self.subTest(role=role.value):
                with self.assertRaises(RuntimeError):
                    agent.run("go", role, Path("/t"))

    def test_a_declared_denial_the_flags_do_not_deliver_is_refused(self):
        lying = HarnessConfig(
            name="lying",
            install=("pkg", "i", "x"),
            argv=("x", "{prompt}"),
            role_argv={role: ("--deny", "WebFetch") for role in AgentRole},
            required_denial=("--deny", ("Bash", "WebFetch")),
        )
        agent = self._agent(lying)
        for role in AgentRole:
            with self.subTest(role=role.value):
                with self.assertRaisesRegex(RuntimeError, "Bash"):
                    agent.run("go", role, Path("/t"))

    def test_claude_code_satisfies_its_own_declared_denial(self):
        agent = self._agent(CLAUDE_CODE)
        for role in AgentRole:
            with self.subTest(role=role.value):
                agent.run("go", role, Path("/t"))  # must not raise

    def test_removing_bash_from_the_deny_list_is_refused(self):
        """The regression this exists to catch: a well-meaning narrowing.

        `Bash` looks redundant next to `--allowedTools Read,Edit,Write`, so it
        is the natural thing for someone to trim. It is not redundant:
        --allowedTools is an allow-RULE list, and a built-in read-only Bash set
        runs unprompted in every permission mode, which is enough to read the
        provider token out of the environment.
        """
        weakened = replace(
            CLAUDE_CODE,
            role_argv={
                role: tuple(
                    "WebFetch,WebSearch,Agent" if tok.startswith("Bash,") else tok
                    for tok in argv
                )
                for role, argv in CLAUDE_CODE.role_argv.items()
            },
        )
        agent = self._agent(weakened)
        for role in AgentRole:
            with self.subTest(role=role.value):
                with self.assertRaisesRegex(RuntimeError, "Bash"):
                    agent.run("go", role, Path("/t"))


class TheOpencodeConfigDefinesTheAgentsTheHarnessSelects(unittest.TestCase):
    """The config file and the harness name the same agents, with the same rules.

    OpenCode's restriction lives in the file; argv only picks an agent by name.
    So the two drift independently, and drift is silent in the dangerous
    direction: an agent the file does not define resolves to the unrestricted
    default at run time.
    """

    CONFIG = json.loads(
        (Path(__file__).resolve().parents[1] / "opencode.json").read_text(encoding="utf-8")
    )

    @staticmethod
    def _selected(argv):
        """The agent name an argv tuple selects, or None if it selects none."""
        argv = list(argv)
        if "--agent" not in argv:
            return None
        i = argv.index("--agent")
        return argv[i + 1] if i + 1 < len(argv) else None

    def test_it_defines_one_agent_per_role(self):
        # Derived from role_argv, never written out. A hand-kept literal passes
        # a rename that touches every real site, which is precisely the rename
        # someone doing it carefully would perform — and the failure is silent
        # in the dangerous direction, because `opencode run` treats an unknown
        # --agent as a warning and continues under the unrestricted default.
        selected = {self._selected(argv) for argv in OPENCODE.role_argv.values()}
        self.assertEqual(set(self.CONFIG["agent"]), selected)

    def test_every_role_selects_an_agent(self):
        # Guards the check above from passing vacuously: an empty role_argv
        # would make both sides empty and agree.
        for role in AgentRole:
            with self.subTest(role=role.value):
                self.assertIsNotNone(self._selected(OPENCODE.role_argv[role]))

    def test_the_preflight_checks_the_agent_the_run_actually_selects(self):
        """Three-way, because the preflight is the only thing that fails closed.

        `--agent` fails OPEN on an unknown name. `opencode debug agent <name>`
        exits non-zero on the same input, which is why it is the restriction
        proof for this harness. A rename that updates `role_argv` and the config
        but misses the preflight leaves the loop verifying an agent it is not
        about to run — the check passes, and the run is unrestricted.
        """
        for role in AgentRole:
            with self.subTest(role=role.value):
                run_name = self._selected(OPENCODE.role_argv[role])
                preflight = list(OPENCODE.preflight_argv[role])
                self.assertEqual(
                    preflight[-1],
                    run_name,
                    f"{role.value} runs as {run_name} but preflights {preflight[-1]}",
                )
                self.assertIn(run_name, self.CONFIG["agent"])

    def test_every_agent_denies_by_default(self):
        for name, spec in self.CONFIG["agent"].items():
            with self.subTest(agent=name):
                self.assertEqual(spec["permission"]["*"], "deny")

    def test_only_the_fix_agent_may_edit(self):
        for name, spec in self.CONFIG["agent"].items():
            with self.subTest(agent=name):
                edit = spec["permission"].get("edit")
                if name == "shl-fix":
                    self.assertIsInstance(edit, dict, "Fix's edit must be path-scoped")
                else:
                    self.assertIsNone(edit)

    LOOP_TREE = "**/.shl/**"

    def test_the_fix_agent_cannot_edit_the_loop_tree(self):
        """The one rule standing between an injected log line and the gate.

        Fix's working directory IS the installed loop: `gate.py`, `cli.py`, the
        scrubber and `loop.py` all sit in it, and the steps after Fix import
        that code while holding both tokens. Allowing `edit` without scoping it
        hands the agent the judge of its own diff.
        """
        edit = self.CONFIG["agent"]["shl-fix"]["permission"]["edit"]
        self.assertEqual(edit.get(self.LOOP_TREE), "deny")

    def test_the_fix_agent_cannot_edit_gits_own_directory(self):
        """git executes its config and its hooks; `.git/` is in no diff.

        `core.fsmonitor`, `core.pager`, `core.sshCommand`, `diff.external` and
        `.git/hooks/*` are all run by git itself, and the next `git` calls in
        the job are `git add -A` at the gate and `git commit`/`git push` under
        `GH_TOKEN`. Nothing tracked changes, so the gate's predicates and the
        Review agent both see a clean diff.

        The workflow's snapshot comparison is the control that does not depend
        on a harness honouring a pattern; this is the harness half, pinned
        because a deny nobody asserts is a deny that can be dropped silently.
        """
        edit = self.CONFIG["agent"]["shl-fix"]["permission"]["edit"]
        self.assertEqual(edit.get("**/.git/**"), "deny")

    def test_the_fix_agent_cannot_edit_gitattributes(self):
        """`* -diff` empties the content the gate and the reviewer read.

        Paths still appear, so every path-based check passes; only the content
        checks go blind, and they go blind silently.
        """
        edit = self.CONFIG["agent"]["shl-fix"]["permission"]["edit"]
        self.assertEqual(edit.get("**/.gitattributes"), "deny")

    def test_every_deny_is_written_after_the_wildcard_allow(self):
        """Rules are last-match-wins, so order is the whole protection.

        Put a deny first and the following `"*": "allow"` overrides it, which
        reads as a configured restriction while restricting nothing.

        Asserted as the RULE rather than as "the loop-tree deny is last". That
        proxy held only while there was exactly one deny, so adding a second
        legitimate one broke a test whose stated invariant was untouched — and
        the tempting repair is to re-pin whichever deny now happens to sit last,
        which would leave the next one unchecked. This form covers every deny
        there is and every deny added later.
        """
        edit = self.CONFIG["agent"]["shl-fix"]["permission"]["edit"]
        patterns = list(edit)
        self.assertIn("*", patterns, f"no wildcard to override, got {patterns}")
        wildcard = patterns.index("*")
        denies = [p for p in patterns if edit[p] == "deny"]
        self.assertTrue(denies, "the fix agent's edit rules deny nothing")
        for pattern in denies:
            with self.subTest(pattern=pattern):
                self.assertGreater(
                    patterns.index(pattern), wildcard,
                    f"`{pattern}` is denied before `*` is allowed, so the allow "
                    f"wins and the deny is decorative; order: {patterns}",
                )

    def test_no_agent_leaves_external_directory_to_the_built_in_ask(self):
        """Unset, this resolves to `ask`, and a headless `ask` never returns.

        The role needs to reach the target's source one level up from its cwd,
        so the permission is exercised on every cycle. Leaving it unset does not
        deny the access — it suspends the run until the job timeout kills it,
        with no output saying why.
        """
        for name, spec in self.CONFIG["agent"].items():
            with self.subTest(agent=name):
                self.assertIn(
                    "external_directory",
                    spec["permission"],
                    f"{name} leaves external_directory unset, which hangs headless",
                )

    def test_no_agent_may_reach_a_shell_or_the_network(self):
        # Denied by the wildcard rather than named, so assert absence: naming
        # one of these with "allow" is the edit this test exists to catch.
        for name, spec in self.CONFIG["agent"].items():
            for tool in ("bash", "webfetch", "websearch"):
                with self.subTest(agent=name, tool=tool):
                    self.assertNotEqual(spec["permission"].get(tool), "allow")


class APreflightRefusesAnUnresolvedRestriction(unittest.TestCase):
    """Selecting a restriction by name is safe only if a bad name stops the run.

    A harness that treats an unresolved name as a warning and continues under
    an unrestricted default turns one wrong character into full tool access,
    signalled by nothing the loop reads. The preflight is what converts that
    into a refusal, so it must run before the agent and its failure must be
    fatal.
    """

    def _agent_with_preflight_exit(self, code: int):
        calls = []

        def runner(argv, env, cwd):
            calls.append(list(argv))
            failed = code if list(argv)[:2] == ["h", "check"] else 0
            return AgentRun(stdout='```json\n{"ok": true}\n```', returncode=failed)

        harness = HarnessConfig(
            name="named-restriction",
            install=("pkg", "i", "h"),
            argv=("h", "run", "{prompt}"),
            role_argv={role: ("--agent", "restricted") for role in AgentRole},
            preflight_argv={role: ("h", "check", "restricted") for role in AgentRole},
        )
        return calls, ConfiguredAgent(
            harness, ModelConfig(model="m", auth_token="k", auth_env="K"), runner=runner
        )

    def test_a_failing_preflight_refuses_the_role(self):
        _, agent = self._agent_with_preflight_exit(1)
        for role in AgentRole:
            with self.subTest(role=role.value):
                with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                    agent.run("go", role, Path("/t"))

    def test_the_agent_never_runs_when_the_preflight_fails(self):
        calls, agent = self._agent_with_preflight_exit(1)
        with self.assertRaises(RuntimeError):
            agent.run("go", AgentRole.FIX, Path("/t"))
        self.assertEqual([c[:2] for c in calls], [["h", "check"]])

    def test_a_passing_preflight_lets_the_role_run(self):
        calls, agent = self._agent_with_preflight_exit(0)
        agent.run("go", AgentRole.FIX, Path("/t"))
        self.assertEqual([c[:2] for c in calls], [["h", "check"], ["h", "run"]])


class AnUnsetRepoVariableIsRefusedRatherThanForwarded(unittest.TestCase):
    """An unset GitHub repo variable expands to `""`, not to nothing.

    So `SHL_MODEL` unset does not raise a KeyError on the way in: it renders
    `--model ""` or sets `ANTHROPIC_MODEL=""`, and the failure surfaces as a
    provider error three steps later with nothing naming the variable. The same
    reasoning `load_adapter` already applies to `SHL_ADAPTER`.
    """

    def test_an_empty_model_names_the_variable(self):
        with self.assertRaises(ValueError) as caught:
            ModelConfig(model="", auth_token="k")
        self.assertIn("SHL_MODEL", str(caught.exception))

    def test_an_empty_auth_token_names_the_variable(self):
        with self.assertRaises(ValueError) as caught:
            ModelConfig(model="m", auth_token="")
        self.assertIn("SHL_AUTH_TOKEN", str(caught.exception))

    def test_the_optional_fields_stay_optional(self):
        # base_url and auth_env are legitimately empty on a native provider;
        # refusing those would break the primary path.
        ModelConfig(model="m", auth_token="k")

    def test_install_pins_an_exact_version(self):
        """Headless `run` is broken on some released builds, so the pin is real.

        Asserted as a version NUMBER rather than as the presence of an `@`.
        `any("@" in part)` is true of `@latest`, of `@next`, and of every scoped
        package name — so the check passed while the install was free to fetch
        whatever build shipped that morning, which is the exact failure the pin
        exists to prevent.
        """
        self.assertRegex(
            OPENCODE.install[-1],
            r"@\d+\.\d+\.\d+$",
            f"OpenCode install must pin an exact version: {OPENCODE.install}",
        )

    def test_a_floating_tag_is_not_a_pin(self):
        # The distinction the assertion above turns on, stated as its own case
        # so a future loosening has to argue with it directly.
        for floating in ("opencode-ai@latest", "opencode-ai@next", "@scope/opencode-ai"):
            with self.subTest(spec=floating):
                self.assertNotRegex(floating, r"@\d+\.\d+\.\d+$")

    def test_argv_carries_no_flag_the_pinned_cli_rejects(self):
        """`--auto` does not exist on the pinned CLI, and the docs still show it.

        The pinned CLI spells it --dangerously-skip-permissions, so a config
        copied from OpenCode's own documentation carries a flag this version
        does not accept. The framework wants neither name: --auto's whole job
        is auto-approving everything not explicitly denied, and the deny set it
        pairs with does not load at all.
        """
        self.assertNotIn("--auto", OPENCODE.argv)


class ClaimedHarnessesAreShippedHarnesses(unittest.TestCase):
    """Naming a harness we do not ship is a promise the registry cannot keep."""

    _FRAMEWORK = Path(__file__).resolve().parents[1]
    # The developer-facing three, plus every file an OPERATOR reads to decide
    # which harness to pick. Naming an unshipped one there is the version that
    # actually costs something: it is read while making the choice.
    PROSE = (
        _FRAMEWORK / "README.md",
        _FRAMEWORK / "agent" / "harness.py",
        _FRAMEWORK / "agent" / "base.py",
        _FRAMEWORK / "SKILL.md",
        _FRAMEWORK / "reference" / "harnesses.md",
        _FRAMEWORK / "artifacts" / "setup.md",
        _FRAMEWORK / "loop_context" / "CLAUDE.md",
    )
    # Harnesses that exist in the world but have no HarnessConfig here.
    UNSHIPPED = ("codex", "hermes", "aider", "cursor")

    def test_a_harness_with_no_auth_default_is_named_where_that_variable_is_set(self):
        """`SHL_AUTH_ENV` is skippable only where the harness supplies a default.

        `render()` resolves `m.auth_env or h.auth_env` and sets the token only
        `if auth_env`. `CLAUDE_CODE.auth_env` is `ANTHROPIC_AUTH_TOKEN`;
        `OPENCODE.auth_env` is the empty string. So "skip both lines on the
        harness's native provider" was true for one harness and left the other
        running with NO credential in the agent's environment — and an
        unauthenticated run hangs to the job timeout rather than failing, which
        is the hardest symptom this project has to trace.

        The membership is computed from the registry, so a third harness with no
        default inherits the requirement instead of being forgotten.
        """
        setup = (self._FRAMEWORK / "artifacts" / "setup.md").read_text(encoding="utf-8")
        lines = setup.splitlines()
        # The comment block immediately above the command, not every line
        # mentioning the name: guidance is prose across several lines and only
        # the first repeats the variable.
        anchor = next(
            i for i, line in enumerate(lines)
            if line.startswith("gh variable set SHL_AUTH_ENV")
        )
        guidance = "\n".join(lines[max(0, anchor - 4): anchor + 1]).lower()
        self.assertTrue(guidance, "artifacts/setup.md never mentions SHL_AUTH_ENV")
        defaultless = [name for name, h in REGISTRY.items() if not h.auth_env]
        self.assertTrue(
            defaultless,
            "every harness now has an auth default, so this check proves nothing; "
            "delete it or re-derive what it should assert",
        )
        for name in defaultless:
            with self.subTest(harness=name):
                self.assertIn(
                    name.lower(), guidance,
                    f"'{name}' supplies no auth_env default, so SHL_AUTH_ENV is "
                    f"mandatory there — setup.md's guidance must say so by name "
                    f"rather than calling the variable optional in general",
                )

    def test_no_unshipped_harness_is_named_as_supported(self):
        for path in self.PROSE:
            text = path.read_text(encoding="utf-8").lower()
            for name in self.UNSHIPPED:
                if name in REGISTRY:
                    continue
                with self.subTest(file=path.name, harness=name):
                    self.assertNotIn(
                        name, text, f"{path.name} names '{name}', which REGISTRY does not ship"
                    )


class TestConfiguredAgent(unittest.TestCase):
    def test_run_substitutes_prompt_and_passes_env(self):
        calls, runner = _capture()
        m = ModelConfig(
            model="example-model-v1",
            auth_token="sk-test-key",
            base_url="https://api.example-provider.test/v1",
        )
        agent = ConfiguredAgent(CLAUDE_CODE, m, runner=runner)
        out = agent.run("diagnose this", AgentRole.DIAGNOSE, Path("/t"))

        argv, env, cwd = calls[0]
        self.assertEqual(cwd, Path("/t"))
        self.assertNotIn("{prompt}", argv)
        self.assertIn("diagnose this", argv)
        # Prompt lands in the documented spot (after -p).
        self.assertEqual(argv[argv.index("-p") + 1], "diagnose this")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-test-key")
        self.assertEqual(env["ANTHROPIC_MODEL"], "example-model-v1")
        self.assertIn("dontAsk", argv)  # read-only role
        self.assertIn('{"ok": true}', out)  # raw stdout from the runner


class TestUniversality(unittest.TestCase):
    def test_registry_lookup_by_name(self):
        self.assertIs(get_harness("claude-code"), CLAUDE_CODE)
        self.assertIs(get_harness("opencode"), OPENCODE)

    def test_unknown_harness_raises_with_known_list(self):
        with self.assertRaises(KeyError) as cm:
            get_harness("nope")
        self.assertIn("claude-code", str(cm.exception))

    def test_third_harness_via_data_no_new_code(self):
        # Adding a harness = authoring a HarnessConfig, zero framework code.
        # A made-up harness proves the mechanism is not coupled to the two
        # shipped examples.
        fictional = HarnessConfig(
            name="fictional",
            install=("pkg", "i", "fictional-agent"),
            argv=("fictional", "ask", "--llm", "{model}", "{prompt}"),
            role_argv={AgentRole.FIX: ("--can-edit",)},
            model_env=("FICTIONAL_MODEL",),
            auth_env="FICTIONAL_KEY",
        )
        m = ModelConfig(model="fm-1", auth_token="fk")
        argv, env = render(fictional, m, AgentRole.FIX)
        self.assertEqual(env, {"FICTIONAL_KEY": "fk", "FICTIONAL_MODEL": "fm-1"})
        self.assertEqual(argv, ["fictional", "ask", "--llm", "fm-1", "{prompt}", "--can-edit"])


class TheDenialGuardComparesToolsNotSubstrings(unittest.TestCase):
    """A scoped deny is not a deny, and a substring test cannot tell them apart.

    The guard exists to answer "does this argv actually restrict the role", and
    its own docstring says a non-empty config proves someone filled it in rather
    than that the values restrict anything. Matching `tool in value` reintroduces
    exactly that: `Bash` is a substring of `Bash(rm:*)`, of `BashOutput`, and of
    any comment mentioning bash — so narrowing the deny, which is the natural
    edit when a role seems over-restricted, leaves the guard satisfied while the
    Fix role holds both the provider token and `GH_TOKEN` with a live shell.
    """

    def _fix_argv(self):
        return list(render(CLAUDE_CODE, ModelConfig(model="m", auth_token="k"), AgentRole.FIX)[0])

    def _with_deny(self, value):
        flag, _ = CLAUDE_CODE.required_denial
        argv = self._fix_argv()
        argv[argv.index(flag) + 1] = value
        return argv

    def setUp(self):
        self.agent = ConfiguredAgent(CLAUDE_CODE, ModelConfig(model="m", auth_token="k"))

    def test_the_shipped_argv_still_passes(self):
        self.agent._assert_denies_what_it_claims(AgentRole.FIX, self._fix_argv())

    def test_a_scoped_bash_deny_is_refused(self):
        argv = self._with_deny("Bash(rm:*),WebFetch,WebSearch,Agent,Edit(./**)")
        with self.assertRaises(RuntimeError) as cm:
            self.agent._assert_denies_what_it_claims(AgentRole.FIX, argv)
        self.assertIn("Bash", str(cm.exception))

    def test_a_longer_tool_name_does_not_stand_in_for_the_short_one(self):
        argv = self._with_deny("BashOutput,WebFetch,WebSearch,Agent,Edit(./**)")
        with self.assertRaises(RuntimeError):
            self.agent._assert_denies_what_it_claims(AgentRole.FIX, argv)

    def test_prose_mentioning_a_tool_does_not_count_as_denying_it(self):
        argv = self._with_deny("we should probably deny Bash and WebFetch and WebSearch and Agent and Edit(./**)")
        with self.assertRaises(RuntimeError):
            self.agent._assert_denies_what_it_claims(AgentRole.FIX, argv)

    def test_whitespace_around_a_real_entry_is_still_a_real_entry(self):
        argv = self._with_deny(" Bash , WebFetch ,WebSearch, Agent ,Edit(./**) ")
        self.agent._assert_denies_what_it_claims(AgentRole.FIX, argv)


class EveryHarnessInstallsAPinnedVersion(unittest.TestCase):
    """An unpinned install makes the loop's behaviour a function of the date.

    The runner installs the harness fresh on every cycle, so an unpinned spec
    means a cycle that worked yesterday can break today with nothing in this
    repo having changed — and the only evidence of what ran is whatever the
    Actions log happened to print. That is the hardest class of failure to
    diagnose here, because the framework's whole diagnostic story assumes the
    tree explains the behaviour.

    The pins also carry a real cost, stated so it is a choice rather than an
    oversight: nothing here bumps them, so upgrading is a manual edit. Argv
    flags and permission keys are what break across harness releases, and both
    are exactly what this framework depends on.
    """

    def test_no_install_spec_floats(self):
        for name, harness in REGISTRY.items():
            with self.subTest(harness=name):
                package = harness.install[-1]
                self.assertRegex(
                    package, r".+@\d+\.\d+\.\d+$",
                    f"{name} installs {package!r}, which resolves to whatever "
                    "the registry serves that morning",
                )


if __name__ == "__main__":
    unittest.main()
