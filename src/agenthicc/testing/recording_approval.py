"""RecordingApprovalService — wraps ApprovalService and saves responses to JSONL.

Wired by the TUI session alongside RecordingTransport when --record-cassette
is given.  Delegates all calls to the inner service; after each approval
response is returned, appends one line to ``approvals_path``::

    {
      "index": 0,
      "kind": "plan_review",
      "tool_name": "request_plan_approval",
      "allowed": true,
      "message": "approved",
      "remember": false,
      "remember_all": false
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agenthicc.tools.approval import ApprovalRequest, ApprovalService


class RecordingApprovalService:
    """Transparent proxy around ApprovalService that records each response.

    Designed to be substituted directly for the session's ``approval_svc``
    attribute; all methods delegate to the inner service so the TUI overlay
    (which calls ``respond()``) continues to work normally.
    """

    def __init__(self, inner: ApprovalService, approvals_path: Path) -> None:
        self._inner = inner
        self._path = approvals_path
        self._index = 0
        approvals_path.parent.mkdir(parents=True, exist_ok=True)
        approvals_path.write_text("", encoding="utf-8")

    # ── ApprovalService interface ─────────────────────────────────────────────

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        response = await self._inner.request_approval(req)
        self._append(req, response)
        return response

    def respond(
        self,
        allowed: bool,
        *,
        remember: bool = False,
        remember_all: bool = False,
        message: str = "",
    ) -> None:
        self._inner.respond(
            allowed,
            remember=remember,
            remember_all=remember_all,
            message=message,
        )

    def reset_turn_memory(self) -> None:
        self._inner.reset_turn_memory()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # ── Recording ─────────────────────────────────────────────────────────────

    def _append(self, req: ApprovalRequest, response: ApprovalResponse) -> None:
        entry: dict[str, Any] = {
            "index": self._index,
            "kind": req.kind,
            "tool_name": req.tool_name,
            "allowed": response.allowed,
            "message": response.message,
            "remember": response.remember,
            "remember_all": response.remember_all,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._index += 1
