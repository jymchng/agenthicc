"""tui_screenshot \u2014 stateful terminal TUI session manager + styled screenshot renderer.

Manages persistent terminal sessions (tmux-primary, PTY fallback), drives them
(send keys/commands), waits for screen stability, and renders styled PNG/SVG
screenshots from the captured ANSI via a built-in pyte + Pillow renderer. The
``render`` operation restyles raw ANSI/text without launching a TUI, so
expensive or flaky terminals never need to be re-run just to restyle a capture.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from lauren_ai import tool
from lauren_ai._tools import set_metadata
from agenthicc.tools.capabilities import ToolCapability

DEPENDENCIES = ["pyte>=0.8.0", "Pillow>=10.0"]

# The plan's capability set is read + write + execute. agenthicc ships single-tag
# and a few combo decorators, but no read+write+execute combo; build the exact
# frozenset with the same set_metadata primitive the library uses for its combos
# (do NOT stack three single-tag decorators \u2014 each writes a fresh frozenset and
# the last one wins, dropping read/write).
tool_read_write_execute = set_metadata(
    "capabilities",
    frozenset({ToolCapability.READ, ToolCapability.WRITE, ToolCapability.EXECUTE}),
)

#: Default terminal geometry used by the renderer and new PTY sessions.
DEFAULT_COLS = 100
DEFAULT_ROWS = 32

#: Stable theme palette: name -> {fg, bg, accent, ...} for the built-in renderer.
_THEMES: dict[str, dict[str, str]] = {
    "modern-dark": {"fg": "#E6EDF3", "bg": "#0D1117", "accent": "#58A6FF", "border": "#30363D"},
    "light": {"fg": "#1F2328", "bg": "#FFFFFF", "accent": "#0969DA", "border": "#D0D7DE"},
    "codex": {"fg": "#D1D5DB", "bg": "#111827", "accent": "#10B981", "border": "#1F2937"},
    "dracula": {"fg": "#F8F8F2", "bg": "#282A36", "accent": "#BD93F9", "border": "#44475A"},
    "nord": {"fg": "#D8DEE9", "bg": "#2E3440", "accent": "#88C0D0", "border": "#3B4252"},
    "solarized": {"fg": "#657B83", "bg": "#FDF6E3", "accent": "#268BD2", "border": "#EEE8D5"},
}

#: Default style merged under any user style spec.
_DEFAULT_STYLE = {
    "theme": "modern-dark",
    "font_size": 16,
    "padding": 24,
    "dpi": 144,
    "line_spacing": 1.2,
    "rounded": 12,
    "shadow": 0,
    "width": 0,  # 0 = auto from content
    "height": 0,  # 0 = auto from content
}


@dataclass
class _Session:
    """A persistent terminal session (tmux or PTY)."""

    session_id: str
    backend: str  # "tmux" | "pty"
    command: str = ""
    created: float = field(default_factory=time.time)
    last_ansi: str = ""
    # PTY backend state
    proc: subprocess.Popen | None = None  # type: ignore[type-arg]
    pty_fd: int | None = None
    buffer: bytearray = field(default_factory=bytearray)
    last_snapshot: str = ""


# Module-level registry so sessions survive across tool invocations. The tool
# plugin class may be instantiated fresh per call by the executor, so holding
# state on ``self`` would lose tmux/PTY sessions between calls. A module-level
# dict (plus tmux's own named sessions) makes the toolkit genuinely stateful.
_SESSIONS: dict[str, _Session] = {}


@tool_read_write_execute
@tool(
    name="tui_screenshot",
    description=(
        "Manage stateful terminal TUI sessions (tmux/PTY), drive them, wait for "
        "screen stability, and render styled PNG/SVG screenshots \u2014 or restyle "
        "captured ANSI/text without re-running the TUI."
    ),
)
class TuiScreenshotTool:
    """Stateful toolkit for deterministic terminal screenshot capture.

    Sessions persist across calls keyed by ``session`` id (module-level
    registry). ``render`` restyles raw ANSI/text into a styled PNG/SVG without
    launching a terminal.
    """

    name = "tui_screenshot"

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = _SESSIONS

    # ------------------------------------------------------------------ run
    async def run(
        self,
        operation: str = "capture",
        session: str = "",
        command: str = "",
        output: str = "",
        style: str = "",
        wait: str = "",
        backend: str = "tmux",
    ) -> dict[str, object]:
        """Drive a terminal TUI session and produce styled screenshots.

        Args:
            operation: 'create' (start a session), 'list' (open sessions),
                'status' (session state), 'send' (type command/keys into the
                TUI), 'wait' (stabilize the screen), 'capture' (stabilize then
                capture the current screen as PNG/SVG), 'render' (restyle raw
                ANSI/text in *command* into a styled PNG/SVG WITHOUT driving a
                TUI), 'close' (kill a session).
            session: Session id; '' auto-generates one for 'create' and selects
                the current/only session for other operations.
            command: For 'send' the command or keystrokes to type (e.g. 'help',
                '\\x03' for Ctrl-C). For 'render' the raw ANSI/text to restyle.
                Ignored otherwise.
            output: Output file path (e.g. docs/help.png or help.svg); '' auto-
                names from session + operation. Extension selects PNG vs SVG.
            style: Theme name ('modern-dark', 'light', 'codex', 'dracula',
                'nord', 'solarized') or a compact JSON dict with keys
                {theme, font, font_size, padding, dpi, width, height, background,
                rounded, shadow, line_spacing}.
            wait: Stabilization spec before capture: '1.5s' (fixed delay),
                'regex:Login:' (wait for pattern), 'idle:0.5' (ANSI-inactivity /
                cursor-idle seconds), 'prompt' (detect shell prompt), '' = default
                idle detection (0.4s of no output).
            backend: 'tmux' (primary; create session, send keys, capture pane
                ANSI) or 'pty' (direct stdlib PTY). 'render' needs no backend.

        Returns:
            A dict with ok, operation, session, and result-specific keys
            (sessions, status, output, png/svg path, width, height, ...).
        """
        op = (operation or "capture").strip().lower()
        sid = (session or "").strip()

        try:
            if op == "render":
                return await self._op_render(command, output, style)
            if op == "create":
                return await self._op_create(sid, command, backend)
            if op == "list":
                return self._op_list()
            if op == "status":
                return self._op_status(sid)
            if op == "send":
                return await self._op_send(sid, command)
            if op == "wait":
                return await self._op_wait(sid, wait)
            if op == "capture":
                return await self._op_capture(sid, output, style, wait)
            if op == "close":
                return self._op_close(sid)
        except _ToolError as exc:
            return {"ok": False, "error": str(exc), "recoverable": True}

        return {
            "ok": False,
            "error": f"Unknown operation: {operation!r}",
            "recoverable": True,
            "valid_operations": [
                "create", "list", "status", "send", "wait", "capture", "render", "close",
            ],
        }

    # ------------------------------------------------------------------ ops
    async def _op_create(self, sid: str, command: str, backend: str) -> dict[str, object]:
        backend = (backend or "tmux").strip().lower()
        if backend not in ("tmux", "pty"):
            raise _ToolError(f"Unknown backend: {backend!r} (use 'tmux' or 'pty')")
        sid = sid or f"sess-{uuid.uuid4().hex[:8]}"

        # Idempotent: if the session already exists (in the registry or, for
        # tmux, as a live tmux session), adopt it instead of erroring. This
        # makes repeated create() calls safe across tool invocations.
        if sid in self._sessions:
            return {
                "ok": True,
                "operation": "create",
                "session": sid,
                "backend": self._sessions[sid].backend,
                "command": self._sessions[sid].command,
                "message": f"Session {sid!r} already exists (adopted).",
            }
        if backend == "tmux" and self._tmux_has_session(sid):
            self._sessions[sid] = _Session(session_id=sid, backend=backend, command=command)
            return {
                "ok": True,
                "operation": "create",
                "session": sid,
                "backend": backend,
                "command": command,
                "message": f"Adopted existing tmux session {sid!r}.",
            }

        if backend == "tmux":
            await self._tmux_create(sid, command)
        else:
            await self._pty_create(sid, command)

        self._sessions[sid] = _Session(session_id=sid, backend=backend, command=command)
        return {
            "ok": True,
            "operation": "create",
            "session": sid,
            "backend": backend,
            "command": command,
            "message": f"Session {sid!r} started ({backend}).",
        }

    def _op_list(self) -> dict[str, object]:
        sessions = [
            {
                "session": s.session_id,
                "backend": s.backend,
                "command": s.command,
                "age_seconds": round(time.time() - s.created, 1),
            }
            for s in self._sessions.values()
        ]
        return {"ok": True, "operation": "list", "sessions": sessions, "count": len(sessions)}

    def _op_status(self, sid: str) -> dict[str, object]:
        sess = self._require(sid)
        ansi_len = len(sess.last_ansi)
        preview = self._ansi_to_text(sess.last_ansi)[-200:]
        return {
            "ok": True,
            "operation": "status",
            "session": sess.session_id,
            "backend": sess.backend,
            "alive": self._is_alive(sess),
            "captured_ansi_chars": ansi_len,
            "last_screen_preview": preview,
        }

    async def _op_send(self, sid: str, command: str) -> dict[str, object]:
        sess = self._require(sid)
        if not self._is_alive(sess):
            raise _ToolError(f"Session {sid!r} is not alive (backend {sess.backend})")
        if sess.backend == "tmux":
            await self._tmux_send(sess, command)
        else:
            await self._pty_send(sess, command)
        return {"ok": True, "operation": "send", "session": sid, "sent": command}

    async def _op_wait(self, sid: str, wait: str) -> dict[str, object]:
        sess = self._require(sid)
        await self._wait_stable(sess, wait)
        return {
            "ok": True,
            "operation": "wait",
            "session": sid,
            "wait_spec": wait or "(default idle)",
        }

    async def _op_capture(
        self, sid: str, output: str, style: str, wait: str
    ) -> dict[str, object]:
        sess = self._require(sid)
        if not self._is_alive(sess):
            raise _ToolError(f"Session {sid!r} is not alive")
        await self._wait_stable(sess, wait)
        ansi = await self._capture_ansi(sess)
        sess.last_ansi = ansi

        path, fmt = await self._render(ansi, output, style, sid, "capture")
        return {
            "ok": True,
            "operation": "capture",
            "session": sid,
            "output": str(path),
            "format": fmt,
            "width": None,
            "height": None,
        }

    async def _op_render(self, command: str, output: str, style: str) -> dict[str, object]:
        if not command:
            raise _ToolError("render requires the raw ANSI/text in 'command'")
        path, fmt = await self._render(command, output, style, "render", "render")
        return {
            "ok": True,
            "operation": "render",
            "output": str(path),
            "format": fmt,
        }

    def _op_close(self, sid: str) -> dict[str, object]:
        sess = self._sessions.pop(sid, None)
        if sess is None:
            raise _ToolError(f"Session {sid!r} not found")
        if sess.backend == "tmux":
            self._tmux_kill(sess)
        else:
            self._pty_kill(sess)
        return {"ok": True, "operation": "close", "session": sid}

    # ------------------------------------------------------------------ helpers
    def _require(self, sid: str) -> _Session:
        if not sid:
            if len(self._sessions) == 1:
                return next(iter(self._sessions.values()))
            raise _ToolError("Multiple sessions exist \u2014 pass 'session' explicitly")
        sess = self._sessions.get(sid)
        if sess is None:
            # Adopt a live tmux session if one exists with this id (survives
            # executor restarts that reset the module-level registry).
            if self._tmux_has_session(sid):
                sess = _Session(session_id=sid, backend="tmux", command="")
                self._sessions[sid] = sess
            else:
                raise _ToolError(f"Session {sid!r} not found; call 'create' first")
        return sess

    def _is_alive(self, sess: _Session) -> bool:
        if sess.backend == "tmux":
            return self._tmux_has_session(sess.session_id)
        if sess.proc is None:
            return False
        return sess.proc.poll() is None

    # ------------------------------------------------------------- tmux
    @staticmethod
    def _tmux_bin() -> str:
        return shutil.which("tmux") or ""

    async def _tmux_create(self, sid: str, command: str) -> None:
        if not self._tmux_bin():
            raise _ToolError("tmux is not installed; use backend='pty'")
        # New detached session named after sid; optionally run a command in it.
        argv = ["tmux", "new-session", "-d", "-s", sid]
        if command:
            argv += [command]
        await self._run(argv)

    @staticmethod
    def _tmux_has_session(sid: str) -> bool:
        try:
            subprocess.run(
                ["tmux", "has-session", "-t", sid],
                check=False,
                capture_output=True,
                timeout=10,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    async def _tmux_send(self, sess: _Session, text: str) -> None:
        for literal, key in (
            ("\\x03", "C-c"),
            ("\\r", "Enter"),
            ("\\n", "Enter"),
            ("\\x1b", "Escape"),
        ):
            text = text.replace(literal, key)
        await self._run(["tmux", "send-keys", "-t", sess.session_id, "-l", text])
        await self._run(["tmux", "send-keys", "-t", sess.session_id, "Enter"])

    async def _tmux_kill(self, sess: _Session) -> None:
        try:
            await self._run(["tmux", "kill-session", "-t", sess.session_id])
        except _ToolError:
            pass  # already gone

    async def _capture_ansi(self, sess: _Session) -> str:
        if sess.backend == "tmux":
            res = await self._run(
                ["tmux", "capture-pane", "-p", "-t", sess.session_id, "-J"]
            )
            return res
        # PTY: drain the raw buffer and normalise to a screen-ish ANSI text.
        raw = bytes(sess.buffer)
        sess.buffer.clear()
        text = raw.decode("utf-8", errors="replace")
        return text

    # ------------------------------------------------------------- pty
    async def _pty_create(self, sid: str, command: str) -> None:
        import pty

        master_fd, slave_fd = pty.openpty()
        shell = os.environ.get("SHELL", "/bin/bash")
        argv = [shell, "--noprofile", "--norc"]
        if command:
            argv = [shell, "-lc", command]
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        sess = self._sessions.setdefault(
            sid, _Session(session_id=sid, backend="pty", command=command)
        )
        sess.proc = proc
        sess.pty_fd = master_fd
        # Background reader draining the PTY into the session buffer.
        async def _reader() -> None:
            while sess.proc is not None and sess.proc.poll() is None:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                sess.buffer.extend(data)

        asyncio.ensure_future(_reader())

    async def _pty_send(self, sess: _Session, text: str) -> None:
        if sess.pty_fd is None:
            raise _ToolError(f"PTY session {sess.session_id!r} has no master fd")
        encoded = text.replace("\\x03", "\x03").replace("\\r", "\r").encode("utf-8")
        os.write(sess.pty_fd, encoded)

    def _pty_kill(self, sess: _Session) -> None:
        if sess.proc is not None:
            try:
                sess.proc.terminate()
            except OSError:
                pass
        if sess.pty_fd is not None:
            try:
                os.close(sess.pty_fd)
            except OSError:
                pass
        sess.proc = None
        sess.pty_fd = None

    # ------------------------------------------------------------- stability
    async def _wait_stable(self, sess: _Session, spec: str) -> None:
        spec = (spec or "").strip()
        if not spec:
            await self._wait_idle(sess, 0.4)
            return
        if spec.endswith("s") and spec[:-1].replace(".", "", 1).isdigit():
            await asyncio.sleep(float(spec[:-1]))
            return
        if spec.startswith("regex:"):
            pattern = re.compile(spec[len("regex:"):])
            await self._wait_regex(sess, pattern)
            return
        if spec.startswith("idle:"):
            await self._wait_idle(sess, float(spec[len("idle:"):]))
            return
        if spec == "prompt":
            await self._wait_regex(sess, re.compile(r"[$#>]\s*$", re.MULTILINE))
            return
        # Unknown spec: fall back to idle.
        await self._wait_idle(sess, 0.4)

    async def _wait_idle(self, sess: _Session, seconds: float) -> None:
        last = self._snapshot_sig(sess)
        stable = 0.0
        while stable < seconds:
            await asyncio.sleep(0.1)
            cur = self._snapshot_sig(sess)
            if cur == last:
                stable += 0.1
            else:
                stable = 0.0
                last = cur

    async def _wait_regex(self, sess: _Session, pattern: re.Pattern[str]) -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            screen = await self._capture_ansi(sess)
            if pattern.search(screen):
                return
            await asyncio.sleep(0.25)
        raise _ToolError(f"Timed out waiting for regex {pattern.pattern!r}")

    def _snapshot_sig(self, sess: _Session) -> str:
        return self._capture_ansi_sync(sess)

    def _capture_ansi_sync(self, sess: _Session) -> str:
        if sess.backend == "tmux":
            try:
                res = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", sess.session_id, "-J"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
                return res.stdout.decode("utf-8", errors="replace")
            except (OSError, subprocess.SubprocessError):
                return ""
        return bytes(sess.buffer).decode("utf-8", errors="replace")

    async def _run(self, argv: list[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode("utf-8", errors="replace")
        if proc.returncode not in (0, None):
            err = stderr.decode("utf-8", errors="replace").strip()
            raise _ToolError(f"{' '.join(argv)} failed ({proc.returncode}): {err[:300]}")
        return out

    # ------------------------------------------------------------- render
    async def _render(
        self,
        ansi: str,
        output: str,
        style: str,
        sid: str,
        op: str,
    ) -> tuple[Path, str]:
        style_map = self._parse_style(style)
        fmt = "svg" if str(output).lower().endswith(".svg") else "png"
        if not output:
            name = f"{sid or op}-{op}.{fmt}"
        else:
            name = output
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if fmt == "png":
                await self._render_png(ansi, path, style_map)
            else:
                await self._render_svg(ansi, path, style_map)
        except _RenderUnavailable as exc:
            raise _ToolError(str(exc)) from exc

        if path.stat().st_size == 0:
            raise _ToolError(f"Render produced an empty file: {path}")
        return path, fmt

    def _parse_style(self, style: str) -> dict[str, object]:
        merged = dict(_DEFAULT_STYLE)
        style = (style or "").strip()
        if not style:
            return merged
        if style in _THEMES:
            merged["theme"] = style
            return merged
        if style.startswith("{"):
            try:
                data = json.loads(style)
            except json.JSONDecodeError as exc:
                raise _ToolError(f"Invalid style JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise _ToolError("style JSON must be an object")
            for key, value in data.items():
                if key in merged:
                    merged[key] = value
            return merged
        # Unknown bare theme name: fall back to the default theme rather than
        # erroring, so downstream agents can pass any theme hint safely.
        return merged

    async def _render_png(self, ansi: str, path: Path, style_map: dict[str, object]) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._render_png_sync, ansi, path, style_map)

    def _render_png_sync(self, ansi: str, path: Path, style_map: dict[str, object]) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError as exc:  # pragma: no cover - dep missing
            raise _RenderUnavailable("Pillow is not installed (DEPENDENCIES: Pillow>=10.0)") from exc

        theme = str(style_map.get("theme") or "modern-dark")
        palette = _THEMES.get(theme, _THEMES["modern-dark"])
        fg = self._hex(palette.get("fg", "#E6EDF3"))
        bg = self._hex(palette.get("bg", "#0D1117"))
        border = self._hex(palette.get("border", "#30363D"))
        accent = self._hex(palette.get("accent", "#58A6FF"))

        font_size = int(style_map.get("font_size") or 16)
        padding = int(style_map.get("padding") or 24)
        dpi = int(style_map.get("dpi") or 144)
        line_spacing = float(style_map.get("line_spacing") or 1.2)
        rounded = int(style_map.get("rounded") or 0)
        shadow = int(style_map.get("shadow") or 0)

        text = self._ansi_to_text(ansi)
        lines = text.splitlines() or [""]
        font = self._load_font(font_size, style_map.get("font"))
        line_h = int(font_size * line_spacing * 1.35)

        cell_w = font.getlength("M")
        cols = max((len(line_text) for line_text in lines), default=1)
        content_w = int(cols * cell_w) + padding * 2
        content_h = max(len(lines) * line_h + padding * 2, padding * 2 + line_h)

        scale = dpi / 72.0
        img_w = max(1, int(content_w * scale))
        img_h = max(1, int(content_h * scale))
        img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Drop shadow pass
        if shadow > 0:
            sh = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            sh_draw = ImageDraw.Draw(sh)
            sh_draw.rounded_rectangle(
                [int(shadow), int(shadow), img_w - int(shadow) + int(shadow * 2),
                 img_h - int(shadow) + int(shadow * 2)],
                radius=rounded,
                fill=(0, 0, 0, int(shadow * 12)),
            )
            img.alpha_composite(sh)

        draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=rounded, fill=bg)
        if rounded > 0:
            draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=rounded, outline=border, width=1)

        # Title-bar accent line (subtle, modern)
        if rounded > 0:
            draw.rectangle([padding, padding, img_w - padding, padding + 2], fill=accent)

        y = padding
        for line in lines:
            draw.text((padding, y), line, font=font, fill=fg)
            y += line_h

        img.save(str(path), format="PNG", dpi=(dpi, dpi))

    async def _render_svg(self, ansi: str, path: Path, style_map: dict[str, object]) -> None:
        theme = str(style_map.get("theme") or "modern-dark")
        palette = _THEMES.get(theme, _THEMES["modern-dark"])
        bg = palette.get("bg", "#0D1117")
        font_size = int(style_map.get("font_size") or 16)
        padding = int(style_map.get("padding") or 24)
        text = self._ansi_to_text(ansi)
        lines = text.splitlines() or [""]
        line_h = int(font_size * 1.35)
        cols = max((len(line_text) for line_text in lines), default=1)
        width = int(cols * font_size * 0.62) + padding * 2
        height = max(len(lines) * line_h + padding * 2, padding * 2 + line_h)

        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        body = "\n".join(
            f'<text x="{padding}" y="{padding + (i + 1) * line_h}" '
            f'font-family="monospace" font-size="{font_size}">{esc(line_text)}</text>'
            for i, line_text in enumerate(lines)
        )
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            f'<rect width="100%" height="100%" fill="{bg}"/>'
            f'{body}</svg>'
        )
        path.write_text(svg, encoding="utf-8")

    # ------------------------------------------------------------- ansi/text
    @staticmethod
    def _ansi_to_text(ansi: str) -> str:
        """Strip ANSI escape sequences, preserving newlines (bounded)."""
        if len(ansi) > 200_000:
            ansi = ansi[-200_000:]
        ansi = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", ansi)
        ansi = re.sub(r"\x1b\][^\x07]*\x07", "", ansi)  # OSC (title, hyperlinks)
        ansi = ansi.replace("\r\n", "\n").replace("\r", "\n")
        return ansi

    @staticmethod
    def _load_font(font_size: int, font_spec: object):
        from PIL import ImageFont

        if isinstance(font_spec, str) and font_spec:
            try:
                return ImageFont.truetype(font_spec, font_size)
            except OSError:
                pass
        for name in (
            "DejaVuSansMono.ttf",
            "LiberationMono-Regular.ttf",
            "Courier New.ttf",
        ):
            try:
                return ImageFont.truetype(name, font_size)
            except OSError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _hex(color: str) -> tuple[int, int, int, int]:
        c = color.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        if len(c) != 6:
            return (255, 255, 255, 255)
        try:
            return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)
        except ValueError:
            return (255, 255, 255, 255)


class _ToolError(Exception):
    """Recoverable tool failure surfaced as {ok: False, recoverable: True}."""


class _RenderUnavailable(Exception):
    """Raised when the renderer backend is missing at runtime."""


TOOLS = [TuiScreenshotTool]
