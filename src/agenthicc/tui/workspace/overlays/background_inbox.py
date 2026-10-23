"""TUI for durable background approvals and question requests (PRD-227)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from agenthicc.background.inbox import (
    BackgroundInboxItem,
    BackgroundInteractionInbox,
    InboxConflict,
    response_from_text,
)
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.prompt import PromptOverlay

if TYPE_CHECKING:
    from rich.console import RenderableType


class BackgroundInboxOverlay(PromptOverlay):
    """Browse pending interactions and answer one exact operation at a time."""

    name = "background-inbox"
    _PAGE_SIZE = 8

    def __init__(
        self,
        inbox: BackgroundInteractionInbox,
        on_close: Callable[[], None],
        *,
        loader: Callable[[], tuple[BackgroundInboxItem, ...]] | None = None,
    ) -> None:
        super().__init__()
        self._inbox = inbox
        self._on_close = on_close
        self._loader = loader or inbox.list_items
        self._items: list[BackgroundInboxItem] = []
        self._selected = 0
        self._typing = False
        self._typed_approval_mode: str | None = None
        self._message = ""
        self._refresh()

    def _refresh(self) -> None:
        selected_id = self._items[self._selected].item_id if self._items else None
        try:
            self._items = list(self._loader())
        except Exception as exc:  # noqa: BLE001
            self._items = []
            self._message = f"Unable to load inbox: {type(exc).__name__}: {exc}"
        if selected_id:
            self._selected = next(
                (i for i, item in enumerate(self._items) if item.item_id == selected_id),
                min(self._selected, max(0, len(self._items) - 1)),
            )
        else:
            self._selected = min(self._selected, max(0, len(self._items) - 1))

    @property
    def selected_item(self) -> BackgroundInboxItem | None:
        if not self._items:
            return None
        self._selected = max(0, min(self._selected, len(self._items) - 1))
        return self._items[self._selected]

    def _resolve(self, response: dict[str, object]) -> None:
        item = self.selected_item
        if item is None:
            return
        try:
            updated, replay = self._inbox.respond(
                item.item_id,
                response,
                expected_version=item.version,
                idempotency_key=uuid.uuid4().hex,
            )
            self._message = f"Response accepted · {updated.status} (version {updated.version})" + (
                " · idempotent replay" if replay else ""
            )
        except InboxConflict as exc:
            current = exc.current
            self._message = (
                f"Not delivered: {exc}. Current state: {current.status}. "
                "If appropriate, use the session details view to send fresh input."
                if current is not None
                else f"Not delivered: {exc}"
            )
        except (KeyError, ValueError, OSError) as exc:
            self._message = f"Not delivered: {type(exc).__name__}: {exc}"
        self._refresh()

    def _expire(self) -> None:
        item = self.selected_item
        if item is None:
            return
        try:
            updated = self._inbox.expire(
                item.item_id,
                expected_version=item.version,
                idempotency_key=uuid.uuid4().hex,
            )
            self._message = f"Request expired · {updated.status}"
        except InboxConflict as exc:
            current = exc.current
            self._message = (
                f"Not expired: {exc}. Current state: {current.status}"
                if current is not None
                else f"Not expired: {exc}"
            )
        except (KeyError, ValueError, OSError) as exc:
            self._message = f"Not expired: {type(exc).__name__}: {exc}"
        self._refresh()

    def render(self) -> "RenderableType":
        from rich import box
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich.markup import escape

        self._refresh()
        page = self._selected // self._PAGE_SIZE + 1
        pages = max(1, (len(self._items) + self._PAGE_SIZE - 1) // self._PAGE_SIZE)
        table = Table(
            title=f"Background Inbox · page {page}/{pages} · {len(self._items)} pending",
            box=box.SIMPLE,
            expand=True,
        )
        for title in ("", "Type", "Status", "Workspace", "Session", "Request"):
            table.add_column(title, no_wrap=title != "Request")
        start = (self._selected // self._PAGE_SIZE) * self._PAGE_SIZE
        for index in range(start, min(len(self._items), start + self._PAGE_SIZE)):
            item = self._items[index]
            table.add_row(
                "▶" if index == self._selected else "",
                item.kind,
                item.status,
                item.workspace,
                item.session_id[:16],
                str(item.target.get("tool_name", item.kind))[:100],
            )
        if not self._items:
            table.add_row("", "—", "—", "—", "—", "No pending interactions")

        selected = self.selected_item
        if selected is None:
            detail = "No pending background interaction is selected."
        else:
            target = dict(selected.target)
            detail = (
                f"[bold]Item[/bold] {escape(selected.item_id)}  [bold]Status[/bold] {selected.status}\n"
                f"[bold]Session[/bold] "
                f"{escape(selected.session_id)} · attempt {selected.attempt} · version {selected.version}\n"
                f"[bold]Request[/bold] {escape(str(target.get('tool_name', selected.kind)))}  "
                f"[bold]Created[/bold] {selected.created_at:.0f}\n"
                f"[bold]Target[/bold]\n{escape(json.dumps(target, ensure_ascii=False, indent=2))}"
            )
            if selected.resolution:
                detail += f"\n[bold]Why unavailable[/bold] {escape(selected.resolution)}"
        if self._typing:
            detail += "\n\n[bold]Answer[/bold] " + self._render_prompt_line()
        if self._message:
            detail += f"\n\n[dim]{escape(self._message)}[/dim]"
        actions = (
            "Enter submit   Esc cancel answer"
            if self._typing
            else "↑↓/j/k select   PgUp/PgDn   Home/End   Enter answer/review   a approve   y YOLO   s Safe   n reject   x expire   r refresh   Esc close"
        )
        return Group(
            table,
            Panel(detail, title="Selected Interaction", border_style="cyan"),
            Text(actions, style="dim"),
        )

    def _move(self, delta: int) -> None:
        if self._items:
            self._selected = max(0, min(len(self._items) - 1, self._selected + delta))

    def handle_key(self, key: Key, ch: str) -> bool:
        self._refresh()
        if self._typing:
            if key is Key.ESC:
                self._typing = False
                self._buf.clear()
                self._message = "Answer cancelled; no response was sent."
                return True
            if key is Key.ENTER:
                item = self.selected_item
                if item is None:
                    self._typing = False
                    return True
                if item.kind == "approval":
                    target = item.target
                    options = target.get("mode_options")
                    response: dict[str, object] = {"allowed": True, "message": self._prompt_text}
                    if isinstance(options, list) and options:
                        response["mode"] = self._typed_approval_mode or options[0]
                    self._typing = False
                    self._buf.clear()
                    self._resolve(response)
                    return True
                try:
                    response = response_from_text(item, self._prompt_text)
                except ValueError as exc:
                    self._message = str(exc)
                    return True
                self._typing = False
                self._buf.clear()
                self._resolve(response)
                return True
            if key is Key.PASTE and ch:
                self._buf.insert_many(list(ch))
                return True
            return self._handle_prompt_key(key, ch)

        match key:
            case Key.ESC:
                self._on_close()
            case Key.UP:
                self._move(-1)
            case Key.DOWN:
                self._move(1)
            case Key.PAGE_UP:
                self._move(-self._PAGE_SIZE)
            case Key.PAGE_DOWN:
                self._move(self._PAGE_SIZE)
            case Key.HOME:
                self._selected = 0
            case Key.END:
                self._selected = max(0, len(self._items) - 1)
            case Key.CHAR if ch.lower() == "k":
                self._move(-1)
            case Key.CHAR if ch.lower() == "j":
                self._move(1)
            case Key.CHAR if ch.lower() == "r":
                self._refresh()
            case Key.CHAR if ch.lower() == "a":
                if self.selected_item is not None and self.selected_item.kind == "approval":
                    item = self.selected_item
                    approval_response: dict[str, object] = {"allowed": True}
                    target = item.target
                    if target.get("request_kind") == "plan_review":
                        options = target.get("mode_options")
                        if isinstance(options, list) and options:
                            approval_response["mode"] = options[0]
                    if target.get("workspace_access"):
                        approval_response["scope_grant"] = "target_once"
                    self._resolve(approval_response)
            case Key.CHAR if ch.lower() in {"y", "s"}:
                item = self.selected_item
                if item is not None and item.kind == "approval":
                    target = item.target
                    options = target.get("mode_options")
                    if target.get("request_kind") == "plan_review" and isinstance(options, list):
                        desired = "Yolo" if ch.lower() == "y" else "Safe"
                        mode = next(
                            (
                                option
                                for option in options
                                if isinstance(option, str)
                                and option.casefold() == desired.casefold()
                            ),
                            options[0] if options else None,
                        )
                        response = {"allowed": True}
                        if isinstance(mode, str):
                            response["mode"] = mode
                        self._resolve(response)
                    elif ch.lower() == "y":
                        response = {"allowed": True}
                        if target.get("workspace_access"):
                            response["scope_grant"] = "target_once"
                        self._resolve(response)
            case Key.CHAR if ch.lower() == "n":
                if self.selected_item is not None and self.selected_item.kind == "approval":
                    self._resolve({"allowed": False})
            case Key.CHAR if ch.lower() == "x":
                self._expire()
            case Key.ENTER:
                item = self.selected_item
                if item is not None and item.kind in {"question", "input"}:
                    self._typing = True
                    self._buf.clear()
                    self._message = "Enter the exact answer; multi-question answers use a JSON object keyed by question ID."
                elif (
                    item is not None
                    and item.kind == "approval"
                    and item.target.get("request_kind") == "plan_review"
                ):
                    self._typing = True
                    self._buf.clear()
                    options = item.target.get("mode_options")
                    self._typed_approval_mode = (
                        options[0]
                        if isinstance(options, list) and options and isinstance(options[0], str)
                        else None
                    )
                    self._message = "Type optional review feedback/instructions; Enter approves, Esc sends nothing."
        return True
