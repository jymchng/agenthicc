---
title: "PRD-17: Microsoft Outlook and Office Tools"
status: draft
version: 0.1.0
created: 2025-01-01
---

# PRD-17: Microsoft Outlook and Office Tools

## Executive Summary

Agents handling tasks in enterprise environments need to read and send email, create calendar events, and interact with Office documents. This PRD specifies an `OutlookToolKit` with 12 tools delivered via two backends: a **Windows-native COM backend** using `pywin32` (direct Outlook automation, zero latency) and a **cross-platform Graph API backend** using `httpx` and the Microsoft Graph REST API. Backend selection is automatic (`"auto"` tries COM first, falls back to Graph). All tools return structured dicts; write operations (send, create event) are behind `outlook:send:allow` and `outlook:calendar:write:allow` permissions.

---

## Goals

| ID | Goal |
|----|------|
| G1 | 12 tools covering email read/write, search, calendar, and Office file access |
| G2 | Dual backend: COM (Windows + Outlook installed) and Graph API (cross-platform) |
| G3 | Backend auto-detected at runtime; explicit override via config |
| G4 | Send/create operations behind explicit permission rules |
| G5 | Graph API token from `AuthClient` (PRD-11) or `MSGRAPH_TOKEN` env var |
| G6 | COM tests skipped on non-Windows via `pytest.mark.skipif(sys.platform != "win32")` |

## Non-Goals
- SharePoint / Teams integration (separate PRD)
- On-premise Exchange without Graph

---

## Tool Catalog

| Tool | Description | COM? | Graph? |
|------|-------------|------|--------|
| `outlook_list_emails` | List emails from a folder | ✓ | ✓ |
| `outlook_read_email` | Read full email by ID | ✓ | ✓ |
| `outlook_send_email` | Send a new email | ✓ | ✓ |
| `outlook_reply_email` | Reply to an email | ✓ | ✓ |
| `outlook_search_emails` | Full-text search | ✓ | ✓ |
| `outlook_move_email` | Move to folder | ✓ | ✓ |
| `outlook_list_folders` | List mail folders | ✓ | ✓ |
| `outlook_calendar_events` | List events in date range | ✓ | ✓ |
| `outlook_create_event` | Create calendar event | ✓ | ✓ |
| `outlook_com_run_macro` | Run VBA macro | ✓ | ✗ |
| `word_read_document` | Read Word file text | ✓ | ✗ |
| `excel_read_range` | Read Excel cell range | ✓ | ✗ |

---

## Data Structures and Interfaces

```python
# src/agenthicc/tools/outlook/__init__.py
from __future__ import annotations

import abc
import os
import sys
from typing import Any

from agenthicc.tools.base import Tool

__all__ = ["OutlookToolKit"]

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class OutlookBackend(abc.ABC):
    """Abstract backend — COM or Graph API."""

    @abc.abstractmethod
    async def list_emails(self, folder: str, n: int, unread_only: bool) -> list[dict]: ...

    @abc.abstractmethod
    async def read_email(self, email_id: str) -> dict: ...

    @abc.abstractmethod
    async def send_email(self, to: list[str], subject: str, body: str,
                         cc: list[str] | None, attachments: list[str] | None) -> dict: ...

    @abc.abstractmethod
    async def reply_email(self, email_id: str, body: str, reply_all: bool) -> dict: ...

    @abc.abstractmethod
    async def search_emails(self, query: str, folder: str, n: int) -> list[dict]: ...

    @abc.abstractmethod
    async def move_email(self, email_id: str, destination: str) -> dict: ...

    @abc.abstractmethod
    async def list_folders(self) -> list[dict]: ...

    @abc.abstractmethod
    async def calendar_events(self, start_date: str, end_date: str) -> list[dict]: ...

    @abc.abstractmethod
    async def create_event(self, subject: str, start: str, end: str,
                           attendees: list[str] | None, body: str | None) -> dict: ...


class GraphApiOutlookBackend(OutlookBackend):
    """Microsoft Graph REST API backend (cross-platform)."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.getenv("MSGRAPH_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    async def _get(self, path: str) -> Any:
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{GRAPH_BASE}{path}", headers=self._headers(), timeout=15.0)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{GRAPH_BASE}{path}", json=body,
                                  headers=self._headers(), timeout=15.0)
            r.raise_for_status()
            return r.json()

    async def list_emails(self, folder: str = "Inbox", n: int = 20, unread_only: bool = False) -> list[dict]:
        params = f"$top={n}&$select=id,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview"
        if unread_only:
            params += "&$filter=isRead eq false"
        data = await self._get(f"/me/mailFolders/{folder}/messages?{params}")
        return [
            {"id": m["id"], "subject": m["subject"],
             "from": m["from"]["emailAddress"]["address"],
             "date": m["receivedDateTime"],
             "unread": not m["isRead"],
             "body_preview": m["bodyPreview"]}
            for m in data.get("value", [])
        ]

    async def read_email(self, email_id: str) -> dict:
        m = await self._get(f"/me/messages/{email_id}?$select=id,subject,from,toRecipients,ccRecipients,receivedDateTime,body")
        return {
            "subject": m["subject"],
            "from": m["from"]["emailAddress"]["address"],
            "to": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
            "cc": [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])],
            "date": m["receivedDateTime"],
            "body_html": m["body"]["content"] if m["body"]["contentType"] == "html" else "",
            "body_text": m["body"]["content"] if m["body"]["contentType"] == "text" else "",
        }

    async def send_email(self, to: list[str], subject: str, body: str,
                         cc: list[str] | None = None, attachments: list[str] | None = None) -> dict:
        msg = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
                "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
            }
        }
        await self._post("/me/sendMail", msg)
        return {"ok": True, "message_id": ""}

    async def reply_email(self, email_id: str, body: str, reply_all: bool = False) -> dict:
        endpoint = "replyAll" if reply_all else "reply"
        await self._post(f"/me/messages/{email_id}/{endpoint}",
                         {"message": {}, "comment": body})
        return {"ok": True, "message_id": email_id}

    async def search_emails(self, query: str, folder: str = "Inbox", n: int = 20) -> list[dict]:
        import urllib.parse  # noqa: PLC0415
        q = urllib.parse.quote(query)
        data = await self._get(f"/me/mailFolders/{folder}/messages?$search=\"{q}\"&$top={n}&$select=id,subject,from,receivedDateTime,bodyPreview")
        return [{"id": m["id"], "subject": m["subject"],
                 "from": m["from"]["emailAddress"]["address"],
                 "date": m["receivedDateTime"],
                 "body_preview": m["bodyPreview"]}
                for m in data.get("value", [])]

    async def move_email(self, email_id: str, destination: str) -> dict:
        await self._post(f"/me/messages/{email_id}/move",
                         {"destinationId": destination})
        return {"ok": True}

    async def list_folders(self) -> list[dict]:
        data = await self._get("/me/mailFolders?$select=id,displayName,totalItemCount,unreadItemCount")
        return [{"id": f["id"], "name": f["displayName"],
                 "unread_count": f["unreadItemCount"]}
                for f in data.get("value", [])]

    async def calendar_events(self, start_date: str, end_date: str) -> list[dict]:
        params = f"startDateTime={start_date}&endDateTime={end_date}&$select=id,subject,start,end,location,attendees,body"
        data = await self._get(f"/me/calendarView?{params}")
        events = []
        for e in data.get("value", []):
            events.append({
                "id": e["id"], "subject": e["subject"],
                "start": e["start"]["dateTime"], "end": e["end"]["dateTime"],
                "location": e.get("location", {}).get("displayName", ""),
                "attendees": [a["emailAddress"]["address"] for a in e.get("attendees", [])],
            })
        return events

    async def create_event(self, subject: str, start: str, end: str,
                           attendees: list[str] | None = None, body: str | None = None) -> dict:
        event = {
            "subject": subject,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in (attendees or [])],
        }
        if body:
            event["body"] = {"contentType": "Text", "content": body}
        result = await self._post("/me/events", event)
        return {"ok": True, "event_id": result.get("id", "")}


# ── Tool wrappers ─────────────────────────────────────────────────────────


class _OutlookToolBase(Tool):
    def __init__(self, backend: OutlookBackend) -> None:
        self._backend = backend


class OutlookListEmailsTool(_OutlookToolBase):
    name = "outlook_list_emails"
    description = "List emails from an Outlook folder."
    parameters = {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "default": "Inbox"},
            "n": {"type": "integer", "default": 20},
            "unread_only": {"type": "boolean", "default": False},
        },
    }
    async def execute(self, args, context):
        emails = await self._backend.list_emails(
            args.get("folder", "Inbox"), int(args.get("n", 20)), bool(args.get("unread_only", False))
        )
        return {"emails": emails, "count": len(emails)}


class OutlookSendEmailTool(_OutlookToolBase):
    name = "outlook_send_email"
    description = "Send an email via Outlook."
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["to", "subject", "body"],
    }
    async def execute(self, args, context):
        return await self._backend.send_email(
            args["to"], args["subject"], args["body"],
            args.get("cc"), args.get("attachments"),
        )


class OutlookCalendarEventsTool(_OutlookToolBase):
    name = "outlook_calendar_events"
    description = "List calendar events in a date range."
    parameters = {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "ISO 8601 datetime"},
            "end_date": {"type": "string"},
        },
        "required": ["start_date", "end_date"],
    }
    async def execute(self, args, context):
        events = await self._backend.calendar_events(args["start_date"], args["end_date"])
        return {"events": events, "count": len(events)}


# (Additional tool classes follow the same pattern for each tool in the catalog)


class OutlookToolKit:
    """Factory — auto-selects backend and returns all tools."""

    def __init__(
        self,
        backend: str = "auto",
        token: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self._backend_name = backend
        self._token = token
        self._tenant_id = tenant_id

    def _build_backend(self) -> OutlookBackend:
        if self._backend_name in ("auto", "win32") and sys.platform == "win32":
            try:
                import win32com.client  # noqa: PLC0415 F401
                from agenthicc.tools.outlook.win32_backend import Win32OutlookBackend  # noqa: PLC0415
                return Win32OutlookBackend()
            except ImportError:
                if self._backend_name == "win32":
                    raise ImportError("pywin32 required for win32 backend: pip install pywin32")
        return GraphApiOutlookBackend(token=self._token)

    def tools(self) -> list[Tool]:
        backend = self._build_backend()
        return [
            OutlookListEmailsTool(backend),
            OutlookSendEmailTool(backend),
            OutlookCalendarEventsTool(backend),
            # ... (all 12 tools instantiated with same backend)
        ]
```

---

## Configuration Reference

```toml
[tools.outlook]
backend = "auto"
graph_tenant_id = ""
max_emails_per_query = 50
allow_send = true
allow_delete = false
calendar_timezone = "UTC"

[[security.permission_rules]]
tool_pattern = "outlook_send_email"
action = "require_confirmation"

[[security.permission_rules]]
tool_pattern = "outlook_create_event"
action = "require_confirmation"

[[security.permission_rules]]
tool_pattern = "outlook_list_*"
action = "allow"

[[security.permission_rules]]
tool_pattern = "outlook_read_*"
action = "allow"
```

---

## Tests

```python
# tests/unit/test_outlook_tools.py
"""Unit tests for Outlook/Graph API tools (PRD-17)."""
from __future__ import annotations
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agenthicc.tools.outlook import GraphApiOutlookBackend, OutlookListEmailsTool, OutlookSendEmailTool

pytestmark = pytest.mark.unit


class TestGraphApiBackend:
    async def test_list_emails_returns_structured_dicts(self):
        backend = GraphApiOutlookBackend(token="fake")
        mock_data = {"value": [
            {"id": "msg1", "subject": "Hello", "from": {"emailAddress": {"address": "a@b.com"}},
             "receivedDateTime": "2025-01-01T10:00:00Z", "isRead": False, "bodyPreview": "Hi there"},
        ]}
        with patch.object(backend, "_get", new_callable=AsyncMock, return_value=mock_data):
            emails = await backend.list_emails("Inbox", 10, False)
        assert len(emails) == 1
        assert emails[0]["subject"] == "Hello"
        assert emails[0]["unread"] is True

    async def test_send_email_calls_post(self):
        backend = GraphApiOutlookBackend(token="fake")
        with patch.object(backend, "_post", new_callable=AsyncMock, return_value={}) as mock_post:
            result = await backend.send_email(["user@example.com"], "Subj", "Body")
        assert result["ok"] is True
        mock_post.assert_called_once()
        call_body = mock_post.call_args[0][1]
        assert call_body["message"]["subject"] == "Subj"

    async def test_list_emails_tool_returns_count(self):
        backend = MagicMock()
        backend.list_emails = AsyncMock(return_value=[{"id": "1"}, {"id": "2"}])
        tool = OutlookListEmailsTool(backend)
        result = await tool.execute({"n": 2}, {})
        assert result["count"] == 2

    @pytest.mark.skipif(sys.platform != "win32", reason="COM only on Windows")
    async def test_win32_backend_available_on_windows(self):
        from agenthicc.tools.outlook import OutlookToolKit
        kit = OutlookToolKit(backend="auto")
        # Should not raise on Windows with Outlook installed
        backend = kit._build_backend()
        assert backend is not None
```

---

## Open Questions

1. **OAuth for Graph API**: Graph API requires app registration in Azure AD. For users without an Azure tenant, a pre-registered agenthicc.ai app can provide delegated access (same OAuth flow as PRD-11, different scopes: `Mail.Read Mail.Send Calendars.ReadWrite`).
2. **Attachments in `send_email`**: the Graph API supports inline attachments up to 4 MB. For `run_bash`/`run_python` generated files, read the file and base64-encode it. Document the flow.
3. **Win32 thread safety**: COM dispatch must run on the thread that called `CoInitialize`. Wrap all win32 calls in `asyncio.to_thread` with a dedicated single thread per COM object.
