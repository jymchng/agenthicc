"""Authentication commands — login, logout, whoami."""
from __future__ import annotations

import time

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command


@command("login", help="Authenticate with agenthicc.ai")
async def login(ctx: CLIContext) -> None:
    """Authenticate with agenthicc.ai and store credentials."""
    from agenthicc.auth import AuthClient  # noqa: PLC0415
    client = AuthClient()
    bundle = await client.login()
    print(f"Logged in as {bundle.email}  [plan: {bundle.plan}]")


@command("logout", help="Log out and revoke stored tokens")
async def logout(ctx: CLIContext) -> None:
    """Log out and revoke stored tokens."""
    from agenthicc.auth import AuthClient  # noqa: PLC0415
    await AuthClient().logout()
    print("Logged out.")


@command("whoami", help="Show the currently authenticated user")
def whoami(ctx: CLIContext) -> None:
    """Print the currently authenticated user and token expiry."""
    from agenthicc.auth import AuthClient  # noqa: PLC0415
    bundle = AuthClient().current_bundle()
    if bundle is None:
        print("Not logged in. Run: agenthicc login")
    else:
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(bundle.expires_at))
        print(f"{bundle.email}  plan={bundle.plan}  token_expires={exp}")
