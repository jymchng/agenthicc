"""Dialog — the one frame every decision overlay draws (TUX-07).

Tool approvals, workspace-access approvals, plan review and ``ask_user``
questions all compose a :class:`Dialog`. It owns the layout and the keys that
are the same everywhere, so the three no longer disagree:

- a **title** (a verb phrase, never a tool's internal name) on the top border,
  and a **badge** on its right (the mode, or the subagent worker that asks);
- a **body** that scrolls with ``↑ N more`` / ``↓ N more`` markers when it is
  taller than the room left, and shrinks to fit when it is short;
- one muted **reason** line;
- numbered **options**: ``1``–``9`` choose and confirm, the selected row has
  the ``accent`` bar on ``accent_bg``, and a destructive choice is drawn in
  ``danger`` only while it is selected;
- the **key legend** in the bottom border, from the ``dialog`` keymap context.

The width is ``clamp(cols - 4, 60, 120)`` (never wider than the terminal),
centred when the terminal is wider than 124 columns; the height is at most
the overlay viewport the workspace publishes, or ``rows - 6`` before one is.

``Ctrl+T`` swaps the body for the full arguments or diff, scrollable inside
the dialog. TUX-09 will move that view to its transcript view.

With ``screen_reader`` the dialog draws no box at all, and
:func:`announcement` gives the sentence the announcer prints when it opens.

Every colour is a theme token and every non-ASCII character a glyph from
:mod:`agenthicc.tui.theme`.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from agenthicc.tui.cbreak_reader import Key, key_name
from agenthicc.tui.theme import glyphs, palette, style

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agenthicc.tools.workspace_access import WorkspaceScope
    from agenthicc.tui.input.embedded import ComposerContext
    from agenthicc.tui.keymap import Keymap

__all__ = [
    "DEFAULT_DIALOG_SETTINGS",
    "Dialog",
    "DialogInput",
    "DialogKey",
    "DialogOption",
    "DialogSettings",
    "announcement",
    "dialog_width",
    "render_lines",
]

#: Never narrower than this (unless the terminal is), never wider than _MAX.
_MIN_WIDTH = 60
_MAX_WIDTH = 120
#: Above this many columns the dialog is centred.
_CENTRE_ABOVE = 124
#: Rows kept free for the status line and footer before a viewport is known.
_RESERVED_ROWS = 6


def dialog_width(cols: int) -> int:
    """``clamp(cols - 4, 60, 120)``, and never wider than the terminal."""
    return max(1, min(cols, max(_MIN_WIDTH, min(_MAX_WIDTH, cols - 4))))


def announcement(title: str, summary: str, reason: str, options: int) -> str:
    """The sentence a screen reader hears when a dialog opens (AGX-13)."""
    parts = [f"{title}: {summary.rstrip('.')}." if summary else f"{title}."]
    if reason:
        parts.append(reason if reason.endswith(".") else f"{reason}.")
    if options:
        parts.append(f"{options} option{'s' if options != 1 else ''}.")
    return " ".join(parts)


def render_lines(renderables: Sequence[RenderableType], width: int) -> list[Text]:
    """Render *renderables* at *width* into one styled :class:`Text` per row."""
    console = Console(
        width=max(1, width),
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        emoji=False,
    )
    options = console.options.update(width=max(1, width))
    rows: list[Text] = []
    for renderable in renderables:
        for segments in console.render_lines(renderable, options, pad=False):
            row = Text()
            for segment in segments:
                if not segment.control:
                    row.append(segment.text, segment.style)
            rows.append(row)
    return rows


@dataclass(frozen=True)
class DialogOption:
    """One numbered choice."""

    label: str
    destructive: bool = False
    #: A glyph shown after the label (an answered question's ✓).
    mark: str = ""


@dataclass(frozen=True)
class DialogSettings:
    """What the session supplies to every dialog."""

    #: The session keymap (user rebinds); the defaults when ``None``.
    keymap: Keymap | None = None
    #: ``[ui] screen_reader``: no box drawing.
    plain: bool = False
    #: The active mode's name, shown as the badge.
    mode: str = ""
    #: Where an in-dialog composer's pickers and mentions are scoped.
    composer: ComposerContext | None = None
    #: The workspace an approval's diff is computed in.
    workspace: WorkspaceScope | None = None
    #: Repaints the dialog after an in-dialog composer dispatched a key, which
    #: it does in a task after the overlay host's own repaint.
    redraw: Callable[[], None] = field(default=lambda: None)


#: The settings of a dialog nothing configured (tests, plugins): default keys,
#: boxed layout, no badge.
DEFAULT_DIALOG_SETTINGS = DialogSettings()


class DialogKey(Enum):
    """What a key meant to a dialog, for its overlay to act on."""

    NONE = auto()
    CHOOSE = auto()
    CANCEL = auto()
    NOTE = auto()
    ALIAS = auto()
    OPEN_REQUESTER = auto()
    PREVIOUS = auto()
    NEXT = auto()


@dataclass(frozen=True)
class DialogInput:
    kind: DialogKey
    #: The option chosen (CHOOSE, NOTE).
    index: int = -1
    #: The alias action's name (ALIAS), e.g. ``"dialog.allow_turn"``.
    action: str = ""


_NOTHING = DialogInput(DialogKey.NONE)

_default_keymap: Keymap | None = None


def _keymap(settings: DialogSettings) -> Keymap:
    global _default_keymap
    if settings.keymap is not None:
        return settings.keymap
    if _default_keymap is None:
        from agenthicc.tui.actions import DEFAULT_ACTIONS
        from agenthicc.tui.keymap import Keymap

        _default_keymap = Keymap.build(DEFAULT_ACTIONS)
    return _default_keymap


def _ascii_arrows(text: str) -> str:
    """Keymap key names use arrows; draw them with the active glyph set."""
    g = glyphs()
    return text.replace("↑", g.up).replace("↓", g.down).replace("←", g.left).replace("→", g.right)


@dataclass
class _BodyCache:
    key: object = None
    width: int = 0
    lines: list[Text] = field(default_factory=list)


class Dialog:
    """A titled, bordered decision frame with numbered options."""

    def __init__(
        self,
        title: str,
        *,
        settings: DialogSettings,
        badge: str | None = None,
        badge_token: str = "",
        cancel_label: str = "cancel",
    ) -> None:
        from agenthicc.tui.keymap import Dispatcher

        self.title = title
        self.settings = settings
        self.badge = settings.mode if badge is None else badge
        self.badge_token = badge_token or _mode_token(self.badge)
        self.cancel_label = cancel_label
        self.reason = ""
        #: One line describing the body, for the screen-reader sentence.
        self.summary = ""
        self.options: list[DialogOption] = []
        self.selected = 0
        self.scroll = 0
        self.showing_details = False
        #: Flags for the keymap: approval, note, details, requester, questions.
        self.conditions: frozenset[str] = frozenset()
        #: Extra legend entries before the keymap's (``"enter send"``).
        self.legend_override: tuple[str, ...] = ()
        #: Rows below the options, such as an embedded composer.
        self.footer: Callable[[int], list[Text]] | None = None
        self._body_factory: Callable[[], list[RenderableType]] = list
        self._details_factory: Callable[[], list[RenderableType]] | None = None
        self._body = _BodyCache()
        self._details = _BodyCache()
        self._viewport: tuple[int, int] | None = None
        self._dispatcher = Dispatcher(_keymap(settings))
        #: Body rows shown by the last render; a page scroll moves this far.
        self._page = 1
        self._body_total = 0

    # ── content ─────────────────────────────────────────────────────────────

    def set_body(self, key: object, factory: Callable[[], list[RenderableType]]) -> None:
        """Use *factory*'s renderables as the body; re-rendered when *key* changes."""
        self._body_factory = factory
        if key != self._body.key:
            self._body = _BodyCache(key=key)
            self.scroll = 0

    def set_details(self, factory: Callable[[], list[RenderableType]] | None) -> None:
        """The full arguments or diff that ``Ctrl+T`` shows; ``None`` disables it."""
        self._details_factory = factory
        self._details = _BodyCache()

    @property
    def has_details(self) -> bool:
        return self._details_factory is not None

    # ── geometry ─────────────────────────────────────────────────────────────

    def set_viewport(self, width: int, height: int) -> None:
        self._viewport = (max(1, width), max(1, height))

    def _size(self) -> tuple[int, int]:
        """``(cols, height budget)``: the published viewport, else the terminal."""
        if self._viewport is not None:
            return self._viewport
        term = shutil.get_terminal_size((80, 24))
        return term.columns, max(1, term.lines - _RESERVED_ROWS)

    # ── keys ─────────────────────────────────────────────────────────────────

    def handle_key(self, key: Key, ch: str) -> DialogInput:
        """Apply navigation; return what the overlay must act on."""
        conditions = self.conditions | ({"details"} if self.has_details else set())
        action = self._dispatcher.dispatch("dialog", key_name(key, ch), frozenset(conditions))
        name = action.name if action is not None else ""
        if self.showing_details:
            match name:
                case "dialog.up":
                    self.scroll = max(0, self.scroll - 1)
                case "dialog.down":
                    self.scroll += 1
                case "dialog.scroll_up":
                    self.scroll = max(0, self.scroll - self._step(ch))
                case "dialog.scroll_down":
                    self.scroll += self._step(ch)
                case "dialog.details" | "dialog.cancel":
                    self.showing_details = False
                    self.scroll = 0
            return _NOTHING
        count = len(self.options)
        match name:
            case "dialog.up" if count:
                self.selected = (self.selected - 1) % count
            case "dialog.down" if count:
                self.selected = (self.selected + 1) % count
            # A dialog without options is a pager: the arrows scroll its body.
            case "dialog.up":
                self.scroll = max(0, self.scroll - 1)
            case "dialog.down":
                self.scroll += 1
            case "dialog.confirm" if count:
                return DialogInput(DialogKey.CHOOSE, self.selected)
            case "dialog.choose" if ch.isdigit() and 0 < int(ch) <= count:
                self.selected = int(ch) - 1
                return DialogInput(DialogKey.CHOOSE, self.selected)
            case "dialog.cancel":
                return DialogInput(DialogKey.CANCEL)
            case "dialog.note" if count:
                return DialogInput(DialogKey.NOTE, self.selected)
            case "dialog.details":
                self.showing_details = True
                self.scroll = 0
            case "dialog.scroll_up":
                self.scroll = max(0, self.scroll - self._step(ch))
            case "dialog.scroll_down":
                self.scroll += self._step(ch)
            case "dialog.previous_question":
                return DialogInput(DialogKey.PREVIOUS)
            case "dialog.next_question":
                return DialogInput(DialogKey.NEXT)
            case "dialog.open_requester":
                return DialogInput(DialogKey.OPEN_REQUESTER)
            case "dialog.allow_once" | "dialog.allow_turn" | "dialog.allow_session" | "dialog.deny":
                return DialogInput(DialogKey.ALIAS, action=name)
        return _NOTHING

    def _step(self, ch: str) -> int:
        """``[``/``]`` move one row; Page Up/Down a page."""
        return 1 if ch in ("[", "]") else self.page_rows

    def scroll_by(self, rows: int) -> None:
        self.scroll = max(0, self.scroll + rows)

    @property
    def page_rows(self) -> int:
        """Body rows the last render showed: one Page Up/Down."""
        return max(1, self._page)

    # ── rendering ────────────────────────────────────────────────────────────

    def announcement(self) -> str:
        return announcement(self.title, self.summary, self.reason, len(self.options))

    def _lines(
        self, cache: _BodyCache, factory: Callable[[], list[RenderableType]], width: int
    ) -> list[Text]:
        if cache.width != width or not cache.lines:
            lines = render_lines(factory(), width)
            # Renderers such as Markdown pad with blank rows; the frame has its own.
            while lines and not lines[0].plain.strip():
                lines.pop(0)
            while lines and not lines[-1].plain.strip():
                lines.pop()
            cache.lines = lines or [Text("")]
            cache.width = width
        return cache.lines

    def body_lines(self, width: int) -> list[Text]:
        return self._lines(self._body, self._body_factory, width)

    def legend(self) -> str:
        """The key legend: overrides first, then the keymap's hints."""
        conditions = self.conditions | ({"details"} if self.has_details else set())
        if self.showing_details:
            g = glyphs()
            return f"{g.up}{g.down} scroll {g.sep} Ctrl+T back"
        entries = list(self.legend_override)
        if not entries:
            for hint in _keymap(self.settings).hints("dialog", frozenset(conditions)):
                label = self.cancel_label if hint.action == "dialog.cancel" else hint.label
                entries.append(f"{_ascii_arrows(hint.keys)} {label}")
        return f" {glyphs().sep} ".join(entries)

    def render(self) -> RenderableType:
        cols, budget = self._size()
        if self.settings.plain:
            return Group(*self._plain_rows(cols, budget))
        return Group(*self._boxed_rows(cols, budget))

    def _windowed_body(self, lines: list[Text], rows: int) -> list[Text]:
        """At most *rows* body rows, with ``↑/↓ N more`` markers when clipped."""
        p, g = palette(), glyphs()
        total = len(lines)
        self._body_total = total
        rows = max(1, rows)
        if total <= rows:
            self.scroll = 0
            self._page = rows
            return list(lines)
        visible = max(1, rows - 2) if rows >= 3 else 1
        self.scroll = max(0, min(self.scroll, total - visible))
        self._page = visible
        shown = lines[self.scroll : self.scroll + visible]
        above = self.scroll
        below = total - self.scroll - len(shown)
        out: list[Text] = []
        if rows >= 3:
            out.append(Text(f"{g.up} {above} more" if above else "", style=p.muted))
        out.extend(shown)
        if rows >= 3:
            out.append(Text(f"{g.down} {below} more" if below else "", style=p.muted))
        return out

    def _option_rows(self, width: int) -> list[Text]:
        p, g = palette(), glyphs()
        rows: list[Text] = []
        for index, option in enumerate(self.options):
            selected = index == self.selected
            number = f"{index + 1}" if index < 9 else " "
            label_style = p.danger if (selected and option.destructive) else ""
            row = Text()
            if self.settings.plain:
                row.append(f"{number}. {option.label}", label_style)
                if option.mark:
                    row.append(f" {option.mark}", p.success)
                if selected:
                    row.append(" (selected)")
                rows.append(row)
                continue
            row.append(g.bar if selected else " ", p.accent if selected else "")
            row.append(f" {number}  ", p.accent if selected else p.muted)
            row.append(option.label, label_style)
            if option.mark:
                row.append(f"  {option.mark}", p.success)
            for line in row.wrap(_measure_console(width), width, overflow="fold"):
                if selected:
                    line.align("left", width)
                    line.stylize(p.on("accent_bg"))
                rows.append(line)
        return rows

    def _reason_rows(self, width: int) -> list[Text]:
        if not self.reason:
            return []
        reason = Text(self.reason, style=palette().muted)
        return list(reason.wrap(_measure_console(width), width, overflow="fold"))

    def _boxed_rows(self, cols: int, budget: int) -> list[Text]:
        p, g = palette(), glyphs()
        width = dialog_width(cols)
        left = (cols - width) // 2 if cols > _CENTRE_ABOVE else 0
        inner = max(1, width - 4)
        body_width = max(1, inner - 1)
        tl, tr, bl, br, h, v = g.box

        def frame(content: Text) -> Text:
            line = content.copy()
            line.truncate(inner, overflow="crop", pad=True)
            row = Text(" " * left)
            row.append(v, p.subtle)
            row.append(" ")
            row.append_text(line)
            row.append(" ")
            row.append(v, p.subtle)
            return row

        def indent(line: Text) -> Text:
            row = Text(" ")
            row.append_text(line)
            return row

        top = self._top_border(width, left)
        reason = self._reason_rows(body_width)
        footer = self.footer(inner) if self.footer is not None else []
        if self.showing_details and self._details_factory is not None:
            details = self._lines(self._details, self._details_factory, body_width)
            body_rows = max(1, budget - 4)
            body = self._windowed_body(details, body_rows)
            middle = [frame(Text()), *(frame(indent(line)) for line in body), frame(Text())]
        else:
            options = self._option_rows(inner)
            # top, blank, blank after body, [reason, blank], options, [footer], blank, bottom
            chrome = 5 + (len(reason) + 1 if reason else 0) + len(options) + len(footer)
            body = self._windowed_body(self.body_lines(body_width), budget - chrome)
            middle = [frame(Text())]
            middle += [frame(indent(line)) for line in body]
            middle.append(frame(Text()))
            if reason:
                middle += [frame(indent(line)) for line in reason]
                middle.append(frame(Text()))
            middle += [frame(line) for line in options]
            middle += [frame(line) for line in footer]
            middle.append(frame(Text()))
        bottom = self._bottom_border(width, left)
        return [top, *middle, bottom]

    def _top_border(self, width: int, left: int) -> Text:
        p, g = palette(), glyphs()
        tl, tr, _bl, _br, h, _v = g.box
        badge = f" {self.badge} " if self.badge else ""
        room = max(1, width - 4 - cell_len(badge) - 2)
        title = Text(f" {self.title} ", style="bold")
        title.truncate(room, overflow="ellipsis")
        fill = max(1, width - 3 - title.cell_len - cell_len(badge) - 1)
        row = Text(" " * left)
        row.append(f"{tl}{h}", p.subtle)
        row.append_text(title)
        row.append(h * fill, p.subtle)
        if badge:
            row.append(badge, style(self.badge_token))
        row.append(f"{h}{tr}", p.subtle)
        return row

    def _bottom_border(self, width: int, left: int) -> Text:
        p, g = palette(), glyphs()
        _tl, _tr, bl, br, h, _v = g.box
        legend = Text(f" {self.legend()} ", style=p.muted)
        legend.truncate(max(1, width - 5), overflow="ellipsis")
        fill = max(1, width - 3 - legend.cell_len)
        row = Text(" " * left)
        row.append(f"{bl}{h}", p.subtle)
        row.append_text(legend)
        row.append(h * fill + br, p.subtle)
        return row

    def _plain_rows(self, cols: int, budget: int) -> list[Text]:
        """Screen-reader layout: the same content, no box drawing."""
        p = palette()
        width = max(1, cols)
        title = Text(self.title, style="bold")
        if self.badge:
            title.append(f" ({self.badge})", p.muted)
        rows = [title]
        reason = self._reason_rows(width)
        if self.showing_details and self._details_factory is not None:
            details = self._lines(self._details, self._details_factory, width)
            rows += self._windowed_body(details, budget - 2)
        else:
            options = self._option_rows(width)
            footer = self.footer(width) if self.footer is not None else []
            chrome = 2 + len(reason) + len(options) + len(footer)
            rows += self._windowed_body(self.body_lines(width), budget - chrome)
            rows += reason
            rows += options
            rows += footer
        rows.append(Text(self.legend(), style=p.muted))
        return rows


def _mode_token(badge: str) -> str:
    token = f"mode_{badge.casefold()}"
    return token if token in ("mode_safe", "mode_plan", "mode_yolo") else "muted"


_measure: Console | None = None


def _measure_console(width: int) -> Console:
    """A console for :meth:`Text.wrap`, which needs one only for its options."""
    global _measure
    if _measure is None:
        _measure = Console(file=io.StringIO(), width=max(1, width), highlight=False)
    return _measure
