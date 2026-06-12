---
title: "PRD-11: OAuth Authentication with agenthicc.ai and TUI Text Advertisements"
status: draft
version: 0.1.0
created: 2025-01-01
---

# PRD-11: OAuth with agenthicc.ai and TUI Text Advertisements

## 1. Executive Summary

This PRD specifies two tightly coupled monetisation and identity features:

**OAuth Authentication** — `agenthicc` integrates with `agenthicc.ai` as the identity
provider. Users run `agenthicc login` (or are prompted on first run) to open a browser
to `https://agenthicc.ai/oauth/authorize`; the local CLI receives the auth code via a
loopback HTTP server on a randomised port, exchanges it for an access token and refresh
token, and stores both securely in the system keychain (via the `keyring` package with
a plain-file fallback). Every API call from the kernel's headless API and every agent
run is authenticated with a `Bearer` token; token refresh is automatic.

**TUI Text Advertisements** — authenticated free-tier users see **one text advertisement
at a time** rendered in the TUI's status area between agent turns. Ads are fetched from
`https://api.agenthicc.ai/v1/ads` after login, cached for 1 hour, and rotated every
60 seconds of active session time. Ads are plain UTF-8 text with a max length of 120
characters, rendered in a `rich.Panel` with a `[dim]` style and a "Sponsored" label.
**Paid (Pro) users see no advertisements.** The ad panel never blocks the transcript,
never intercepts input, and can be dismissed for 5 minutes with Esc.

---

## 2. Goals

| ID | Goal |
|----|------|
| G1 | `agenthicc login` opens browser to `agenthicc.ai` OAuth flow; no password stored |
| G2 | Access token and refresh token stored in OS keychain via `keyring` |
| G3 | Token refresh happens transparently before any authenticated request |
| G4 | `agenthicc logout` revokes the token and clears the keychain entry |
| G5 | Unauthenticated usage remains possible with a feature-limited free mode |
| G6 | Free-tier authenticated users see one rotating text ad in the TUI status area |
| G7 | Pro users see zero ads |
| G8 | Ads are fetched and cached; the TUI never blocks waiting for an ad |
| G9 | Ads are plain text ≤ 120 chars; no images, no HTML, no tracking pixels |
| G10 | User can dismiss the current ad for 5 minutes with Esc |

## 3. Non-Goals

| ID | Non-Goal |
|----|----------|
| NG1 | Graphical or image ads |
| NG2 | Click-through tracking (terminal has no clicks; URL in ad text is display-only) |
| NG3 | Personalised ad targeting — ads are served based on tier only |
| NG4 | Multiple simultaneous ads |
| NG5 | Ads in headless mode — JSON-lines output has no ad channel |

---

## 4. Architecture

### 4.1 OAuth flow

```
agenthicc login
      │
      ▼
1. Generate PKCE code_verifier + code_challenge (S256)
2. Start loopback HTTP server on random port 8000-9000
3. Open browser:
   https://agenthicc.ai/oauth/authorize
     ?client_id=agenthicc-cli
     &redirect_uri=http://localhost:{port}/callback
     &response_type=code
     &code_challenge={challenge}
     &code_challenge_method=S256
     &scope=openid profile agent:run
4. Wait for GET /callback?code=...
5. POST https://agenthicc.ai/oauth/token
     {code, code_verifier, redirect_uri, grant_type=authorization_code}
6. Receive {access_token, refresh_token, expires_in, token_type, plan}
7. Store in keychain: keyring.set_password("agenthicc", "tokens", json(tokens))
8. Print "Logged in as {email}. Plan: {plan}"
```

### 4.2 Token refresh

```
Before any authenticated HTTP request:
  load tokens from keychain
  if access_token expires_in < 60 seconds:
    POST https://agenthicc.ai/oauth/token
      {grant_type=refresh_token, refresh_token=..., client_id=...}
    store new tokens
  attach Authorization: Bearer {access_token} to request
```

### 4.3 Ad fetch and rotation

```
On TUI start (authenticated free-tier):
  GET https://api.agenthicc.ai/v1/ads
    Authorization: Bearer {access_token}
  → [{id, text, cta_url}, ...]  (cached 1 hour in .agenthicc/ads_cache.json)

AdRotator (background asyncio.Task):
  every 60 seconds: advance to next ad in cache
  if cache empty or expired: re-fetch
  emit UIAdUpdate event → TUIEventAdapter handles → renders Panel above status bar

User presses Esc:
  dismiss_until = time.monotonic() + 300   # 5 minutes
  Ad panel hidden until dismiss_until passes
```

### 4.4 TUI ad rendering (rich)

```
┌──────────────────────────────────────────────────────────────────────┐
│  ● agent:refactor  transcript continues...                           │
│    [tool] write_file auth.py  ✓  8ms                                 │
│                                                                       │
├──────────────────────────────────────────────────────────────────────┤
│ [Sponsored] Try Depot — 40% faster CI builds. depot.dev              │  ← Ad panel
│             (Esc to dismiss for 5 min · upgrade to Pro for no ads)   │    dim styled
├──────────────────────────────────────────────────────────────────────┤
│  session-abc | 1 agent | $0.004                                       │  ← status
├──────────────────────────────────────────────────────────────────────┤
│  > _                                                                  │  ← input bar
└──────────────────────────────────────────────────────────────────────┘
```

---

## 5. Data Structures and Interfaces

### 5.1 `auth.py` — new top-level module

```python
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
                self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
                self._fallback_path.write_text(data, mode=0o600)

    @property
    def _fallback_path(self) -> Path:
        return Path.home() / ".agenthicc" / "tokens.json"


class AuthClient:
    """Handles the OAuth PKCE flow and token refresh."""

    def __init__(self, store: TokenStore | None = None) -> None:
        self._store = store or TokenStore()

    async def login(self) -> TokenBundle:
        """Run the browser OAuth flow; return and store the token bundle."""
        import asyncio
        import webbrowser
        from aiohttp import web

        code_verifier  = secrets.token_urlsafe(64)
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
        print(f"Opening browser… if it doesn't open, visit:\n  {auth_url}")

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

    # ── internal ──────────────────────────────────────────────────────

    async def _exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenBundle:
        import httpx, time
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
        import httpx, time
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
```

### 5.2 `ads.py` — new top-level module

```python
# src/agenthicc/ads.py
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["AdRotator", "AdRecord", "AdCache"]

AGENTHICC_ADS_URL = "https://api.agenthicc.ai/v1/ads"
AD_CACHE_TTL      = 3600   # 1 hour
AD_ROTATION_SEC   = 60     # advance every 60 active seconds
AD_DISMISS_SEC    = 300    # Esc dismisses for 5 minutes
AD_MAX_LENGTH     = 120    # characters


@dataclass(frozen=True)
class AdRecord:
    ad_id: str
    text: str        # ≤ AD_MAX_LENGTH chars, plain UTF-8
    cta_url: str     # display-only URL, not a hyperlink

    def truncated(self) -> str:
        return self.text[:AD_MAX_LENGTH]


@dataclass
class AdCache:
    ads: list[AdRecord] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return time.time() - self.fetched_at > AD_CACHE_TTL

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fetched_at": self.fetched_at,
            "ads": [{"ad_id": a.ad_id, "text": a.text, "cta_url": a.cta_url}
                    for a in self.ads],
        }))

    @classmethod
    def load(cls, path: Path) -> AdCache:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            return cls(
                fetched_at=data.get("fetched_at", 0.0),
                ads=[AdRecord(**a) for a in data.get("ads", [])],
            )
        except Exception:
            return cls()


class AdRotator:
    """Manages ad fetching, caching, rotation, and dismiss state.

    Designed to run as a background asyncio.Task. Emits UIAdUpdate events
    to the kernel when the displayed ad changes.
    """

    def __init__(
        self,
        auth_client: Any,         # AuthClient
        processor: Any | None,    # EventProcessor — may be None (headless)
        cache_path: Path = Path(".agenthicc/ads_cache.json"),
    ) -> None:
        self._auth = auth_client
        self._processor = processor
        self._cache = AdCache.load(cache_path)
        self._cache_path = cache_path
        self._index: int = 0
        self._dismissed_until: float = 0.0
        self._running = False

    @property
    def current_ad(self) -> AdRecord | None:
        if not self._cache.ads:
            return None
        if time.monotonic() < self._dismissed_until:
            return None
        return self._cache.ads[self._index % len(self._cache.ads)]

    def dismiss(self) -> None:
        """Dismiss the current ad for AD_DISMISS_SEC seconds."""
        self._dismissed_until = time.monotonic() + AD_DISMISS_SEC

    async def run(self) -> None:
        """Background loop: refresh cache and rotate ads."""
        self._running = True
        while self._running:
            if self._cache.is_expired:
                await self._fetch_ads()
            if self._processor is not None and self.current_ad is not None:
                from agenthicc.kernel import Event
                await self._processor.emit(Event.create(
                    "UIAdUpdate",
                    {"ad_id": self.current_ad.ad_id,
                     "text": self.current_ad.truncated(),
                     "cta_url": self.current_ad.cta_url},
                ))
            await asyncio.sleep(AD_ROTATION_SEC)
            self._index += 1

    async def stop(self) -> None:
        self._running = False

    async def _fetch_ads(self) -> None:
        try:
            import httpx
            token = await self._auth.get_token()
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    AGENTHICC_ADS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5.0,
                )
                resp.raise_for_status()
                data = resp.json()
            self._cache = AdCache(
                ads=[AdRecord(ad_id=a["id"], text=a["text"], cta_url=a.get("cta_url", ""))
                     for a in data.get("ads", [])],
                fetched_at=time.time(),
            )
            self._cache.save(self._cache_path)
        except Exception:
            pass   # best-effort — never fail the TUI over ads
```

### 5.3 TUI integration — `TUIEventAdapter` additions

```python
# tui/events.py — add to _handlers dict:
"UIAdUpdate": self._on_ad_update,

def _on_ad_update(self, event: Any, payload: dict) -> None:
    self.model.set_current_ad(AdRecord(
        ad_id=payload.get("ad_id", ""),
        text=payload.get("text", ""),
        cta_url=payload.get("cta_url", ""),
    ))
```

### 5.4 `TranscriptModel` additions

```python
# transcript.py — additions

@dataclass
class AdRecord:
    ad_id: str
    text: str
    cta_url: str

class TranscriptModel:
    def __init__(self) -> None:
        ...
        self._current_ad: AdRecord | None = None

    def set_current_ad(self, ad: AdRecord | None) -> None:
        self._current_ad = ad

    def current_ad(self) -> AdRecord | None:
        return self._current_ad

    def render_ad_panel(self) -> str | None:
        """Return a one-line ad string for the renderer, or None."""
        if self._current_ad is None:
            return None
        text = self._current_ad.text[:120]
        url  = self._current_ad.cta_url
        return f"[Sponsored] {text}" + (f"  {url}" if url else "")
```

### 5.5 `InlineRenderer` ad display

```python
# tui/app.py — InlineRenderer additions

def _render_ad(self) -> Panel | None:
    """Build the ad panel, or None when no ad or user is Pro."""
    ad_line = self.model.render_ad_panel()
    if ad_line is None:
        return None
    from rich.panel import Panel
    from rich.text import Text
    body = Text(ad_line, style="dim")
    footer = Text("Esc to dismiss · upgrade to Pro for no ads",
                  style="dim italic")
    return Panel(body, subtitle=footer, border_style="dim",
                 padding=(0, 1), title="[dim]Sponsored[/dim]")
```

The ad panel is rendered inside `_update_spinner` — it sits between the transcript
and the status line. When both a spinner panel and an ad panel are needed, the ad
panel appears below the spinner panel.

### 5.6 `__main__.py` — login/logout subcommands

```python
# New argparse subcommands:
subparsers = parser.add_subparsers(dest="command")
subparsers.add_parser("login",  help="Authenticate with agenthicc.ai")
subparsers.add_parser("logout", help="Log out and revoke tokens")
subparsers.add_parser("whoami", help="Show current authenticated user")

def main() -> None:
    args = _parse_args()
    if args.command == "login":
        asyncio.run(_do_login())
    elif args.command == "logout":
        asyncio.run(_do_logout())
    elif args.command == "whoami":
        _do_whoami()
    else:
        ...  # existing TUI/headless launch

async def _do_login() -> None:
    from agenthicc.auth import AuthClient
    client = AuthClient()
    bundle = await client.login()
    print(f"Logged in as {bundle.email}  [plan: {bundle.plan}]")

async def _do_logout() -> None:
    from agenthicc.auth import AuthClient
    await AuthClient().logout()
    print("Logged out.")

def _do_whoami() -> None:
    from agenthicc.auth import AuthClient, NotLoggedInError
    bundle = AuthClient().current_bundle()
    if bundle is None:
        print("Not logged in. Run: agenthicc login")
    else:
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(bundle.expires_at))
        print(f"{bundle.email}  plan={bundle.plan}  token_expires={exp}")
```

---

## 6. Implementation Plan

### Phase 1 — Dependencies (30 min)
1. Add to `pyproject.toml`: `aiohttp>=3.9`, `httpx>=0.27` (already present), `keyring>=25.0`
2. Add `auth` and `ads` to optional extras: `[project.optional-dependencies] cloud = ["aiohttp>=3.9", "keyring>=25.0"]`
3. `uv sync --extra cloud`

### Phase 2 — `auth.py` (3 h)
1. Write `TokenBundle`, `TokenStore`, `AuthClient` as specified in §5.1
2. Test manually: `agenthicc login` (use a mock OAuth server in tests)
3. Test: `agenthicc logout`, `agenthicc whoami`

### Phase 3 — `ads.py` (2 h)
1. Write `AdRecord`, `AdCache`, `AdRotator` as specified in §5.2
2. `AdCache.load`/`save` round-trip test
3. `AdRotator.run()` with mocked `httpx` — verify rotation and cache TTL

### Phase 4 — TUI integration (2 h)
1. Add `UIAdUpdate` handler to `TUIEventAdapter`
2. Add `set_current_ad`/`render_ad_panel` to `TranscriptModel`
3. Add `_render_ad` and Esc-dismiss to `InlineRenderer`
4. Wire `AdRotator` into `_start_session()` in `__main__.py` — start only when `bundle.plan == "free"`

### Phase 5 — Tests (2 h)
See §7.

---

## 7. Tests

### 7.1 `tests/unit/test_auth.py`

```python
"""Unit tests for AuthClient and TokenStore (PRD-11)."""
from __future__ import annotations

import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from agenthicc.auth import AuthClient, NotLoggedInError, TokenBundle, TokenStore

pytestmark = pytest.mark.unit


def _bundle(plan="free", expires_offset=3600) -> TokenBundle:
    return TokenBundle(
        access_token="tok_access",
        refresh_token="tok_refresh",
        expires_at=time.time() + expires_offset,
        plan=plan,
        email="user@example.com",
        user_id="u1",
    )


class TestTokenBundle:
    def test_not_expired_when_fresh(self):
        assert not _bundle(expires_offset=3600).is_expired

    def test_expired_within_buffer(self):
        # expires in 30s < TOKEN_REFRESH_BUFFER (60s)
        assert _bundle(expires_offset=30).is_expired

    def test_is_pro_for_pro_plan(self):
        assert _bundle(plan="pro").is_pro

    def test_is_not_pro_for_free(self):
        assert not _bundle(plan="free").is_pro

    def test_round_trip_dict(self):
        b = _bundle()
        assert TokenBundle.from_dict(b.to_dict()).access_token == b.access_token


class TestTokenStore:
    def test_returns_none_when_empty(self, tmp_path):
        store = TokenStore()
        store._fallback_path = tmp_path / "tokens.json"
        with patch("keyring.get_password", return_value=None):
            assert store.load() is None

    def test_save_and_load_via_fallback(self, tmp_path):
        store = TokenStore()
        store._fallback_path = tmp_path / "tokens.json"
        bundle = _bundle()
        with patch("keyring.set_password"), patch("keyring.get_password", side_effect=Exception):
            store.save(bundle)
        with patch("keyring.get_password", side_effect=Exception):
            loaded = store.load()
        assert loaded is not None
        assert loaded.access_token == bundle.access_token

    def test_clear_removes_entry(self, tmp_path):
        store = TokenStore()
        store._fallback_path = tmp_path / "tokens.json"
        bundle = _bundle()
        with patch("keyring.set_password"), patch("keyring.get_password", side_effect=Exception):
            store.save(bundle)
        with patch("keyring.delete_password"), patch("keyring.get_password", side_effect=Exception):
            store.clear()
        with patch("keyring.get_password", side_effect=Exception):
            assert store.load() is None


class TestAuthClientGetToken:
    async def test_returns_token_when_valid(self):
        store = MagicMock()
        store.load.return_value = _bundle(expires_offset=3600)
        client = AuthClient(store)
        token = await client.get_token()
        assert token == "tok_access"

    async def test_raises_when_not_logged_in(self):
        store = MagicMock()
        store.load.return_value = None
        client = AuthClient(store)
        with pytest.raises(NotLoggedInError):
            await client.get_token()

    async def test_refreshes_expired_token(self):
        store = MagicMock()
        store.load.return_value = _bundle(expires_offset=30)  # expires soon
        client = AuthClient(store)
        new_bundle = _bundle(expires_offset=3600)
        with patch.object(client, "_refresh", new_callable=AsyncMock,
                          return_value=new_bundle):
            token = await client.get_token()
        assert token == new_bundle.access_token
        store.save.assert_called_once_with(new_bundle)
```

### 7.2 `tests/unit/test_ads.py`

```python
"""Unit tests for AdRotator and AdCache (PRD-11)."""
from __future__ import annotations

import json
import time
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agenthicc.ads import AdCache, AdRecord, AdRotator

pytestmark = pytest.mark.unit


def _ad(n: int = 1) -> AdRecord:
    return AdRecord(ad_id=f"ad{n}", text=f"Ad number {n}", cta_url="https://example.com")


class TestAdCache:
    def test_empty_cache_is_expired(self):
        assert AdCache().is_expired

    def test_fresh_cache_not_expired(self):
        cache = AdCache(ads=[_ad()], fetched_at=time.time())
        assert not cache.is_expired

    def test_save_load_round_trip(self, tmp_path):
        path = tmp_path / "ads.json"
        cache = AdCache(ads=[_ad(1), _ad(2)], fetched_at=time.time())
        cache.save(path)
        loaded = AdCache.load(path)
        assert len(loaded.ads) == 2
        assert loaded.ads[0].ad_id == "ad1"

    def test_load_missing_file_returns_empty(self, tmp_path):
        loaded = AdCache.load(tmp_path / "nonexistent.json")
        assert loaded.ads == []


class TestAdRotator:
    def test_no_ad_when_cache_empty(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        assert r.current_ad is None

    def test_returns_first_ad(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        r._cache = AdCache(ads=[_ad(1), _ad(2)], fetched_at=time.time())
        assert r.current_ad == _ad(1)

    def test_rotates_on_index_advance(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        r._cache = AdCache(ads=[_ad(1), _ad(2)], fetched_at=time.time())
        r._index = 1
        assert r.current_ad == _ad(2)

    def test_dismiss_hides_ad(self):
        r = AdRotator(auth_client=MagicMock(), processor=None)
        r._cache = AdCache(ads=[_ad(1)], fetched_at=time.time())
        r.dismiss()
        assert r.current_ad is None

    def test_truncates_long_ad_text(self):
        long_text = "x" * 200
        ad = AdRecord(ad_id="a1", text=long_text, cta_url="")
        assert len(ad.truncated()) == 120
```

### 7.3 `tests/unit/test_tui_ads.py`

```python
"""Unit tests for ad rendering in TranscriptModel and InlineRenderer (PRD-11)."""
from __future__ import annotations

import io
import pytest
from rich.console import Console

from agenthicc.ads import AdRecord
from agenthicc.tui.transcript import TranscriptModel

pytestmark = pytest.mark.unit


def _ad(text="Buy our product", url="https://example.com") -> AdRecord:
    return AdRecord(ad_id="a1", text=text, cta_url=url)


class TestTranscriptModelAds:
    def test_no_ad_panel_when_no_ad_set(self):
        m = TranscriptModel()
        assert m.render_ad_panel() is None

    def test_ad_panel_contains_text(self):
        m = TranscriptModel()
        m.set_current_ad(_ad("Try Depot — faster CI"))
        panel = m.render_ad_panel()
        assert panel is not None
        assert "Depot" in panel

    def test_ad_panel_includes_cta_url(self):
        m = TranscriptModel()
        m.set_current_ad(_ad(url="https://depot.dev"))
        panel = m.render_ad_panel()
        assert "depot.dev" in panel

    def test_clear_ad(self):
        m = TranscriptModel()
        m.set_current_ad(_ad())
        m.set_current_ad(None)
        assert m.render_ad_panel() is None

    def test_long_ad_truncated_in_panel(self):
        m = TranscriptModel()
        m.set_current_ad(_ad(text="x" * 200))
        panel = m.render_ad_panel()
        assert len(panel) < 300   # truncated to 120 chars in the ad line
```

---

## 8. Dependency additions

```toml
# pyproject.toml additions

[project.optional-dependencies]
cloud = [
    "aiohttp>=3.9",
    "keyring>=25.0",
]

# Update tui extra:
tui = [
    "rich>=13.0",
    "prompt_toolkit>=3.0",
]
```

`httpx` is already a core dependency. `aiohttp` is used only for the loopback OAuth
callback server (it's lightweight and well-suited for a one-shot HTTP server).

---

## 9. Security Notes

- **PKCE** (RFC 7636, S256) is mandatory — the CLI never sends a client secret.
- Tokens are stored in the OS keychain (macOS Keychain, Linux Secret Service,
  Windows Credential Manager) via `keyring`. The plaintext fallback at
  `~/.agenthicc/tokens.json` is created with `mode=0o600` (user-read-only).
- The loopback redirect URI (`http://localhost:{port}/callback`) is only bound
  during the login flow and immediately torn down after receiving the code.
- `agenthicc.ai` must validate that `redirect_uri` matches the registered
  `http://localhost:*/callback` pattern (wildcard port).
- Ad content is fetched over HTTPS, cached locally, and never executed — it is
  rendered as plain text with `markup=False` to prevent Rich markup injection.

---

## 10. Open Questions

1. **What happens offline?** If the machine has no internet, `get_token()` raises
   from the refresh call. Should we cache the last known token even if expired and
   operate in degraded mode (no server sync, local kernel only)?

2. **Free-tier ad frequency** — 60 seconds feels frequent; 5 minutes might be more
   user-friendly. Exact cadence TBD by product.

3. **Ad analytics** — should the CLI `POST /v1/ads/{ad_id}/impression` when an ad is
   displayed? This requires a network call per impression; it's privacy-adjacent.
   Decision deferred.

4. **Headless mode and auth** — `agenthicc --headless` currently doesn't authenticate.
   Should the headless REST API require a Bearer token? If so, users would need to
   `agenthicc login` before running headless jobs, or provide a token via an env var
   `AGENTHICC_TOKEN`.

5. **Enterprise SSO** — OAuth scope includes `openid profile agent:run` but not
   SAML/OIDC federation. Enterprise SSO is a future item.
