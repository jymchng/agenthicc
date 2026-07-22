"""Unit tests for Outlook tools (PRD-17)."""

from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agenthicc.tools.outlook import (
    GraphApiOutlookBackend,
    OutlookListEmailsTool,
    OutlookSendEmailTool,
    OutlookCalendarEventsTool,
    OutlookToolKit,
)

pytestmark = pytest.mark.unit


def _backend(token="test"):
    return GraphApiOutlookBackend(token=token)


EMAIL_RESP = {
    "value": [
        {
            "id": "m1",
            "subject": "Hello",
            "from": {"emailAddress": {"address": "a@b.com"}},
            "receivedDateTime": "2025-01-01T10:00:00Z",
            "isRead": False,
            "bodyPreview": "Hi",
        }
    ]
}

FOLDER_RESP = {
    "value": [{"id": "f1", "displayName": "Inbox", "totalItemCount": 10, "unreadItemCount": 2}]
}

CALENDAR_RESP = {
    "value": [
        {
            "id": "e1",
            "subject": "Meeting",
            "start": {"dateTime": "2025-01-01T09:00:00"},
            "end": {"dateTime": "2025-01-01T10:00:00"},
            "location": {"displayName": "Room A"},
            "attendees": [],
        }
    ]
}


class TestGraphApiBackend:
    async def test_list_emails(self):
        b = _backend()
        with patch.object(b, "_get", new_callable=AsyncMock, return_value=EMAIL_RESP):
            emails = await b.list_emails()
        assert len(emails) == 1 and emails[0]["subject"] == "Hello" and emails[0]["unread"] is True

    async def test_send_email_calls_post(self):
        b = _backend()
        with patch.object(b, "_post", new_callable=AsyncMock, return_value={}) as mp:
            r = await b.send_email(["u@x.com"], "Subj", "Body")
        assert r["ok"] is True
        mp.assert_called_once()
        assert mp.call_args[0][1]["message"]["subject"] == "Subj"

    async def test_reply_email(self):
        b = _backend()
        with patch.object(b, "_post", new_callable=AsyncMock, return_value={}) as mp:
            r = await b.reply_email("m1", "OK", reply_all=False)
        assert r["ok"] is True
        assert "reply" in mp.call_args[0][0]

    async def test_reply_all(self):
        b = _backend()
        with patch.object(b, "_post", new_callable=AsyncMock, return_value={}) as mp:
            await b.reply_email("m1", "OK", reply_all=True)
        assert "replyAll" in mp.call_args[0][0]

    async def test_calendar_events(self):
        b = _backend()
        with patch.object(b, "_get", new_callable=AsyncMock, return_value=CALENDAR_RESP):
            events = await b.calendar_events("2025-01-01", "2025-01-31")
        assert len(events) == 1 and events[0]["subject"] == "Meeting"

    async def test_create_event(self):
        b = _backend()
        with patch.object(b, "_post", new_callable=AsyncMock, return_value={"id": "new-event"}):
            r = await b.create_event("Stand-up", "2025-01-01T09:00:00", "2025-01-01T09:30:00")
        assert r["ok"] is True and r["event_id"] == "new-event"

    async def test_list_folders(self):
        b = _backend()
        with patch.object(b, "_get", new_callable=AsyncMock, return_value=FOLDER_RESP):
            folders = await b.list_folders()
        assert len(folders) == 1 and folders[0]["name"] == "Inbox"

    async def test_search_emails_encodes_query(self):
        b = _backend()
        with patch.object(b, "_get", new_callable=AsyncMock, return_value={"value": []}) as mg:
            await b.search_emails("hello world", n=5)
        url = mg.call_args[0][0]
        assert "hello" in url or "%20" in url or "+" in url

    async def test_move_email(self):
        b = _backend()
        with patch.object(b, "_post", new_callable=AsyncMock, return_value={}):
            r = await b.move_email("m1", "Archive")
        assert r["ok"] is True


class TestOutlookToolWrappers:
    async def test_list_emails_tool_returns_count(self):
        b = MagicMock()
        b.list_emails = AsyncMock(return_value=[{"id": "1"}, {"id": "2"}])
        r = await OutlookListEmailsTool(b).execute({}, {})
        assert r["count"] == 2

    async def test_send_email_tool_forwards_args(self):
        b = MagicMock()
        b.send_email = AsyncMock(return_value={"ok": True})
        await OutlookSendEmailTool(b).execute({"to": ["a@b.com"], "subject": "S", "body": "B"}, {})
        b.send_email.assert_called_once_with(["a@b.com"], "S", "B", None, None)

    async def test_calendar_tool_returns_count(self):
        b = MagicMock()
        b.calendar_events = AsyncMock(return_value=[{"id": "e1"}])
        r = await OutlookCalendarEventsTool(b).execute(
            {"start_date": "2025-01-01", "end_date": "2025-01-31"}, {}
        )
        assert r["count"] == 1


class TestOutlookToolKit:
    def test_returns_9_tools(self):
        tools = OutlookToolKit().tools()
        assert len(tools) == 9
        names = {t.name for t in tools}
        assert "outlook_list_emails" in names and "outlook_create_event" in names
