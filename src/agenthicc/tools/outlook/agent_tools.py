"""@tool() wrappers for Outlook/Graph API tools — for use with lauren-ai AgentRunnerBase.

Uses the Win32 COM backend on Windows (pywin32) and the Microsoft Graph API
backend on all other platforms.  Set MSGRAPH_TOKEN env var for the Graph API.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""
import os
from lauren_ai._tools import tool
from agenthicc.tools.capabilities import (
    tool_network_read, tool_network_write, tool_network_search,
)

__all__ = [
    "list_emails",
    "read_email",
    "send_email",
    "reply_email",
    "search_emails",
    "move_email",
    "list_folders",
    "calendar_events",
    "create_event",
    "OUTLOOK_AGENT_TOOLS",
]


def _backend():
    """Return the appropriate OutlookBackend for the current platform."""
    import sys  # noqa: PLC0415
    if sys.platform == "win32":
        try:
            from agenthicc.tools.outlook.win32_backend import Win32OutlookBackend  # noqa: PLC0415
            return Win32OutlookBackend()
        except ImportError:
            pass
    from agenthicc.tools.outlook import GraphApiOutlookBackend  # noqa: PLC0415
    return GraphApiOutlookBackend(token=os.getenv("MSGRAPH_TOKEN", ""))


@tool_network_read
@tool()
async def list_emails(
    folder: str = "Inbox",
    n: int = 20,
    unread_only: bool = False,
) -> list:
    """List emails from an Outlook folder.

    Args:
        folder: Folder name to list from (default: Inbox).
        n: Maximum number of emails to return (default 20).
        unread_only: Only return unread messages when True.
    """
    return await _backend().list_emails(folder=folder, n=n, unread_only=unread_only)


@tool_network_read
@tool()
async def read_email(email_id: str) -> dict:
    """Read the full content of an email by ID.

    Args:
        email_id: The email identifier returned by list_emails or search_emails.
    """
    return await _backend().read_email(email_id=email_id)


@tool_network_write
@tool()
async def send_email(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    attachments: list[str] | None = None,
) -> dict:
    """Send an email via Outlook.

    Args:
        to: List of recipient email addresses.
        subject: Email subject line.
        body: Email body (plain text or HTML).
        cc: Optional list of CC email addresses.
        attachments: Optional list of file paths to attach.
    """
    return await _backend().send_email(
        to=to, subject=subject, body=body, cc=cc, attachments=attachments
    )


@tool_network_write
@tool()
async def reply_email(
    email_id: str,
    body: str,
    reply_all: bool = False,
) -> dict:
    """Reply to an email.

    Args:
        email_id: The email identifier to reply to.
        body: Reply body text.
        reply_all: If True, reply to all recipients (default False).
    """
    return await _backend().reply_email(email_id=email_id, body=body, reply_all=reply_all)


@tool_network_search
@tool()
async def search_emails(
    query: str,
    folder: str = "Inbox",
    n: int = 20,
) -> list:
    """Search emails in Outlook by keyword.

    Args:
        query: Search query string.
        folder: Folder to search within (default: Inbox).
        n: Maximum number of results to return (default 20).
    """
    return await _backend().search_emails(query=query, folder=folder, n=n)


@tool_network_write
@tool()
async def move_email(email_id: str, destination: str) -> dict:
    """Move an email to a different folder.

    Args:
        email_id: The email identifier to move.
        destination: Name of the destination folder (e.g. "Archive").
    """
    return await _backend().move_email(email_id=email_id, destination=destination)


@tool_network_read
@tool()
async def list_folders() -> list:
    """List all available Outlook mail folders."""
    return await _backend().list_folders()


@tool_network_read
@tool()
async def calendar_events(start_date: str, end_date: str) -> list:
    """Retrieve calendar events within a date range.

    Args:
        start_date: Start of the range in ISO-8601 format (e.g. "2026-06-01").
        end_date: End of the range in ISO-8601 format (e.g. "2026-06-30").
    """
    return await _backend().calendar_events(start_date=start_date, end_date=end_date)


@tool_network_write
@tool()
async def create_event(
    subject: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    body: str | None = None,
) -> dict:
    """Create a new calendar event in Outlook.

    Args:
        subject: Event title.
        start: Start datetime in ISO-8601 format (e.g. "2026-06-15T09:00:00").
        end: End datetime in ISO-8601 format (e.g. "2026-06-15T10:00:00").
        attendees: Optional list of attendee email addresses.
        body: Optional event description / agenda.
    """
    return await _backend().create_event(
        subject=subject, start=start, end=end, attendees=attendees, body=body
    )


#: All 9 Outlook agent tools — ready to pass to @use_tools().
OUTLOOK_AGENT_TOOLS = [
    list_emails,
    read_email,
    send_email,
    reply_email,
    search_emails,
    move_email,
    list_folders,
    calendar_events,
    create_event,
]
