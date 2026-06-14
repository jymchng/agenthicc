"""``agenthicc login/logout/whoami`` subcommand handlers."""
from __future__ import annotations

import time


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
    from agenthicc.auth import AuthClient
    bundle = AuthClient().current_bundle()
    if bundle is None:
        print("Not logged in. Run: agenthicc login")
    else:
        exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(bundle.expires_at))
        print(f"{bundle.email}  plan={bundle.plan}  token_expires={exp}")
