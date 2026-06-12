# src/agenthicc/auth.py
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AuthClient",
    "TokenStore",
    "TokenBundle",
    "NotLoggedInError",
]

AGENTHICC_CLIENT_ID  = "agenthicc-cli"
AGENTHICC_AUTH_URL   = "https://agenthicc.ai/oauth/authorize"
AGENTHICC_TOKEN_URL  = "https://agenthicc.ai/oauth/token"
AGENTHICC_REVOKE_URL = "https://agenthicc.ai/oauth/revoke"
KEYRING_SERVICE      = "agenthicc"
KEYRING_USERNAME     = "tokens"
TOKEN_REFRESH_BUFFER = 60   # seconds before expiry to trigger refresh


class NotLoggedInError(Exception):
    """Raised when an authenticated operation is attempted without a token."""


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: float       # Unix timestamp
    plan: str               # "free" | "pro" | "enterprise"
    email: str
    user_id: str

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - TOKEN_REFRESH_BUFFER

    @property
    def is_pro(self) -> bool:
        return self.plan in ("pro", "enterprise")

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "plan": self.plan,
            "email": self.email,
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenBundle:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})


class TokenStore:
    """Persist tokens in the OS keychain; fall back to a local file."""

    def __init__(self) -> None:
        # Tests may set this to a tmp_path to avoid touching $HOME.
        self._fallback_path: Path = Path.home() / ".agenthicc" / "tokens.json"

    def load(self) -> TokenBundle | None:
        """Return stored tokens or None."""
        raw = self._read()
        if raw is None:
            return None
        try:
            return TokenBundle.from_dict(json.loads(raw))
        except (KeyError, json.JSONDecodeError):
            return None

    def save(self, bundle: TokenBundle) -> None:
        self._write(json.dumps(bundle.to_dict()))

    def clear(self) -> None:
        self._write(None)

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
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

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
        bundle = self._store.load()
        if bundle is None:
            raise NotLoggedInError(
                "Not logged in. Run: agenthicc login"
            )
        if bundle.is_expired:
            bundle = await self._refresh(bundle)
            self._store.save(bundle)
        return bundle.access_token

    async def logout(self) -> None:
        bundle = self._store.load()
        if bundle:
            await self._revoke(bundle.access_token)
        self._store.clear()

    def current_bundle(self) -> TokenBundle | None:
        return self._store.load()

    # -- internal -------------------------------------------------------------

    async def _exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenBundle:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(AGENTHICC_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "client_id": AGENTHICC_CLIENT_ID,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            })
            resp.raise_for_status()
            data = resp.json()
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + data.get("expires_in", 3600),
            plan=data.get("plan", "free"),
            email=data.get("email", ""),
            user_id=data.get("user_id", ""),
        )

    async def _refresh(self, bundle: TokenBundle) -> TokenBundle:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(AGENTHICC_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": AGENTHICC_CLIENT_ID,
                "refresh_token": bundle.refresh_token,
            })
            resp.raise_for_status()
            data = resp.json()
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", bundle.refresh_token),
            expires_at=time.time() + data.get("expires_in", 3600),
            plan=data.get("plan", bundle.plan),
            email=bundle.email,
            user_id=bundle.user_id,
        )

    async def _revoke(self, access_token: str) -> None:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                await client.post(AGENTHICC_REVOKE_URL, data={
                    "token": access_token,
                    "client_id": AGENTHICC_CLIENT_ID,
                })
        except Exception:
            pass  # best-effort revoke

    @staticmethod
    def _find_free_port() -> int:
        import socket
        with socket.socket() as s:
            s.bind(("localhost", 0))
            return s.getsockname()[1]
