"""Safe project-guidance bootstrap for ``agenthicc init`` and ``/init``.

The bootstrapper is intentionally deterministic and local-only.  It inspects
small, well-known project manifests and directory names, then proposes a
managed section in ``AGENTS.md``.  User-authored content is preserved outside
the managed markers, and writes are atomic and constrained to the project root.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agenthicc.tools.sandbox import WorkspaceView

__all__ = [
    "AGENTS_FILENAME",
    "MANAGED_END",
    "MANAGED_START",
    "BootstrapError",
    "BootstrapWriteError",
    "ProjectBootstrapPlan",
    "ProjectSnapshot",
    "build_bootstrap_plan",
    "inspect_project",
    "write_bootstrap_plan",
]

AGENTS_FILENAME: Final[str] = "AGENTS.md"
MANAGED_START: Final[str] = "<!-- agenthicc:init:start -->"
MANAGED_END: Final[str] = "<!-- agenthicc:init:end -->"
_MAX_MANIFEST_BYTES: Final[int] = 512 * 1024
_IGNORED_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    {
        ".agenthicc",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
_KNOWN_INSTRUCTIONS: Final[tuple[str, ...]] = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "README.md",
)


class BootstrapError(RuntimeError):
    """Raised when a project cannot be safely inspected or planned."""


class BootstrapWriteError(BootstrapError):
    """Raised when a bootstrap plan cannot be written safely."""


@dataclass(frozen=True)
class ProjectSnapshot:
    """Small, non-sensitive summary of a project used for guidance generation."""

    root: Path
    project_name: str
    stacks: tuple[str, ...]
    top_level_entries: tuple[str, ...]
    manifests: tuple[str, ...]
    test_paths: tuple[str, ...]
    instruction_files: tuple[str, ...]
    architecture_hints: tuple[str, ...]
    verification_commands: tuple[str, ...]


@dataclass(frozen=True)
class ProjectBootstrapPlan:
    """A reviewable proposed update to the root ``AGENTS.md`` file."""

    root: Path
    target: Path
    snapshot: ProjectSnapshot
    current_content: str | None
    proposed_content: str

    @property
    def exists(self) -> bool:
        """Whether the target existed when this plan was created."""

        return self.current_content is not None

    @property
    def changed(self) -> bool:
        """Whether applying the proposal would change the target file."""

        return self.current_content != self.proposed_content

    def diff(self) -> str:
        """Return a unified diff suitable for a CLI or terminal preview."""

        old = [] if self.current_content is None else self.current_content.splitlines(keepends=True)
        new = self.proposed_content.splitlines(keepends=True)
        from_name = "/dev/null" if self.current_content is None else AGENTS_FILENAME
        return "".join(
            difflib.unified_diff(
                old,
                new,
                fromfile=from_name,
                tofile=AGENTS_FILENAME,
            )
        )

    def preview(self) -> str:
        """Return human-readable status and diff text for review."""

        if not self.changed:
            return f"{AGENTS_FILENAME} is already up to date."
        return self.diff()


def _read_small(view: WorkspaceView, relative: str) -> str | None:
    """Read a known manifest only when it is a regular, bounded file."""

    try:
        path = view.resolve(relative)
        if not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, PermissionError, UnicodeError):
        return None


def _parse_project_name(manifests: dict[str, str], fallback: str) -> str:
    pyproject = manifests.get("pyproject.toml")
    if pyproject is not None:
        try:
            project = tomllib.loads(pyproject).get("project", {})
            name = project.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (tomllib.TOMLDecodeError, AttributeError):
            pass

    package_json = manifests.get("package.json")
    if package_json is not None:
        try:
            name = json.loads(package_json).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (json.JSONDecodeError, AttributeError):
            pass

    cargo = manifests.get("Cargo.toml")
    if cargo is not None:
        match = re.search(r"(?m)^\s*name\s*=\s*[\"']([^\"']+)[\"']", cargo)
        if match:
            return match.group(1).strip()

    go_mod = manifests.get("go.mod")
    if go_mod is not None:
        match = re.search(r"(?m)^\s*module\s+([^\s]+)", go_mod)
        if match:
            return match.group(1).rsplit("/", 1)[-1]

    return fallback


def _verification_commands(
    *,
    entries: set[str],
    manifests: dict[str, str],
    stacks: tuple[str, ...],
) -> tuple[str, ...]:
    commands: list[str] = []

    def add(command: str) -> None:
        if command not in commands:
            commands.append(command)

    if "Python" in stacks:
        runner = "uv run" if "uv.lock" in entries else "python -m"
        if "tests" in entries or "test" in entries:
            add(f"{runner} pytest tests/ -q")
        else:
            add(f"{runner} pytest -q")
        pyproject = manifests.get("pyproject.toml", "")
        if "ruff" in pyproject:
            add("uv run ruff check src/ tests/" if "uv.lock" in entries else "ruff check .")
        if "mypy" in pyproject:
            add("uv run mypy src" if "uv.lock" in entries else "mypy .")

    if "Node.js" in stacks:
        try:
            scripts = json.loads(manifests["package.json"]).get("scripts", {})
        except (json.JSONDecodeError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict):
            for name in ("test", "lint", "typecheck", "build"):
                if name in scripts:
                    add(f"npm run {name}")

    if "Rust" in stacks:
        add("cargo test")
        add("cargo fmt --check")

    if "Go" in stacks:
        add("go test ./...")
        add("go vet ./...")

    makefile = manifests.get("Makefile", "")
    if makefile:
        targets = set(re.findall(r"(?m)^([A-Za-z0-9_.-]+):", makefile))
        for name in ("test", "check", "lint", "format"):
            if name in targets:
                add(f"make {name}")

    if not commands:
        add("Inspect the project-specific build and test commands before changes.")
    return tuple(commands)


def inspect_project(root: str | Path = ".") -> ProjectSnapshot:
    """Inspect bounded project metadata through :class:`WorkspaceView`.

    No network calls, shell commands, prompt contents, or arbitrary project
    files are read.  Only known small manifests and directory names are used.
    """

    candidate = Path(root).expanduser()
    if not candidate.exists() or not candidate.is_dir():
        raise BootstrapError(f"Project root is not a directory: {candidate}")

    view = WorkspaceView(candidate)
    project_root = view.root
    try:
        raw_entries = view.list_dir(".")
    except (OSError, PermissionError) as exc:
        raise BootstrapError(f"Cannot inspect project root {project_root}: {exc}") from exc

    entries = {name for name in raw_entries if name not in _IGNORED_TOP_LEVEL}
    visible_entries = tuple(
        sorted(name for name in entries if not name.startswith(".") and name != AGENTS_FILENAME)
    )
    manifest_names = (
        "pyproject.toml",
        "setup.py",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Makefile",
    )
    manifests = {
        name: content for name in manifest_names if (content := _read_small(view, name)) is not None
    }

    stacks: list[str] = []
    if "pyproject.toml" in manifests or "setup.py" in manifests:
        stacks.append("Python")
    if "package.json" in manifests:
        stacks.append("Node.js")
    if "Cargo.toml" in manifests:
        stacks.append("Rust")
    if "go.mod" in manifests:
        stacks.append("Go")
    if not stacks:
        stacks.append("Unknown")

    tests = tuple(name for name in ("tests", "test", "spec", "__tests__") if name in entries)
    # AGENTS.md is the target of this operation, so represent it consistently
    # before and after creation instead of making the generated content drift.
    instructions = (AGENTS_FILENAME,) + tuple(
        name for name in _KNOWN_INSTRUCTIONS if name != AGENTS_FILENAME and name in entries
    )
    architecture = tuple(
        f"{name}/ contains source or project components"
        for name in ("src", "app", "lib", "packages", "cmd", "internal", "docs")
        if name in entries
    )
    if not architecture:
        architecture = ("Start with the top-level files and project manifests.",)

    manifest_payloads = dict(manifests)
    project_name = _parse_project_name(manifest_payloads, project_root.name)
    commands = _verification_commands(
        entries=entries,
        manifests=manifest_payloads,
        stacks=tuple(stacks),
    )
    return ProjectSnapshot(
        root=project_root,
        project_name=project_name,
        stacks=tuple(stacks),
        top_level_entries=visible_entries,
        manifests=tuple(sorted(manifests)),
        test_paths=tests,
        instruction_files=instructions,
        architecture_hints=architecture,
        verification_commands=commands,
    )


def _managed_section(snapshot: ProjectSnapshot) -> str:
    stacks = ", ".join(snapshot.stacks)
    entries = ", ".join(f"`{name}`" for name in snapshot.top_level_entries) or "(none detected)"
    manifests = ", ".join(f"`{name}`" for name in snapshot.manifests) or "(none detected)"
    tests = ", ".join(f"`{name}/`" for name in snapshot.test_paths) or "(none detected)"
    instructions = (
        ", ".join(f"`{name}`" for name in snapshot.instruction_files) or "(none detected)"
    )

    lines = [
        MANAGED_START,
        "## Project snapshot",
        "",
        f"- Project: `{snapshot.project_name}`",
        f"- Primary stack: {stacks}",
        f"- Top-level entries: {entries}",
        f"- Detected manifests: {manifests}",
        f"- Test directories: {tests}",
        f"- Existing guidance files: {instructions}",
        "",
        "## Repository layout",
        "",
    ]
    lines.extend(f"- {hint}" for hint in snapshot.architecture_hints)
    lines.extend(
        [
            "",
            "## Verification commands",
            "",
        ]
    )
    lines.extend(f"- `{command}`" for command in snapshot.verification_commands)
    lines.extend(
        [
            "",
            "## Agenthicc working rules",
            "",
            "- Read the relevant current module and its tests before changing code.",
            "- Preserve unrelated user changes and inspect `git status --short` first.",
            "- Keep filesystem access inside the project boundary and do not expose secrets.",
            "- Run focused checks for the touched surface before reporting completion.",
            "",
            MANAGED_END,
        ]
    )
    return "\n".join(lines) + "\n"


def _merge_managed_section(existing: str | None, managed: str) -> str:
    if existing is None or not existing.strip():
        return "# AGENTS.md — Project guidance\n\n" + managed

    start = existing.find(MANAGED_START)
    end = existing.find(MANAGED_END)
    if (start == -1) != (end == -1) or (start != -1 and end < start):
        raise BootstrapError(
            f"{AGENTS_FILENAME} contains an incomplete agenthicc:init marker pair; "
            "repair it manually before running init again."
        )
    if start != -1 and end != -1:
        end += len(MANAGED_END)
        if existing[end : end + 1] == "\n":
            end += 1
        return existing[:start] + managed + existing[end:]

    return existing.rstrip() + "\n\n" + managed


def build_bootstrap_plan(root: str | Path = ".") -> ProjectBootstrapPlan:
    """Build a reviewable plan without writing to disk."""

    snapshot = inspect_project(root)
    target = snapshot.root / AGENTS_FILENAME
    if target.is_symlink():
        raise BootstrapError(f"Refusing to use symlink target: {target}")
    if target.exists() and not target.is_file():
        raise BootstrapError(f"Bootstrap target is not a regular file: {target}")
    try:
        current = target.read_text(encoding="utf-8") if target.exists() else None
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"Cannot read {target}: {exc}") from exc
    managed = _managed_section(snapshot)
    proposed = _merge_managed_section(current, managed)
    return ProjectBootstrapPlan(
        root=snapshot.root,
        target=target,
        snapshot=snapshot,
        current_content=current,
        proposed_content=proposed,
    )


def write_bootstrap_plan(plan: ProjectBootstrapPlan, *, force: bool = False) -> Path:
    """Atomically write *plan* after explicit confirmation for existing files."""

    target = plan.target
    if target.is_symlink():
        raise BootstrapWriteError(f"Refusing to overwrite symlink target: {target}")
    try:
        current_now = target.read_text(encoding="utf-8") if target.exists() else None
    except (OSError, UnicodeError) as exc:
        raise BootstrapWriteError(f"Cannot read {target}: {exc}") from exc
    if current_now != plan.current_content:
        raise BootstrapWriteError(
            f"{target} changed after the preview; run init again to create a fresh plan."
        )
    if not plan.changed:
        return target
    if plan.exists and not force:
        raise BootstrapWriteError(
            f"Refusing to overwrite existing {target.name}; review the diff and use force."
        )

    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as temporary:
            temporary.write(plan.proposed_content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, target)
    except OSError as exc:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise BootstrapWriteError(f"Could not write {target}: {exc}") from exc
    return target
