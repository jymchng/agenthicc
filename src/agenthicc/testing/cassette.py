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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"


# ── Wire format dataclasses ──────────────────────────────────────────────────

@dataclass(frozen=True)
class CassetteEntry:
    """One recorded transport.complete() call."""
    index:                 int
    model:                 str
    tool_names_available:  list[str]
    response_content:      str
    response_stop_reason:  str
    response_tool_calls:   list[dict[str, Any]]   # [{"name", "tool_use_id", "input"}]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CassetteEntry:
        resp = d.get("response", {})
        return cls(
            index=int(d.get("index", 0)),
            model=str(d.get("model", "")),
            tool_names_available=list(d.get("tool_names_available", [])),
            response_content=str(resp.get("content", "")),
            response_stop_reason=str(resp.get("stop_reason", "end_turn")),
            response_tool_calls=list(resp.get("tool_calls", [])),
        )


@dataclass(frozen=True)
class ApprovalEntry:
    """One recorded approval gate response."""
    index:        int
    kind:         str    # "tool" | "plan_review"
    tool_name:    str
    allowed:      bool
    message:      str
    remember:     bool
    remember_all: bool

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApprovalEntry:
        return cls(
            index=int(d.get("index", 0)),
            kind=str(d.get("kind", "tool")),
            tool_name=str(d.get("tool_name", "")),
            allowed=bool(d.get("allowed", True)),
            message=str(d.get("message", "")),
            remember=bool(d.get("remember", False)),
            remember_all=bool(d.get("remember_all", False)),
        )


# ── SessionCassette ───────────────────────────────────────────────────────────

@dataclass
class SessionCassette:
    """Immutable in-memory representation of a recorded session cassette.

    Construct via the class-method factories:

    * :meth:`from_path` — explicit file paths (preferred for tests)
    * :meth:`from_session` — auto-discover from a live session directory
    """

    entries:   list[CassetteEntry]    = field(default_factory=list)
    approvals: list[ApprovalEntry]    = field(default_factory=list)
    intent:    str                    = ""

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
        cassette_path  = cassette_dir / "cassette.jsonl"
        approvals_path = cassette_dir / "approvals.jsonl"
        meta_path      = cassette_dir / "meta.json"

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

    def to_mock_transport(self) -> Any:
        """Return a configured MockTransport with all responses queued.

        Each cassette entry is converted to a sequence of
        :class:`~lauren_ai._transport.CompletionChunk` objects so that the
        streaming agent runner receives proper tool-call deltas.
        """
        from lauren_ai._transport import (  # noqa: PLC0415
            CompletionChunk, ToolCallDelta, TokenUsage,
        )
        from lauren_ai._transport._mock import MockTransport  # noqa: PLC0415

        mock = MockTransport()
        for entry in sorted(self.entries, key=lambda e: e.index):
            chunks: list[CompletionChunk] = []

            if entry.response_tool_calls:
                for tc in entry.response_tool_calls:
                    name = str(tc.get("name", ""))
                    tid  = str(tc.get("tool_use_id", f"tu_{entry.index}"))
                    inp  = tc.get("input", {})

                    # First chunk: announce tool name
                    chunks.append(CompletionChunk(
                        tool_call_delta=ToolCallDelta(
                            tool_use_id=tid, name=name, input_delta="",
                        )
                    ))
                    # Second chunk: full input JSON
                    chunks.append(CompletionChunk(
                        tool_call_delta=ToolCallDelta(
                            tool_use_id=tid, name=None,
                            input_delta=json.dumps(inp, ensure_ascii=False),
                        )
                    ))
                # Terminal chunk
                chunks.append(CompletionChunk(
                    stop_reason="tool_use",
                    usage=TokenUsage(input_tokens=0, output_tokens=0),
                ))
            else:
                # Plain text response
                if entry.response_content:
                    chunks.append(CompletionChunk(delta=entry.response_content))
                chunks.append(CompletionChunk(
                    stop_reason=entry.response_stop_reason,
                    usage=TokenUsage(input_tokens=0, output_tokens=0),
                ))

            mock.queue_stream(chunks)
        return mock

    def to_mock_approval_service(self) -> MockApprovalService:
        """Return a :class:`MockApprovalService` pre-loaded with recorded responses."""
        from agenthicc.testing.mock_approval import MockApprovalService  # noqa: PLC0415
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


# Circular-import guard: only import for type annotation.
from agenthicc.testing.mock_approval import MockApprovalService  # noqa: E402
