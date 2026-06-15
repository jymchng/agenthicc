"""Windows-native Outlook backend using pywin32 COM automation (PRD-17).

This module is **Windows-only**.  It wraps ``win32com.client.Dispatch`` to drive
a locally installed Outlook application directly, with no network or OAuth overhead.

On non-Windows platforms every public function returns a ``{"ok": False, "error":
"win32 Outlook is only available on Windows"}`` dict so callers can degrade
gracefully rather than raising.
"""
from __future__ import annotations

import sys
from typing import Any

__all__ = [
    "Win32OutlookBackend",
    "WIN32_AVAILABLE",
]

WIN32_AVAILABLE: bool = sys.platform == "win32"

try:  # pragma: no cover — only importable on Windows
    if WIN32_AVAILABLE:
        import win32com.client
except ImportError:  # pragma: no cover
    WIN32_AVAILABLE = False


_NOT_WINDOWS: dict[str, Any] = {
    "ok": False,
    "error": "win32 Outlook is only available on Windows (pywin32 not installed or not on win32 platform)",
}


def _outlook_app() -> Any:  # pragma: no cover
    """Return (or create) the Outlook Application COM object."""
    return win32com.client.Dispatch("Outlook.Application")  # type: ignore[name-defined]


def _ns() -> Any:  # pragma: no cover
    """Return the MAPI namespace from the Outlook application."""
    return _outlook_app().GetNamespace("MAPI")


# ── Email ─────────────────────────────────────────────────────────────────


def list_emails(folder_name: str = "Inbox", n: int = 20, unread_only: bool = False) -> dict:
    """List recent emails from an Outlook folder.

    Returns ``{"emails": [...], "count": int}``.
    Each entry: ``{index, subject, sender, date, unread, body_preview}``.

    Args:
        folder_name: Outlook folder name (default "Inbox").
        n: Maximum number of emails to return.
        unread_only: If True, return only unread messages.
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        ns = _ns()
        folder = ns.GetDefaultFolder(6)  # 6 = olFolderInbox
        if folder_name.lower() != "inbox":
            folder = ns.Folders.Item(1).Folders[folder_name]

        messages = folder.Items
        messages.Sort("[ReceivedTime]", True)   # descending

        emails = []
        for i in range(1, messages.Count + 1):
            if len(emails) >= n:
                break
            try:
                msg = messages.Item(i)
                if unread_only and not getattr(msg, "UnRead", False):
                    continue
                emails.append({
                    "index": i,
                    "subject": getattr(msg, "Subject", ""),
                    "sender": getattr(msg, "SenderEmailAddress", ""),
                    "date": str(getattr(msg, "ReceivedTime", "")),
                    "unread": bool(getattr(msg, "UnRead", False)),
                    "body_preview": str(getattr(msg, "Body", ""))[:200],
                })
            except Exception:  # noqa: BLE001
                continue
        return {"emails": emails, "count": len(emails)}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def read_email(index: int, folder_name: str = "Inbox") -> dict:
    """Read the full content of an email by its 1-based index.

    Returns ``{subject, sender, to, cc, date, body_text, body_html, attachments}``.

    Args:
        index: 1-based message index within the folder.
        folder_name: Outlook folder name (default "Inbox").
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        ns = _ns()
        folder = ns.GetDefaultFolder(6)
        if folder_name.lower() != "inbox":
            folder = ns.Folders.Item(1).Folders[folder_name]

        msg = folder.Items.Item(index)
        attachments = [
            {"name": msg.Attachments.Item(j).FileName,
             "size": msg.Attachments.Item(j).Size}
            for j in range(1, msg.Attachments.Count + 1)
        ]
        return {
            "subject": getattr(msg, "Subject", ""),
            "sender": getattr(msg, "SenderEmailAddress", ""),
            "to": getattr(msg, "To", ""),
            "cc": getattr(msg, "CC", ""),
            "date": str(getattr(msg, "ReceivedTime", "")),
            "body_text": getattr(msg, "Body", ""),
            "body_html": getattr(msg, "HTMLBody", ""),
            "attachments": attachments,
        }
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def send_email(to: list[str], subject: str, body: str,
               cc: list[str] | None = None, html: bool = False) -> dict:
    """Send an email via Outlook.

    Args:
        to: List of recipient email addresses.
        subject: Email subject.
        body: Email body text (or HTML if html=True).
        cc: Optional CC recipients.
        html: If True, body is treated as HTML.
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        app = _outlook_app()
        mail = app.CreateItem(0)   # 0 = olMailItem
        mail.Subject = subject
        mail.To = "; ".join(to)
        if cc:
            mail.CC = "; ".join(cc)
        if html:
            mail.HTMLBody = body
        else:
            mail.Body = body
        mail.Send()
        return {"ok": True}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def reply_email(index: int, body: str, reply_all: bool = False,
                folder_name: str = "Inbox") -> dict:
    """Reply to an email.

    Args:
        index: 1-based message index.
        body: Reply body text.
        reply_all: If True, reply to all recipients.
        folder_name: Outlook folder name (default "Inbox").
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        ns = _ns()
        folder = ns.GetDefaultFolder(6)
        if folder_name.lower() != "inbox":
            folder = ns.Folders.Item(1).Folders[folder_name]

        msg = folder.Items.Item(index)
        reply = msg.ReplyAll() if reply_all else msg.Reply()
        reply.Body = body + "\n\n" + reply.Body
        reply.Send()
        return {"ok": True}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def search_emails(query: str, folder_name: str = "Inbox", n: int = 20) -> dict:
    """Search emails in a folder by subject or body text.

    Args:
        query: Text to search for in subject and body.
        folder_name: Folder to search (default "Inbox").
        n: Maximum results to return.
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        ns = _ns()
        folder = ns.GetDefaultFolder(6)
        if folder_name.lower() != "inbox":
            folder = ns.Folders.Item(1).Folders[folder_name]

        q = query.lower()
        results = []
        for i in range(1, folder.Items.Count + 1):
            if len(results) >= n:
                break
            try:
                msg = folder.Items.Item(i)
                subj = str(getattr(msg, "Subject", "")).lower()
                body = str(getattr(msg, "Body", "")).lower()
                if q in subj or q in body:
                    results.append({
                        "index": i,
                        "subject": getattr(msg, "Subject", ""),
                        "sender": getattr(msg, "SenderEmailAddress", ""),
                        "date": str(getattr(msg, "ReceivedTime", "")),
                    })
            except Exception:  # noqa: BLE001
                continue
        return {"emails": results, "count": len(results)}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def move_email(index: int, destination_folder: str,
               source_folder: str = "Inbox") -> dict:
    """Move an email to another folder.

    Args:
        index: 1-based message index in source_folder.
        destination_folder: Target folder name.
        source_folder: Source folder name (default "Inbox").
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        ns = _ns()
        src = ns.GetDefaultFolder(6)
        if source_folder.lower() != "inbox":
            src = ns.Folders.Item(1).Folders[source_folder]
        dst = ns.Folders.Item(1).Folders[destination_folder]
        src.Items.Item(index).Move(dst)
        return {"ok": True}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def list_folders() -> dict:
    """List all mail folders in the default store.

    Returns ``{"folders": [{name, item_count}], "count": int}``.
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        ns = _ns()
        store = ns.Folders.Item(1)
        folders = [
            {"name": store.Folders.Item(i).Name,
             "item_count": store.Folders.Item(i).Items.Count}
            for i in range(1, store.Folders.Count + 1)
        ]
        return {"folders": folders, "count": len(folders)}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── Calendar ──────────────────────────────────────────────────────────────


def calendar_events(start_date: str, end_date: str) -> dict:
    """List calendar events between two dates.

    Args:
        start_date: Start date in "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS" format.
        end_date: End date in the same format.
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        ns = _ns()
        cal = ns.GetDefaultFolder(9)   # 9 = olFolderCalendar
        items = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        restriction = (
            f"[Start] >= '{start_date}' AND [Start] <= '{end_date}'"
        )
        restricted = items.Restrict(restriction)
        events = []
        for i in range(1, restricted.Count + 1):
            try:
                ev = restricted.Item(i)
                events.append({
                    "subject": getattr(ev, "Subject", ""),
                    "start": str(getattr(ev, "Start", "")),
                    "end": str(getattr(ev, "End", "")),
                    "location": getattr(ev, "Location", ""),
                    "body": getattr(ev, "Body", "")[:300],
                    "organizer": getattr(ev, "Organizer", ""),
                })
            except Exception:  # noqa: BLE001
                continue
        return {"events": events, "count": len(events)}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def create_event(subject: str, start: str, end: str,
                 location: str = "", body: str = "",
                 attendees: list[str] | None = None) -> dict:
    """Create a new calendar event.

    Args:
        subject: Event title.
        start: Start datetime string "YYYY-MM-DDTHH:MM:SS".
        end: End datetime string "YYYY-MM-DDTHH:MM:SS".
        location: Event location (optional).
        body: Event description (optional).
        attendees: List of attendee email addresses (optional).
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        app = _outlook_app()
        appt = app.CreateItem(1)   # 1 = olAppointmentItem
        appt.Subject = subject
        appt.Start = start
        appt.End = end
        if location:
            appt.Location = location
        if body:
            appt.Body = body
        for email in (attendees or []):
            recipient = appt.Recipients.Add(email)
            recipient.Type = 1   # olRequired
        appt.Save()
        return {"ok": True, "entry_id": appt.EntryID}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── Office documents ──────────────────────────────────────────────────────


def word_read_document(path: str) -> dict:
    """Read the full text of a Word (.docx/.doc) file via COM.

    Args:
        path: Absolute or workspace-relative path to the Word file.
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        word = win32com.client.Dispatch("Word.Application")  # type: ignore[name-defined]
        word.Visible = False
        doc = word.Documents.Open(path)
        text = doc.Content.Text
        pages = doc.ComputeStatistics(2)   # 2 = wdStatisticPages
        words = doc.ComputeStatistics(0)   # 0 = wdStatisticWords
        doc.Close(False)
        word.Quit()
        return {"text": text, "pages": pages, "word_count": words}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def excel_read_range(path: str, sheet: str = "Sheet1",
                     range_str: str = "A1:Z100") -> dict:
    """Read a range of cells from an Excel (.xlsx/.xls) file via COM.

    Args:
        path: Absolute or workspace-relative path to the Excel file.
        sheet: Sheet name (default "Sheet1").
        range_str: Excel range string (default "A1:Z100").
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        excel = win32com.client.Dispatch("Excel.Application")  # type: ignore[name-defined]
        excel.Visible = False
        excel.DisplayAlerts = False
        wb = excel.Workbooks.Open(path)
        ws = wb.Sheets(sheet)
        rng = ws.Range(range_str)
        data: list[list[Any]] = []
        for row in rng.Value:
            data.append([cell for cell in row])
        wb.Close(False)
        excel.Quit()
        headers = data[0] if data else []
        return {"data": data, "headers": [str(h) for h in headers]}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


def run_vba_macro(macro_name: str, args: list[Any] | None = None) -> dict:
    """Run a VBA macro in the currently open Outlook session.

    Args:
        macro_name: Fully qualified macro name, e.g. "Module1.MyMacro".
        args: Optional list of arguments passed to the macro.
    """
    if not WIN32_AVAILABLE:
        return _NOT_WINDOWS
    try:  # pragma: no cover
        app = _outlook_app()
        result = app.Run(macro_name, *(args or []))
        return {"ok": True, "result": str(result) if result is not None else None}
    except Exception as exc:  # pragma: no cover  noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── Win32OutlookBackend class (implements OutlookBackend ABC) ─────────────


class Win32OutlookBackend:
    """Adapts the free functions above to the OutlookBackend ABC interface.

    Pass this as the backend when constructing ``OutlookToolKit`` on Windows::

        from agenthicc.tools.outlook import OutlookToolKit
        from agenthicc.tools.outlook.win32_backend import Win32OutlookBackend
        kit = OutlookToolKit(backend_instance=Win32OutlookBackend())
        tools = kit.tools()
    """

    async def list_emails(self, folder: str, n: int, unread_only: bool) -> list[dict]:
        return list_emails(folder, n, unread_only).get("emails", [])  # type: ignore[return-value]

    async def read_email(self, email_id: str) -> dict:
        try:
            return read_email(int(email_id))
        except (ValueError, TypeError):
            return {"ok": False, "error": f"Invalid email index: {email_id!r}"}

    async def send_email(self, to: list[str], subject: str, body: str,
                         cc: list[str] | None, attachments: list[str] | None) -> dict:
        return send_email(to, subject, body, cc)

    async def reply_email(self, email_id: str, body: str, reply_all: bool) -> dict:
        try:
            return reply_email(int(email_id), body, reply_all)
        except (ValueError, TypeError):
            return {"ok": False, "error": f"Invalid email index: {email_id!r}"}

    async def search_emails(self, query: str, folder: str, n: int) -> list[dict]:
        return search_emails(query, folder, n).get("emails", [])  # type: ignore[return-value]

    async def move_email(self, email_id: str, destination: str) -> dict:
        try:
            return move_email(int(email_id), destination)
        except (ValueError, TypeError):
            return {"ok": False, "error": f"Invalid email index: {email_id!r}"}

    async def list_folders(self) -> list[dict]:
        return list_folders().get("folders", [])  # type: ignore[return-value]

    async def calendar_events(self, start_date: str, end_date: str) -> list[dict]:
        return calendar_events(start_date, end_date).get("events", [])  # type: ignore[return-value]

    async def create_event(self, subject: str, start: str, end: str,
                           attendees: list[str] | None, body: str | None) -> dict:
        return create_event(subject, start, end, body=body or "", attendees=attendees)
