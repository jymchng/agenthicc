from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def prompt_install(
    path: Path,
    missing: list[str],
    *,
    auto_install: bool = False,
    install_target: str = "venv",
    interactive: bool = True,
) -> bool:
    """Handle missing dependencies for *path*.

    Returns True if the caller should retry loading (deps installed),
    False if the plugin should be skipped.
    """
    if not missing:
        return True

    if not interactive:
        log.warning(
            "Plugin %s skipped — missing: %s  (headless mode, skipping install)",
            path, missing,
        )
        return False

    if auto_install:
        log.info("[plugins] auto_install — installing %s for %s", missing, path.name)
        _run_install(missing, target=install_target)
        return True

    # Interactive prompt
    deps_str = " ".join(missing)
    print(
        f"\n⚠  Plugin {path} requires missing packages:\n"
        f"     {', '.join(missing)}\n"
    )
    while True:
        choice = input("   [I]nstall now  [S]kip  [Q]uit  > ").strip().upper()
        if choice == "I":
            _run_install(missing, target=install_target)
            return True   # caller retries _load_plugin_file
        if choice == "S":
            return False
        if choice == "Q":
            raise SystemExit(0)


def _run_install(requirements: list[str], target: str = "venv") -> None:
    flags = ["--user"] if target == "user" else []
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *flags, *requirements]
    )
