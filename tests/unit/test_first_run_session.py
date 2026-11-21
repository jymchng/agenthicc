"""TUX-03 §2.3, §2.4, §2.1: what the session prints and asks before the first prompt."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.plugins.trust import current_extension_trust
from agenthicc.runners.tui_session import TUISession
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime.commands import SendMessageCommand
from agenthicc.tui.workspace.overlays.workspace_trust import WorkspaceTrustOverlay
from agenthicc.user_state import UserState, save_user_state
from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.unit

_MISSING_KEY = (
    "No API key for anthropic. Set ANTHROPIC_API_KEY, or run /model to pick another provider."
)


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home of its own and a git repository with an AGENTS.md as the working directory."""
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", sessions)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSION_INDEX", sessions / "index.json")
    monkeypatch.setattr("agenthicc.cli.version.package_version", lambda: "0.5.0")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    # The pre-flight ripgrep notice must not depend on the host: a stub ``rg`` on PATH.
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    (stub_bin / "rg").write_text("#!/bin/sh\n", encoding="utf-8")
    (stub_bin / "rg").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(stub_bin), os.environ.get("PATH", ""))))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    monkeypatch.chdir(project)
    return project


def _session(*, width: int = 100) -> tuple[TUISession, object]:
    session, ctx, _workspace, _input = _make_session()
    assert isinstance(session, TUISession)
    ctx.console = Console(file=io.StringIO(), width=width, record=True, force_terminal=False)
    ctx.model_label = "anthropic/claude-opus-4-8"
    input_session = UnifiedInputSession(ctx.app_state, ctx.command_bus)  # type: ignore[attr-defined]
    input_session.request_exit = lambda: None  # the real one restores the terminal
    session._input_session = input_session  # type: ignore[assignment]
    return session, ctx


def _printed(ctx: object) -> str:
    return ctx.console.export_text(clear=False)  # type: ignore[attr-defined]


# ── the header ────────────────────────────────────────────────────────────────


async def test_the_header_is_three_lines_then_the_missing_key_notice(machine: Path) -> None:
    save_user_state(UserState(version="0.5.0", release_notes_seen="0.5.0"))
    session, ctx = _session()

    await session._show_welcome()

    rows = [row for row in _printed(ctx).splitlines() if row.strip()]
    assert rows == [
        " ◆ agenthicc 0.5.0",
        "   opus-4.8 · anthropic · Safe mode",
        f"   {machine}  (main)",
        f" ! {_MISSING_KEY}",
    ]


async def test_the_first_run_shows_the_mascot_and_a_later_run_does_not(machine: Path) -> None:
    first, first_ctx = _session()
    await first._show_welcome()
    later, later_ctx = _session()
    await later._show_welcome()

    assert "state-driven agent operating system" in _printed(first_ctx)
    assert "state-driven agent operating system" not in _printed(later_ctx)


async def test_banner_off_prints_nothing_before_the_composer_except_a_failed_check(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    monkeypatch.setenv("PATH", str(machine))  # no rg: that finding is an info notice too
    session, ctx = _session()
    ctx.cfg.ui = replace(ctx.cfg.ui, banner="off")  # type: ignore[attr-defined]

    await session._show_welcome()

    printed = [row for row in _printed(ctx).splitlines() if row.strip()]
    assert not any("agenthicc 0.5.0" in row for row in printed)
    assert printed[0].strip() == f"! {_MISSING_KEY}"


async def test_banner_off_with_every_check_passing_prints_nothing_at_all(
    machine: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "rg").write_text("#!/bin/sh\n", encoding="utf-8")
    (tools / "rg").chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    session, ctx = _session()
    ctx.cfg.ui = replace(ctx.cfg.ui, banner="off")  # type: ignore[attr-defined]

    await session._show_welcome()

    assert _printed(ctx) == ""


async def test_with_every_check_passing_the_header_is_followed_by_nothing(
    machine: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "rg").write_text("#!/bin/sh\n", encoding="utf-8")
    (tools / "rg").chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    session, ctx = _session()

    await session._show_welcome()

    assert "!" not in _printed(ctx)
    assert session._credential_issue is None


async def test_the_session_records_a_preflight_phase_and_the_suggestion(machine: Path) -> None:
    from agenthicc.runners.startup import StartupCoordinator

    session, ctx = _session()
    ctx.startup = StartupCoordinator()  # type: ignore[attr-defined]

    await session._show_welcome()

    report = ctx.startup.report("preflight")  # type: ignore[attr-defined]
    assert report.state.value == "ready"
    assert report.elapsed_s is not None and report.elapsed_s < 1.0
    suggestion = ctx.app_state.conversation.suggestion()  # type: ignore[attr-defined]
    assert suggestion is not None and suggestion.kind == "tip"


async def test_a_failing_preflight_degrades_the_phase_and_never_the_session(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.runners.startup import StartupCoordinator

    def explode(**_kwargs: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("agenthicc.runners.first_run.prepare_first_run", explode)
    session, ctx = _session()
    ctx.startup = StartupCoordinator()  # type: ignore[attr-defined]

    await session._show_welcome()

    assert ctx.startup.report("preflight").state.value == "degraded"  # type: ignore[attr-defined]
    assert _printed(ctx) == ""


# ── AC 2: no start-up path reaches the network ────────────────────────────────


async def test_starting_up_makes_no_network_request(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    calls: list[str] = []

    def refuse(*args: object, **kwargs: object) -> None:
        calls.append("http")
        raise AssertionError("start-up must not make a network request")

    monkeypatch.setattr("agenthicc.tools.http.agenthicc_http_client", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    session, _ctx = _session()

    await session._show_welcome()

    assert calls == []


# ── the credential gate ───────────────────────────────────────────────────────


async def test_a_prompt_with_no_key_is_not_sent_and_opens_model(machine: Path) -> None:
    session, ctx = _session()
    dispatched: list[str] = []
    session.dispatch_slash = lambda text: dispatched.append(text) or True  # type: ignore[method-assign]
    await session._show_welcome()
    before = _printed(ctx).count(_MISSING_KEY)

    await session.handle_send(SendMessageCommand(text="hello"))

    assert session._agent_task is None
    assert dispatched == ["/model"]
    # What was typed is kept in the prompt for the next try.
    assert "".join(ctx.app_state.input.buf()) == "hello"  # type: ignore[attr-defined]
    assert _printed(ctx).count(_MISSING_KEY) == before + 1  # the same notice, again


async def test_slash_commands_are_not_held_by_the_gate(machine: Path) -> None:
    session, _ctx = _session()
    await session._show_welcome()

    assert not session._blocked_by_credentials("/status")
    assert not session._blocked_by_credentials("/model anthropic")


async def test_a_key_that_appears_unblocks_the_prompt(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _ctx = _session()
    await session._show_welcome()
    assert session._credential_issue is not None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    assert not session._blocked_by_credentials("hello")
    assert session._credential_issue is None


async def test_switching_to_a_provider_that_needs_no_key_unblocks_the_prompt(
    machine: Path,
) -> None:
    session, ctx = _session()
    await session._show_welcome()
    ctx.cfg.execution.provider = "ollama"  # type: ignore[attr-defined]

    assert not session._blocked_by_credentials("hello")


async def test_a_session_whose_checks_passed_never_holds_a_prompt(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    session, _ctx = _session()
    await session._show_welcome()

    assert not session._blocked_by_credentials("hello")


# ── /init goes away once AGENTS.md exists ─────────────────────────────────────


async def test_the_init_suggestion_goes_when_agents_md_appears(
    machine: Path,
) -> None:
    (machine / "AGENTS.md").unlink()
    session, ctx = _session()
    await session._show_welcome()
    conversation = ctx.app_state.conversation  # type: ignore[attr-defined]
    assert conversation.suggestion().kind == "init"

    (machine / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    session._refresh_suggestion()

    assert conversation.suggestion().kind == "tip"


# ── "Work in <directory>?" ────────────────────────────────────────────────────


@pytest.fixture
def downloads(machine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home" / "Downloads"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


async def test_an_unknown_directory_opens_the_dialog_once(downloads: Path) -> None:
    session, ctx = _session()
    await session._show_welcome()

    overlay = session._workspace.overlays.widget
    assert isinstance(overlay, WorkspaceTrustOverlay)
    assert overlay.dialog.title == "Work in ~/Downloads?"

    session._workspace.overlays.handle_key(Key.CHAR, "1")  # Yes
    assert session._workspace.overlays.widget is None
    decision = current_extension_trust().workspace_decision(downloads)
    assert decision is not None and decision.decision == "allow"

    later, _later_ctx = _session()
    await later._show_welcome()
    assert later._workspace.overlays.widget is None


async def test_yes_in_plan_mode_switches_the_session_to_plan(downloads: Path) -> None:
    session, ctx = _session()
    await session._show_welcome()

    session._workspace.overlays.handle_key(Key.CHAR, "2")

    assert ctx.app_state.active_mode().name == "Plan"  # type: ignore[attr-defined]
    decision = current_extension_trust().workspace_decision(downloads)
    assert decision is not None and decision.decision == "plan"


async def test_a_remembered_plan_answer_starts_the_next_session_in_plan_mode(
    downloads: Path,
) -> None:
    current_extension_trust().remember_workspace(downloads, "plan")
    session, ctx = _session()

    await session._show_welcome()

    assert ctx.app_state.active_mode().name == "Plan"  # type: ignore[attr-defined]
    assert session._workspace.overlays.widget is None


async def test_not_now_remembers_nothing(downloads: Path) -> None:
    session, _ctx = _session()
    await session._show_welcome()

    session._workspace.overlays.handle_key(Key.ESC, "")

    assert session._workspace.overlays.widget is None
    assert current_extension_trust().workspace_decision(downloads) is None


async def test_choosing_another_directory_restarts_the_session_there(
    downloads: Path, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    session, _ctx = _session()
    exited: list[bool] = []
    session._input_session.request_exit = lambda: exited.append(True)  # type: ignore[method-assign]
    await session._show_welcome()
    overlays = session._workspace.overlays

    overlays.handle_key(Key.CHAR, "3")
    overlay = overlays.widget
    assert isinstance(overlay, WorkspaceTrustOverlay)
    for ch in str(other):
        overlays.handle_key(Key.CHAR, ch)
    overlays.handle_key(Key.ENTER, "")
    composer = overlay._composer
    assert composer is not None
    await composer.settle()

    assert exited == [True]
    switch = session.switch
    assert switch is not None
    assert (switch.action, switch.target) == ("directory", str(other.resolve()))
    # Nothing is remembered about the directory that was left.
    assert current_extension_trust().workspace_decision(downloads) is None


async def test_a_directory_that_does_not_exist_is_refused_in_the_dialog(
    downloads: Path, tmp_path: Path
) -> None:
    session, _ctx = _session()
    await session._show_welcome()
    overlays = session._workspace.overlays
    overlays.handle_key(Key.CHAR, "3")
    overlay = overlays.widget
    assert isinstance(overlay, WorkspaceTrustOverlay)

    for ch in str(tmp_path / "missing"):
        overlays.handle_key(Key.CHAR, ch)
    overlays.handle_key(Key.ENTER, "")
    assert overlay._composer is not None
    await overlay._composer.settle()

    assert "is not a directory" in overlay.dialog.reason
    assert session.switch is None


async def test_a_directory_switch_starts_a_new_session_there_with_its_own_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abandoned session does not reopen, and the new project's file is read."""
    from agenthicc.config import AgenthiccConfig
    from agenthicc.runners import tui_session
    from agenthicc.runners.tui_session import SessionSwitch

    calls: list[tuple[str | None, str | None, AgenthiccConfig | None]] = []

    async def fake_session(directory: str | None, **kwargs: object) -> SessionSwitch | None:
        calls.append((directory, kwargs["resume_id"], kwargs["config"]))  # type: ignore[arg-type]
        if len(calls) == 1:
            return SessionSwitch("directory", str(tmp_path), origin="first", origin_cwd="/origin")
        return None

    monkeypatch.setattr(tui_session, "_run_tui_session_in", fake_session)
    config = AgenthiccConfig()

    await tui_session._run_tui_session(config=config)

    assert calls == [(None, None, config), (str(tmp_path), None, None)]


async def test_a_screen_reader_never_gets_the_mascot(machine: Path) -> None:
    from dataclasses import replace

    session, ctx = _session()
    ctx.cfg.ui = replace(ctx.cfg.ui, screen_reader=True)  # type: ignore[attr-defined]

    await session._show_welcome()  # a first run: the mascot would show

    printed = _printed(ctx)
    assert "state-driven" not in printed and "o.o" not in printed
    assert "Warning: No API key for anthropic." in printed  # the severity is a word, not a glyph


async def test_a_scheduled_prompt_is_not_held_by_the_gate(machine: Path) -> None:
    """A `/loop` or goal prompt fails as a turn; it must not open `/model` every interval."""
    session, _ctx = _session()
    asked: list[str] = []
    session._blocked_by_credentials = lambda text: asked.append(text) or True  # type: ignore[method-assign]
    session.route = lambda text: True  # type: ignore[method-assign]

    await session.handle_send(SendMessageCommand(text="again", source="loop"))
    assert asked == []

    await session.handle_send(SendMessageCommand(text="hello"))
    assert asked == ["hello"]
