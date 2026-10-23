"""agenthicc trust — review and trust a repository's Python extensions (PRD-250 S-02).

Project extensions under ``.agenthicc/`` (tools, commands, workflows, modes,
agents and CLI plugins) are imported only after the user trusts their exact
content. Trust records live in ``~/.agenthicc/trust.json`` and bind each
file's path to its SHA-256; editing a file revokes its trust.
"""

from __future__ import annotations

from pathlib import Path

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group, optional_positionals
from agenthicc.plugins.trust import current_extension_trust

#: Extension directories under ``.agenthicc/`` and the kind each one holds.
_EXTENSION_DIRS: dict[str, str] = {
    "tools": "tool",
    "commands": "command",
    "workflows": "workflow",
    "modes": "mode",
    "agents": "agent",
    "cli": "cli",
}


def project_extension_files(project: Path) -> list[tuple[Path, str]]:
    """Every file under *project*'s ``.agenthicc/`` that a loader may execute.

    All Python files count, including private helpers, because a workflow
    package or plugin can import them. ``cli.toml`` counts because its
    commands run shell templates.
    """
    root = project / ".agenthicc"
    found: list[tuple[Path, str]] = []
    for directory, kind in _EXTENSION_DIRS.items():
        base = root / directory
        if base.is_dir():
            found.extend((path, kind) for path in sorted(base.rglob("*.py")))
    cli_toml = root / "cli.toml"
    if cli_toml.is_file():
        found.append((cli_toml, "cli"))
    return found


def _kind_for(path: Path) -> str:
    """The extension kind of *path*, from the ``.agenthicc/<dir>`` it lives in."""
    parts = path.resolve().parts
    for index, part in enumerate(parts[:-1]):
        if part == ".agenthicc" and parts[index + 1] in _EXTENSION_DIRS:
            return _EXTENSION_DIRS[parts[index + 1]]
    return "extension"


def _targets(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(path.rglob("*.py"))
    return [path]


@group("trust", help="Review and trust this repository's Python extensions")
def _() -> None: ...


@command("trust", "list", help="Show project extensions and whether each is trusted")
def trust_list(ctx: CLIContext) -> None:
    trust = current_extension_trust()
    files = project_extension_files(Path.cwd())
    if not files:
        print("No project extensions under .agenthicc/.")
    for path, kind in files:
        refusal = trust.check(path, kind=kind)
        status = "trusted" if refusal is None else refusal.reason
        print(f"  {status:<30} {kind:<9} {path}")
    print(f"\nTrust store: {trust.store_path} ({len(trust.records())} record(s))")


@optional_positionals("path")
@command("trust", "add", help="Trust the current content of extension files")
def trust_add(ctx: CLIContext, path: str = "") -> None:
    """Trust PATH (a file, or every Python file under a directory).

    Without PATH, trusts every project extension under ``.agenthicc/``.
    Review the files first: they run with your permissions.
    """
    trust = current_extension_trust()
    if path:
        targets = [(target, _kind_for(target)) for target in _targets(Path(path))]
    else:
        targets = project_extension_files(Path.cwd())
    if not targets:
        print("Nothing to trust.")
        return
    for target, kind in targets:
        if not target.is_file():
            print(f"  skipped (not a file): {target}")
            continue
        record = trust.trust(target, kind=kind)
        print(f"  trusted {kind:<9} {record.path}  (sha256 {record.sha256[:16]}…)")
    print(f"\nRecorded in {trust.store_path}. Editing a file revokes its trust.")


@command("trust", "revoke", help="Revoke trust for a file or every file under a directory")
def trust_revoke(ctx: CLIContext, path: str) -> None:
    trust = current_extension_trust()
    revoked = [target for target in _targets(Path(path)) if trust.revoke(target)]
    for target in revoked:
        print(f"  revoked {target}")
    if not revoked:
        print(f"No trust records for {path}.")
