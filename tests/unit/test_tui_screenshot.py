"""Focused tests for the stateful terminal screenshot tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import agenthicc.tools.tui_screenshot as screenshot


@pytest.fixture
def tool() -> screenshot.TuiScreenshotTool:
    screenshot._SESSIONS.clear()
    instance = screenshot.TuiScreenshotTool()
    yield instance
    for session in list(instance._sessions.values()):
        if session.backend == "pty":
            instance._pty_kill(session)
    instance._sessions.clear()


def test_text_and_style_helpers_cover_normal_and_fallback_inputs(
    tool: screenshot.TuiScreenshotTool,
) -> None:
    raw = "\x1b[31mred\x1b[0m\r\nhttps://example\x1b]8;;url\x07link\x1b]8;;\x07"
    assert tool._ansi_to_text(raw) == "red\nhttps://examplelink"
    assert len(tool._ansi_to_text("x" * 200_001)) == 200_000

    assert tool._parse_style("light")["theme"] == "light"
    custom = tool._parse_style('{"theme":"nord","font_size":20,"ignored":true}')
    assert custom["theme"] == "nord"
    assert custom["font_size"] == 20
    assert "ignored" not in custom
    assert tool._parse_style("unknown-theme")["theme"] == "modern-dark"
    with pytest.raises(screenshot._ToolError, match="Invalid style JSON"):
        tool._parse_style("{")
    with pytest.raises(screenshot._ToolError, match="must be an object"):
        tool._parse_style("[]")

    assert tool._hex("#abc") == (170, 187, 204, 255)
    assert tool._hex("#abcdef") == (171, 205, 239, 255)
    assert tool._hex("badbadbad") == (255, 255, 255, 255)
    assert tool._hex("#gggggg") == (255, 255, 255, 255)


@pytest.mark.asyncio
async def test_render_operations_write_svg_and_png(tmp_path: Path) -> None:
    instance = screenshot.TuiScreenshotTool()
    svg = tmp_path / "screen.svg"
    result = await instance.run(
        operation="render",
        command="hello <world>\nsecond",
        output=str(svg),
        style='{"theme":"dracula","rounded":4}',
    )
    assert result["ok"] is True
    assert result["format"] == "svg"
    assert "&lt;world&gt;" in svg.read_text(encoding="utf-8")

    png = tmp_path / "screen.png"
    result = await instance.run(
        operation="render",
        command="hello\nworld",
        output=str(png),
        style='{"theme":"codex","font_size":12,"shadow":2,"dpi":96}',
    )
    assert result["ok"] is True
    assert png.stat().st_size > 0

    missing = await instance.run(operation="render")
    assert missing["ok"] is False
    assert "requires" in str(missing["error"])


@pytest.mark.asyncio
async def test_run_dispatches_operations_and_reports_errors(
    tool: screenshot.TuiScreenshotTool,
) -> None:
    listed = await tool.run(operation="list")
    assert listed == {"ok": True, "operation": "list", "sessions": [], "count": 0}
    unknown = await tool.run(operation="nope")
    assert unknown["ok"] is False
    assert "valid_operations" in unknown

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tool, "_tmux_has_session", lambda _sid: False)
    status = await tool.run(operation="status", session="missing")
    assert status["ok"] is False
    assert status["recoverable"] is True
    monkeypatch.undo()

    created = await tool.run(operation="create", session="bad", backend="invalid")
    assert created["ok"] is False
    assert "Unknown backend" in str(created["error"])


@pytest.mark.asyncio
async def test_pty_session_lifecycle_and_status(
    tool: screenshot.TuiScreenshotTool, tmp_path: Path
) -> None:
    created = await tool.run(operation="create", session="pty-test", command="cat", backend="pty")
    assert created["ok"] is True
    assert created["backend"] == "pty"
    session = tool._sessions["pty-test"]

    await asyncio.sleep(0.05)
    sent = await tool.run(operation="send", session="pty-test", command="echo hi\n")
    assert sent["ok"] is True
    captured = await tool.run(
        operation="capture",
        session="pty-test",
        output=str(tmp_path / "capture.png"),
        wait="0s",
    )
    assert captured["ok"] is True
    status = await tool.run(operation="status", session="pty-test")
    assert status["captured_ansi_chars"] >= 0
    assert session.last_ansi != ""
    closed = await tool.run(operation="close", session="pty-test")
    assert closed["ok"] is True
    assert (await tool.run(operation="close", session="pty-test"))["ok"] is False


@pytest.mark.asyncio
async def test_session_selection_and_tmux_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = screenshot.TuiScreenshotTool()
    monkeypatch.setattr(instance, "_tmux_has_session", lambda _sid: True)
    adopted = await instance.run(operation="create", session="existing", backend="tmux")
    assert adopted["ok"] is True
    assert "Adopted" in str(adopted["message"])
    status = await instance.run(operation="status", session="existing")
    assert status["backend"] == "tmux"

    instance._sessions.clear()
    adopted_again = await instance.run(operation="status", session="existing")
    assert adopted_again["ok"] is True

    instance._sessions["a"] = screenshot._Session("a", "pty")
    instance._sessions["b"] = screenshot._Session("b", "pty")
    ambiguous = await instance.run(operation="status")
    assert ambiguous["ok"] is False
    assert "Multiple sessions" in str(ambiguous["error"])


@pytest.mark.asyncio
async def test_tmux_commands_and_subprocess_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = screenshot.TuiScreenshotTool()
    calls: list[list[str]] = []

    async def fake_run(argv: list[str]) -> str:
        calls.append(argv)
        return "captured"

    monkeypatch.setattr(instance, "_run", fake_run)
    monkeypatch.setattr(instance, "_tmux_bin", lambda: "/usr/bin/tmux")
    await instance._tmux_create("s", "bash")
    session = screenshot._Session("s", "tmux", command="bash")
    await instance._tmux_send(session, r"a\x03b\r")
    assert calls[0][:3] == ["tmux", "new-session", "-d"]
    assert any("C-c" in call for call in calls[1])

    async def failed_run(_argv: list[str]) -> str:
        raise screenshot._ToolError("already gone")

    monkeypatch.setattr(instance, "_run", failed_run)
    await instance._tmux_kill(session)

    monkeypatch.setattr(
        screenshot.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no tmux")),
    )
    assert instance._tmux_has_session("missing") is False


@pytest.mark.asyncio
async def test_wait_specifications_and_capture_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = screenshot.TuiScreenshotTool()
    session = screenshot._Session("s", "pty")
    snapshots = iter(["first", "first", "second", "second"])
    monkeypatch.setattr(instance, "_snapshot_sig", lambda _session: next(snapshots, "second"))

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await instance._wait_stable(session, "0s")
    await instance._wait_stable(session, "idle:0")
    await instance._wait_stable(session, "unknown")

    async def capture(_session: screenshot._Session) -> str:
        return "prompt>"

    monkeypatch.setattr(instance, "_capture_ansi", capture)
    await instance._wait_stable(session, "regex:prompt")
    await instance._wait_stable(session, "prompt")

    instance._sessions["s"] = session
    session.proc = SimpleNamespace(poll=lambda: 0)
    not_alive = await instance.run(operation="capture", session="s")
    assert not_alive["ok"] is False


@pytest.mark.asyncio
async def test_pty_helpers_and_capture_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = screenshot.TuiScreenshotTool()
    proc = SimpleNamespace(poll=lambda: None, terminate=lambda: None)
    session = screenshot._Session("s", "pty", proc=proc, pty_fd=99)
    session.buffer.extend(b"ansi output")
    assert await instance._capture_ansi(session) == "ansi output"
    assert session.buffer == bytearray()

    writes: list[tuple[int, bytes]] = []
    monkeypatch.setattr(
        screenshot.os, "write", lambda fd, data: writes.append((fd, data)) or len(data)
    )
    await instance._pty_send(session, r"hello\x03\r")
    assert writes == [(99, b"hello\x03\r")]
    instance._pty_kill(session)
    assert session.proc is None
    assert session.pty_fd is None

    with pytest.raises(screenshot._ToolError, match="no master"):
        await instance._pty_send(screenshot._Session("empty", "pty"), "x")


@pytest.mark.asyncio
async def test_run_helper_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = screenshot.TuiScreenshotTool()

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"out", b""

    async def create(*_argv: str, **_kwargs: object) -> _Proc:
        return _Proc()

    monkeypatch.setattr(screenshot.asyncio, "create_subprocess_exec", create)
    assert await instance._run(["echo", "ok"]) == "out"

    class _Failed(_Proc):
        returncode = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"bad news"

    async def failed(*_argv: str, **_kwargs: object) -> _Failed:
        return _Failed()

    monkeypatch.setattr(screenshot.asyncio, "create_subprocess_exec", failed)
    with pytest.raises(screenshot._ToolError, match=r"failed \(2\)"):
        await instance._run(["false"])


def test_font_fallback_and_render_error_translation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance = screenshot.TuiScreenshotTool()
    font = instance._load_font(12, str(tmp_path / "missing.ttf"))
    assert font is not None

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise screenshot._RenderUnavailable("renderer unavailable")

    monkeypatch.setattr(instance, "_render_png", unavailable)
    with pytest.raises(screenshot._ToolError, match="renderer unavailable"):
        asyncio.run(instance._render("text", str(tmp_path / "out.png"), "", "s", "capture"))


def test_png_renderer_handles_invalid_font_and_styles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance = screenshot.TuiScreenshotTool()
    monkeypatch.setattr(
        instance, "_load_font", lambda _size, _font: SimpleNamespace(getlength=lambda _text: 8)
    )

    class _Image:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.alpha = False

        def alpha_composite(self, _other: object) -> None:
            self.alpha = True

        def save(self, path: str, **_kwargs: object) -> None:
            Path(path).write_bytes(b"png")

    class _Draw:
        def __init__(self, _image: object) -> None:
            pass

        def rounded_rectangle(self, *_args: object, **_kwargs: object) -> None:
            pass

        def rectangle(self, *_args: object, **_kwargs: object) -> None:
            pass

        def text(self, *_args: object, **_kwargs: object) -> None:
            pass

    fake_pil = SimpleNamespace(
        Image=SimpleNamespace(new=lambda *args, **kwargs: _Image()),
        ImageDraw=SimpleNamespace(Draw=_Draw),
    )
    monkeypatch.setitem(__import__("sys").modules, "PIL", fake_pil)
    output = tmp_path / "fake.png"
    instance._render_png_sync("one\ntwo", output, {"theme": "missing", "shadow": 1, "rounded": 2})
    assert output.read_bytes() == b"png"
