"""Trust management — agenthicc trust cli."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group


@group("trust", help="Manage trust for project-local plugins")
def _() -> None: ...


@command("trust", "cli", help="Trust .agenthicc/cli/ Python plugins for this project")
def trust_cli(ctx: CLIContext) -> None:
    """Hash and trust all .agenthicc/cli/*.py files.

    After running this command, agenthicc will load the project-local CLI
    plugins on startup.  Re-run if any plugin file changes.
    """
    cli_dir    = Path(".agenthicc") / "cli"
    trust_file = Path(".agenthicc") / "trusted_cli.json"

    if not cli_dir.is_dir():
        print(f"No CLI plugins found at {cli_dir}/")
        print("Create .agenthicc/cli/deploy.py with @command decorators first.")
        return

    files: dict[str, str] = {}
    for py in sorted(cli_dir.glob("*.py")):
        if py.name.startswith("_"):
            continue
        rel    = str(py.relative_to(cli_dir.parent))
        digest = f"sha256:{hashlib.sha256(py.read_bytes()).hexdigest()}"
        files[rel] = digest
        print(f"  trusted: {rel}  ({digest[:18]}…)")

    if not files:
        print("No non-private .py files found in .agenthicc/cli/")
        return

    manifest = {
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "files":     files,
    }
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_text(json.dumps(manifest, indent=2))
    print(f"\n✓ Wrote {trust_file}  ({len(files)} file(s) trusted)")
    print("  Project-local CLI plugins will now load on next startup.")
