"""TranscriptModel — mutable presentation model for the TUI transcript."""
from __future__ import annotations

import difflib
import io
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TURNS_IN_MEMORY: int = 200
MAX_LINES_PER_TURN: int = 500
MAX_DIFF_LINES: int = 50
MAX_STREAMING_ROWS: int = 8
_MD_SENTINEL: str = "\x00MD\x00"

SPINNER_FRAMES: list[str] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
AGENT_COLORS: list[str] = ["35", "36", "33", "34", "32", "31"]  # ANSI color codes

__all__ = [
    "SPINNER_FRAMES",
    "AGENT_COLORS",
    "MAX_TURNS_IN_MEMORY",
    "MAX_LINES_PER_TURN",
    "MAX_DIFF_LINES",
    "_MD_SENTINEL",
    "ToolCallState",
    "TurnState",
    "ToolCallEntry",
    "AgentTurnEntry",
    "MentionChip",
    "TranscriptModel",
    "diff_lines",
]

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ToolCallState(Enum):
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILURE = auto()
    APPROVAL_NEEDED = auto()
    DENIED = auto()


class TurnState(Enum):
    PENDING = auto()
    STREAMING = auto()
    COMPLETE = auto()
    FINALIZED = auto()
    CANCELLED = auto()
    ERROR = auto()


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ToolCallEntry:
    """A single tool invocation attached to an AgentTurnEntry."""

    tool_use_id: str
    name: str
    args: dict = field(default_factory=dict)
    state: ToolCallState = field(default=ToolCallState.RUNNING)
    duration_ms: float = 0.0
    error: str = ""
    result_summary: str = ""
    output_lines: list[str] = field(default_factory=list)
    diff_text: str = ""
    committed_line: str = ""
    spinner_frame: int = 0

    @property
    def symbol(self) -> str:
        """Single character representing the current state."""
        if self.state == ToolCallState.PENDING:
            return "."
        if self.state == ToolCallState.RUNNING:
            frame = self.spinner_frame % len(SPINNER_FRAMES)
            return SPINNER_FRAMES[frame]
        if self.state == ToolCallState.SUCCESS:
            return "✓"  # ✓
        if self.state == ToolCallState.FAILURE:
            return "✗"  # ✗
        if self.state == ToolCallState.APPROVAL_NEEDED:
            return "⚠"  # ⚠
        if self.state == ToolCallState.DENIED:
            return "✗"  # ✗
        return "?"

    def render(self) -> str:
        """Render tool call as a single display line."""
        sym = self.symbol
        args_str = _format_args(self.args)
        call = f"{self.name}({args_str})" if args_str else self.name
        if self.state == ToolCallState.SUCCESS:
            summary = f"  {sym} {self.result_summary}" if self.result_summary else f"  {sym}"
            if self.duration_ms:
                summary += f" ({self.duration_ms:.0f}ms)"
            return f"  ⎿ {call}{summary}"
        if self.state == ToolCallState.FAILURE:
            err = f"  {sym} {self.error}" if self.error else f"  {sym}"
            if self.duration_ms:
                err += f" ({self.duration_ms:.0f}ms)"
            return f"  ⎿ {call}{err}"
        # running / pending / approval_needed
        return f"  ⎿ {call}  {sym}"


@dataclass
class AgentTurnEntry:
    """A single agent turn including all tool calls made during the turn."""

    agent_id: str
    agent_name: str
    timestamp: float
    state: TurnState = field(default=TurnState.STREAMING)
    output_lines: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    streaming_text: str = ""
    color_index: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    header_committed: bool = False
    mention_chips: list["MentionChip"] = field(default_factory=list)
    mention_content: dict[str, str] = field(default_factory=dict)
    _evicted: bool = field(default=False, init=False, repr=False)

    def footer(self) -> str | None:
        """Return a footer line showing tokens/cost, or None if nothing to show."""
        has_tokens = bool(self.tokens and self.tokens > 0)
        has_cost = bool(self.cost_usd and self.cost_usd > 0.0)
        if not has_tokens and not has_cost:
            return None
        parts: list[str] = []
        if has_tokens:
            parts.append(f"{self.tokens:,} tokens")
        if has_cost:
            parts.append(f"${self.cost_usd:.3f} cost")
        return "  → " + "  ".join(parts)


@dataclass
class MentionChip:
    """Represents a resolved @mention chip for display in the transcript."""

    raw: str
    kind: str
    display_size: str
    ok: bool
    error: str | None = None
    expanded: bool = False
    # content lines stored by set_mention_content (only rendered when expanded=True)
    _content_lines: list[str] = field(default_factory=list, repr=False)


# ---------------------------------------------------------------------------
# Private render helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _format_args(args: dict) -> str:
    """Format tool call arguments as a short string (up to 2 key=value pairs)."""
    if not args:
        return ""
    items = list(args.items())[:2]
    parts = []
    for k, v in items:
        if isinstance(v, str):
            short_v = v[:30] + "..." if len(v) > 30 else v
            parts.append(f"{k}='{short_v}'")
        else:
            parts.append(f"{k}={v!r}")
    result = ", ".join(parts)
    if len(args) > 2:
        result += ", ..."
    return result


def _render_turn_header(turn: AgentTurnEntry) -> str:
    """Render the turn header line with ANSI color."""
    color_code = AGENT_COLORS[turn.color_index % len(AGENT_COLORS)]
    ts = time.strftime("%H:%M:%S", time.localtime(turn.timestamp))
    name = turn.agent_name
    return f"\033[{color_code}m●\033[0m \033[{color_code};1m{name}\033[0m  \033[2m{ts}\033[0m"


def _render_tool_call_committed(tc: ToolCallEntry) -> str:
    """Render a completed tool call as a single committed line."""
    args_str = _format_args(tc.args)
    call = f"{tc.name}({args_str})" if args_str else tc.name
    if tc.state == ToolCallState.SUCCESS:
        summary = f" \033[32m✓\033[0m"
        if tc.result_summary:
            summary += f" {tc.result_summary}"
        if tc.duration_ms:
            summary += f" ({tc.duration_ms:.0f}ms)"
        return f"  ⎿ {call}{summary}"
    # FAILURE
    err = f" \033[31m✗\033[0m"
    if tc.duration_ms:
        err += f" {tc.duration_ms:.0f}ms"
    if tc.error:
        err += f"  {tc.error}"
    return f"  ⎿ {call}{err}"


def _render_separator(cols: int) -> str:
    """Render a horizontal separator."""
    return "\033[2m" + "─" * cols + "\033[0m"


def _render_mention_chip(chip: "MentionChip") -> list[str]:
    """Render a single MentionChip to one or more display lines."""
    lines: list[str] = []
    if chip.ok:
        size_part = f"  \033[2m{chip.display_size}\033[0m" if chip.display_size else ""
        lines.append(f"  \033[32m✓\033[0m {chip.raw}{size_part}")
    else:
        err = f": {chip.error}" if chip.error else ""
        lines.append(f"  \033[31m✗\033[0m {chip.raw}{err}")
    if chip.expanded and chip._content_lines:
        total = len(chip._content_lines)
        shown = chip._content_lines[:50]
        for cl in shown:
            lines.append(f"  \033[2m{cl}\033[0m")
        if total > 50:
            lines.append(f"  \033[2m... {total - 50} more lines\033[0m")
    return lines


def _render_turn(turn: AgentTurnEntry) -> list[str]:
    """Render a complete turn (header + body + tool calls + separator) to lines."""
    lines: list[str] = []
    lines.append(_render_turn_header(turn))
    for line in turn.output_lines:
        lines.append(line)
    for chip in turn.mention_chips:
        lines.extend(_render_mention_chip(chip))
    for tc in turn.tool_calls:
        if tc.committed_line:
            lines.append(tc.committed_line)
        else:
            lines.append(tc.render())
    footer = turn.footer()
    if footer:
        lines.append(footer)
    return lines


def _render_markdown_to_lines(text: str, cols: int) -> list[str]:
    """Render Markdown text to ANSI lines via Rich."""
    if not text:
        return []
    try:
        from rich.console import Console  # noqa: PLC0415
        from rich.markdown import Markdown as RichMarkdown  # noqa: PLC0415

        buf = io.StringIO()
        console = Console(
            file=buf,
            width=cols,
            highlight=False,
            markup=False,
            force_terminal=True,
            no_color=False,
        )
        console.print(RichMarkdown(text))
        raw = buf.getvalue()
        result = raw.splitlines()
        # Strip trailing blank lines
        while result and not result[-1].strip():
            result.pop()
        return result
    except ImportError:
        return text.splitlines()


def _render_diff(file_path: str, diff_text: str, cols: int) -> list[str]:
    """Render a unified diff to ANSI lines."""
    header = f"\033[2m─── Proposed change: {file_path} ───\033[0m"
    lines: list[str] = [header]
    diff_lines_raw = diff_text.splitlines()
    truncated = False
    remaining = 0
    if len(diff_lines_raw) > MAX_DIFF_LINES:
        truncated = True
        remaining = len(diff_lines_raw) - MAX_DIFF_LINES
        diff_lines_raw = diff_lines_raw[:MAX_DIFF_LINES]
    for line in diff_lines_raw:
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(f"\033[32m{line}\033[0m")
        elif line.startswith("-") and not line.startswith("---"):
            lines.append(f"\033[31m{line}\033[0m")
        elif line.startswith("@@"):
            lines.append(f"\033[36m{line}\033[0m")
        else:
            lines.append(f"\033[2m{line}\033[0m")
    if truncated:
        lines.append(f"\033[2m... {remaining} more lines\033[0m")
    lines.append(_render_separator(cols))
    return lines


# ---------------------------------------------------------------------------
# diff_lines — module-level function
# ---------------------------------------------------------------------------


def diff_lines(
    old: list[str],
    new: list[str],
) -> list[tuple[str, str]]:
    """
    Compute a line-level diff between old and new.

    Returns a list of (op, line) tuples where op is one of:
      "keep"   — line is unchanged
      "add"    — line was added in new
      "remove" — line was present in old but not in new
    """
    result: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in old[i1:i2]:
                result.append(("keep", line))
        elif tag == "insert":
            for line in new[j1:j2]:
                result.append(("add", line))
        elif tag == "delete":
            for line in old[i1:i2]:
                result.append(("remove", line))
        elif tag == "replace":
            for line in old[i1:i2]:
                result.append(("remove", line))
            for line in new[j1:j2]:
                result.append(("add", line))
    return result


# ---------------------------------------------------------------------------
# TranscriptModel
# ---------------------------------------------------------------------------


class TranscriptModel:
    """
    Mutable presentation model for the session transcript.

    Single source of truth for all agent turns, streaming partial text,
    committed-line list, and spinner state.
    """

    def __init__(self) -> None:
        self._turns: list[AgentTurnEntry] = []
        self._tool_index: dict[str, ToolCallEntry] = {}
        self._streaming_partial: str = ""
        self._committed_lines: list[str] = []
        self._committed_cursor: int = 0
        self._agent_color_map: dict[str, int] = {}
        self._next_color_index: int = 0
        self._spinner_frame: int = 0
        self._cols: int = 80
        self._current_ad: Any | None = None

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------

    def append_turn(
        self,
        agent_id: str,
        agent_name: str,
        timestamp: float | None = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> AgentTurnEntry:
        """
        Start a new agent turn.

        Commits the turn header line immediately to _committed_lines.
        Returns the new AgentTurnEntry.
        """
        if timestamp is None:
            timestamp = time.monotonic()
        if agent_id not in self._agent_color_map:
            self._agent_color_map[agent_id] = self._next_color_index % len(AGENT_COLORS)
            self._next_color_index += 1
        color_index = self._agent_color_map[agent_id]
        turn = AgentTurnEntry(
            agent_id=agent_id,
            agent_name=agent_name,
            timestamp=timestamp,
            color_index=color_index,
            cost_usd=cost_usd,
            tokens=tokens,
        )
        self._turns.append(turn)
        header = _render_turn_header(turn)
        self._committed_lines.append(header)
        turn.header_committed = True
        if len(self._turns) > MAX_TURNS_IN_MEMORY:
            self._evict_old_turns()
        return turn

    def append_line(self, agent_id: str, text: str) -> None:
        """
        Append a text line to the most-recent turn for agent_id.

        If no turn exists, auto-creates one. Does NOT commit to _committed_lines.
        """
        turn = self._turn_for(agent_id)
        turn.output_lines.append(text[:MAX_LINES_PER_TURN])

    def _turn_for(self, agent_id: str) -> AgentTurnEntry:
        """
        Return the most-recent turn for agent_id.
        Auto-creates a turn if none found.
        """
        for turn in reversed(self._turns):
            if turn.agent_id == agent_id:
                return turn
        return self.append_turn(agent_id, agent_id)

    def _get_turn_for_agent(self, agent_id: str) -> AgentTurnEntry | None:
        """Return the most-recent turn for agent_id, or None if not found."""
        for turn in reversed(self._turns):
            if turn.agent_id == agent_id:
                return turn
        return None

    # ------------------------------------------------------------------
    # Tool call management
    # ------------------------------------------------------------------

    def add_tool_call(
        self,
        agent_id: str,
        tool_use_id: str,
        name: str,
        args: dict | None = None,
        state: ToolCallState = ToolCallState.RUNNING,
    ) -> ToolCallEntry:
        """Register a new tool call for agent_id's current turn."""
        turn = self._get_turn_for_agent(agent_id)
        tc = ToolCallEntry(
            tool_use_id=tool_use_id,
            name=name,
            args=args or {},
            state=state,
        )
        if turn is not None:
            turn.tool_calls.append(tc)
        self._tool_index[tool_use_id] = tc
        return tc

    def update_tool_call(
        self,
        tool_use_id: str,
        state: ToolCallState | None = None,
        **kwargs: object,
    ) -> ToolCallEntry | None:
        """
        Update a tool call's state and optional fields.

        If state is SUCCESS or FAILURE, generates and appends committed_line
        (unless one already exists). Returns None if tool_use_id not found.
        """
        tc = self._tool_index.get(tool_use_id)
        if tc is None:
            return None
        if state is not None:
            tc.state = state
        for k, v in kwargs.items():
            setattr(tc, k, v)
        if state in (ToolCallState.SUCCESS, ToolCallState.FAILURE) and not tc.committed_line:
            tc.committed_line = _render_tool_call_committed(tc)
            self._committed_lines.append(tc.committed_line)
        return tc

    def finish_tool_call(
        self,
        tool_use_id: str,
        success: bool,
        duration_ms: float = 0.0,
        result_summary: str = "",
        error: str = "",
        output_lines: list[str] | None = None,
        diff_text: str = "",
    ) -> ToolCallEntry | None:
        """Mark a tool call as SUCCESS or FAILURE and commit its line."""
        tc = self._tool_index.get(tool_use_id)
        if tc is None:
            return None
        tc.state = ToolCallState.SUCCESS if success else ToolCallState.FAILURE
        tc.duration_ms = duration_ms
        tc.result_summary = result_summary
        tc.error = error
        if output_lines:
            tc.output_lines = output_lines[:MAX_LINES_PER_TURN]
        if diff_text:
            tc.diff_text = diff_text
        tc.committed_line = _render_tool_call_committed(tc)
        self._committed_lines.append(tc.committed_line)
        return tc

    def _get_tool_call(self, tool_use_id: str) -> ToolCallEntry | None:
        """Find a ToolCallEntry by tool_use_id."""
        return self._tool_index.get(tool_use_id)

    # ------------------------------------------------------------------
    # Spinner
    # ------------------------------------------------------------------

    def advance_spinner(self) -> None:
        """Advance spinner for all RUNNING tool calls."""
        self._spinner_frame = (self._spinner_frame + 1) % len(SPINNER_FRAMES)
        for tc in self._tool_index.values():
            if tc.state == ToolCallState.RUNNING:
                tc.spinner_frame = (tc.spinner_frame + 1) % len(SPINNER_FRAMES)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def get_streaming_partial(self) -> str | None:
        """Return streaming partial text, or None if none active."""
        return self._streaming_partial if self._streaming_partial else None

    def set_streaming_partial(self, text: str) -> None:
        """Set streaming partial text (not committed to scrollback)."""
        self._streaming_partial = text

    def clear_streaming_partial(self) -> None:
        """Clear the streaming partial text."""
        self._streaming_partial = ""

    # ------------------------------------------------------------------
    # Turn finalization
    # ------------------------------------------------------------------

    def finalize_turn(
        self,
        agent_id: str,
        final_text: str,
        tokens: int = 0,
        cost_usd: float = 0.0,
        cols: int | None = None,
    ) -> list[str]:
        """Finalize the most-recent turn for agent_id; commits body + separator."""
        if cols is None:
            cols = self._cols
        turn = self._get_turn_for_agent(agent_id)
        if turn is None:
            return []
        rendered = _render_markdown_to_lines(final_text, cols)
        rendered = rendered[:MAX_LINES_PER_TURN]
        turn.output_lines = rendered
        turn.tokens = tokens
        turn.cost_usd = cost_usd
        turn.state = TurnState.COMPLETE
        turn.streaming_text = ""
        separator = _render_separator(cols)
        new_lines = rendered + [separator]
        self._committed_lines.extend(new_lines)
        self._check_finalization(turn)
        return new_lines

    def set_turn_error(self, agent_id: str, error_text: str) -> list[str]:
        """Mark the most-recent turn for agent_id as ERROR."""
        turn = self._get_turn_for_agent(agent_id)
        if turn is None:
            return []
        turn.state = TurnState.ERROR
        turn.streaming_text = ""
        error_line = f"  \033[31m✗\033[0m {error_text}"
        separator = _render_separator(self._cols)
        new_lines = [error_line, separator]
        self._committed_lines.extend(new_lines)
        return new_lines

    def cancel_turn(self, agent_id: str) -> list[str]:
        """Mark the most-recent turn for agent_id as CANCELLED."""
        turn = self._get_turn_for_agent(agent_id)
        if turn is None:
            return []
        turn.state = TurnState.CANCELLED
        turn.streaming_text = ""
        self._streaming_partial = ""
        cancel_line = "  \033[2m[cancelled]\033[0m"
        separator = _render_separator(self._cols)
        new_lines = [cancel_line, separator]
        self._committed_lines.extend(new_lines)
        return new_lines

    # ------------------------------------------------------------------
    # System messages
    # ------------------------------------------------------------------

    def commit_system_message(self, text: str, level: str = "info") -> list[str]:
        """Commit a system-level message directly to scrollback."""
        if level == "warning":
            line = f"\033[33m⚠\033[0m {text}"
        elif level == "error":
            line = f"\033[31m✗\033[0m {text}"
        else:
            line = f"\033[2m{text}\033[0m"
        self._committed_lines.append(line)
        return [line]

    def commit_diff_block(self, file_path: str, diff_text_str: str) -> list[str]:
        """Commit a unified diff block to scrollback."""
        lines = _render_diff(file_path, diff_text_str, self._cols)
        self._committed_lines.extend(lines)
        return lines

    # ------------------------------------------------------------------
    # Committed-line cursor
    # ------------------------------------------------------------------

    def get_new_committed_lines(self) -> list[str]:
        """Return committed lines not yet sent to Terminal, advancing cursor."""
        new = self._committed_lines[self._committed_cursor:]
        self._committed_cursor = len(self._committed_lines)
        return new

    def peek_new_committed_lines(self) -> list[str]:
        """Return committed lines not yet sent, WITHOUT advancing cursor."""
        return self._committed_lines[self._committed_cursor:]

    def finalized_line_count(self) -> int:
        """Return total number of committed lines."""
        return len(self._committed_lines)

    # ------------------------------------------------------------------
    # Memory eviction
    # ------------------------------------------------------------------

    def evict_old_turns(self, keep_last: int = 200) -> int:
        """Evict output_lines from old turns to save memory. Returns eviction count."""
        if len(self._turns) <= keep_last:
            return 0
        evict_before = len(self._turns) - keep_last
        count = 0
        for turn in self._turns[:evict_before]:
            if not turn._evicted:
                turn.output_lines = []
                for tc in turn.tool_calls:
                    tc.output_lines = []
                turn._evicted = True
                count += 1
        return count

    def _evict_old_turns(self) -> None:
        """Internal eviction called when MAX_TURNS_IN_MEMORY exceeded."""
        self.evict_old_turns(keep_last=MAX_TURNS_IN_MEMORY)

    def _check_finalization(self, turn: AgentTurnEntry) -> None:
        """Promote a COMPLETE turn to FINALIZED if all tool calls are terminal."""
        if turn.state != TurnState.COMPLETE:
            return
        if all(
            tc.state in (ToolCallState.SUCCESS, ToolCallState.FAILURE, ToolCallState.DENIED)
            for tc in turn.tool_calls
        ):
            turn.state = TurnState.FINALIZED

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, finalized_only: bool = False) -> list[str]:
        """Render all turns to a list of strings."""
        lines: list[str] = []
        for turn in self._turns:
            if finalized_only and turn.state == TurnState.STREAMING:
                continue
            lines.extend(_render_turn(turn))
        return lines

    def update_cols(self, cols: int) -> None:
        """Update the column width used for rendering."""
        self._cols = cols

    # ------------------------------------------------------------------
    # Session replay
    # ------------------------------------------------------------------

    def replay_from_store(
        self,
        conv_store: Any,
        session_id: str,
        last_n: int = 20,
        cols: int = 80,
    ) -> None:
        """Populate TranscriptModel from persisted session data."""
        self._cols = cols
        turns = conv_store.load_turns(session_id)[-last_n:]
        self.commit_system_message(
            f"── resumed session {session_id[:8]} · {len(turns)} turns ──",
            level="info",
        )
        for t in turns:
            self.append_turn(t["agent_id"], t["agent_name"], t["timestamp"])
            self.finalize_turn(
                t["agent_id"],
                t.get("final_text", ""),
                tokens=t.get("tokens", 0),
                cost_usd=t.get("cost_usd", 0.0),
                cols=cols,
            )
            for tc_dict in t.get("tool_calls", []):
                self.add_tool_call(
                    t["agent_id"],
                    tc_dict["tool_use_id"],
                    tc_dict["name"],
                    tc_dict.get("args", {}),
                )
                self.finish_tool_call(
                    tc_dict["tool_use_id"],
                    success=(tc_dict.get("state") == "SUCCESS"),
                    duration_ms=tc_dict.get("duration_ms", 0),
                    result_summary=tc_dict.get("result_summary", ""),
                    error=tc_dict.get("error", ""),
                )

    # ------------------------------------------------------------------
    # Ad support (PRD-20)
    # ------------------------------------------------------------------

    def set_current_ad(self, ad: Any | None) -> None:
        """Set the current ad."""
        self._current_ad = ad

    def current_ad(self) -> Any | None:
        """Return the current ad, or None."""
        return self._current_ad

    def add_mention_chips(self, agent_id: str, chips: list[MentionChip]) -> None:
        """Attach @mention chips to the most recent turn for agent_id."""
        for t in reversed(self._turns):
            if t.agent_id == agent_id:
                t.mention_chips.extend(chips)
                return

    def set_mention_content(self, agent_id: str, raw_mention: str, content: str) -> None:
        """Store expanded file content for a @mention chip.

        Stores the content on the turn's mention_content dict and populates the
        chip's _content_lines (used when chip.expanded=True to render inline).
        """
        for t in reversed(self._turns):
            if t.agent_id == agent_id:
                t.mention_content[raw_mention] = content
                lines = content.splitlines()  # store all lines; render truncates at 50
                # Update the matching chip's content lines
                for chip in t.mention_chips:
                    if chip.raw == raw_mention:
                        chip._content_lines = lines
                        return
                # If no chip found, still store content (chip may be added later)
                return

    def render_ad_panel(self) -> str | None:
        """Render the current ad as a string, or return None."""
        if self._current_ad is None:
            return None
        ad = self._current_ad
        text = getattr(ad, "text", "")
        cta_url = getattr(ad, "cta_url", "")
        if not text:
            return None
        if cta_url:
            return f"\033[2m[Ad] {text}  {cta_url}\033[0m"
        return f"\033[2m[Ad] {text}\033[0m"

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def turns(self) -> list[AgentTurnEntry]:
        """Read-only view of all turns."""
        return self._turns

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self._turns if t.cost_usd)

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens for t in self._turns if t.tokens)

    @property
    def spinner_frame(self) -> int:
        return self._spinner_frame

    @property
    def committed_cursor(self) -> int:
        return self._committed_cursor

    @property
    def all_committed_lines(self) -> list[str]:
        """Read-only view of all committed lines."""
        return self._committed_lines

    def has_running_tools(self) -> bool:
        """Return True if any tool call is in PENDING or RUNNING state."""
        for tc in self._tool_index.values():
            if tc.state in (ToolCallState.PENDING, ToolCallState.RUNNING):
                return True
        return False
