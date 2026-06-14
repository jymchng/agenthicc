"""SpinnerPanel widget — live tool-call progress during agent streaming (PRD-55 Phase 4).

Rendered inside TranscriptView during streaming.  Each tool call is displayed
as a single line (or multiple lines when a diff preview is attached).  The
panel is hidden when idle and shown while an agent turn is running.

Message handlers:
  ToolCallStarted  → add_tool_call(tool_use_id, name, args)
  ToolCallComplete → update_tool_call(tool_use_id, done, ok, ms, diff)
"""
from __future__ import annotations

from textual.widget import Widget

from agenthicc.tui.messages import ToolCallComplete, ToolCallStarted

__all__ = ["SpinnerPanel"]


class SpinnerPanel(Widget):
    """Live tool-call progress panel shown while an agent is streaming.

    Hidden (``display: none``) when idle; shown while tool calls are in flight.
    Each tool call is tracked in ``_tool_calls`` keyed by ``tool_use_id``.

    Entry schema::

        {
            "name":  str,   # tool function name
            "args":  str,   # pre-formatted args string
            "done":  bool,  # True once ToolCallComplete fires
            "ok":    bool,  # False when the call errored
            "ms":    float | None,  # duration reported by the runner
            "diff":  str | None,   # unified diff text (file-editing tools only)
        }
    """

    DEFAULT_CSS = """
    SpinnerPanel {
        height: auto;
        display: none;
    }
    SpinnerPanel.active {
        display: block;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        # Insertion-ordered so display order matches call order.
        self._tool_calls: dict[str, dict] = {}

    # ── public API ────────────────────────────────────────────────────────────

    def add_tool_call(self, tool_use_id: str, name: str, args: str) -> None:
        """Register a new tool call and refresh the panel.

        Parameters
        ----------
        tool_use_id:
            Opaque identifier from the runner signal.
        name:
            Tool function name, e.g. ``"read_file"``.
        args:
            Pre-formatted argument string, e.g. ``"'src/main.py'"``.
        """
        self._tool_calls[tool_use_id] = {
            "name": name,
            "args": args,
            "done": False,
            "ok": True,
            "ms": None,
            "diff": None,
        }
        self.show()
        self.refresh()

    def update_tool_call(
        self,
        tool_use_id: str,
        *,
        done: bool,
        ok: bool,
        ms: float | None,
        diff: str | None = None,
    ) -> None:
        """Mark a tool call complete and refresh the panel.

        Parameters
        ----------
        tool_use_id:
            Same identifier passed to :meth:`add_tool_call`.
        done:
            Set to ``True`` to mark the call as finished.
        ok:
            ``True`` for success, ``False`` for failure/error.
        ms:
            Elapsed milliseconds reported by the runner, or ``None``.
        diff:
            Optional unified diff text to preview (first 8 lines shown).
        """
        entry = self._tool_calls.get(tool_use_id)
        if entry is None:
            return
        entry["done"] = done
        entry["ok"] = ok
        entry["ms"] = ms
        if diff is not None:
            entry["diff"] = diff
        self.refresh()

    def show(self) -> None:
        """Make the panel visible."""
        self.add_class("active")

    def hide(self) -> None:
        """Hide the panel and clear tool call state."""
        self._tool_calls.clear()
        self.remove_class("active")

    # ── rendering ─────────────────────────────────────────────────────────────

    def render(self) -> str:
        """Build a Rich Markup string for all tracked tool calls.

        Format per call (finished, success)::

            "   [dim]⎿[/dim] [bold]{name}[/bold][dim]({args})[/dim]  [green]✓[/green] [dim]{ms:.0f}ms[/dim]"

        Format per call (finished, failure)::

            "   [dim]⎿[/dim] [bold]{name}[/bold][dim]({args})[/dim]  [red]✗[/red]"

        Format per call (still running)::

            "   [dim]⎿[/dim] [bold]{name}[/bold][dim]({args})[/dim]  [dim]…[/dim]"

        If ``diff`` is set on a finished call, the first 8 diff lines follow,
        colour-coded by hunk marker (``+`` → green, ``-`` → red, ``@@`` → cyan,
        header → dim), plus a ``… N more lines`` footer when truncated.
        """
        lines: list[str] = []

        for entry in self._tool_calls.values():
            name = entry["name"]
            args = entry["args"]
            prefix = f"   [dim]⎿[/dim] [bold]{name}[/bold][dim]({args})[/dim]"

            if entry["done"]:
                if entry["ok"]:
                    ms_str = (
                        f"  [dim]{entry['ms']:.0f}ms[/dim]"
                        if entry["ms"] is not None
                        else ""
                    )
                    lines.append(f"{prefix}  [green]✓[/green]{ms_str}")
                else:
                    lines.append(f"{prefix}  [red]✗[/red]")

                # ── diff preview (file-editing tools) ────────────────────────
                diff_text: str = entry.get("diff") or ""
                if diff_text:
                    diff_all = diff_text.splitlines()
                    preview = diff_all[:8]
                    for dl in preview:
                        if dl.startswith("+++") or dl.startswith("---"):
                            lines.append(f"      [dim]{dl}[/dim]")
                        elif dl.startswith("@@"):
                            lines.append(f"      [dim cyan]{dl}[/dim cyan]")
                        elif dl.startswith("+"):
                            lines.append(f"      [green]{dl}[/green]")
                        elif dl.startswith("-"):
                            lines.append(f"      [red]{dl}[/red]")
                        else:
                            lines.append(f"      [dim]{dl}[/dim]")
                    remaining = len(diff_all) - 8
                    if remaining > 0:
                        lines.append(
                            f"      [dim]… {remaining} more diff lines[/dim]"
                        )
            else:
                lines.append(f"{prefix}  [dim]…[/dim]")

        return "\n".join(lines)

    # ── message handlers ──────────────────────────────────────────────────────

    def on_tool_call_started(self, event: ToolCallStarted) -> None:
        """Handle ToolCallStarted posted to this widget or its ancestors."""
        event.stop()
        # Format args the same way _run_agent_turn does: first value if single,
        # otherwise key=val pairs (max 3 keys) with a trailing ellipsis.
        items = list((event.args or {}).items())
        if len(items) == 1:
            args_str = repr(items[0][1])[:50]
        elif items:
            args_str = ", ".join(f"{k}={repr(v)[:25]}" for k, v in items[:3])
            if len(items) > 3:
                args_str += ", …"
        else:
            args_str = ""
        self.add_tool_call(event.tool_use_id, event.name, args_str)

    def on_tool_call_complete(self, event: ToolCallComplete) -> None:
        """Handle ToolCallComplete posted to this widget or its ancestors."""
        event.stop()
        self.update_tool_call(
            event.tool_use_id,
            done=True,
            ok=event.success,
            ms=event.duration_ms,
            diff=event.diff,
        )
