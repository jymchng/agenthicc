# src/agenthicc/auth.py
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "ACCOUNT_TIER_TTL_S",
    "AccountTier",
    "AuthClient",
    "TokenStore",
    "TokenBundle",
    "NotLoggedInError",
]

AccountTier = Literal["signed_out", "free", "pro"]
#: How long a cached account tier is trusted before the token store is read.
ACCOUNT_TIER_TTL_S = 24 * 60 * 60
_ACCOUNT_TIER_SCHEMA = 1
_ACCOUNT_TIERS: dict[str, AccountTier] = {"signed_out": "signed_out", "free": "free", "pro": "pro"}

AGENTHICC_CLIENT_ID = "agenthicc-cli"
AGENTHICC_AUTH_URL = "https://agenthicc.ai/oauth/authorize"
AGENTHICC_TOKEN_URL = "https://agenthicc.ai/oauth/token"
AGENTHICC_REVOKE_URL = "https://agenthicc.ai/oauth/revoke"
KEYRING_SERVICE = "agenthicc"
KEYRING_USERNAME = "tokens"
TOKEN_REFRESH_BUFFER = 60  # seconds before expiry to trigger refresh


class NotLoggedInError(Exception):
    """Raised when an authenticated operation is attempted without a token."""


class AuthNetworkError(Exception):
    """Raised when an OAuth HTTP call fails due to a network or timeout error.

    Callers (CLI login command, token refresh) catch this and display a
    human-readable message rather than a raw httpx traceback.
    """


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    plan: str  # "free" | "pro" | "enterprise"
    email: str
    user_id: str

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - TOKEN_REFRESH_BUFFER

    @property
    def is_pro(self) -> bool:
        return self.plan in ("pro", "enterprise")

    @property
    def tier(self) -> AccountTier:
        return "pro" if self.is_pro else "free"

    def to_dict(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "plan": self.plan,
            "email": self.email,
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TokenBundle:
        values = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at": data.get("expires_at"),
            "plan": data.get("plan"),
            "email": data.get("email"),
            "user_id": data.get("user_id"),
        }
        string_fields = ("access_token", "refresh_token", "plan", "email", "user_id")
        if not all(isinstance(values[key], str) for key in string_fields):
            raise KeyError("invalid token fields")
        if not isinstance(values["expires_at"], (int, float)):
            raise KeyError("invalid token expiry")
        access_token = values["access_token"]
        refresh_token = values["refresh_token"]
        plan = values["plan"]
        email = values["email"]
        user_id = values["user_id"]
        assert isinstance(access_token, str)
        assert isinstance(refresh_token, str)
        assert isinstance(plan, str)
        assert isinstance(email, str)
        assert isinstance(user_id, str)
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(values["expires_at"]),
            plan=plan,
            email=email,
            user_id=user_id,
        )


class TokenStore:
    """Persist tokens in the OS keychain; fall back to a local file.

    Alongside the tokens the store keeps the derived account tier in a small
    owner-only file. Start-up reads only that file: the first keyring call
    performs backend discovery and may talk to D-Bus (AGX-02). The tier file
    never contains token material.
    """

    def __init__(self) -> None:
        # Tests may set this to a tmp_path to avoid touching $HOME; the cached
        # account tier lives next to it.
        self._fallback_path: Path = Path.home() / ".agenthicc" / "tokens.json"

    @property
    def _tier_path(self) -> Path:
        return self._fallback_path.with_name("account-tier.json")

    def load(self) -> TokenBundle | None:
        """Return stored tokens or None."""
        raw = self._read()
        if raw is None:
            return None
        try:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                return None
            return TokenBundle.from_dict(loaded)
        except (KeyError, json.JSONDecodeError):
            return None

    def save(self, bundle: TokenBundle) -> None:
        self._write(json.dumps(bundle.to_dict()))
        self._remember_tier(bundle.tier)

    def clear(self) -> None:
        self._write(None)
        self._remember_tier("signed_out")

    def account_tier(self, *, now: float | None = None) -> AccountTier:
        """Return the account tier, reading the token store only when stale.

        Blocking: on a stale cache this reads the keyring, so call it from a
        worker thread when an event loop is running.
        """
        cached = self.cached_tier(now=now)
        if cached is not None:
            return cached
        bundle = self.load()
        tier: AccountTier = "signed_out" if bundle is None else bundle.tier
        self._remember_tier(tier, now=now)
        return tier

    def cached_tier(self, *, now: float | None = None) -> AccountTier | None:
        """Return the cached tier while it is fresh, otherwise ``None``."""
        try:
            raw: object = json.loads(self._tier_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("schema") != _ACCOUNT_TIER_SCHEMA:
            return None
        tier = raw.get("tier")
        checked_at = raw.get("checked_at")
        if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
            return None
        age = (time.time() if now is None else now) - checked_at
        if not 0 <= age < ACCOUNT_TIER_TTL_S:
            return None
        return _ACCOUNT_TIERS.get(tier) if isinstance(tier, str) else None

    def _remember_tier(self, tier: AccountTier, *, now: float | None = None) -> None:
        """Atomically record *tier* with owner-only permissions (best effort)."""
        payload = json.dumps(
            {
                "schema": _ACCOUNT_TIER_SCHEMA,
                "tier": tier,
                "checked_at": time.time() if now is None else now,
            }
        )
        path = self._tier_path
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, path)
        except OSError:
            # The cache only avoids a keyring read; the next start re-derives it.
            with contextlib.suppress(OSError):
                temporary.unlink()

    def _read(self) -> str | None:
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception:
            return self._fallback_path.read_text() if self._fallback_path.exists() else None

    def _write(self, data: str | None) -> None:
        try:
            import keyring

            if data is None:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
            else:
                keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, data)
        except Exception:
            if data is None:
                self._fallback_path.unlink(missing_ok=True)
            else:
                path = self._fallback_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
                os.chmod(path, 0o600)


class AuthClient:
    """Handles the OAuth PKCE flow and token refresh."""

    def __init__(self, store: TokenStore | None = None) -> None:
        self._store = store or TokenStore()

    async def login(self) -> TokenBundle:
        """Run the browser OAuth flow; return and store the token bundle."""
        import asyncio
        import webbrowser
        from aiohttp import web

        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

        port = self._find_free_port()
        redirect_uri = f"http://localhost:{port}/callback"

        auth_url = (
            f"{AGENTHICC_AUTH_URL}"
            f"?client_id={AGENTHICC_CLIENT_ID}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code"
            f"&code_challenge={code_challenge}"
            f"&code_challenge_method=S256"
            f"&scope=openid+profile+agent:run"
        )

        code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

        async def callback(request: web.Request) -> web.Response:
            code = request.query.get("code")
            if code:
                code_future.set_result(code)
            return web.Response(text="Login successful! You can close this tab.")

        app = web.Application()
        app.router.add_get("/callback", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", port)
        await site.start()

        webbrowser.open(auth_url)
        print(f"Opening browser... if it doesn't open, visit:\n  {auth_url}")

        try:
            code = await asyncio.wait_for(code_future, timeout=120)
        finally:
            await runner.cleanup()

        bundle = await self._exchange_code(code, code_verifier, redirect_uri)
        self._store.save(bundle)
        return bundle

    async def get_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        import asyncio

        # Keyring access can block on backend discovery or D-Bus.
        bundle = await asyncio.to_thread(self._store.load)
        if bundle is None:
            raise NotLoggedInError("Not logged in. Run: agenthicc login")
        if bundle.is_expired:
            bundle = await self._refresh(bundle)
            await asyncio.to_thread(self._store.save, bundle)
        return bundle.access_token

    async def logout(self) -> None:
        bundle = self._store.load()
        if bundle:
            await self._revoke(bundle.access_token)
        self._store.clear()

    def current_bundle(self) -> TokenBundle | None:
        return self._store.load()

    def account_tier(self) -> AccountTier:
        """Return the cached account tier (see :meth:`TokenStore.account_tier`)."""
        return self._store.account_tier()

    # -- internal -------------------------------------------------------------

    async def _exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> TokenBundle:
        from agenthicc.tools.http import agenthicc_http_client
        import httpx

        try:
            async with agenthicc_http_client(timeout=15.0) as client:
                resp = await client.post(
                    AGENTHICC_TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": AGENTHICC_CLIENT_ID,
                        "code": code,
                        "code_verifier": code_verifier,
                        "redirect_uri": redirect_uri,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise AuthNetworkError(
                f"Auth server timed out ({type(exc).__name__}). "
                "Check your connection and try again."
            ) from exc
        except httpx.HTTPError as exc:
            raise AuthNetworkError(
                f"Auth request failed ({type(exc).__name__}: {exc}). "
                "Check your connection and try again."
            ) from exc
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + float(data.get("expires_in", 3600)),
            plan=str(data.get("plan", "free")),
            email=str(data.get("email", "")),
            user_id=str(data.get("user_id", "")),
        )

    async def _refresh(self, bundle: TokenBundle) -> TokenBundle:
        from agenthicc.tools.http import agenthicc_http_client
        import httpx

        try:
            async with agenthicc_http_client(timeout=15.0) as client:
                resp = await client.post(
                    AGENTHICC_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": AGENTHICC_CLIENT_ID,
                        "refresh_token": bundle.refresh_token,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise AuthNetworkError(
                f"Token refresh timed out ({type(exc).__name__}). "
                "Check your connection and try again."
            ) from exc
        except httpx.HTTPError as exc:
            raise AuthNetworkError(
                f"Token refresh failed ({type(exc).__name__}: {exc}). "
                "Check your connection and try again."
            ) from exc
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", bundle.refresh_token),
            expires_at=time.time() + float(data.get("expires_in", 3600)),
            plan=str(data.get("plan", bundle.plan)),
            email=bundle.email,
            user_id=bundle.user_id,
        )

    async def _revoke(self, access_token: str) -> None:
        from agenthicc.tools.http import agenthicc_http_client

        try:
            async with agenthicc_http_client(timeout=10.0) as client:
                await client.post(
                    AGENTHICC_REVOKE_URL,
                    data={
                        "token": access_token,
                        "client_id": AGENTHICC_CLIENT_ID,
                    },
                )
        except Exception:
            pass  # best-effort revoke

    @staticmethod
    def _find_free_port() -> int:
        import socket

        with socket.socket() as s:
            s.bind(("localhost", 0))
            return int(s.getsockname()[1])
