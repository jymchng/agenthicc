"""Header widget — product name, project path, git branch, session state."""
from __future__ import annotations

import os
from pathlib import Path

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget

from agenthicc.tui.messages import GitStatusUpdated

__all__ = ["Header"]


class Header(Widget):
    """Top bar: agenthicc | project path | git branch + clean state."""

    DEFAULT_CSS = """
    Header {
        height: 1;
        background: $primary-darken-3;
        color: $text;
        padding: 0 1;
        layout: horizontal;
    }
    """

    branch: reactive[str] = reactive("main")
    is_clean: reactive[bool] = reactive(True)

    # ── rendering ─────────────────────────────────────────────────────────────

    def render(self) -> str:
        project = self._project_display()
        dirty_icon = "[dim]○[/dim]" if self.is_clean else "[yellow]●[/yellow]"
        clean_label = "[dim]clean[/dim]" if self.is_clean else "[yellow]dirty[/yellow]"
        return (
            f"[bold cyan]agenthicc[/bold cyan]"
            f"  [dim]{project}[/dim]"
            f"  [bold]{self.branch}[/bold] {dirty_icon} {clean_label}"
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _project_display() -> str:
        cwd = Path(os.getcwd())
        home = Path.home()
        try:
            rel = cwd.relative_to(home)
            return "~/" + str(rel)
        except ValueError:
            return str(cwd)

    # ── message handlers ──────────────────────────────────────────────────────

    def on_git_status_updated(self, event: GitStatusUpdated) -> None:
        self.branch = event.branch
        self.is_clean = event.is_clean

    def on_mount(self) -> None:
        """Refresh git status on mount."""
        self._refresh_git()

    def _refresh_git(self) -> None:
        import subprocess  # noqa: PLC0415
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            status = subprocess.check_output(
                ["git", "status", "--porcelain"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            self.branch = branch or "HEAD"
            self.is_clean = not bool(status)
        except Exception:
            self.branch = "—"
            self.is_clean = True
