"""Optional CloakBrowser transports behind a small typed adapter boundary.

Only this module imports optional browser packages, and it does so inside
methods.  The rest of agenthicc can import the browser tool package in a base
installation without importing Playwright or starting a browser.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from agenthicc.config import CloakBrowserSettings

from .errors import BrowserErrorKind, BrowserToolError
from .policy import BrowserPolicy, redact_link, redact_url

log = logging.getLogger(__name__)

__all__ = [
    "BrowserHealth",
    "PageSnapshot",
    "PageState",
    "ScreenshotData",
    "BrowserClient",
    "UnavailableBrowserClient",
    "LocalCloakBrowserClient",
    "CdpCloakBrowserClient",
]


@dataclass(frozen=True, slots=True)
class BrowserHealth:
    status: str
    transport: str
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "transport": self.transport, "message": self.message}


@dataclass(frozen=True, slots=True)
class PageState:
    page_id: str
    url: str
    title: str

    def to_dict(self) -> dict[str, object]:
        return {"page_id": self.page_id, "url": redact_url(self.url), "title": self.title[:500]}


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    page: PageState
    text: str
    links: tuple[dict[str, str], ...] = ()
    controls: tuple[dict[str, str], ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "page": self.page.to_dict(),
            "untrusted": True,
            "text": self.text,
            "links": [dict(item) for item in self.links],
            "controls": [dict(item) for item in self.controls],
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ScreenshotData:
    content: bytes
    mime_type: str


class BrowserClient(Protocol):
    """Transport contract used by :class:`BrowserSessionManager`."""

    async def health(self) -> BrowserHealth: ...

    async def open_page(self, session_id: str, url: str) -> PageState: ...

    async def snapshot(self, session_id: str, page_id: str) -> PageSnapshot: ...

    async def click(self, session_id: str, page_id: str, selector: str) -> PageState: ...

    async def fill(self, session_id: str, page_id: str, selector: str, value: str) -> PageState: ...

    async def press(self, session_id: str, page_id: str, key: str, selector: str) -> PageState: ...

    async def wait_for(
        self,
        session_id: str,
        page_id: str,
        condition: str,
        value: str,
    ) -> PageState: ...

    async def screenshot(
        self,
        session_id: str,
        page_id: str,
        image_type: str,
        full_page: bool,
    ) -> ScreenshotData: ...

    async def close_page(self, session_id: str, page_id: str) -> None: ...

    async def close_session(self, session_id: str) -> None: ...


async def _invoke(target: object, name: str, *args: object, **kwargs: object) -> object:
    """Call an optional-library method and await it when necessary."""
    method = getattr(target, name, None)
    if not callable(method):
        raise BrowserToolError(
            BrowserErrorKind.EXECUTION, "Browser backend does not support this operation."
        )
    result = cast(object, method(*args, **kwargs))
    if inspect.isawaitable(result):
        return await cast(Awaitable[object], result)
    return result


async def _invoke_callable(
    function: Callable[..., object], *args: object, **kwargs: object
) -> object:
    result = function(*args, **kwargs)
    if inspect.isawaitable(result):
        return await cast(Awaitable[object], result)
    return result


async def _page_state(page: object, page_id: str) -> PageState:
    url = getattr(page, "url", "")
    if callable(url):
        url = await _invoke(page, "url")
    title = await _invoke(page, "title")
    return PageState(page_id, str(url), str(title))


class UnavailableBrowserClient:
    """Client used when the feature is disabled or its optional package is absent."""

    def __init__(self, transport: str, error: BrowserErrorKind, message: str) -> None:
        self._health = BrowserHealth(error.value, transport, message)

    async def health(self) -> BrowserHealth:
        return self._health

    def _raise(self) -> None:
        raise BrowserToolError(BrowserErrorKind(self._health.status), self._health.message)

    async def open_page(self, session_id: str, url: str) -> PageState:
        self._raise()
        raise AssertionError

    async def snapshot(self, session_id: str, page_id: str) -> PageSnapshot:
        self._raise()
        raise AssertionError

    async def click(self, session_id: str, page_id: str, selector: str) -> PageState:
        self._raise()
        raise AssertionError

    async def fill(self, session_id: str, page_id: str, selector: str, value: str) -> PageState:
        self._raise()
        raise AssertionError

    async def press(self, session_id: str, page_id: str, key: str, selector: str) -> PageState:
        self._raise()
        raise AssertionError

    async def wait_for(
        self, session_id: str, page_id: str, condition: str, value: str
    ) -> PageState:
        self._raise()
        raise AssertionError

    async def screenshot(
        self,
        session_id: str,
        page_id: str,
        image_type: str,
        full_page: bool,
    ) -> ScreenshotData:
        self._raise()
        raise AssertionError

    async def close_page(self, session_id: str, page_id: str) -> None:
        self._raise()

    async def close_session(self, session_id: str) -> None:
        return None


class LocalCloakBrowserClient:
    """Local async CloakBrowser adapter with no direct model-facing escape hatch."""

    def __init__(
        self,
        settings: CloakBrowserSettings,
        policy: BrowserPolicy,
        workspace_root: Path,
        *,
        session_id: str = "",
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._workspace_root = workspace_root
        self._session_id = session_id or uuid.uuid4().hex
        self._module: object | None = None
        self._context: object | None = None
        self._pages: dict[tuple[str, str], object] = {}
        self._package_error: BrowserToolError | None = None

    def rebind_session(self, session_id: str) -> None:
        """Reattach a not-yet-launched client to a persisted opaque session id."""
        if self._context is not None:
            return
        self._session_id = session_id

    def _load_module(self) -> object:
        if self._module is not None:
            return self._module
        try:
            if importlib.util.find_spec("cloakbrowser") is None:
                raise ModuleNotFoundError("cloakbrowser")
            self._module = importlib.import_module("cloakbrowser")
        except ModuleNotFoundError as exc:
            self._package_error = BrowserToolError(
                BrowserErrorKind.DEPENDENCY_MISSING,
                "CloakBrowser is not installed; install the optional 'cloakbrowser' extra.",
            )
            raise self._package_error from exc
        except Exception as exc:  # noqa: BLE001 — optional package boundary
            self._package_error = BrowserToolError(
                BrowserErrorKind.BINARY_MISSING,
                "CloakBrowser could not initialize its local browser runtime.",
            )
            raise self._package_error from exc
        return self._module

    async def health(self) -> BrowserHealth:
        try:
            module = self._load_module()
        except BrowserToolError as exc:
            return BrowserHealth(exc.kind.value, "local", exc.safe_message)
        # The wrapper may expose a diagnostic helper; do not require it because
        # supported versions have differed in this detail.
        checker = getattr(module, "check_installation", None)
        if callable(checker):
            try:
                result = await _invoke_callable(cast(Callable[..., object], checker))
                if result is False:
                    return BrowserHealth(
                        BrowserErrorKind.BINARY_MISSING.value,
                        "local",
                        "CloakBrowser browser runtime is unavailable.",
                    )
            except Exception:  # noqa: BLE001 — health must stay safe and bounded
                return BrowserHealth(
                    BrowserErrorKind.BINARY_MISSING.value,
                    "local",
                    "CloakBrowser browser runtime is unavailable.",
                )
        binary_info = getattr(module, "binary_info", None)
        if callable(binary_info):
            try:
                info = await _invoke_callable(cast(Callable[..., object], binary_info))
                if isinstance(info, dict) and info.get("installed") is False:
                    return BrowserHealth(
                        BrowserErrorKind.BINARY_MISSING.value,
                        "local",
                        "CloakBrowser browser runtime is unavailable.",
                    )
            except Exception:  # noqa: BLE001 — health must stay safe and bounded
                return BrowserHealth(
                    BrowserErrorKind.BINARY_MISSING.value,
                    "local",
                    "CloakBrowser browser runtime is unavailable.",
                )
        return BrowserHealth("ready", "local", "")

    async def _get_context(self) -> object:
        if self._context is not None:
            return self._context
        module = self._load_module()
        launch_options: dict[str, object] = {
            "headless": self._settings.headless,
            "proxy": None,
            "args": [],
            "stealth_args": False,
            "geoip": False,
            "humanize": False,
            "extension_paths": [],
        }
        license_key = os.environ.get(self._settings.license_key_env, "")
        if license_key:
            launch_options["license_key"] = license_key
        if self._settings.allow_persistent_profiles:
            launch_persistent = getattr(module, "launch_persistent_context_async", None)
            if not callable(launch_persistent):
                raise BrowserToolError(
                    BrowserErrorKind.EXECUTION,
                    "The installed CloakBrowser version does not support persistent contexts.",
                )
            # Each agenthicc session receives its own profile directory.  The
            # manager passes an opaque session id to this transport; never
            # derive a browser profile from the provider conversation id.
            profile = self._workspace_root / self._settings.profile_root / self._session_id
            profile.mkdir(parents=True, exist_ok=True)
            self._context = await _invoke_callable(
                cast(Callable[..., object], launch_persistent),
                str(profile),
                **launch_options,
            )
        else:
            launch = getattr(module, "launch_context_async", None) or getattr(
                module, "launch_async", None
            )
            if not callable(launch):
                raise BrowserToolError(
                    BrowserErrorKind.EXECUTION,
                    "The installed CloakBrowser version has no supported async launcher.",
                )
            self._context = await _invoke_callable(
                cast(Callable[..., object], launch),
                **launch_options,
            )
        return self._context

    async def _get_page(self, session_id: str, page_id: str) -> object:
        page = self._pages.get((session_id, page_id))
        if page is None:
            raise BrowserToolError(BrowserErrorKind.NOT_FOUND, "Browser page is no longer open.")
        return page

    async def _attach_policy(self, page: object) -> None:
        route = getattr(page, "route", None)
        if not callable(route):
            return

        async def guard(request_route: object, request: object) -> None:
            request_url = str(getattr(request, "url", ""))
            try:
                await self._policy.validate_url(request_url)
            except BrowserToolError:
                await _invoke(request_route, "abort", error_code="blockedbyclient")
                return
            await _invoke(request_route, "continue_")

        await _invoke(page, "route", "**/*", guard)
        on = getattr(page, "on", None)
        if callable(on):

            async def reject_popup(popup: object) -> None:
                try:
                    await _invoke(popup, "close")
                except Exception:  # noqa: BLE001 — popup cleanup is best effort
                    log.debug("blocked browser popup cleanup failed", exc_info=True)

            def popup_handler(popup: object) -> None:
                import asyncio  # noqa: PLC0415

                asyncio.create_task(reject_popup(popup))

            await _invoke(page, "on", "popup", popup_handler)

    async def _navigate(self, page: object, url: str) -> None:
        await self._policy.validate_url(url)
        await self._attach_policy(page)
        await _invoke(
            page,
            "goto",
            url,
            wait_until="domcontentloaded",
            timeout=int(self._settings.navigation_timeout_s * 1000),
        )
        current_url = str(getattr(page, "url", url))
        await self._policy.validate_url(current_url)

    async def open_page(self, session_id: str, url: str) -> PageState:
        context = await self._get_context()
        page = await _invoke(context, "new_page")
        page_id = uuid.uuid4().hex
        self._pages[(session_id, page_id)] = page
        try:
            await self._navigate(page, url)
            return await _page_state(page, page_id)
        except Exception:
            self._pages.pop((session_id, page_id), None)
            try:
                await _invoke(page, "close")
            except Exception:  # noqa: BLE001 — cleanup should not mask policy failure
                pass
            raise

    async def snapshot(self, session_id: str, page_id: str) -> PageSnapshot:
        page = await self._get_page(session_id, page_id)
        state = await _page_state(page, page_id)
        body = await _invoke(page, "locator", "body")
        text = str(
            await _invoke(
                cast(object, getattr(body, "first", body)),
                "inner_text",
                timeout=int(self._settings.action_timeout_s * 1000),
            )
        )
        truncated = len(text) > self._settings.max_snapshot_chars
        text = text[: self._settings.max_snapshot_chars]
        links = await self._collect_elements(page, "a", "href")
        controls = await self._collect_elements(page, "button,input,textarea,select", "aria-label")
        return PageSnapshot(state, text, tuple(links), tuple(controls), truncated)

    async def _collect_elements(
        self, page: object, selector: str, attribute: str
    ) -> list[dict[str, str]]:
        locator = await _invoke(page, "locator", selector)
        count = await _invoke(locator, "count")
        bounded = min(count if isinstance(count, int) else 0, 50)
        values: list[dict[str, str]] = []
        for index in range(bounded):
            item = await _invoke(locator, "nth", index)
            try:
                label = await _invoke(item, "get_attribute", attribute)
                text = await _invoke(item, "inner_text", timeout=1000)
            except Exception:  # noqa: BLE001 — one malformed control must not fail a snapshot
                continue
            rendered_label = str(label or "")
            if attribute == "href":
                rendered_label = redact_link(rendered_label)
            values.append({attribute: rendered_label[:500], "text": str(text or "")[:500]})
        return values

    async def _action(self, page: object, method: str, selector: str, *args: object) -> PageState:
        locator = await _invoke(page, "locator", selector)
        target = cast(object, getattr(locator, "first", locator))
        await _invoke(target, method, *args, timeout=int(self._settings.action_timeout_s * 1000))
        state = await _page_state(page, "")
        await self._policy.validate_url(state.url)
        return state

    async def click(self, session_id: str, page_id: str, selector: str) -> PageState:
        return await self._action(await self._get_page(session_id, page_id), "click", selector)

    async def fill(self, session_id: str, page_id: str, selector: str, value: str) -> PageState:
        return await self._action(
            await self._get_page(session_id, page_id), "fill", selector, value
        )

    async def press(self, session_id: str, page_id: str, key: str, selector: str) -> PageState:
        return await self._action(await self._get_page(session_id, page_id), "press", selector, key)

    async def wait_for(
        self, session_id: str, page_id: str, condition: str, value: str
    ) -> PageState:
        page = await self._get_page(session_id, page_id)
        timeout = int(self._settings.action_timeout_s * 1000)
        if condition == "selector":
            locator = await _invoke(page, "locator", value)
            await _invoke(
                cast(object, getattr(locator, "first", locator)), "wait_for", timeout=timeout
            )
        elif condition == "text":
            locator = await _invoke(page, "get_by_text", value)
            await _invoke(
                cast(object, getattr(locator, "first", locator)), "wait_for", timeout=timeout
            )
        elif condition == "url":
            await _invoke(page, "wait_for_url", value, timeout=timeout)
            await self._policy.validate_url(str(getattr(page, "url", value)))
        else:
            if value not in {"domcontentloaded", "load", "networkidle"}:
                raise BrowserToolError(BrowserErrorKind.INVALID_INPUT, "Load state is invalid.")
            await _invoke(page, "wait_for_load_state", value, timeout=timeout)
        return await _page_state(page, page_id)

    async def screenshot(
        self,
        session_id: str,
        page_id: str,
        image_type: str,
        full_page: bool,
    ) -> ScreenshotData:
        page = await self._get_page(session_id, page_id)
        image_type = image_type.lower()
        if image_type not in {"png", "jpeg"}:
            raise BrowserToolError(
                BrowserErrorKind.INVALID_INPUT, "Screenshot format must be png or jpeg."
            )
        content = await _invoke(
            page,
            "screenshot",
            type=image_type,
            full_page=full_page,
            timeout=int(self._settings.action_timeout_s * 1000),
        )
        data = bytes(cast(bytes, content))
        if len(data) > self._settings.max_screenshot_bytes:
            raise BrowserToolError(
                BrowserErrorKind.OUTPUT_LIMIT, "Screenshot exceeds the configured size limit."
            )
        return ScreenshotData(data, "image/jpeg" if image_type == "jpeg" else "image/png")

    async def close_page(self, session_id: str, page_id: str) -> None:
        page = await self._get_page(session_id, page_id)
        self._pages.pop((session_id, page_id), None)
        await _invoke(page, "close")

    async def close_session(self, session_id: str) -> None:
        pages = [key for key in self._pages if key[0] == session_id]
        for key in pages:
            page = self._pages.pop(key)
            try:
                await _invoke(page, "close")
            except Exception:  # noqa: BLE001 — best-effort shutdown
                log.debug("browser page cleanup failed", exc_info=True)
        if self._context is not None:
            try:
                await _invoke(self._context, "close")
            finally:
                self._context = None


class CdpCloakBrowserClient(LocalCloakBrowserClient):
    """Loopback-only CDP transport.

    CDP is deliberately separate from local launch.  It never accepts a model
    supplied endpoint; the validated operator setting is the only endpoint.
    """

    async def _get_context(self) -> object:
        if self._context is not None:
            return self._context
        try:
            async_api = importlib.import_module("playwright.async_api")
        except ModuleNotFoundError as exc:
            raise BrowserToolError(
                BrowserErrorKind.DEPENDENCY_MISSING,
                "CDP transport requires the optional browser extra.",
            ) from exc
        start = getattr(async_api, "async_playwright", None)
        if not callable(start):
            raise BrowserToolError(
                BrowserErrorKind.EXECUTION, "CDP browser transport is unavailable."
            )
        context_manager = await _invoke_callable(cast(Callable[..., object], start))
        self._playwright = await _invoke(context_manager, "start")
        chromium = getattr(self._playwright, "chromium", None)
        if chromium is None:
            raise BrowserToolError(
                BrowserErrorKind.EXECUTION, "CDP browser transport is unavailable."
            )
        browser = await _invoke(chromium, "connect_over_cdp", self._settings.cdp_endpoint)
        new_context = getattr(browser, "new_context", None)
        if not callable(new_context):
            await _invoke(self._playwright, "stop")
            self._playwright = None
            raise BrowserToolError(
                BrowserErrorKind.EXECUTION,
                "CDP browser transport cannot create an isolated session context.",
            )
        try:
            # Never attach a conversation to the operator's default CDP
            # context. A fresh context is the isolation boundary for cookies,
            # storage, pages, and permissions.
            self._context = await _invoke(browser, "new_context")
        except Exception:
            try:
                await _invoke(self._playwright, "stop")
            finally:
                self._playwright = None
            raise
        return self._context

    async def health(self) -> BrowserHealth:
        try:
            await self._get_context()
        except BrowserToolError as exc:
            return BrowserHealth(exc.kind.value, "cdp", exc.safe_message)
        except Exception:  # noqa: BLE001 — CDP health must not leak endpoint details
            return BrowserHealth(
                "browser_unavailable", "cdp", "CDP browser endpoint is unavailable."
            )
        return BrowserHealth("ready", "cdp", "")

    async def close_session(self, session_id: str) -> None:
        await super().close_session(session_id)
        playwright = getattr(self, "_playwright", None)
        if playwright is not None:
            try:
                await _invoke(playwright, "stop")
            finally:
                self._playwright = None
