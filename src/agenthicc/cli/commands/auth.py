"""Authentication commands — login, logout, whoami."""

from __future__ import annotations

import time

from agenthicc.cli.context import CLIContext


async def login(ctx: CLIContext) -> None:
    """Authenticate with agenthicc.ai and store credentials."""
    from agenthicc.auth import AuthClient

    client = AuthClient()
    bundle = await client.login()
    print(f"Logged in as {bundle.email}  [plan: {bundle.plan}]")


async def logout(ctx: CLIContext) -> None:
    """Log out and revoke stored tokens."""
    from agenthicc.auth import AuthClient

    await AuthClient().logout()
    print("Logged out.")


def whoami(ctx: CLIContext) -> None:
    """Print the currently authenticated user and token expiry."""
    from agenthicc.auth import AuthClient

    bundle = AuthClient().current_bundle()
    if bundle is None:
        print("Not logged in. Run: agenthicc login")
    else:
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(bundle.expires_at))
        print(f"{bundle.email}  plan={bundle.plan}  token_expires={exp}")
