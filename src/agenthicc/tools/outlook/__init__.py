"""Microsoft Outlook tools via Graph API (PRD-17)."""
from __future__ import annotations

import abc
import os
import urllib.parse
from typing import Any

from agenthicc.tools.base import Tool

__all__ = ["GraphApiOutlookBackend", "OutlookBackend", "OutlookToolKit"]

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class OutlookBackend(abc.ABC):
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
    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.getenv("MSGRAPH_TOKEN", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def _get(self, path: str) -> Any:
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{GRAPH_BASE}{path}", headers=self._headers(), timeout=15.0)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{GRAPH_BASE}{path}", json=body, headers=self._headers(), timeout=15.0)
            r.raise_for_status()
            return r.json() if r.content else {}

    async def list_emails(self, folder: str = "Inbox", n: int = 20, unread_only: bool = False) -> list[dict]:
        params = f"$top={n}&$select=id,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview"
        if unread_only:
            params += "&$filter=isRead eq false"
        data = await self._get(f"/me/mailFolders/{folder}/messages?{params}")
        return [
            {"id": m["id"], "subject": m.get("subject", ""),
             "from": m.get("from", {}).get("emailAddress", {}).get("address", ""),
             "date": m.get("receivedDateTime", ""),
             "unread": not m.get("isRead", True),
             "body_preview": m.get("bodyPreview", "")}
            for m in data.get("value", [])
        ]

    async def read_email(self, email_id: str) -> dict:
        m = await self._get(f"/me/messages/{email_id}?$select=id,subject,from,toRecipients,ccRecipients,receivedDateTime,body")
        return {
            "subject": m.get("subject", ""),
            "from": m.get("from", {}).get("emailAddress", {}).get("address", ""),
            "to": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
            "cc": [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])],
            "date": m.get("receivedDateTime", ""),
            "body_html": m.get("body", {}).get("content", "") if m.get("body", {}).get("contentType") == "html" else "",
            "body_text": m.get("body", {}).get("content", "") if m.get("body", {}).get("contentType") == "text" else "",
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
        await self._post(f"/me/messages/{email_id}/{endpoint}", {"comment": body})
        return {"ok": True, "message_id": email_id}

    async def search_emails(self, query: str, folder: str = "Inbox", n: int = 20) -> list[dict]:
        q = urllib.parse.quote(query)
        data = await self._get(f"/me/mailFolders/{folder}/messages?$search=\"{q}\"&$top={n}&$select=id,subject,from,receivedDateTime,bodyPreview")
        return [
            {"id": m["id"], "subject": m.get("subject", ""),
             "from": m.get("from", {}).get("emailAddress", {}).get("address", ""),
             "date": m.get("receivedDateTime", ""),
             "body_preview": m.get("bodyPreview", "")}
            for m in data.get("value", [])
        ]

    async def move_email(self, email_id: str, destination: str) -> dict:
        await self._post(f"/me/messages/{email_id}/move", {"destinationId": destination})
        return {"ok": True}

    async def list_folders(self) -> list[dict]:
        data = await self._get("/me/mailFolders?$select=id,displayName,totalItemCount,unreadItemCount")
        return [{"id": f["id"], "name": f.get("displayName", ""),
                 "unread_count": f.get("unreadItemCount", 0)}
                for f in data.get("value", [])]

    async def calendar_events(self, start_date: str, end_date: str) -> list[dict]:
        params = f"startDateTime={start_date}&endDateTime={end_date}&$select=id,subject,start,end,location,attendees"
        data = await self._get(f"/me/calendarView?{params}")
        return [
            {"id": e["id"], "subject": e.get("subject", ""),
             "start": e.get("start", {}).get("dateTime", ""),
             "end": e.get("end", {}).get("dateTime", ""),
             "location": e.get("location", {}).get("displayName", ""),
             "attendees": [a["emailAddress"]["address"] for a in e.get("attendees", [])]}
            for e in data.get("value", [])
        ]

    async def create_event(self, subject: str, start: str, end: str,
                           attendees: list[str] | None = None, body: str | None = None) -> dict:
        event: dict[str, Any] = {
            "subject": subject,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in (attendees or [])],
        }
        if body:
            event["body"] = {"contentType": "Text", "content": body}
        result = await self._post("/me/events", event)
        return {"ok": True, "event_id": result.get("id", "")}


class _OutlookToolBase(Tool):
    def __init__(self, backend: OutlookBackend) -> None:
        self._backend = backend


class OutlookListEmailsTool(_OutlookToolBase):
    name = "outlook_list_emails"
    description = "List emails from an Outlook folder."
    parameters = {"type": "object", "properties": {
        "folder": {"type": "string", "default": "Inbox"},
        "n": {"type": "integer", "default": 20},
        "unread_only": {"type": "boolean", "default": False},
    }}
    async def execute(self, args, context):
        emails = await self._backend.list_emails(args.get("folder", "Inbox"), int(args.get("n", 20)), bool(args.get("unread_only", False)))
        return {"emails": emails, "count": len(emails)}


class OutlookReadEmailTool(_OutlookToolBase):
    name = "outlook_read_email"
    description = "Read a specific email by ID."
    parameters = {"type": "object", "properties": {"email_id": {"type": "string"}}, "required": ["email_id"]}
    async def execute(self, args, context):
        return await self._backend.read_email(args["email_id"])


class OutlookSendEmailTool(_OutlookToolBase):
    name = "outlook_send_email"
    description = "Send an email."
    parameters = {"type": "object", "properties": {
        "to": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"}, "body": {"type": "string"},
        "cc": {"type": "array", "items": {"type": "string"}},
    }, "required": ["to", "subject", "body"]}
    async def execute(self, args, context):
        return await self._backend.send_email(args["to"], args["subject"], args["body"], args.get("cc"), args.get("attachments"))


class OutlookReplyEmailTool(_OutlookToolBase):
    name = "outlook_reply_email"
    description = "Reply to an email."
    parameters = {"type": "object", "properties": {
        "email_id": {"type": "string"}, "body": {"type": "string"},
        "reply_all": {"type": "boolean", "default": False},
    }, "required": ["email_id", "body"]}
    async def execute(self, args, context):
        return await self._backend.reply_email(args["email_id"], args["body"], bool(args.get("reply_all", False)))


class OutlookSearchEmailsTool(_OutlookToolBase):
    name = "outlook_search_emails"
    description = "Search emails."
    parameters = {"type": "object", "properties": {
        "query": {"type": "string"}, "folder": {"type": "string", "default": "Inbox"},
        "n": {"type": "integer", "default": 20},
    }, "required": ["query"]}
    async def execute(self, args, context):
        emails = await self._backend.search_emails(args["query"], args.get("folder", "Inbox"), int(args.get("n", 20)))
        return {"emails": emails, "count": len(emails)}


class OutlookMoveEmailTool(_OutlookToolBase):
    name = "outlook_move_email"
    description = "Move an email to another folder."
    parameters = {"type": "object", "properties": {
        "email_id": {"type": "string"}, "destination_folder": {"type": "string"},
    }, "required": ["email_id", "destination_folder"]}
    async def execute(self, args, context):
        return await self._backend.move_email(args["email_id"], args["destination_folder"])


class OutlookListFoldersTool(_OutlookToolBase):
    name = "outlook_list_folders"
    description = "List mail folders."
    parameters = {"type": "object", "properties": {}}
    async def execute(self, args, context):
        folders = await self._backend.list_folders()
        return {"folders": folders, "count": len(folders)}


class OutlookCalendarEventsTool(_OutlookToolBase):
    name = "outlook_calendar_events"
    description = "List calendar events in a date range."
    parameters = {"type": "object", "properties": {
        "start_date": {"type": "string"}, "end_date": {"type": "string"},
    }, "required": ["start_date", "end_date"]}
    async def execute(self, args, context):
        events = await self._backend.calendar_events(args["start_date"], args["end_date"])
        return {"events": events, "count": len(events)}


class OutlookCreateEventTool(_OutlookToolBase):
    name = "outlook_create_event"
    description = "Create a calendar event."
    parameters = {"type": "object", "properties": {
        "subject": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
        "attendees": {"type": "array", "items": {"type": "string"}},
        "body": {"type": "string"},
    }, "required": ["subject", "start", "end"]}
    async def execute(self, args, context):
        return await self._backend.create_event(args["subject"], args["start"], args["end"], args.get("attendees"), args.get("body"))


class OutlookToolKit:
    def __init__(self, backend: str = "auto", token: str | None = None, tenant_id: str | None = None) -> None:
        self._token = token
    def _build_backend(self) -> OutlookBackend:
        return GraphApiOutlookBackend(token=self._token)
    def tools(self) -> list[Tool]:
        b = self._build_backend()
        return [OutlookListEmailsTool(b), OutlookReadEmailTool(b), OutlookSendEmailTool(b),
                OutlookReplyEmailTool(b), OutlookSearchEmailsTool(b), OutlookMoveEmailTool(b),
                OutlookListFoldersTool(b), OutlookCalendarEventsTool(b), OutlookCreateEventTool(b)]
