"""The status line's outside inputs: git, a user command, and ``/status`` (TUX-02).

- :func:`read_git_branch` and :func:`refresh_git_status` keep the
  ``git_branch`` / ``git_dirty`` signals current without running git on a
  render: the branch is read from ``HEAD``; the dirty mark comes from one
  bounded ``git status`` after each turn.
- :class:`StatusLineCommand` runs the user's ``[ui.statusline] command``: at
  most once a second, only after the state changed, with the state as JSON on
  stdin and a 500 ms timeout. Its first line of output, stripped of every
  control sequence except SGR, replaces the built-in segments 3–7. A failure
  restores the built-in segments and says so once.
- :func:`status_report` is what ``/status`` prints: identity, model, usage and
  where the session's files are, with every absent value explained.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tui.workspace.formatters import context_percent

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState

__all__ = [
    "StatusLineCommand",
    "read_git_branch",
    "refresh_git_status",
    "sanitize_status_output",
    "status_report",
    "statusline_input",
]

#: The command's deadline: a slower command is killed (TUX-02 §2.3).
STATUSLINE_TIMEOUT_S = 0.5
#: The shortest time between two runs.
STATUSLINE_INTERVAL_S = 1.0
#: Bytes of output kept before the first line is taken.
_MAX_OUTPUT = 4096

JsonValue = str | int | float | None


# ── git ───────────────────────────────────────────────────────────────────────


def _git_dir(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return marker
        if marker.is_file():
            try:
                text = marker.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                target = Path(text.removeprefix("gitdir:").strip())
                return target if target.is_absolute() else (directory / target).resolve()
            return None
    return None


def read_git_branch(cwd: str | Path) -> str:
    """The branch checked out in *cwd*'s repository (a short hash when detached).

    ``HEAD`` is read directly, so this never starts a process. ``""`` outside
    a repository or when ``HEAD`` cannot be read.
    """
    git_dir = _git_dir(Path(cwd))
    if git_dir is None:
        return ""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if head.startswith("ref:"):
        return head.removeprefix("ref:").strip().removeprefix("refs/heads/")
    return head[:7]


async def _git_dirty(cwd: str, timeout_s: float) -> bool | None:
    from agenthicc.tools.exec.child import child_env

    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            "--untracked-files=no",
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env(),
        )
    except OSError:
        return None
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    return bool(output.strip()) if process.returncode == 0 else None


async def refresh_git_status(app_state: AppState, cwd: str, *, timeout_s: float = 2.0) -> None:
    """Update the branch and dirty-mark signals for *cwd*."""
    conv = app_state.conversation
    branch = read_git_branch(cwd)
    conv.git_branch.set(branch)
    if not branch:
        conv.git_dirty.set(False)
        return
    dirty = await _git_dirty(cwd, timeout_s)
    if dirty is not None:
        conv.git_dirty.set(dirty)


# ── the user status-line command ─────────────────────────────────────────────

_SGR = r"\x1b\[[0-9;]*m"
_ESCAPE = re.compile(
    rf"({_SGR})"  # kept
    r"|\x1b\[[0-9;?<>=!]*[ -/]*[@-~]"  # any other CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC
    r"|\x1b[@-_]?"  # a lone or two-byte escape
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # C0/C1 controls (tab handled below)
)


def sanitize_status_output(raw: bytes) -> str:
    """The first line of *raw*, with every control sequence except SGR removed."""
    text = raw[:_MAX_OUTPUT].decode("utf-8", errors="replace")
    first = next((line for line in re.split(r"\r\n|\r|\n", text) if line.strip()), "")
    first = first.replace("\t", " ")
    return _ESCAPE.sub(lambda match: match.group(1) or "", first).strip()


def _state_word(app_state: AppState) -> str:
    conv = app_state.conversation
    if app_state.pending_approval() is not None:
        return "waiting"
    if conv.compaction_active():
        return "compacting"
    return conv.agent_state().name.lower()


def statusline_input(app_state: AppState) -> dict[str, JsonValue]:
    """The JSON object a status-line command reads on stdin; unknown is ``null``."""
    from agenthicc.tui.workspace.formatters import pool_status_counts

    conv = app_state.conversation
    provider, separator, model = conv.model_name().partition("/")
    if not separator:
        provider, model = "", provider
    usage_known = conv.usage_status() in {"complete", "partial"}
    cost_known = conv.cost_status() in {"authoritative", "estimated"}
    pool = conv.subagent_pool_state()
    workers = 0
    if pool is not None:
        workers = pool_status_counts(pool).running
    return {
        "session_id": conv.session_id() or None,
        "title": conv.session_title() or None,
        "cwd": conv.current_directory() or None,
        "git_branch": conv.git_branch() or None,
        "model": model or None,
        "provider": provider or None,
        "mode": app_state.active_mode().name,
        "state": _state_word(app_state),
        "context_used_pct": context_percent(conv.context_tokens(), conv.context_window()),
        "tokens_in": conv.tokens_in() if usage_known else None,
        "tokens_out": conv.tokens_out() if usage_known else None,
        "cost_usd": conv.cost_usd() if cost_known else None,
        "agents_need_you": conv.background_needs_you(),
        "workers_running": workers,
    }


class StatusLineCommand:
    """Run ``[ui.statusline] command`` after state changes and publish its line."""

    def __init__(
        self,
        command: str,
        app_state: AppState,
        cwd: str,
        *,
        timeout_s: float = STATUSLINE_TIMEOUT_S,
        interval_s: float = STATUSLINE_INTERVAL_S,
    ) -> None:
        self.command = command
        self._state = app_state
        self._cwd = cwd
        self._timeout_s = timeout_s
        self._interval_s = interval_s
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        self._noticed = False
        self.failures = 0

    def start(self) -> None:
        """Watch the state and run the command once now (needs a running loop)."""
        conv = self._state.conversation
        for watched in (
            conv.agent_state,
            conv.model_name,
            conv.context_tokens,
            conv.context_window,
            conv.tokens_in,
            conv.tokens_out,
            conv.cost_usd,
            conv.background_needs_you,
            conv.subagent_pool_state,
            conv.session_title,
            conv.git_branch,
            conv.git_dirty,
            conv.compaction_active,
            self._state.active_mode,
            self._state.pending_approval,
            self._state.workflow_run,
        ):
            self._unsubscribe.append(watched.subscribe(self._changed.set))
        self._changed.set()
        self._task = asyncio.get_running_loop().create_task(
            self._serve(), name="agenthicc-statusline"
        )

    def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    async def _serve(self) -> None:
        last = float("-inf")
        while True:
            await self._changed.wait()
            wait = last + self._interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._changed.clear()
            last = time.monotonic()
            await self.run_once()

    async def run_once(self) -> None:
        """Run the command once and publish its line, or fall back and say why."""
        payload = json.dumps(statusline_input(self._state)).encode()
        try:
            line = await self._run(payload)
        except _CommandFailed as exc:
            self._fail(str(exc))
            return
        self._state.conversation.statusline_text.set(line)

    async def _run(self, payload: bytes) -> str:
        from agenthicc.tools.exec.child import child_env, child_preexec

        try:
            process = await asyncio.create_subprocess_shell(
                self.command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env(),
                preexec_fn=child_preexec(),
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise _CommandFailed(f"could not start: {exc}") from exc
        try:
            output, errors = await asyncio.wait_for(process.communicate(payload), self._timeout_s)
        except TimeoutError:
            _kill(process)
            await process.wait()
            raise _CommandFailed(f"timed out after {round(self._timeout_s * 1000)} ms") from None
        if process.returncode != 0:
            reason = sanitize_status_output(errors)
            detail = f": {reason}" if reason else ""
            raise _CommandFailed(f"exited with status {process.returncode}{detail}")
        return sanitize_status_output(output)

    def _fail(self, reason: str) -> None:
        self.failures += 1
        conv = self._state.conversation
        conv.statusline_text.set("")
        if self._noticed:
            return
        self._noticed = True
        conv.notify_transient(
            f"Status line command `{self.command}` failed ({reason}); "
            "showing the built-in status line",
            duration=8.0,
        )


class _CommandFailed(Exception):
    pass


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass


# ── /status ───────────────────────────────────────────────────────────────────


def status_report(app_state: AppState) -> list[tuple[str, str]]:
    """``/status``: (label, value) rows, each absent value said in words."""
    from agenthicc.memory.journal import journal_path_for
    from agenthicc.tui.runtime.session_log import get_session_log_path

    conv = app_state.conversation
    label = conv.model_name()
    provider, separator, model = label.partition("/")
    if not separator:
        provider, model = "", provider
    provider_name = provider or "the provider"
    session_id = conv.session_id()
    branch = conv.git_branch()
    rows: list[tuple[str, str]] = [
        ("Session", session_id or "not saved yet"),
        ("Title", conv.session_title() or "new session (titled by the first prompt)"),
        ("Directory", conv.current_directory()),
        (
            "Branch",
            f"{branch}{' (uncommitted changes)' if conv.git_dirty() else ''}"
            if branch
            else "not a git repository",
        ),
        ("Model", model or "unknown"),
        ("Provider", provider or "unknown"),
        ("Mode", app_state.active_mode().name),
        ("Turns", str(conv.turn_count())),
    ]
    usage = conv.usage_status()
    if usage in {"complete", "partial"}:
        tokens = f"{conv.tokens_in():,} in, {conv.tokens_out():,} out"
        if usage == "partial":
            tokens += f" (partial: {provider_name} did not report every call)"
        rows.append(("Tokens", tokens))
    elif conv.usage_calls() == 0 and conv.turn_count() == 0:
        rows.append(("Tokens", "none yet: no request has been sent"))
    else:
        rows.append(("Tokens", f"not reported by {provider_name}"))
    cost_status = conv.cost_status()
    if cost_status == "authoritative":
        rows.append(("Cost", f"${conv.cost_usd():.4f}"))
    elif cost_status == "estimated":
        rows.append(("Cost", f"${conv.cost_usd():.4f} (estimated from the model's prices)"))
    else:
        rows.append(
            (
                "Cost",
                f"not reported by {provider_name}, and no price is known for this model",
            )
        )
    window = conv.context_window()
    used = conv.context_tokens()
    percent = context_percent(used, window)
    if percent is not None:
        rows.append(("Context", f"{percent}% ({used:,} of {window:,} tokens)"))
    elif window <= 0:
        rows.append(("Context", "the model's context window is not known"))
    else:
        rows.append(("Context", f"no response yet (window {window:,} tokens)"))
    if session_id:
        rows.append(("Log", str(get_session_log_path(session_id))))
        rows.append(("Memory", str(journal_path_for(session_id))))
    return rows
