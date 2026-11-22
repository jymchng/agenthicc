"""Session-owned discovery and publication of extension surfaces."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

from agenthicc.kernel.events import Event
from agenthicc.tools.base import tool_name

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agenthicc.commands.command import Command
    from agenthicc.commands.plugin_loader import CommandLoadResult
    from agenthicc.commands.registry import UnifiedCommandRegistry
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.plugins.discovery import LoadResult
    from agenthicc.skills.loader import SkillDef
    from agenthicc.subagents.types import SubagentTypeRegistry
    from agenthicc.tools.base import ToolLike
    from agenthicc.workflows.loader import WorkflowLoadCode
    from agenthicc.workflows.registry import WorkflowRegistry

__all__ = [
    "RELOAD_SURFACES",
    "ReloadChange",
    "ReloadDiagnostic",
    "ReloadReason",
    "ReloadReport",
    "ReloadService",
    "ReloadSurface",
    "SurfaceHolder",
    "SurfaceSnapshot",
]

ReloadSurface = Literal["tools", "skills", "workflows", "commands"]
ReloadReason = Literal["command", "workflow_return", "startup", "api"]
RELOAD_SURFACES: tuple[ReloadSurface, ...] = ("skills", "tools", "workflows", "commands")
_ChangeKind = Literal["added", "updated", "removed", "unchanged"]
_DiagnosticCode = Literal[
    "untrusted",
    "untrusted_modified",
    "import_error",
    "missing_dependency",
    "name_conflict",
    "pending_marker",
    "invalid_manifest",
]

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class SurfaceSnapshot(Generic[_T]):
    """An extension value and the generation at which it was published."""

    generation: int
    value: _T


class SurfaceHolder(Generic[_T]):
    """Publish one immutable snapshot at a time while readers stay lock-free."""

    def __init__(self, initial: _T) -> None:
        self._snapshot = SurfaceSnapshot(0, initial)

    def current(self) -> SurfaceSnapshot[_T]:
        return self._snapshot

    def _publish(self, generation: int, value: _T) -> None:
        self._snapshot = SurfaceSnapshot(generation, value)


@dataclass(frozen=True, slots=True)
class ReloadChange:
    """One named item in a requested surface."""

    surface: ReloadSurface
    name: str
    kind: _ChangeKind


@dataclass(frozen=True, slots=True)
class ReloadDiagnostic:
    """A bounded reason a source was skipped or a surface retained."""

    surface: ReloadSurface
    path: str
    code: _DiagnosticCode
    message: str
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class ReloadReport:
    """Changes, diagnostics, and the surfaces published by one reload."""

    generation: int
    reason: ReloadReason
    requested: frozenset[ReloadSurface]
    published: frozenset[ReloadSurface]
    changes: tuple[ReloadChange, ...] = ()
    diagnostics: tuple[ReloadDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return self.published == self.requested

    def summary(self) -> str:
        parts: list[str] = []
        for surface in RELOAD_SURFACES:
            if surface not in self.requested:
                continue
            counts: Counter[str] = Counter(
                change.kind for change in self.changes if change.surface == surface
            )
            edits = [
                f"{prefix}{counts[kind]}"
                for kind, prefix in (("added", "+"), ("updated", "~"), ("removed", "−"))
                if counts[kind]
            ]
            if edits:
                parts.append(f"{surface} {' '.join(edits)}")
            elif surface in self.published:
                parts.append(f"{surface} unchanged")
            else:
                parts.append(f"{surface} kept")
        if self.diagnostics:
            parts.append(f"{len(self.diagnostics)} diagnostic(s)")
        return " · ".join(parts) or "No surfaces requested"

    def to_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "reason": self.reason,
            "requested": sorted(self.requested),
            "published": sorted(self.published),
            "changes_summary": [
                {"surface": change.surface, "name": change.name, "kind": change.kind}
                for change in self.changes
                if change.kind != "unchanged"
            ],
            "diagnostics": [
                {
                    "surface": item.surface,
                    "path": item.path,
                    "code": item.code,
                    "message": item.message,
                    "digest": item.digest,
                }
                for item in self.diagnostics
            ],
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    versions: dict[str, str | None]
    diagnostics: tuple[ReloadDiagnostic, ...]
    apply: Callable[[int], None] | None


@dataclass(frozen=True, slots=True)
class _Flight:
    surfaces: frozenset[ReloadSurface]
    task: asyncio.Task[ReloadReport]


def _digest(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


def _changes(
    surface: ReloadSurface,
    before: Mapping[str, str | None],
    after: Mapping[str, str | None],
) -> tuple[ReloadChange, ...]:
    changes: list[ReloadChange] = []
    for name in sorted(before.keys() | after.keys()):
        kind: _ChangeKind
        if name not in before:
            kind = "added"
        elif name not in after:
            kind = "removed"
        else:
            kind = "unchanged" if before[name] == after[name] else "updated"
        changes.append(ReloadChange(surface, name, kind))
    return tuple(changes)


def _source_diagnostic(
    surface: ReloadSurface, result: LoadResult | CommandLoadResult, *, kind: str
) -> ReloadDiagnostic:
    from agenthicc.plugins.trust import current_extension_trust

    if result.untrusted:
        refusal = current_extension_trust().check(result.path, kind=kind)
        modified = refusal is not None and refusal.reason == "changed since it was trusted"
        return ReloadDiagnostic(
            surface,
            str(result.path),
            "untrusted_modified" if modified else "untrusted",
            "Extension content changed since trust." if modified else "Extension requires trust.",
            _digest(result.path),
        )
    if result.missing_deps:
        return ReloadDiagnostic(
            surface, str(result.path), "missing_dependency", "A required dependency is missing."
        )
    return ReloadDiagnostic(
        surface, str(result.path), "import_error", "The extension could not be imported."
    )


def _skill_commands(
    registry: UnifiedCommandRegistry, skills: Mapping[str, SkillDef]
) -> tuple[ReloadDiagnostic, ...]:
    from agenthicc.commands.builtins import _make_skill_handler
    from agenthicc.commands.command import Command

    diagnostics: list[ReloadDiagnostic] = []
    for slug, skill in skills.items():
        command = Command(
            name=f"${slug}",
            description=skill.description or skill.name,
            argument_hint="[args…]",
            group="Skills",
            handler=_make_skill_handler(slug, skill),
            aliases=tuple(f"${alias}" for alias in skill.aliases),
            source_id=f"skill:{slug}",
        )
        if any(registry.get(name) is not None for name in (command.name, *command.aliases)):
            diagnostics.append(
                ReloadDiagnostic(
                    "skills",
                    str(skill.path),
                    "name_conflict",
                    f"{command.name} conflicts with an existing command or alias.",
                )
            )
        else:
            registry.register(command)
    return tuple(diagnostics)


class ReloadService:
    """Run extension discovery off-loop and serialize session publication."""

    def __init__(
        self,
        *,
        processor: EventProcessor,
        project_dir: Path,
        user_dir: Path,
        workflow_registry: WorkflowRegistry,
        command_registry: UnifiedCommandRegistry,
        subagent_registry: SubagentTypeRegistry,
        skill_user_dir: Path | None = None,
        skills: Mapping[str, SkillDef] | None = None,
        tools: Sequence[ToolLike] = (),
        command_plugin_names: Sequence[str] = (),
    ) -> None:
        self._processor = processor
        self._project_dir = project_dir
        self._user_dir = user_dir
        self._skill_user_dir = skill_user_dir or user_dir
        self._workflow_registry = workflow_registry
        self._command_registry = command_registry
        self._subagent_registry = subagent_registry
        self._command_plugin_names = frozenset(command_plugin_names)
        self._skills: SurfaceHolder[Mapping[str, SkillDef]] = SurfaceHolder(
            MappingProxyType(dict(skills or {}))
        )
        self._tools: SurfaceHolder[tuple[ToolLike, ...]] = SurfaceHolder(tuple(tools))
        self._versions: dict[ReloadSurface, dict[str, str | None]] = {
            "skills": dict.fromkeys(self._skills.current().value),
            "tools": dict.fromkeys(map(tool_name, tools)),
            "workflows": dict.fromkeys(workflow_registry.names()),
            "commands": dict.fromkeys(command_plugin_names),
        }
        self._generation = processor.get_state().reload_generation
        self._discovery_lock = asyncio.Lock()
        self._flight: _Flight | None = None
        self._closed = False

    @property
    def skills(self) -> SurfaceHolder[Mapping[str, SkillDef]]:
        return self._skills

    @property
    def tools(self) -> SurfaceHolder[tuple[ToolLike, ...]]:
        return self._tools

    @property
    def generation(self) -> int:
        return self._generation

    async def reload(
        self, surfaces: frozenset[ReloadSurface], *, reason: ReloadReason
    ) -> ReloadReport:
        while not self._closed:
            flight = self._flight
            if flight is None:
                task = asyncio.create_task(
                    self._run_reload(surfaces, reason), name="session-reload"
                )
                self._flight = _Flight(surfaces, task)
                return await asyncio.shield(task)
            if surfaces <= flight.surfaces:
                return await asyncio.shield(flight.task)
            await asyncio.shield(flight.task)
        raise RuntimeError("session reload service is closed")

    async def preview(self, surfaces: frozenset[ReloadSurface]) -> ReloadReport:
        async with self._discovery_lock:
            if self._closed:
                raise RuntimeError("session reload service is closed")
            return await self._collect(surfaces, "api", preview=True)

    async def close(self) -> None:
        self._closed = True
        flight = self._flight
        if flight is not None:
            await asyncio.gather(flight.task, return_exceptions=True)
        async with self._discovery_lock:
            pass

    async def _run_reload(
        self, surfaces: frozenset[ReloadSurface], reason: ReloadReason
    ) -> ReloadReport:
        try:
            async with self._discovery_lock:
                return await self._collect(surfaces, reason, preview=False)
        finally:
            self._flight = None

    async def _collect(
        self, surfaces: frozenset[ReloadSurface], reason: ReloadReason, *, preview: bool
    ) -> ReloadReport:
        generation = self._generation + 1
        published: set[ReloadSurface] = set()
        diagnostics: list[ReloadDiagnostic] = []
        changes: list[ReloadChange] = []
        for surface in RELOAD_SURFACES:
            if surface not in surfaces:
                continue
            commands = tuple(self._command_registry.all_commands())
            skill_snapshot = self._skills.current().value
            try:
                candidate = await asyncio.to_thread(
                    self._discover, surface, commands, skill_snapshot
                )
            except Exception:  # noqa: BLE001 - a failed discovery keeps the surface intact
                diagnostics.append(
                    ReloadDiagnostic(
                        surface,
                        str(self._project_dir),
                        "import_error",
                        "Discovery failed. The current surface was kept.",
                    )
                )
                continue

            diagnostics.extend(candidate.diagnostics)
            if candidate.apply is None:
                continue
            changes.extend(_changes(surface, self._versions[surface], candidate.versions))
            if not preview:
                candidate.apply(generation)
                self._versions[surface] = candidate.versions
                published.add(surface)

        if published:
            self._generation = generation
        report = ReloadReport(
            self._generation,
            reason,
            surfaces,
            frozenset(published),
            tuple(changes),
            tuple(diagnostics),
        )
        if published:
            self._processor.emit_nowait(Event.create("SessionReloaded", report.to_dict()))
        return report

    def _discover(
        self,
        surface: ReloadSurface,
        commands: tuple[Command, ...],
        skill_snapshot: Mapping[str, SkillDef],
    ) -> _Candidate:
        match surface:
            case "skills":
                return self._scan_skills(commands)
            case "tools":
                return self._scan_tools()
            case "workflows":
                return self._scan_workflows()
            case "commands":
                return self._scan_commands(commands, skill_snapshot)

    def _scan_skills(self, commands: tuple[Command, ...]) -> _Candidate:
        from agenthicc.commands.registry import UnifiedCommandRegistry
        from agenthicc.skills.loader import discover_skills_with_diagnostics

        found = discover_skills_with_diagnostics(
            project_dir=self._project_dir,
            user_dir=self._skill_user_dir,
        )
        diagnostics: list[ReloadDiagnostic] = []
        for item in found.diagnostics:
            if item.severity != "info":
                code: _DiagnosticCode = (
                    "name_conflict" if "conflict" in item.code else "invalid_manifest"
                )
                diagnostics.append(
                    ReloadDiagnostic(
                        "skills", str(item.path), code, f"Skill discovery: {item.code}."
                    )
                )
        if any(item.code in {"invalid-skill-root", "scan-error"} for item in found.diagnostics):
            return _Candidate({}, tuple(diagnostics), None)

        registry = UnifiedCommandRegistry()
        registry.register_many([command for command in commands if not command.is_skill])
        diagnostics.extend(_skill_commands(registry, found.skills))
        skills = MappingProxyType(dict(found.skills))
        versions = {name: _digest(skill.path / "SKILL.md") for name, skill in found.skills.items()}

        def apply(generation: int) -> None:
            self._command_registry.replace_with(registry)
            self._skills._publish(generation, skills)

        return _Candidate(versions, tuple(diagnostics), apply)

    def _scan_tools(self) -> _Candidate:
        from agenthicc.plugins.discovery import discover_project_tools

        found = discover_project_tools(
            project_dir=self._project_dir,
            user_dir=self._user_dir,
            subagent_registry=self._subagent_registry.copy(),
        )
        tools = tuple(found.all_tools)
        diagnostics = tuple(_source_diagnostic("tools", item, kind="tool") for item in found.failed)
        if diagnostics and not tools:
            return _Candidate({}, diagnostics, None)

        versions: dict[str, str | None] = {}
        for item in found.results:
            if item.tools:
                digest = _digest(item.path)
                versions.update((tool_name(tool), digest) for tool in item.tools)

        def apply(generation: int) -> None:
            self._tools._publish(generation, tools)

        return _Candidate(versions, diagnostics, apply)

    def _scan_workflows(self) -> _Candidate:
        from agenthicc.workflows.registry import build_workflow_registry

        diagnostics: list[ReloadDiagnostic] = []
        messages: dict[WorkflowLoadCode, str] = {
            "pending_marker": "The workflow package is pending publication.",
            "untrusted": "Workflow source requires trust.",
            "untrusted_modified": "Workflow source changed since trust.",
            "import_error": "The workflow source could not be imported.",
            "invalid_manifest": "The source declares no workflow plugin.",
            "name_conflict": "A workflow overrides an earlier source.",
        }

        def diagnose(path: Path, code: WorkflowLoadCode) -> None:
            digest = _digest(path) if code in {"untrusted", "untrusted_modified"} else None
            diagnostics.append(
                ReloadDiagnostic("workflows", str(path), code, messages[code], digest)
            )

        found = build_workflow_registry(
            project_dir=self._project_dir, user_dir=self._user_dir, on_skip=diagnose
        )
        versions: dict[str, str | None] = {}
        for name, source in found.source_paths().items():
            if source is None:
                versions[name] = None
                continue
            path = Path(source)
            files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
            fingerprint = hashlib.sha256()
            for source_file in files:
                fingerprint.update(f"{source_file}:{_digest(source_file)}\n".encode())
            versions[name] = fingerprint.hexdigest()

        def apply(_generation: int) -> None:
            self._workflow_registry.replace_with(found)

        return _Candidate(versions, tuple(diagnostics), apply)

    def _scan_commands(
        self, commands: tuple[Command, ...], skills: Mapping[str, SkillDef]
    ) -> _Candidate:
        from agenthicc.commands import build_builtin_registry
        from agenthicc.commands.plugin_loader import discover_command_plugins

        found = discover_command_plugins(project_dir=self._project_dir, user_dir=self._user_dir)
        if found.failed:
            return _Candidate(
                {},
                tuple(
                    _source_diagnostic("commands", item, kind="command") for item in found.failed
                ),
                None,
            )

        registry = build_builtin_registry()
        registry.register_many(
            [
                command
                for command in commands
                if command.name not in self._command_plugin_names
                and command.source_id != "builtin"
                and not command.is_skill
            ]
        )
        registry.register_many(found.all_commands)
        diagnostics = _skill_commands(registry, skills)
        versions: dict[str, str | None] = {}
        for item in found.results:
            if item.commands:
                digest = _digest(item.path)
                versions.update((command.name, digest) for command in item.commands)

        def apply(_generation: int) -> None:
            self._command_registry.replace_with(registry)
            self._command_plugin_names = frozenset(versions)

        return _Candidate(versions, diagnostics, apply)
