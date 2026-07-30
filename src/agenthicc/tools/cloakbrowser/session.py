"""Session-scoped browser lifecycle, quotas, redaction, and checkpoint data."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from agenthicc.config import CloakBrowserSettings
from agenthicc.tools.sandbox import WorkspaceView

from .artifacts import BrowserArtifactStore
from .client import (
    BrowserClient,
    CdpCloakBrowserClient,
    LocalCloakBrowserClient,
    PageState,
    ScreenshotData,
    UnavailableBrowserClient,
)
from .errors import BrowserErrorKind, BrowserToolError
from .policy import BrowserPolicy, redact_link, redact_url

log = logging.getLogger(__name__)

__all__ = [
    "BrowserSessionManager",
    "create_browser_session",
    "is_cloakbrowser_tool",
]

CLOAKBROWSER_TOOL_NAMES = frozenset(
    {
        "cloakbrowser_status",
        "cloakbrowser_open",
        "cloakbrowser_snapshot",
        "cloakbrowser_click",
        "cloakbrowser_fill",
        "cloakbrowser_press",
        "cloakbrowser_wait_for",
        "cloakbrowser_screenshot",
        "cloakbrowser_close",
    }
)

_OPERATION_ID_LIMIT = 128
_OPERATION_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_OPERATION_CACHE_LIMIT = 256


def is_cloakbrowser_tool(tool: object) -> bool:
    """Return whether a callable is one of the built-in browser tools."""
    return str(getattr(tool, "__name__", "")) in CLOAKBROWSER_TOOL_NAMES


class BrowserSessionManager:
    """Own exactly one browser context for one stable agenthicc session.

    Browser objects are process-local and never appear in checkpoint payloads.
    The manager is intentionally the only object captured by browser tool
    closures, which prevents a model from selecting a transport, endpoint,
    profile path, cookie jar, or CDP command surface.
    """

    def __init__(
        self,
        settings: CloakBrowserSettings,
        conversation_id: str,
        workspace_root: Path,
        *,
        client: BrowserClient | None = None,
        policy: BrowserPolicy | None = None,
        artifact_store: BrowserArtifactStore | None = None,
    ) -> None:
        self.settings = settings
        self.conversation_id = conversation_id or "session"
        self._workspace_root = workspace_root
        self._closed = False
        self._actions_used = 0
        self._pages: dict[str, PageState] = {}
        self._policy_error: str | None = None
        self._operation_lock = asyncio.Lock()
        self._operation_in_flight: set[str] = set()
        self._operation_results: dict[str, dict[str, object]] = {}
        self._browser_session_id = uuid.uuid4().hex
        try:
            self.policy = policy or BrowserPolicy(
                tuple(settings.allowed_domains), allow_all_domains=settings.allow_all_domains
            )
        except ValueError as exc:
            # A malformed/empty allow-list must make browser operations
            # unavailable, not prevent the rest of agenthicc from starting.
            self.policy = BrowserPolicy(("invalid.agenthicc.invalid",))
            self._policy_error = str(exc) if settings.enabled else None
        if client is not None:
            self.client = client
        elif not settings.enabled:
            self.client = UnavailableBrowserClient(
                settings.transport,
                BrowserErrorKind.DISABLED,
                "CloakBrowser is disabled; set [tools.cloakbrowser].enabled = true.",
            )
        elif settings.transport == "cdp":
            self.client = CdpCloakBrowserClient(
                settings,
                self.policy,
                workspace_root,
                session_id=self._browser_session_id,
            )
        else:
            self.client = LocalCloakBrowserClient(
                settings,
                self.policy,
                workspace_root,
                session_id=self._browser_session_id,
            )
        self.artifacts = artifact_store or BrowserArtifactStore(WorkspaceView(workspace_root))

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self._policy_error is None and not self._closed

    @property
    def actions_used(self) -> int:
        return self._actions_used

    async def status(self) -> dict[str, object]:
        if self._closed:
            return {
                "ok": False,
                "status": BrowserErrorKind.CLOSED.value,
                "transport": self.settings.transport,
                "session_id": self._browser_session_id,
            }
        if self._policy_error is not None:
            return {
                "ok": False,
                "status": BrowserErrorKind.POLICY_DENIED.value,
                "transport": self.settings.transport,
                "session_id": self._browser_session_id,
                "message": "Browser allow-list configuration is invalid.",
            }
        if not self.policy.allowed_domains and not self.policy.allow_all_domains:
            return {
                "ok": False,
                "status": BrowserErrorKind.DISABLED.value,
                "transport": self.settings.transport,
                "session_id": self._browser_session_id,
                "message": "Configure at least one allowed browser origin.",
            }
        health = await self.client.health()
        result = health.to_dict()
        result.update(
            {
                "ok": health.status == "ready",
                "session_id": self._browser_session_id,
                "open_pages": len(self._pages),
                "max_pages": self.settings.max_pages,
                "actions_used": self._actions_used,
                "max_actions_per_turn": self.settings.max_actions_per_turn,
            }
        )
        return result

    def _ensure_usable(self) -> None:
        if self._closed:
            raise BrowserToolError(BrowserErrorKind.CLOSED, "Browser session is closed.")
        if not self.settings.enabled:
            raise BrowserToolError(BrowserErrorKind.DISABLED, "CloakBrowser is disabled.")
        if self._policy_error is not None:
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED, "Browser allow-list configuration is invalid."
            )
        if not self.policy.allowed_domains and not self.policy.allow_all_domains:
            raise BrowserToolError(
                BrowserErrorKind.POLICY_DENIED,
                "No browser destinations are configured in the allow-list.",
            )

    async def _ensure_ready(self) -> None:
        """Re-check dependency, binary, or endpoint health before an operation."""
        health = await self.client.health()
        if health.status == "ready":
            return
        try:
            kind = BrowserErrorKind(health.status)
        except ValueError:
            kind = BrowserErrorKind.BROWSER_UNAVAILABLE
        if kind is BrowserErrorKind.BINARY_MISSING or kind is BrowserErrorKind.UNHEALTHY:
            kind = BrowserErrorKind.BROWSER_UNAVAILABLE
        raise BrowserToolError(kind, health.message or "Browser backend is unavailable.")

    @staticmethod
    def _validate_operation_id(operation_id: str | None) -> str | None:
        """Validate a caller-supplied id used for at-most-once mutations."""
        if operation_id is None or not operation_id.strip():
            return None
        candidate = operation_id.strip()
        if len(candidate) > _OPERATION_ID_LIMIT or any(
            char not in _OPERATION_ID_CHARS for char in candidate
        ):
            raise BrowserToolError(
                BrowserErrorKind.INVALID_INPUT,
                "Operation id is invalid or too long.",
            )
        return candidate

    def _page(self, page_id: str) -> PageState:
        page = self._pages.get(page_id)
        if page is None:
            raise BrowserToolError(BrowserErrorKind.NOT_FOUND, "Browser page is no longer open.")
        return page

    def _consume_action(self) -> None:
        if self._actions_used >= self.settings.max_actions_per_turn:
            raise BrowserToolError(
                BrowserErrorKind.LIMIT_EXCEEDED, "Browser action limit reached for this turn."
            )
        self._actions_used += 1

    def reset_turn_budget(self) -> None:
        """Reset the action quota at a new agent turn boundary."""
        self._actions_used = 0

    @staticmethod
    def _result_page(page: PageState, page_id: str) -> dict[str, object]:
        return {"ok": True, "page": PageState(page_id, page.url, page.title).to_dict()}

    async def _safe(
        self,
        operation: Callable[[], Awaitable[dict[str, object]]],
        *,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        try:
            normalized_operation_id = self._validate_operation_id(operation_id)
        except BrowserToolError as exc:
            return {"ok": False, "error_kind": exc.kind.value, "error": exc.safe_message}

        if normalized_operation_id is not None:
            cached = self._operation_results.get(normalized_operation_id)
            if cached is not None:
                return dict(cached)
            if normalized_operation_id in self._operation_in_flight:
                return {
                    "ok": False,
                    "error_kind": BrowserErrorKind.EXECUTION.value,
                    "error": "Operation is already in progress; do not retry it yet.",
                    "operation_id": normalized_operation_id,
                }
            self._operation_in_flight.add(normalized_operation_id)

        result: dict[str, object] | None = None
        try:
            async with self._operation_lock:
                result = await operation()
        except BrowserToolError as exc:
            result = {"ok": False, "error_kind": exc.kind.value, "error": exc.safe_message}
        except asyncio.CancelledError:
            # A cancelled mutation must not be retried against an uncertain
            # page. Close the owned context as a conservative cleanup boundary
            # and return a stable result to the tool caller.
            try:
                await asyncio.shield(self.client.close_session(self._browser_session_id))
            except Exception:  # noqa: BLE001 — cancellation cleanup is best effort
                log.debug("cancelled browser cleanup failed", exc_info=True)
            self._pages.clear()
            result = {
                "ok": False,
                "error_kind": BrowserErrorKind.CANCELLED.value,
                "error": "Browser operation was cancelled; the affected session was closed.",
            }
        except TimeoutError:
            result = {
                "ok": False,
                "error_kind": BrowserErrorKind.TIMEOUT.value,
                "error": "Browser operation timed out.",
            }
        except OSError:
            result = {
                "ok": False,
                "error_kind": BrowserErrorKind.NETWORK.value,
                "error": "Browser network operation failed.",
            }
        except Exception:  # noqa: BLE001 — optional browser boundary must not leak details
            log.exception("cloakbrowser operation failed")
            result = {
                "ok": False,
                "error_kind": BrowserErrorKind.EXECUTION.value,
                "error": "Browser operation failed.",
            }
        finally:
            if normalized_operation_id is not None:
                self._operation_in_flight.discard(normalized_operation_id)
                if result is not None:
                    result.setdefault("operation_id", normalized_operation_id)
                    self._operation_results[normalized_operation_id] = dict(result)
                    while len(self._operation_results) > _OPERATION_CACHE_LIMIT:
                        self._operation_results.pop(next(iter(self._operation_results)))

        if result is None:
            # A BaseException such as KeyboardInterrupt may interrupt before a
            # structured result exists.  Preserve the existing exception path.
            raise RuntimeError("browser operation did not produce a result")
        result.setdefault("operation_id", normalized_operation_id or uuid.uuid4().hex)
        return result

    async def open(self, url: str, operation_id: str | None = None) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            if len(self._pages) >= self.settings.max_pages:
                raise BrowserToolError(
                    BrowserErrorKind.LIMIT_EXCEEDED, "Maximum open browser pages reached."
                )
            await self.policy.validate_url(url)
            page = await self.client.open_page(self._browser_session_id, url)
            self._pages[page.page_id] = page
            return self._result_page(page, page.page_id)

        return await self._safe(operation, operation_id=operation_id)

    async def snapshot(self, page_id: str, operation_id: str | None = None) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            self._page(page_id)
            result = await self.client.snapshot(self._browser_session_id, page_id)
            self._pages[page_id] = result.page
            bounded = result.to_dict()
            text = str(bounded.get("text", ""))
            bounded["truncated"] = (
                bool(bounded.get("truncated")) or len(text) > self.settings.max_snapshot_chars
            )
            bounded["text"] = text[: self.settings.max_snapshot_chars]
            for key in ("links", "controls"):
                raw_items = bounded.get(key)
                if not isinstance(raw_items, list):
                    bounded[key] = []
                    continue
                safe_items: list[dict[str, str]] = []
                for raw_item in raw_items[:50]:
                    if not isinstance(raw_item, dict):
                        continue
                    item = {
                        str(name): str(value)[:500]
                        for name, value in raw_item.items()
                        if isinstance(name, str)
                    }
                    if key == "links" and "href" in item:
                        item["href"] = redact_link(item["href"])
                    safe_items.append(item)
                bounded[key] = safe_items
            return {"ok": True, "snapshot": bounded}

        return await self._safe(operation, operation_id=operation_id)

    async def click(
        self, page_id: str, selector: str, operation_id: str | None = None
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            safe_selector = self.policy.validate_selector(selector)
            self._page(page_id)
            self._consume_action()
            page = await self.client.click(self._browser_session_id, page_id, safe_selector)
            self._pages[page_id] = page
            return self._result_page(page, page_id)

        return await self._safe(operation, operation_id=operation_id)

    async def fill(
        self,
        page_id: str,
        selector: str,
        value: str,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            safe_selector, safe_value = self.policy.validate_value(selector, value)
            self._consume_action()
            page = await self.client.fill(
                self._browser_session_id, page_id, safe_selector, safe_value
            )
            self._pages[page_id] = page
            # Never echo the value, even redacted: a password-like value can be
            # distinctive and the caller does not need it for control flow.
            return self._result_page(page, page_id)

        return await self._safe(operation, operation_id=operation_id)

    async def press(
        self,
        page_id: str,
        key: str,
        selector: str = "body",
        operation_id: str | None = None,
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            safe_key = self.policy.validate_key(key)
            safe_selector = self.policy.validate_selector(selector)
            self._consume_action()
            page = await self.client.press(
                self._browser_session_id, page_id, safe_key, safe_selector
            )
            self._pages[page_id] = page
            return self._result_page(page, page_id)

        return await self._safe(operation, operation_id=operation_id)

    async def wait_for(
        self,
        page_id: str,
        condition: str,
        value: str,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            safe_condition, safe_value = self.policy.validate_wait(condition, value)
            self._page(page_id)
            if safe_condition == "url":
                await self.policy.validate_url(safe_value)
            result = await self.client.wait_for(
                self._browser_session_id, page_id, safe_condition, safe_value
            )
            self._pages[page_id] = result
            return self._result_page(result, page_id)

        return await self._safe(operation, operation_id=operation_id)

    async def screenshot(
        self,
        page_id: str,
        image_type: str = "png",
        full_page: bool = False,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            self._page(page_id)
            safe_type = image_type.lower().strip()
            if safe_type not in {"png", "jpeg"}:
                raise BrowserToolError(
                    BrowserErrorKind.INVALID_INPUT, "Screenshot format must be png or jpeg."
                )
            result: ScreenshotData = await self.client.screenshot(
                self._browser_session_id, page_id, safe_type, bool(full_page)
            )
            if len(result.content) > self.settings.max_screenshot_bytes:
                raise BrowserToolError(
                    BrowserErrorKind.OUTPUT_LIMIT, "Screenshot exceeds the configured size limit."
                )
            artifact = self.artifacts.write_screenshot(
                self._browser_session_id,
                result.content,
                mime_type=result.mime_type,
            )
            return {"ok": True, "artifact": artifact.to_dict()}

        return await self._safe(operation, operation_id=operation_id)

    async def close(
        self,
        page_id: str = "",
        all_pages: bool = False,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            await self._ensure_ready()
            if all_pages:
                for current_page in list(self._pages):
                    await self.client.close_page(self._browser_session_id, current_page)
                    self._pages.pop(current_page, None)
                return {"ok": True, "closed_pages": "all"}
            page = self._page(page_id)
            await self.client.close_page(self._browser_session_id, page.page_id)
            self._pages.pop(page.page_id, None)
            return {"ok": True, "closed_page": page.page_id}

        return await self._safe(operation, operation_id=operation_id)

    async def _mutating_page_call(
        self,
        page_id: str,
        call: Callable[[], Awaitable[PageState]],
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            self._ensure_usable()
            self.policy.validate_selector(page_id)
            self._page(page_id)
            self._consume_action()
            page = await call()
            self._pages[page_id] = page
            return self._result_page(page, page_id)

        return await self._safe(operation)

    def checkpoint_payload(self) -> dict[str, object]:
        """Return safe, JSON-compatible metadata; no live browser objects."""
        return {
            "session_id": self._browser_session_id,
            "transport": self.settings.transport,
            "open_pages": [
                {"page_id": page_id, "url": redact_url(page.url), "title": page.title[:200]}
                for page_id, page in self._pages.items()
            ],
            "persistent_profile": bool(self.settings.allow_persistent_profiles),
            "conversation_id": self.conversation_id,
            "completed_operation_ids": list(self._operation_results)[-_OPERATION_CACHE_LIMIT:],
        }

    def restore_checkpoint(self, payload: dict[str, object]) -> None:
        """Restore only safe metadata; the next operation reopens live state."""
        checkpoint_conversation = payload.get("conversation_id")
        legacy_session = payload.get("session_id")
        if (
            checkpoint_conversation is not None
            and str(checkpoint_conversation) != self.conversation_id
        ):
            raise ValueError("browser checkpoint belongs to a different session")
        if checkpoint_conversation is None and legacy_session == self.conversation_id:
            # Accept checkpoints written by the pre-opaque-session adapter.
            pass
        elif checkpoint_conversation is None and legacy_session not in {
            None,
            self._browser_session_id,
        }:
            # An opaque session id cannot prove ownership after a restart, so
            # the workflow/session conversation binding is the authoritative
            # check for new checkpoints.
            raise ValueError("browser checkpoint session metadata is not compatible")
        if checkpoint_conversation is not None and isinstance(legacy_session, str):
            # New managers can reuse the isolated persistent-profile directory,
            # but only for an opaque id generated by this adapter.  Never use a
            # checkpoint value as a filesystem path without this strict check.
            if len(legacy_session) == 32 and all(
                character in "0123456789abcdef" for character in legacy_session
            ):
                # A live transport may still own pages under its current
                # session key. Keep that key in place rather than orphaning
                # those pages; a fresh manager can safely rebind before launch.
                if getattr(self.client, "_context", None) is None:
                    self._browser_session_id = legacy_session
                    rebinder = getattr(self.client, "rebind_session", None)
                    if callable(rebinder):
                        rebinder(legacy_session)
        # Deliberately do not recreate pages from checkpoint data.  A page URL
        # is informational and reopening it would be an unapproved side effect.
        self._pages.clear()
        self._operation_in_flight.clear()
        self._operation_results.clear()
        completed_ids = payload.get("completed_operation_ids", [])
        if isinstance(completed_ids, list):
            for raw_id in completed_ids[:_OPERATION_CACHE_LIMIT]:
                if not isinstance(raw_id, str):
                    continue
                try:
                    operation_id = self._validate_operation_id(raw_id)
                except BrowserToolError:
                    continue
                if operation_id is not None:
                    self._operation_results[operation_id] = {
                        "ok": False,
                        "error_kind": BrowserErrorKind.STALE_PAGE.value,
                        "error": (
                            "This operation completed before resume; reopen the page and "
                            "use a new operation id."
                        ),
                        "operation_id": operation_id,
                    }

    async def close_session(self) -> None:
        if self._closed:
            return
        try:
            async with self._operation_lock:
                await self.client.close_session(self._browser_session_id)
        finally:
            self._pages.clear()
            self._closed = True


def create_browser_session(
    settings: CloakBrowserSettings,
    conversation_id: str,
    workspace_root: Path,
) -> BrowserSessionManager:
    """Construct the session manager without importing the optional package."""
    return BrowserSessionManager(settings, conversation_id, workspace_root)
