"""Optional Playwright transport for the session-owned browser adapter.

The Playwright package is imported only after the backend is selected and a
browser operation needs it.  The client subclasses the existing local browser
adapter so policy routing, bounded snapshots, action methods, and cleanup have
one implementation shared with the CloakBrowser transport.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

from agenthicc.config import CloakBrowserSettings, PlaywrightSettings
from agenthicc.tools.cloakbrowser.client import (
    BrowserHealth,
    LocalCloakBrowserClient,
    _invoke,
    _invoke_callable,
)
from agenthicc.tools.cloakbrowser.errors import BrowserErrorKind, BrowserToolError
from agenthicc.tools.cloakbrowser.policy import BrowserPolicy

log = logging.getLogger(__name__)

__all__ = ["PlaywrightBrowserClient"]


class PlaywrightBrowserClient(LocalCloakBrowserClient):
    """Run the common browser client contract on Microsoft Playwright."""

    def __init__(
        self,
        settings: PlaywrightSettings,
        policy: BrowserPolicy,
        workspace_root: Path,
        *,
        session_id: str = "",
    ) -> None:
        # The inherited action/snapshot implementation only requires the
        # common browser settings attributes.  Keep the CloakBrowser type
        # annotation at that compatibility boundary without importing either
        # optional browser package.
        super().__init__(
            cast(CloakBrowserSettings, settings),
            policy,
            workspace_root,
            session_id=session_id,
        )
        self._playwright: object | None = None
        self._browser: object | None = None
        self._playwright_settings = settings

    async def _get_context(self) -> object:
        if self._context is not None:
            return self._context

        try:
            async_api = importlib.import_module("playwright.async_api")
        except ModuleNotFoundError as exc:
            raise BrowserToolError(
                BrowserErrorKind.DEPENDENCY_MISSING,
                "Playwright is not installed; install the optional 'playwright' extra.",
            ) from exc

        start = getattr(async_api, "async_playwright", None)
        if not callable(start):
            raise BrowserToolError(
                BrowserErrorKind.EXECUTION,
                "Playwright's async API is unavailable.",
            )

        try:
            context_manager = await _invoke_callable(cast(Callable[..., object], start))
            self._playwright = await _invoke(context_manager, "start")
            browser_type = getattr(self._playwright, self._playwright_settings.browser_type, None)
            if browser_type is None:
                raise BrowserToolError(
                    BrowserErrorKind.INVALID_INPUT,
                    "Configured Playwright browser type is unavailable.",
                )

            launch_options: dict[str, object] = {
                "headless": self._playwright_settings.headless,
            }
            if self._playwright_settings.browser_channel:
                launch_options["channel"] = self._playwright_settings.browser_channel
            if self._playwright_settings.executable_path:
                launch_options["executable_path"] = self._playwright_settings.executable_path

            if self._playwright_settings.allow_persistent_profiles:
                profile = (
                    self._workspace_root / self._playwright_settings.profile_root / self._session_id
                )
                profile.mkdir(parents=True, exist_ok=True)
                launch_options["accept_downloads"] = True
                self._context = await _invoke(
                    browser_type,
                    "launch_persistent_context",
                    str(profile),
                    **launch_options,
                )
            else:
                self._browser = await _invoke(browser_type, "launch", **launch_options)
                self._context = await _invoke(
                    self._browser,
                    "new_context",
                    accept_downloads=True,
                )
            return self._context
        except BrowserToolError:
            await self._cleanup_runtime()
            raise
        except Exception as exc:  # noqa: BLE001 — optional runtime boundary
            await self._cleanup_runtime()
            raise BrowserToolError(
                BrowserErrorKind.BROWSER_UNAVAILABLE,
                "Playwright could not start its browser runtime.",
            ) from exc

    async def _cleanup_runtime(self) -> None:
        context = self._context
        self._context = None
        if context is not None:
            try:
                await _invoke(context, "close")
            except Exception:  # noqa: BLE001 — cleanup must not mask startup errors
                log.debug("Playwright context cleanup failed", exc_info=True)
        browser = self._browser
        self._browser = None
        if browser is not None:
            try:
                await _invoke(browser, "close")
            except Exception:  # noqa: BLE001 — cleanup must not mask startup errors
                log.debug("Playwright browser cleanup failed", exc_info=True)
        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            try:
                await _invoke(playwright, "stop")
            except Exception:  # noqa: BLE001 — cleanup must not mask startup errors
                log.debug("Playwright runtime cleanup failed", exc_info=True)

    async def health(self) -> BrowserHealth:
        try:
            await self._get_context()
        except BrowserToolError as exc:
            return BrowserHealth(exc.kind.value, "local", exc.safe_message)
        except Exception:  # noqa: BLE001 — health must remain a safe boundary
            return BrowserHealth(
                BrowserErrorKind.BROWSER_UNAVAILABLE.value,
                "local",
                "Playwright browser runtime is unavailable.",
            )
        return BrowserHealth("ready", "local", "")

    async def close_session(self, session_id: str) -> None:
        try:
            await super().close_session(session_id)
        finally:
            await self._cleanup_runtime()
