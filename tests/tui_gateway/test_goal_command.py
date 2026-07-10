"""Tests for /goal handling in tui_gateway.

The TUI routes ``/goal`` through ``command.dispatch`` (not ``slash.exec``)
because the CLI's ``_handle_goal_command`` queues the kickoff message onto
``_pending_input``, which the slash-worker subprocess has no reader for.
Instead we handle ``/goal`` directly in the server and return a
``{"type": "send", "notice": ..., "message": ...}`` payload the TUI client
uses to render a system line and fire the kickoff prompt.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Bust the goal-module DB cache so it re-resolves HERMES_HOME.
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        # Reset module-level session state without re-importing. importlib.reload
        # would re-register the module's atexit hooks (ThreadPoolExecutor
        # shutdown, _shutdown_sessions); the duplicates race the stderr
        # buffer at interpreter shutdown and surface as Fatal Python error:
        # _enter_buffered_busy. Clearing the per-session dicts gives the
        # next test a clean slate; _methods is NOT cleared because it's
        # populated at module import time and re-registration only happens
        # via reload (which we don't do).
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def session(server):
    sid = "sid-test"
    session_key = "tui-goal-session-1"
    s = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = s
    return sid, session_key, s


def _call(server, method, **params):
    handler = server._methods[method]
    return handler(1, params)


# ── command.dispatch /goal ────────────────────────────────────────────


def test_goal_bare_shows_status_when_none_set(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_whitespace_only_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="   ", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_status_alias_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="status", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_set_returns_send_with_notice(server, session):
    sid, session_key, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="build a rocket", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "build a rocket"
    assert "notice" in result
    assert "Goal set" in result["notice"]
    assert "20-turn budget" in result["notice"]

    # Persisted in SessionDB
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_key)
    assert mgr.state is not None
    assert mgr.state.goal == "build a rocket"
    assert mgr.state.status == "active"


def test_goal_pause_after_set(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "paused" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.status == "paused"


def test_goal_resume_reactivates(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="resume", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "resumed" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.status == "active"


def test_goal_clear_removes_active_goal(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="clear", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "cleared" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    # After clear the row is marked status=cleared (kept for audit);
    # ``has_goal()`` / ``is_active()`` return False so the goal loop
    # stays off and ``status`` reports "No active goal".
    mgr = GoalManager(session_key)
    assert not mgr.has_goal()
    assert not mgr.is_active()
    assert "No active goal" in mgr.status_line()


def test_goal_stop_and_done_are_clear_aliases(server, session):
    sid, _, _ = session
    _call(server, "command.dispatch", name="goal", arg="first goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="stop", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()

    _call(server, "command.dispatch", name="goal", arg="second goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="done", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()


def test_goal_requires_session(server):
    r = _call(server, "command.dispatch", name="goal", arg="nope", session_id="unknown")
    assert "error" in r
    assert r["error"]["code"] == 4001


# ── slash.exec /goal routing ──────────────────────────────────────────


def test_slash_exec_routes_goal_to_command_dispatch(server, session):
    """slash.exec must route /goal directly to command.dispatch internally
    instead of returning an error.  Previously the 4018 error required the
    TUI client to retry via command.dispatch, but some clients failed the
    fallback, leaving the command empty ("empty command")."""
    sid, _, _ = session
    r = _call(server, "slash.exec", command="goal status", session_id=sid)
    # Should succeed by routing to command.dispatch internally
    assert "result" in r
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_pending_input_commands_includes_goal(server):
    """Guard: _PENDING_INPUT_COMMANDS must list 'goal' — removing it would
    silently re-break the TUI."""
    assert "goal" in server._PENDING_INPUT_COMMANDS


# ── command.dispatch /supergoal ───────────────────────────────────────


def test_supergoal_custom_budget_persists(server, session):
    sid, session_key, _ = session
    r = _call(
        server,
        "command.dispatch",
        name="supergoal",
        arg="--turns 80 build a rocket",
        session_id=sid,
    )

    assert r["result"]["type"] == "send"
    assert "80-turn budget" in r["result"]["notice"]

    from hermes_cli.goals import GoalManager

    state = GoalManager(session_key).state
    assert state is not None
    assert state.goal == "build a rocket"
    assert state.max_turns == 80


def test_supergoal_default_budget_is_forty(server, session):
    sid, session_key, _ = session
    _call(
        server,
        "command.dispatch",
        name="supergoal",
        arg="build a rocket",
        session_id=sid,
    )

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.max_turns == 40


def test_supergoal_inline_contract_persists(server, session):
    sid, session_key, _ = session
    r = _call(
        server,
        "command.dispatch",
        name="supergoal",
        arg="--turns=60 ship it\nverify: tests pass",
        session_id=sid,
    )

    assert r["result"]["type"] == "send"
    assert "Completion contract" in r["result"]["notice"]

    from hermes_cli.goals import GoalManager

    state = GoalManager(session_key).state
    assert state is not None
    assert state.max_turns == 60
    assert state.contract.verification == "tests pass"


def test_supergoal_draft_preserves_budget(server, session):
    sid, session_key, _ = session
    from hermes_cli.goals import GoalContract, GoalManager

    with patch(
        "hermes_cli.goals.draft_contract",
        return_value=GoalContract(verification="pytest passes"),
    ):
        r = _call(
            server,
            "command.dispatch",
            name="supergoal",
            arg="--turns 90 draft ship it",
            session_id=sid,
        )

    assert r["result"]["type"] == "send"
    state = GoalManager(session_key).state
    assert state is not None
    assert state.max_turns == 90
    assert state.contract.verification == "pytest passes"


@pytest.mark.parametrize("arg", ["80", "--turns 80", "--turns=80"])
def test_supergoal_budget_without_objective_is_rejected(server, session, arg):
    sid, session_key, _ = session
    from hermes_cli.goals import GoalManager

    # The module-level TUI fixture can reuse the same session key across
    # parametrized cases, so explicitly begin from no active goal.
    GoalManager(session_key).clear()
    r = _call(
        server,
        "command.dispatch",
        name="supergoal",
        arg=arg,
        session_id=sid,
    )

    assert r["error"]["code"] == 4004
    assert "objective" in r["error"]["message"].lower()

    assert not GoalManager(session_key).has_goal()


def test_slash_exec_routes_supergoal_to_command_dispatch(server, session):
    sid, session_key, _ = session
    r = _call(
        server,
        "slash.exec",
        command="supergoal 70 build a rocket",
        session_id=sid,
    )

    assert r["result"]["type"] == "send"

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.max_turns == 70


def test_pending_input_commands_includes_supergoal(server):
    assert "supergoal" in server._PENDING_INPUT_COMMANDS


def test_tui_catalog_lists_supergoal_once(server):
    r = _call(server, "commands.catalog")
    names = [pair[0] for pair in r["result"]["pairs"]]
    assert names.count("/supergoal") == 1


def test_supergoal_builtin_beats_legacy_skill_collision(server, session):
    sid, session_key, _ = session
    legacy = {
        "/supergoal": {
            "name": "supergoal",
            "description": "legacy one-turn skill",
            "skill_md_path": "/tmp/legacy/SKILL.md",
        }
    }

    with patch("agent.skill_commands.scan_skill_commands", return_value=legacy):
        r = _call(
            server,
            "command.dispatch",
            name="supergoal",
            arg="80 build a rocket",
            session_id=sid,
        )

    assert r["result"]["type"] == "send"
    from hermes_cli.goals import GoalManager

    state = GoalManager(session_key).state
    assert state is not None
    assert state.max_turns == 80
    assert state.goal == "build a rocket"


def test_supergoal_rejects_new_goal_while_tui_session_busy(server, session):
    sid, session_key, session_obj = session
    from hermes_cli.goals import GoalManager

    GoalManager(session_key).clear()
    session_obj["running"] = True
    r = _call(
        server,
        "command.dispatch",
        name="supergoal",
        arg="build a rocket",
        session_id=sid,
    )

    assert r["error"]["code"] == 4009
    assert not GoalManager(session_key).has_goal()


def test_supergoal_status_allowed_while_tui_session_busy(server, session):
    sid, session_key, session_obj = session
    from hermes_cli.goals import GoalManager

    GoalManager(session_key).set("build a rocket", max_turns=80)
    session_obj["running"] = True
    r = _call(
        server,
        "command.dispatch",
        name="supergoal",
        arg="status",
        session_id=sid,
    )

    assert r["result"]["type"] == "exec"
    assert "build a rocket" in r["result"]["output"]


def test_supergoal_drafting_prefix_is_plain_objective(server, session):
    sid, session_key, _ = session
    _call(
        server,
        "command.dispatch",
        name="supergoal",
        arg="drafting release notes",
        session_id=sid,
    )

    from hermes_cli.goals import GoalManager

    state = GoalManager(session_key).state
    assert state is not None
    assert state.goal == "drafting release notes"
    assert state.contract.is_empty()


# ── command.dispatch /moa ────────────────────────────────────────────

def _write_moa_config(home, text):
    cfg_path = home / "config.yaml"
    cfg_path.write_text(text)


def test_moa_bare_returns_usage(server, session, hermes_home):
    _write_moa_config(hermes_home, """
moa:
  default_preset: default
  presets:
    default:
      reference_models:
        - provider: openai-codex
          model: gpt-5.5
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""")
    sid, _, s = session
    r = _call(server, "command.dispatch", name="moa", arg="", session_id=sid)
    # Bare /moa is usage-only now; switching to a preset is via the model picker.
    assert "error" in r
    assert "model_override" not in s


def test_moa_arg_is_always_one_shot(server, session, hermes_home):
    # Any arg (even a preset name) is a one-shot prompt through the DEFAULT
    # preset; /moa never does a sticky switch anymore.
    _write_moa_config(hermes_home, """
moa:
  default_preset: default
  presets:
    default: {}
    review:
      reference_models:
        - provider: openrouter
          model: deepseek/deepseek-v4-pro
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""")
    sid, _, s = session
    r = _call(server, "command.dispatch", name="moa", arg="review", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "review"
    assert "one-shot" in result["notice"]
    # Lazy session (no live agent) → MoA preset pinned via model_override for
    # the build, and it is the DEFAULT preset, not the "review" arg.
    assert s["model_override"]["provider"] == "moa"
    assert s["model_override"]["model"] == "default"


def test_moa_non_preset_returns_one_shot_send(server, session, hermes_home):
    _write_moa_config(hermes_home, """
moa:
  default_preset: default
  presets:
    default:
      reference_models:
        - provider: openai-codex
          model: gpt-5.5
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""")
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="moa", arg="inspect this project", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "inspect this project"
    assert "one-shot" in result["notice"]


def test_pending_input_commands_includes_moa(server):
    assert "moa" in server._PENDING_INPUT_COMMANDS
