"""Tests for /supergoal — parse_supergoal_args, registry integration, and behavior."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.goals import (
    DEFAULT_SUPERGOAL_MAX_TURNS,
    parse_supergoal_args,
)
from hermes_cli.commands import (
    COMMAND_REGISTRY,
    resolve_command,
    telegram_menu_commands,
    _TG_NAME_LIMIT,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME — mirrors the fixture in test_goals.py."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


# ──────────────────────────────────────────────────────────────────────
# parse_supergoal_args
# ──────────────────────────────────────────────────────────────────────


class TestParseSupergoalArgs:
    """Verify every accepted budget form and edge case."""

    def test_empty_string_returns_defaults(self):
        text, budget, err = parse_supergoal_args("")
        assert text == ""
        assert budget == DEFAULT_SUPERGOAL_MAX_TURNS
        assert err is None

    def test_none_returns_defaults(self):
        text, budget, err = parse_supergoal_args(None)
        assert text == ""
        assert budget == DEFAULT_SUPERGOAL_MAX_TURNS
        assert err is None

    def test_plain_goal_uses_default_budget(self):
        text, budget, err = parse_supergoal_args("ship the feature")
        assert text == "ship the feature"
        assert budget == DEFAULT_SUPERGOAL_MAX_TURNS
        assert err is None

    # ── positional integer form ───────────────────────────────────────

    def test_positional_budget_with_text(self):
        text, budget, err = parse_supergoal_args("80 ship the feature")
        assert text == "ship the feature"
        assert budget == 80
        assert err is None

    def test_positional_budget_single_word_goal(self):
        text, budget, err = parse_supergoal_args("80 deploy")
        assert text == "deploy"
        assert budget == 80
        assert err is None

    def test_bare_integer_alone_is_missing_objective_error(self):
        """A lone integer is treated as a budget with no text — returns error."""
        _, _, err = parse_supergoal_args("80")
        assert err is not None
        assert "missing" in err.lower() or "objective" in err.lower()

    def test_bare_integer_error_names_the_integer(self):
        _, _, err = parse_supergoal_args("100")
        assert err is not None
        assert "100" in err

    # ── --turns N form ────────────────────────────────────────────────

    def test_turns_flag_space(self):
        text, budget, err = parse_supergoal_args("--turns 80 ship the feature")
        assert text == "ship the feature"
        assert budget == 80
        assert err is None

    def test_max_turns_flag_space(self):
        text, budget, err = parse_supergoal_args("--max-turns 80 objective")
        assert text == "objective"
        assert budget == 80
        assert err is None

    def test_short_t_flag(self):
        text, budget, err = parse_supergoal_args("-t 80 objective")
        assert text == "objective"
        assert budget == 80
        assert err is None

    def test_turns_no_dash(self):
        text, budget, err = parse_supergoal_args("turns 80 objective")
        assert text == "objective"
        assert budget == 80
        assert err is None

    def test_max_turns_no_dash(self):
        text, budget, err = parse_supergoal_args("max-turns 80 objective")
        assert text == "objective"
        assert budget == 80
        assert err is None

    # ── --turns=N form ────────────────────────────────────────────────

    def test_turns_equals(self):
        text, budget, err = parse_supergoal_args("--turns=80 ship the feature")
        assert text == "ship the feature"
        assert budget == 80
        assert err is None

    def test_max_turns_equals(self):
        text, budget, err = parse_supergoal_args("--max-turns=80 objective")
        assert text == "objective"
        assert budget == 80
        assert err is None

    def test_t_equals(self):
        text, budget, err = parse_supergoal_args("-t=80 objective")
        assert text == "objective"
        assert budget == 80
        assert err is None

    def test_turns_no_dash_equals(self):
        text, budget, err = parse_supergoal_args("turns=80 objective")
        assert text == "objective"
        assert budget == 80
        assert err is None

    # ── budget + no following text ────────────────────────────────────

    def test_turns_flag_no_goal_text_is_invalid(self):
        text, budget, err = parse_supergoal_args("--turns 80")
        assert text == ""
        assert budget == 80
        assert err is not None
        assert "objective" in err.lower()

    def test_turns_equals_no_goal_text_is_invalid(self):
        text, budget, err = parse_supergoal_args("--turns=80")
        assert text == ""
        assert budget == 80
        assert err is not None
        assert "objective" in err.lower()

    @pytest.mark.parametrize(
        "raw",
        [
            "60 ship it\nverify: tests pass",
            "--turns 60 ship it\nverify: tests pass",
            "--turns=60 ship it\nverify: tests pass",
        ],
    )
    def test_budget_forms_preserve_multiline_contract(self, raw):
        text, budget, err = parse_supergoal_args(raw)
        assert err is None
        assert budget == 60
        assert text == "ship it\nverify: tests pass"

    # ── budget validation ─────────────────────────────────────────────

    def test_budget_min_boundary(self):
        text, budget, err = parse_supergoal_args("1 objective")
        assert budget == 1
        assert err is None

    def test_budget_max_boundary(self):
        text, budget, err = parse_supergoal_args("200 objective")
        assert budget == 200
        assert err is None

    def test_budget_zero_is_invalid(self):
        _, _, err = parse_supergoal_args("--turns 0 objective")
        assert err is not None
        assert "0" in err

    def test_budget_negative_is_invalid(self):
        _, _, err = parse_supergoal_args("--turns=-5 objective")
        assert err is not None

    def test_budget_above_max_is_invalid(self):
        _, _, err = parse_supergoal_args("201 objective")
        assert err is not None

    def test_budget_non_integer_is_invalid(self):
        _, _, err = parse_supergoal_args("--turns=abc objective")
        assert err is not None

    def test_missing_value_after_turns_flag(self):
        _, _, err = parse_supergoal_args("--turns")
        assert err is not None
        assert "missing" in err.lower()

    # ── subcommand passthrough ────────────────────────────────────────

    def test_status_passthrough(self):
        text, budget, err = parse_supergoal_args("status")
        assert text == "status"
        assert budget == DEFAULT_SUPERGOAL_MAX_TURNS
        assert err is None

    def test_pause_passthrough(self):
        text, budget, err = parse_supergoal_args("pause")
        assert text == "pause"
        assert budget == DEFAULT_SUPERGOAL_MAX_TURNS
        assert err is None

    def test_draft_passthrough(self):
        text, budget, err = parse_supergoal_args("draft ship the thing")
        assert text == "draft ship the thing"
        assert budget == DEFAULT_SUPERGOAL_MAX_TURNS
        assert err is None

    def test_budget_then_subcommand(self):
        # Budget flag + a control word: budget parsed, goal_text is "status".
        text, budget, err = parse_supergoal_args("--turns=40 status")
        assert text == "status"
        assert budget == 40
        assert err is None

    def test_budget_then_draft(self):
        text, budget, err = parse_supergoal_args("--turns=80 draft ship the feature")
        assert text == "draft ship the feature"
        assert budget == 80
        assert err is None

    # ── return-tuple shape ────────────────────────────────────────────

    def test_return_tuple_has_three_elements(self):
        result = parse_supergoal_args("40 do the thing")
        assert len(result) == 3

    def test_success_error_is_none(self):
        _, _, err = parse_supergoal_args("ship the feature")
        assert err is None


# ──────────────────────────────────────────────────────────────────────
# CommandDef registry
# ──────────────────────────────────────────────────────────────────────


class TestSupergoalCommandDef:
    def test_supergoal_in_registry(self):
        names = [cmd.name for cmd in COMMAND_REGISTRY]
        assert "supergoal" in names

    def test_resolve_supergoal(self):
        cmd = resolve_command("supergoal")
        assert cmd is not None
        assert cmd.name == "supergoal"

    def test_resolve_slash_supergoal(self):
        cmd = resolve_command("/supergoal")
        assert cmd is not None
        assert cmd.name == "supergoal"

    def test_supergoal_not_cli_only(self):
        cmd = resolve_command("supergoal")
        assert not cmd.cli_only

    def test_supergoal_session_category(self):
        cmd = resolve_command("supergoal")
        assert cmd.category == "Session"

    def test_supergoal_bypasses_active_session_guard(self):
        from hermes_cli.commands import should_bypass_active_session

        assert should_bypass_active_session("supergoal") is True

    def test_legacy_skill_does_not_duplicate_autocomplete(self):
        from hermes_cli.commands import SlashCommandCompleter
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/supergoal": {"description": "legacy one-turn skill"}
            }
        )
        completions = list(
            completer.get_completions(
                Document(text="/superg"),
                CompleteEvent(completion_requested=True),
            )
        )
        assert [item.display_text for item in completions].count("/supergoal") == 1


# ──────────────────────────────────────────────────────────────────────
# Telegram menu exposure
# ──────────────────────────────────────────────────────────────────────


class TestSupergoalTelegramMenu:
    def test_supergoal_survives_thirty_command_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("")

        menu, hidden = telegram_menu_commands(max_commands=30)
        names = [name for name, _desc in menu]

        assert len(names) == 30
        assert hidden > 0
        assert "supergoal" in names
        assert names.count("supergoal") == 1

    def test_supergoal_name_within_telegram_limit(self):
        assert len("supergoal") <= _TG_NAME_LIMIT

    def test_goal_survives_thirty_command_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("")

        menu, hidden = telegram_menu_commands(max_commands=30)
        names = [name for name, _desc in menu]

        assert "goal" in names


# ──────────────────────────────────────────────────────────────────────
# Budget preservation — GoalManager level
# ──────────────────────────────────────────────────────────────────────


class TestSupergoalBudgetPreservation:
    """Budget propagates through mgr.set and persists across reload."""

    def test_custom_budget_stored(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="budget-test", default_max_turns=40)
        state = mgr.set("ship the feature", max_turns=80)
        assert state.max_turns == 80

    def test_custom_budget_persists_across_reload(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="persist-test", default_max_turns=40)
        mgr.set("ship the feature", max_turns=80)

        mgr2 = GoalManager(session_id="persist-test", default_max_turns=40)
        assert mgr2.state is not None
        assert mgr2.state.max_turns == 80

    def test_default_budget_when_max_turns_is_none(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="default-test", default_max_turns=40)
        state = mgr.set("objective", max_turns=None)
        assert state.max_turns == 40

    def test_parse_then_set_end_to_end(self, hermes_home):
        """Full parse → set pipeline: budget in args propagates to stored state."""
        from hermes_cli.goals import GoalManager
        text, budget, err = parse_supergoal_args("--turns=80 ship the feature")
        assert err is None and budget == 80
        mgr = GoalManager(session_id="e2e-test", default_max_turns=40)
        state = mgr.set(text, max_turns=budget)
        assert state.max_turns == 80
        assert state.goal == "ship the feature"

    def test_draft_path_parse_extracts_budget(self):
        """parse_supergoal_args correctly hands off budget for the draft case."""
        text, budget, err = parse_supergoal_args("--turns=80 draft ship the feature")
        assert err is None
        assert budget == 80
        assert text == "draft ship the feature"
        # The CLI handler would then call:
        #   objective = text[len("draft"):].strip()  → "ship the feature"
        #   mgr.set(objective, max_turns=80, contract=...)
        objective = text[len("draft"):].strip()
        assert objective == "ship the feature"

    def test_draft_budget_stored_via_set(self, hermes_home):
        """Verify that calling mgr.set(objective, max_turns=80, contract=None) as the
        draft path would do actually stores 80, not the manager default."""
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="draft-budget-test", default_max_turns=40)
        # Simulate what _handle_goal_draft(objective, max_turns=80) does after
        # draft_contract returns None (aux model unavailable).
        state = mgr.set("ship the feature", max_turns=80, contract=None)
        assert state.max_turns == 80

    def test_cli_draft_helper_preserves_custom_budget(self, hermes_home):
        """Exercise the actual CLI helper rather than only simulating mgr.set."""
        import queue

        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="cli-draft-budget", default_max_turns=20)
        cli = CLICommandsMixin.__new__(CLICommandsMixin)
        cli._get_goal_manager = lambda: mgr
        cli._pending_input = queue.Queue()

        with (
            patch("hermes_cli.goals.draft_contract", return_value=None),
            patch("cli._cprint"),
        ):
            cli._handle_goal_draft("ship the feature", max_turns=80)

        reloaded = GoalManager(session_id="cli-draft-budget", default_max_turns=20)
        assert reloaded.state is not None
        assert reloaded.state.max_turns == 80
        assert reloaded.state.goal == "ship the feature"

    def test_contract_stored_with_budget(self, hermes_home):
        """Inline contract fields are stored alongside a custom budget."""
        from hermes_cli.goals import GoalManager, GoalContract, parse_contract
        mgr = GoalManager(session_id="contract-test", default_max_turns=40)
        goal_raw = "ship the feature\nverify: tests pass"
        headline, contract = parse_contract(goal_raw)
        state = mgr.set(headline, max_turns=80, contract=contract)
        assert state.max_turns == 80
        assert state.has_contract()
        assert "tests pass" in state.contract.verification

    def test_cli_drafting_prefix_is_plain_objective(self, hermes_home):
        import queue

        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="cli-drafting-prefix", default_max_turns=20)
        cli = CLICommandsMixin.__new__(CLICommandsMixin)
        cli._get_goal_manager = lambda: mgr
        cli._pending_input = queue.Queue()

        with patch("cli._cprint"):
            cli._handle_supergoal_command("/supergoal drafting release notes")

        state = GoalManager("cli-drafting-prefix").state
        assert state is not None
        assert state.goal == "drafting release notes"
        assert state.contract.is_empty()


# ──────────────────────────────────────────────────────────────────────
# Gateway handler behavioral tests
# ──────────────────────────────────────────────────────────────────────


class TestGatewaySupergoalHandler:
    """Verify _handle_supergoal_command persists budget and handles control verbs.

    Uses asyncio.run() since pytest-asyncio is not installed; anyio is available
    but only for the plugin list (not the marker).  The handler is async because
    the gateway platform requires it; wrapping in asyncio.run() is the simplest
    synchronous harness.
    """

    def _make_mixin(self, mgr):
        """Return a minimal GatewaySlashCommandsMixin stub backed by ``mgr``."""
        import asyncio
        from gateway.slash_commands import GatewaySlashCommandsMixin

        mixin = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
        mixin.adapters = {}
        mixin._get_goal_manager_for_event = lambda e: (mgr, None)
        mixin._session_key_for_source = lambda s: None
        mixin._enqueue_fifo = lambda key, ev, ad: None
        mixin._clear_goal_pending_continuations = lambda key, adapter: None
        return mixin

    def _make_event(self, args: str):
        ev = MagicMock()
        ev.get_command_args.return_value = args
        ev.source = None
        ev.message_id = None
        ev.channel_prompt = None
        return ev

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_budget_persists_through_gateway_handler(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-budget", default_max_turns=40)
        mixin = self._make_mixin(mgr)
        event = self._make_event("--turns=80 ship the feature")

        self._run(mixin._handle_supergoal_command(event))

        reloaded = GoalManager(session_id="gw-budget", default_max_turns=40)
        assert reloaded.state is not None
        assert reloaded.state.max_turns == 80
        assert reloaded.state.goal == "ship the feature"

    def test_positional_budget_persists(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-pos", default_max_turns=40)
        mixin = self._make_mixin(mgr)
        event = self._make_event("100 deploy the service")

        self._run(mixin._handle_supergoal_command(event))

        reloaded = GoalManager(session_id="gw-pos", default_max_turns=40)
        assert reloaded.state.max_turns == 100

    def test_default_budget_when_no_specifier(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-def", default_max_turns=40)
        mixin = self._make_mixin(mgr)
        event = self._make_event("ship the feature")

        self._run(mixin._handle_supergoal_command(event))

        reloaded = GoalManager(session_id="gw-def", default_max_turns=40)
        assert reloaded.state.max_turns == DEFAULT_SUPERGOAL_MAX_TURNS

    def test_pause_control_verb(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-pause", default_max_turns=40)
        mgr.set("ship the feature", max_turns=80)
        mixin = self._make_mixin(mgr)
        event = self._make_event("pause")

        result = self._run(mixin._handle_supergoal_command(event))

        assert "paused" in result.lower()
        reloaded = GoalManager(session_id="gw-pause", default_max_turns=40)
        assert reloaded.state.status == "paused"

    def test_clear_control_verb(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-clear", default_max_turns=40)
        mgr.set("ship the feature")
        mixin = self._make_mixin(mgr)
        event = self._make_event("clear")

        self._run(mixin._handle_supergoal_command(event))

        reloaded = GoalManager(session_id="gw-clear", default_max_turns=40)
        assert reloaded.state is None or reloaded.state.status == "cleared"

    def test_status_returns_status_line(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-status", default_max_turns=40)
        mgr.set("ship the feature", max_turns=80)
        mixin = self._make_mixin(mgr)
        event = self._make_event("status")

        result = self._run(mixin._handle_supergoal_command(event))
        assert "ship the feature" in result

    def test_show_includes_contract(self, hermes_home):
        from hermes_cli.goals import GoalManager, GoalContract
        mgr = GoalManager(session_id="gw-show", default_max_turns=40)
        contract = GoalContract(verification="tests pass")
        mgr.set("ship the feature", max_turns=80, contract=contract)
        mixin = self._make_mixin(mgr)
        event = self._make_event("show")

        result = self._run(mixin._handle_supergoal_command(event))
        assert "tests pass" in result

    def test_budget_error_returns_error(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-err", default_max_turns=40)
        mixin = self._make_mixin(mgr)
        event = self._make_event("--turns=999 objective")

        result = self._run(mixin._handle_supergoal_command(event))
        assert "999" in result or "range" in result or "supergoal" in result.lower()

    def test_contract_persists_via_inline_fields(self, hermes_home):
        from hermes_cli.goals import GoalManager
        mgr = GoalManager(session_id="gw-contract", default_max_turns=40)
        mixin = self._make_mixin(mgr)
        event = self._make_event("--turns=60 ship the feature\nverify: tests pass")

        self._run(mixin._handle_supergoal_command(event))

        reloaded = GoalManager(session_id="gw-contract", default_max_turns=40)
        assert reloaded.state.max_turns == 60
        assert reloaded.state.has_contract()
        assert "tests pass" in reloaded.state.contract.verification

    def test_drafting_prefix_is_plain_objective(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="gw-drafting-prefix", default_max_turns=40)
        mixin = self._make_mixin(mgr)
        event = self._make_event("drafting release notes")

        self._run(mixin._handle_supergoal_command(event))

        state = GoalManager("gw-drafting-prefix").state
        assert state is not None
        assert state.goal == "drafting release notes"
        assert state.contract.is_empty()
