"""SessionCassette — load cassette files and produce MockTransport / MockApprovalService.

Cassette files are plain JSONL; users copy them into their test tree and
reference them by path::

    cassette = SessionCassette.from_path(
        cassette_path="tests/fixtures/plan_mode/cassette.jsonl",
        approvals_path="tests/fixtures/plan_mode/approvals.jsonl",
        intent="enhance this repo",
    )

    # Or load from a live session directory (auto-discovers the files)
    cassette = SessionCassette.from_session("67bab0e3-fb9d-4bbc-b971-70d5e9785b49")

    mock_transport = cassette.to_mock_transport()
    mock_approval  = cassette.to_mock_approval_service()
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai import CompletionChunk
    from lauren_ai._transport._mock import MockTransport

    from agenthicc.testing.mock_approval import MockApprovalService
    from agenthicc.testing.replay_transport import ReplayTransport

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"


def _integer(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


# ── Wire format dataclasses ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CassetteEntry:
    """One recorded transport.complete() call."""

    index: int
    model: str
    tool_names_available: list[str]
    response_content: str
    response_stop_reason: str
    response_tool_calls: list[dict[str, object]]  # [{"name", "tool_use_id", "input"}]
    response_reasoning_content: str | None = None

    @property
    def is_tool_free(self) -> bool:
        """True when the recorded request offered no tools at all.

        In agenthicc recordings every agent turn offers the session's tool
        catalog, so a tool-free request is a conversation-summary call made by
        auto-compaction.  A hermetic replay disables compaction, so it skips
        these entries rather than serving a summary to an ordinary turn.
        """
        return not self.tool_names_available

    @property
    def called_tool_names(self) -> tuple[str, ...]:
        """Names of the tools this recorded response called, in order."""
        return tuple(str(call.get("name", "")) for call in self.response_tool_calls)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> CassetteEntry:
        raw_resp = d.get("response", {})
        resp: Mapping[str, object] = raw_resp if isinstance(raw_resp, Mapping) else {}
        raw_names = d.get("tool_names_available", [])
        names = [str(name) for name in raw_names] if isinstance(raw_names, list) else []
        raw_calls = resp.get("tool_calls", [])
        calls = raw_calls if isinstance(raw_calls, list) else []
        raw_reasoning_content = resp.get("reasoning_content")
        if raw_reasoning_content is not None and not isinstance(raw_reasoning_content, str):
            raise ValueError("cassette response reasoning_content must be a string when present")
        reasoning_content = (
            raw_reasoning_content if isinstance(raw_reasoning_content, str) else None
        )
        return cls(
            index=_integer(d.get("index")),
            model=str(d.get("model", "")),
            tool_names_available=names,
            response_content=str(resp.get("content", "")),
            response_stop_reason=str(resp.get("stop_reason", "end_turn")),
            response_tool_calls=[call for call in calls if isinstance(call, dict)],
            response_reasoning_content=reasoning_content,
        )


@dataclass(frozen=True)
class ApprovalEntry:
    """One recorded approval gate response."""

    index: int
    kind: str  # "tool" | "plan_review"
    tool_name: str
    allowed: bool
    message: str
    remember: bool
    remember_all: bool
    mode: str | None = None
    scope_grant: str | None = None
    workspace_access: tuple[dict[str, str | None], ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> ApprovalEntry:
        raw_mode = d.get("mode")
        raw_scope_grant = d.get("scope_grant")
        raw_workspace = d.get("workspace_access", [])
        workspace_access: list[dict[str, str | None]] = []
        if isinstance(raw_workspace, list):
            for item in raw_workspace:
                if not isinstance(item, Mapping):
                    continue
                workspace_access.append(
                    {
                        "requested": (
                            item.get("requested")
                            if isinstance(item.get("requested"), str)
                            else None
                        ),
                        "canonical": (
                            item.get("canonical")
                            if isinstance(item.get("canonical"), str)
                            else None
                        ),
                        "operation": (
                            item.get("operation")
                            if isinstance(item.get("operation"), str)
                            else None
                        ),
                    }
                )
        return cls(
            index=_integer(d.get("index")),
            kind=str(d.get("kind", "tool")),
            tool_name=str(d.get("tool_name", "")),
            allowed=bool(d.get("allowed", True)),
            message=str(d.get("message", "")),
            remember=bool(d.get("remember", False)),
            remember_all=bool(d.get("remember_all", False)),
            mode=raw_mode if isinstance(raw_mode, str) else None,
            scope_grant=raw_scope_grant if isinstance(raw_scope_grant, str) else None,
            workspace_access=tuple(workspace_access),
        )


# ── SessionCassette ───────────────────────────────────────────────────────────


@dataclass
class SessionCassette:
    """Immutable in-memory representation of a recorded session cassette.

    Construct via the class-method factories:

    * :meth:`from_path` — explicit file paths (preferred for tests)
    * :meth:`from_session` — auto-discover from a live session directory
    """

    entries: list[CassetteEntry] = field(default_factory=list)
    approvals: list[ApprovalEntry] = field(default_factory=list)
    intent: str = ""

    # ── Factory methods ───────────────────────────────────────────────────────

    @classmethod
    def from_path(
        cls,
        cassette_path: str | Path,
        approvals_path: str | Path | None = None,
        intent: str = "",
    ) -> SessionCassette:
        """Load from explicit file paths.

        Parameters
        ----------
        cassette_path:
            Path to the ``cassette.jsonl`` file (required).
        approvals_path:
            Path to the ``approvals.jsonl`` file.  Pass ``None`` when the
            session had no approval gates, or when you want all gates
            auto-approved during replay.
        intent:
            The user intent string to pass to ``CodePlanRunner.run()``.  Must
            be supplied when the cassette was not recorded with ``--record-cassette``
            (which writes ``meta.json`` automatically).
        """
        entries = cls._load_entries(Path(cassette_path))
        approvals: list[ApprovalEntry] = []
        if approvals_path is not None:
            approvals = cls._load_approvals(Path(approvals_path))

        # Try to read intent from adjacent meta.json if not given explicitly.
        if not intent:
            meta_path = Path(cassette_path).with_name("meta.json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    intent = str(meta.get("intent", ""))
                except Exception:  # noqa: BLE001
                    pass

        return cls(entries=entries, approvals=approvals, intent=intent)

    @classmethod
    def from_session(cls, session_id: str) -> SessionCassette:
        """Auto-discover cassette files recorded by ``--record-cassette``.

        The files are expected at::

            ~/.agenthicc/sessions/<session_id>/cassette/cassette.jsonl
            ~/.agenthicc/sessions/<session_id>/cassette/approvals.jsonl
            ~/.agenthicc/sessions/<session_id>/cassette/meta.json
        """
        cassette_dir = _SESSIONS_DIR / session_id / "cassette"
        cassette_path = cassette_dir / "cassette.jsonl"
        approvals_path = cassette_dir / "approvals.jsonl"
        meta_path = cassette_dir / "meta.json"

        if not cassette_path.exists():
            raise FileNotFoundError(
                f"No cassette found for session {session_id!r}. "
                f"Run the session with --record-cassette to create one.\n"
                f"Expected: {cassette_path}"
            )

        intent = ""
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                intent = str(meta.get("intent", ""))
            except Exception:  # noqa: BLE001
                pass

        return cls.from_path(
            cassette_path=cassette_path,
            approvals_path=approvals_path if approvals_path.exists() else None,
            intent=intent,
        )

    # ── Transport / approval conversion ───────────────────────────────────────

    def to_mock_transport(self) -> "MockTransport":
        """Return a configured MockTransport with all responses queued.

        Each cassette entry is converted to a sequence of
        :class:`~lauren_ai._transport.CompletionChunk` objects so that the
        streaming agent runner receives proper tool-call deltas.
        """
        from lauren_ai._transport._mock import MockTransport

        mock = MockTransport()
        for entry in sorted(self.entries, key=lambda e: e.index):
            mock.queue_stream(_entry_chunks(entry))
        return mock

    def slice(self, start_index: int, *, skip_approvals: int = 0) -> SessionCassette:
        """Return the recording from entry *start_index* onward.

        A long recording can begin with the tail of an earlier workflow run —
        for example when a session was resumed mid-campaign before a fresh run
        started.  Slicing at the first request of the run under test lets a
        replay start that run from its first phase.  *skip_approvals* drops the
        recorded approval answers that belong to the discarded entries; approval
        records do not name the request that consumed them, so the caller states
        the count explicitly.
        """
        if start_index < 0:
            raise ValueError("start_index must be non-negative")
        if skip_approvals < 0 or skip_approvals > len(self.approvals):
            raise ValueError("skip_approvals must be between 0 and the number of approvals")
        ordered_approvals = sorted(self.approvals, key=lambda e: e.index)
        return SessionCassette(
            entries=[entry for entry in self.entries if entry.index >= start_index],
            approvals=ordered_approvals[skip_approvals:],
            intent=self.intent,
        )

    def replay_entries(self, *, skip_tool_free: bool) -> list[CassetteEntry]:
        """Return the entries a replay serves, in recorded order.

        ``skip_tool_free=True`` drops conversation-summary (auto-compaction)
        calls; use it whenever the replay runs with compaction disabled.
        """
        ordered = sorted(self.entries, key=lambda e: e.index)
        return [entry for entry in ordered if not (skip_tool_free and entry.is_tool_free)]

    def to_replay_transport(self, *, skip_tool_free: bool) -> "ReplayTransport":
        """Return a :class:`ReplayTransport` that parks after the last response."""
        from agenthicc.testing.replay_transport import ReplayTransport

        entries = self.replay_entries(skip_tool_free=skip_tool_free)
        return ReplayTransport(
            [_entry_chunks(entry) for entry in entries],
            [entry.index for entry in entries],
        )

    def to_mock_approval_service(self) -> MockApprovalService:
        """Return a :class:`MockApprovalService` pre-loaded with recorded responses."""
        from agenthicc.testing.mock_approval import MockApprovalService

        return MockApprovalService(list(self.approvals))

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _load_entries(path: Path) -> list[CassetteEntry]:
        if not path.exists():
            return []
        entries: list[CassetteEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(CassetteEntry.from_dict(json.loads(line)))
            except Exception:  # noqa: BLE001
                pass
        return sorted(entries, key=lambda e: e.index)

    @staticmethod
    def _load_approvals(path: Path) -> list[ApprovalEntry]:
        if not path.exists():
            return []
        approvals: list[ApprovalEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                approvals.append(ApprovalEntry.from_dict(json.loads(line)))
            except Exception:  # noqa: BLE001
                pass
        return sorted(approvals, key=lambda e: e.index)


def _entry_chunks(entry: CassetteEntry) -> list[CompletionChunk]:
    """Convert one recorded response into the streamed chunks a provider sends.

    Assistant text precedes tool calls, exactly as a provider streams a turn
    that both comments and calls tools; each tool call is announced by name and
    then completed with its full JSON input.
    """
    from lauren_ai import CompletionChunk, TokenUsage
    from lauren_ai._transport import ToolCallDelta

    chunks: list[CompletionChunk] = []
    if entry.response_reasoning_content is not None:
        chunks.append(CompletionChunk(reasoning_content_delta=entry.response_reasoning_content))
    if entry.response_content:
        chunks.append(CompletionChunk(delta=entry.response_content))
    for call in entry.response_tool_calls:
        tool_use_id = str(call.get("tool_use_id", f"tu_{entry.index}"))
        chunks.append(
            CompletionChunk(
                tool_call_delta=ToolCallDelta(
                    tool_use_id=tool_use_id,
                    name=str(call.get("name", "")),
                    input_delta="",
                )
            )
        )
        chunks.append(
            CompletionChunk(
                tool_call_delta=ToolCallDelta(
                    tool_use_id=tool_use_id,
                    name=None,
                    input_delta=json.dumps(call.get("input", {}), ensure_ascii=False),
                )
            )
        )
    chunks.append(
        CompletionChunk(
            stop_reason="tool_use" if entry.response_tool_calls else entry.response_stop_reason,
            usage=TokenUsage(input_tokens=0, output_tokens=0),
        )
    )
    return chunks
