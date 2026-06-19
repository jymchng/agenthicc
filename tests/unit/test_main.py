"""Unit tests for __main__ (PRD-10 session continuity, auth subcommands) — covers 0% file."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Autouse fixture: redirect session paths to tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path, monkeypatch):
    import agenthicc.sessions as s
    monkeypatch.setattr(s, "_SESSION_INDEX", tmp_path / "sessions.json")
    monkeypatch.setattr(s, "_SESSIONS_DIR", tmp_path / "sessions")


# ---------------------------------------------------------------------------
# TestSessionIndex
# ---------------------------------------------------------------------------

class TestSessionIndex:
    def test_load_empty_index(self, tmp_path):
        """_SESSION_INDEX doesn't exist → returns {}"""
        from agenthicc.sessions import _load_session_index
        result = _load_session_index()
        assert result == {}

    def test_load_corrupt_json_returns_empty(self, tmp_path, monkeypatch):
        """A corrupt JSON file in _SESSION_INDEX returns {} instead of raising."""
        import agenthicc.sessions as s
        # Write invalid JSON to the session index file
        s._SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
        s._SESSION_INDEX.write_text("{not valid json!!!")
        result = s._load_session_index()
        assert result == {}

    def test_register_and_load(self, tmp_path):
        """_register_session saves an entry with cwd and log_path."""
        from agenthicc.sessions import _register_session, _load_session_index
        session_id = uuid.uuid4().hex
        _register_session(session_id)

        index = _load_session_index()
        assert session_id in index
        entry = index[session_id]
        assert "cwd" in entry
        assert "log_path" in entry
        assert session_id in entry["log_path"]

    def test_find_latest_for_cwd(self, tmp_path):
        """Register two sessions for this cwd → _find_latest returns the more recent one."""
        from agenthicc.sessions import (
            _register_session,
            _find_latest_session_for_cwd,
            _load_session_index,
            _save_session_index,
        )
        cwd = os.getcwd()
        sid_old = uuid.uuid4().hex
        sid_new = uuid.uuid4().hex

        _register_session(sid_old)
        # Force older last_used by adjusting the index directly
        index = _load_session_index()
        index[sid_old]["last_used"] = time.time() - 100
        _save_session_index(index)

        _register_session(sid_new)

        result = _find_latest_session_for_cwd()
        assert result == sid_new

    def test_find_latest_returns_none_if_no_match(self, monkeypatch):
        """cwd doesn't match any entry → returns None."""
        from agenthicc.sessions import _find_latest_session_for_cwd, _save_session_index

        # Populate index with a different cwd
        _save_session_index({
            "abc": {"cwd": "/some/other/directory", "last_used": time.time(), "log_path": "/x"}
        })
        # Mock os.getcwd to something that won't match
        monkeypatch.setattr("agenthicc.sessions.os.getcwd", lambda: "/nonexistent/cwd/xyz")
        result = _find_latest_session_for_cwd()
        assert result is None

    def test_touch_updates_last_used(self):
        """Register, touch, verify last_used updated."""
        from agenthicc.sessions import (
            _register_session,
            _touch_session,
            _load_session_index,
            _save_session_index,
        )
        sid = uuid.uuid4().hex
        _register_session(sid)

        # Push last_used way back
        index = _load_session_index()
        old_ts = time.time() - 9999
        index[sid]["last_used"] = old_ts
        _save_session_index(index)

        _touch_session(sid)
        index2 = _load_session_index()
        assert index2[sid]["last_used"] > old_ts

    def test_touch_unknown_session_is_noop(self):
        """_touch_session on unknown id silently does nothing."""
        from agenthicc.sessions import _touch_session, _load_session_index
        _touch_session("does-not-exist")
        # Should not raise and index should remain empty
        assert _load_session_index() == {}

    def test_get_session_log_path(self):
        """Registered session → returns Path; unknown → None."""
        from agenthicc.sessions import (
            _register_session,
            _get_session_log_path,
        )
        sid = uuid.uuid4().hex
        _register_session(sid)

        path = _get_session_log_path(sid)
        assert path is not None
        assert isinstance(path, Path)
        assert sid in str(path)

        unknown = _get_session_log_path("nonexistent-id")
        assert unknown is None

    def test_sessions_saved_as_json(self, tmp_path):
        """File is valid JSON after _register_session."""
        import agenthicc.sessions as s
        sid = uuid.uuid4().hex
        s._register_session(sid)
        raw = s._SESSION_INDEX.read_text()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert sid in parsed


# ---------------------------------------------------------------------------
# TestMainParseArgs
# ---------------------------------------------------------------------------

class TestMainParseArgs:
    def test_headless_flag(self):
        with patch("sys.argv", ["agenthicc", "--headless"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert args.headless is True

    def test_continue_flag(self):
        with patch("sys.argv", ["agenthicc", "--continue"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert args.continue_session is True

    def test_resume_flag(self):
        with patch("sys.argv", ["agenthicc", "--resume", "abc123"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert args.resume == "abc123"

    def test_version(self):
        with patch("sys.argv", ["agenthicc", "--version"]):
            from agenthicc.cli.parser import _parse_args
            with pytest.raises(SystemExit) as exc_info:
                _parse_args()
        assert exc_info.value.code == 0

    def test_login_command(self):
        with patch("sys.argv", ["agenthicc", "login"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert getattr(args, "_entry", None) is not None
        assert args._entry.path == ("login",)

    def test_logout_command(self):
        with patch("sys.argv", ["agenthicc", "logout"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert getattr(args, "_entry", None) is not None
        assert args._entry.path == ("logout",)

    def test_whoami_command(self):
        with patch("sys.argv", ["agenthicc", "whoami"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert getattr(args, "_entry", None) is not None
        assert args._entry.path == ("whoami",)

    def test_sessions_list_command(self):
        with patch("sys.argv", ["agenthicc", "sessions", "list"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert getattr(args, "_entry", None) is not None
        assert args._entry.path == ("sessions", "list")

    def test_no_args_default_values(self):
        with patch("sys.argv", ["agenthicc"]):
            from agenthicc.cli.parser import _parse_args
            args = _parse_args()
        assert args.headless is False
        assert args.continue_session is False
        assert args.resume is None
        assert getattr(args, "_entry", None) is None


# ---------------------------------------------------------------------------
# TestMainDispatching
# ---------------------------------------------------------------------------

class TestMainDispatching:
    def test_main_login_dispatches_via_registry(self, monkeypatch):
        """main() with 'login' dispatches to the login handler via the registry."""
        import agenthicc.__main__ as m

        called = []

        def fake_call(entry, ctx, ns):
            called.append(entry.path)

        monkeypatch.setattr(m, "_call", fake_call)

        with patch("sys.argv", ["agenthicc", "login"]):
            m.main()

        assert any("login" in p for p in called)

    def test_main_logout_dispatches_via_registry(self, monkeypatch):
        """main() with 'logout' dispatches to the logout handler via the registry."""
        import agenthicc.__main__ as m

        called = []

        def fake_call(entry, ctx, ns):
            called.append(entry.path)

        monkeypatch.setattr(m, "_call", fake_call)

        with patch("sys.argv", ["agenthicc", "logout"]):
            m.main()

        assert any("logout" in p for p in called)

    def test_main_whoami_dispatches_via_registry(self, monkeypatch):
        """main() with 'whoami' dispatches to the whoami handler via the registry."""
        import agenthicc.__main__ as m

        called = []

        def fake_call(entry, ctx, ns):
            called.append(entry.path)

        monkeypatch.setattr(m, "_call", fake_call)

        with patch("sys.argv", ["agenthicc", "whoami"]):
            m.main()

        assert any("whoami" in p for p in called)

    def test_main_headless_calls_run_headless(self, monkeypatch):
        """main() with --headless calls asyncio.run(_run_headless(ctx))."""
        import agenthicc.__main__ as m

        called = []

        async def fake_run_headless(ctx=None):
            called.append(ctx)

        monkeypatch.setattr(m, "_run_headless", fake_run_headless)

        coroutines_run = []

        def capturing_asyncio_run(coro):
            coroutines_run.append(type(coro).__name__)
            coro.close()

        with patch("sys.argv", ["agenthicc", "--headless"]):
            with patch("agenthicc.__main__.asyncio.run", side_effect=capturing_asyncio_run):
                m.main()
        assert len(coroutines_run) == 1

    def test_main_no_command_runs_tui(self, monkeypatch):
        """main() with no command calls _run_tui(ctx) with a CLIContext."""
        import agenthicc.__main__ as m
        from agenthicc.cli.context import CLIContext

        called_with = []
        monkeypatch.setattr(m, "_run_tui", lambda ctx: called_with.append(ctx))

        with patch("sys.argv", ["agenthicc"]):
            m.main()
        assert len(called_with) == 1
        assert isinstance(called_with[0], CLIContext)


# ---------------------------------------------------------------------------
# TestDoWhoami
# ---------------------------------------------------------------------------

class TestDoWhoami:
    def test_whoami_when_logged_in(self, capsys, monkeypatch):
        """Mocked AuthClient.current_bundle() returns a bundle; email is printed."""
        from agenthicc.cli.commands.auth import whoami
        from agenthicc.cli.context import CLIContext
        from agenthicc.auth import TokenBundle

        bundle = TokenBundle(
            access_token="tok",
            refresh_token="ref",
            expires_at=time.time() + 3600,
            plan="pro",
            email="alice@example.com",
            user_id="u1",
        )

        mock_client = MagicMock()
        mock_client.current_bundle.return_value = bundle

        with patch("agenthicc.auth.AuthClient", return_value=mock_client):
            whoami(CLIContext())

        captured = capsys.readouterr()
        assert "alice@example.com" in captured.out
        assert "pro" in captured.out

    def test_whoami_when_not_logged_in(self, capsys):
        """Mocked AuthClient.current_bundle() returns None; 'Not logged in' printed."""
        from agenthicc.cli.commands.auth import whoami
        from agenthicc.cli.context import CLIContext

        mock_client = MagicMock()
        mock_client.current_bundle.return_value = None

        with patch("agenthicc.auth.AuthClient", return_value=mock_client):
            whoami(CLIContext())

        captured = capsys.readouterr()
        assert "Not logged in" in captured.out


# ---------------------------------------------------------------------------
# TestDoSessions
# ---------------------------------------------------------------------------

class TestDoSessions:
    def test_no_sessions(self, capsys):
        """Empty index → prints 'No saved sessions.'"""
        from agenthicc.sessions import _do_sessions
        _do_sessions()
        captured = capsys.readouterr()
        assert "No saved sessions." in captured.out

    def test_shows_sessions(self, capsys):
        """Index with two entries → both session IDs appear in output."""
        from agenthicc.sessions import _register_session, _do_sessions
        sid_a = uuid.uuid4().hex
        sid_b = uuid.uuid4().hex
        _register_session(sid_a)
        _register_session(sid_b)

        _do_sessions()
        captured = capsys.readouterr()

        # Only the first 12 chars are shown per session
        assert sid_a[:12] in captured.out
        assert sid_b[:12] in captured.out

    def test_current_cwd_marked_with_asterisk(self, capsys):
        """A session whose cwd matches the current directory is marked with *."""
        from agenthicc.sessions import _do_sessions, _save_session_index
        # Register a session for a different cwd
        _save_session_index({
            "aabbccddeeff": {
                "cwd": "/some/other/path",
                "last_used": time.time(),
                "log_path": "/x/aabbccddeeff.jsonl",
            },
            "112233445566": {
                "cwd": os.getcwd(),
                "last_used": time.time() + 1,
                "log_path": "/x/112233445566.jsonl",
            },
        })
        _do_sessions()
        captured = capsys.readouterr()
        assert " *" in captured.out


# ---------------------------------------------------------------------------
# TestRunTui (just the import-error branch)
# ---------------------------------------------------------------------------

class TestRunTui:
    def _make_ctx(self, **kwargs):
        from agenthicc.cli.context import CLIContext, CLIFlags
        defaults = dict(resume_id=None, headless=False, continue_session=False,
                        set_overrides=(), flags=CLIFlags(), record_cassette=None)
        defaults.update(kwargs)
        return CLIContext(**defaults)

    def test_tui_import_error_exits(self, monkeypatch):
        """_run_tui exits with code 1 when TUI libs are missing."""
        import agenthicc.runners.tui_session as ts

        ctx = self._make_ctx()

        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *a, **kw):
            if name in ("rich.console",):
                raise ImportError(f"No module named '{name}'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr("builtins.__import__", fake_import)

        with pytest.raises(SystemExit) as exc_info:
            ts._run_tui(ctx)
        assert exc_info.value.code == 1

    def test_tui_continue_no_previous_session_prints_message(self, capsys, monkeypatch):
        """--continue with no prior session prints 'No previous session found'."""
        import agenthicc.runners.tui_session as ts

        ctx = self._make_ctx(continue_session=True)

        monkeypatch.setattr(ts, "_find_latest_session_for_cwd", lambda: None)

        async def noop_tui_session(resume_id=None, **kwargs):
            pass

        monkeypatch.setattr(ts, "_run_tui_session", noop_tui_session)

        def consuming_asyncio_run(coro):
            coro.close()

        with patch.dict("sys.modules", {"rich.console": MagicMock()}):
            with patch("agenthicc.runners.tui_session.asyncio.run", side_effect=consuming_asyncio_run):
                ts._run_tui(ctx)

        captured = capsys.readouterr()
        assert "No previous session found" in captured.out

    def test_tui_resume_sets_resume_id(self, monkeypatch):
        """--resume <id> passes the id to _run_tui_session."""
        import agenthicc.runners.tui_session as ts

        resume_id_used = []
        ctx = self._make_ctx(resume_id="myresumeid")

        async def capturing_tui_session(resume_id=None, **kwargs):
            resume_id_used.append(resume_id)

        monkeypatch.setattr(ts, "_run_tui_session", capturing_tui_session)

        with patch.dict("sys.modules", {"rich.console": MagicMock()}):
            with patch("agenthicc.runners.tui_session.asyncio.run", side_effect=lambda coro: asyncio.new_event_loop().run_until_complete(coro)):
                ts._run_tui(ctx)

        assert resume_id_used == ["myresumeid"]

    def test_tui_exception_exits_with_code_1(self, monkeypatch, capsys):
        """If asyncio.run raises, _run_tui exits with code 1."""
        import agenthicc.runners.tui_session as ts

        ctx = self._make_ctx()

        monkeypatch.setattr(ts, "_find_latest_session_for_cwd", lambda: None)

        def raising_asyncio_run(coro):
            coro.close()
            raise RuntimeError("boom")

        with patch.dict("sys.modules", {"rich.console": MagicMock()}):
            with patch("agenthicc.runners.tui_session.asyncio.run", side_effect=raising_asyncio_run):
                with pytest.raises(SystemExit) as exc_info:
                    ts._run_tui(ctx)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "TUI error" in captured.err


# ---------------------------------------------------------------------------
# TestDoLogin / TestDoLogout
# ---------------------------------------------------------------------------

class TestDoLogin:
    async def test_do_login_prints_email_and_plan(self, capsys):
        """login() calls AuthClient.login() and prints the returned bundle info."""
        from agenthicc.cli.commands.auth import login
        from agenthicc.cli.context import CLIContext
        from agenthicc.auth import TokenBundle

        bundle = TokenBundle(
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
            plan="pro",
            email="bob@example.com",
            user_id="u2",
        )

        mock_client = MagicMock()
        mock_client.login = AsyncMock(return_value=bundle)

        with patch("agenthicc.auth.AuthClient", return_value=mock_client):
            await login(CLIContext())

        captured = capsys.readouterr()
        assert "bob@example.com" in captured.out
        assert "pro" in captured.out


class TestDoLogout:
    async def test_do_logout_prints_logged_out(self, capsys):
        """logout() calls AuthClient.logout() and prints 'Logged out.'"""
        from agenthicc.cli.commands.auth import logout
        from agenthicc.cli.context import CLIContext

        from agenthicc.cli.commands.auth import logout
        from agenthicc.cli.context import CLIContext

        mock_client = MagicMock()
        mock_client.logout = AsyncMock()

        with patch("agenthicc.auth.AuthClient", return_value=mock_client):
            await logout(CLIContext())

        captured = capsys.readouterr()
        assert "Logged out." in captured.out


# ---------------------------------------------------------------------------
# TestRunHeadless
# ---------------------------------------------------------------------------

class TestRunHeadless:
    async def test_run_headless_emits_ready_on_eof(self, capsys, tmp_path):
        """_run_headless prints ready JSON then exits cleanly on EOF stdin."""
        from agenthicc.runners.headless import _run_headless

        # Empty stdin → immediate EOF
        lines = iter([""])
        with patch("asyncio.get_event_loop") as mock_get_loop:
            loop = asyncio.get_event_loop()
            mock_get_loop.return_value = loop
            original_run_in_executor = loop.run_in_executor

            async def fake_executor(_exec, fn):
                return next(lines, "")

            loop.run_in_executor = fake_executor
            try:
                await _run_headless()
            finally:
                loop.run_in_executor = original_run_in_executor

        out = capsys.readouterr().out
        first = json.loads(out.strip().split("\n")[0])
        assert first["status"] == "ready"
        assert first["mode"] == "headless"

    def test_run_headless_skips_empty_lines_is_handled(self):
        """Headless mode ignores empty/whitespace-only input lines."""
        from agenthicc.runners.headless import _run_headless
        assert callable(_run_headless)

    async def test_run_headless_processes_input_and_emits_intent(self, capsys):
        """_run_headless reads a text line, emits IntentCreated, prints result JSON."""
        from agenthicc.runners.headless import _run_headless

        # Return "hello world" then EOF
        input_lines = iter(["hello world\n", ""])

        loop = asyncio.get_event_loop()
        original_exec = loop.run_in_executor

        async def fake_executor(_exec, fn):
            return next(input_lines, "")

        loop.run_in_executor = fake_executor
        try:
            await _run_headless()
        finally:
            loop.run_in_executor = original_exec

        out = capsys.readouterr().out
        output_lines = [l for l in out.strip().split("\n") if l]
        assert len(output_lines) >= 2
        # Second line should be the IntentCreated JSON
        data = json.loads(output_lines[1])
        assert data["event_type"] == "IntentCreated"
        assert "intent_id" in data

    async def test_run_headless_skips_whitespace_only_lines(self, capsys):
        """_run_headless skips whitespace-only input and doesn't emit any event."""
        from agenthicc.runners.headless import _run_headless

        input_lines = iter(["   \n", "\t\n", ""])

        loop = asyncio.get_event_loop()
        original_exec = loop.run_in_executor

        async def fake_executor(_exec, fn):
            return next(input_lines, "")

        loop.run_in_executor = fake_executor
        try:
            await _run_headless()
        finally:
            loop.run_in_executor = original_exec

        out = capsys.readouterr().out
        # Only the ready line should be printed
        output_lines = [l for l in out.strip().split("\n") if l]
        assert len(output_lines) == 1
        data = json.loads(output_lines[0])
        assert data["status"] == "ready"

    async def test_run_headless_timeout_path(self, capsys):
        """_run_headless prints error JSON when wait_for times out."""
        from agenthicc.runners.headless import _run_headless

        input_lines = iter(["do something\n", ""])

        loop = asyncio.get_event_loop()
        original_exec = loop.run_in_executor

        async def fake_executor(_exec, fn):
            return next(input_lines, "")

        async def instant_timeout(coro, timeout):
            raise asyncio.TimeoutError()

        loop.run_in_executor = fake_executor
        try:
            with patch("asyncio.wait_for", side_effect=instant_timeout):
                await _run_headless()
        finally:
            loop.run_in_executor = original_exec

        out = capsys.readouterr().out
        output_lines = [l for l in out.strip().split("\n") if l]
        assert len(output_lines) >= 2
        timeout_line = json.loads(output_lines[-1])
        assert timeout_line["event_type"] == "Error"
        assert timeout_line["message"] == "timeout"
