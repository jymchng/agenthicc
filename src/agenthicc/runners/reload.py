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
from agenthicc.tools.surface import ToolSurface, ToolSurfaceHolder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

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
    "parse_reload_surfaces",
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
    "capability_denied",
]

_T = TypeVar("_T")


def parse_reload_surfaces(args: str) -> frozenset[ReloadSurface]:
    """Parse the optional surface names shared by the TUI and headless command."""
    wanted = frozenset(args.casefold().split())
    if wanted - frozenset(RELOAD_SURFACES):
        raise ValueError("Usage: /reload [skills tools workflows commands]")
    return frozenset(surface for surface in RELOAD_SURFACES if not wanted or surface in wanted)


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
    skills: Mapping[str, SkillDef] | None = None


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
    from agenthicc.plugins.discovery import LoadResult
    from agenthicc.plugins.trust import current_extension_trust

    if result.untrusted:
        refusal = current_extension_trust().check(result.path, kind=kind)
        if refusal is not None and refusal.reason == "authored publication pending":
            return ReloadDiagnostic(
                surface, str(result.path), "pending_marker", "Authored publication is pending."
            )
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
    if isinstance(result, LoadResult):
        if result.reserved_tool_names:
            names = ", ".join(result.reserved_tool_names)
            return ReloadDiagnostic(
                surface, str(result.path), "name_conflict", f"Reserved tool names: {names}."
            )
        if result.denied_capabilities:
            names = ", ".join(result.denied_capabilities)
            return ReloadDiagnostic(
                surface,
                str(result.path),
                "capability_denied",
                f"Invalid authored capability declaration: {names}.",
            )
    return ReloadDiagnostic(
        surface, str(result.path), "import_error", "The extension could not be imported."
    )


def _skill_commands(
    registry: UnifiedCommandRegistry, skills: Mapping[str, SkillDef]
) -> tuple[ReloadDiagnostic, ...]:
    from agenthicc.commands.builtins import _build_skill_command

    diagnostics: list[ReloadDiagnostic] = []
    for slug, skill in skills.items():
        command = _build_skill_command(slug, skill)
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
        tool_sources: Mapping[str, str] | None = None,
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
        self._generation = processor.get_state().reload_generation
        self._tools = ToolSurfaceHolder(
            ToolSurface(self._generation, tuple(tools), tool_sources or {})
        )
        self._versions: dict[ReloadSurface, dict[str, str | None]] = {
            "skills": dict.fromkeys(self._skills.current().value),
            "tools": dict.fromkeys(map(tool_name, tools)),
            "workflows": dict.fromkeys(workflow_registry.names()),
            "commands": dict.fromkeys(command_plugin_names),
        }
        self._discovery_lock = asyncio.Lock()
        self._flight: _Flight | None = None
        self._published: set[ReloadSurface] = set()
        self._closed = False

    @property
    def skills(self) -> SurfaceHolder[Mapping[str, SkillDef]]:
        return self._skills

    @property
    def tools(self) -> ToolSurfaceHolder:
        return self._tools

    @property
    def generation(self) -> int:
        return self._generation

    def install_initial(
        self,
        *,
        skills: Mapping[str, SkillDef],
        tools: Sequence[ToolLike],
        tool_sources: Mapping[str, str] | None = None,
        commands: Sequence[Command],
        workflows: WorkflowRegistry,
    ) -> None:
        """Install deferred startup data only for surfaces still at startup."""
        if self._closed:
            return
        if "skills" not in self._published:
            self._skills._publish(self._generation, MappingProxyType(dict(skills)))
            self._versions["skills"] = dict.fromkeys(skills)
        if "tools" not in self._published:
            self._tools.publish(ToolSurface(self._generation, tuple(tools), tool_sources or {}))
            self._versions["tools"] = dict.fromkeys(map(tool_name, tools))
        if "commands" not in self._published:
            self._command_registry.register_many(commands)
            self._command_plugin_names = frozenset(command.name for command in commands)
            self._versions["commands"] = dict.fromkeys(self._command_plugin_names)
        _skill_commands(self._command_registry, self._skills.current().value)
        if "workflows" not in self._published:
            self._workflow_registry.replace_with(workflows)
            self._versions["workflows"] = dict.fromkeys(workflows.names())

    async def reload(
        self,
        surfaces: frozenset[ReloadSurface],
        *,
        reason: ReloadReason,
        prepare: Callable[[], Awaitable[None]] | None = None,
        before_publish: Callable[[ReloadReport], Awaitable[bool]] | None = None,
    ) -> ReloadReport:
        if prepare is not None or before_publish is not None:
            async with self._discovery_lock:
                if self._closed:
                    raise RuntimeError("session reload service is closed")
                if prepare is not None:
                    await prepare()
                return await self._collect(
                    surfaces, reason, preview=False, before_publish=before_publish
                )
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
        self,
        surfaces: frozenset[ReloadSurface],
        reason: ReloadReason,
        *,
        preview: bool,
        before_publish: Callable[[ReloadReport], Awaitable[bool]] | None = None,
    ) -> ReloadReport:
        generation = self._generation + 1
        ready: dict[ReloadSurface, _Candidate] = {}
        diagnostics: list[ReloadDiagnostic] = []
        changes: list[ReloadChange] = []
        commands = tuple(self._command_registry.all_commands())
        skill_snapshot = self._skills.current().value
        for surface in RELOAD_SURFACES:
            if surface not in surfaces:
                continue
            try:
                candidate = await self._discover_off_loop(surface, commands, skill_snapshot)
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
            before = self._versions[surface]
            if surface == "workflows":
                before = {name: before.get(name) for name in self._workflow_registry.names()}
            changes.extend(_changes(surface, before, candidate.versions))
            ready[surface] = candidate
            if candidate.skills is not None:
                skill_snapshot = candidate.skills

        proposed = ReloadReport(
            generation if ready else self._generation,
            reason,
            surfaces,
            frozenset(ready),
            tuple(changes),
            tuple(diagnostics),
        )
        if preview or (before_publish is not None and not await before_publish(proposed)):
            return ReloadReport(
                self._generation, reason, surfaces, frozenset(), tuple(changes), tuple(diagnostics)
            )
        for surface, candidate in ready.items():
            apply = candidate.apply
            assert apply is not None
            apply(generation)
            self._versions[surface] = candidate.versions
        if ready:
            self._published.update(ready)
            self._generation = generation
            self._processor.emit_nowait(Event.create("SessionReloaded", proposed.to_dict()))
        return proposed

    async def _discover_off_loop(
        self,
        surface: ReloadSurface,
        commands: tuple[Command, ...],
        skills: Mapping[str, SkillDef],
    ) -> _Candidate:
        work = asyncio.create_task(asyncio.to_thread(self._discover, surface, commands, skills))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # A cancelled preview still owns its discovery until the thread has
            # stopped importing. The next discovery must not overlap it.
            await asyncio.gather(work, return_exceptions=True)
            raise

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

        return _Candidate(versions, tuple(diagnostics), apply, skills=skills)

    def _scan_tools(self) -> _Candidate:
        from agenthicc.agent_tools import AGENT_TOOLS
        from agenthicc.plugins.discovery import discover_project_tools

        found = discover_project_tools(
            project_dir=self._project_dir,
            user_dir=self._user_dir,
            subagent_registry=self._subagent_registry.copy(),
        )
        tools = tuple(found.all_tools)
        sources = found.tool_sources(self._project_dir)
        diagnostics = [_source_diagnostic("tools", item, kind="tool") for item in found.failed]
        refused = {
            "untrusted",
            "untrusted_modified",
            "pending_marker",
            "capability_denied",
            "name_conflict",
        }
        if diagnostics and not tools and not any(item.code in refused for item in diagnostics):
            return _Candidate({}, tuple(diagnostics), None)

        versions: dict[str, str | None] = {}
        names = set(map(tool_name, AGENT_TOOLS))
        for item in found.results:
            if item.tools:
                digest = _digest(item.path)
                for tool in item.tools:
                    name = tool_name(tool)
                    if name in names:
                        diagnostics.append(
                            ReloadDiagnostic(
                                "tools",
                                str(item.path),
                                "name_conflict",
                                f"{name} overrides an earlier tool source.",
                            )
                        )
                    names.add(name)
                    versions[name] = digest

        def apply(generation: int) -> None:
            self._tools.publish(ToolSurface(generation, tools, sources))

        return _Candidate(versions, tuple(diagnostics), apply)

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
        if found.failed and not any(item.untrusted for item in found.failed):
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
        diagnostics = [
            _source_diagnostic("commands", item, kind="command") for item in found.failed
        ]
        versions: dict[str, str | None] = {}
        for item in found.results:
            if not item.commands:
                continue
            digest = _digest(item.path)
            for command in item.commands:
                if any(registry.get(name) is not None for name in (command.name, *command.aliases)):
                    diagnostics.append(
                        ReloadDiagnostic(
                            "commands",
                            str(item.path),
                            "name_conflict",
                            f"{command.name} overrides an earlier command or alias.",
                        )
                    )
                registry.register(command)
                versions[command.name] = digest
        diagnostics.extend(_skill_commands(registry, skills))

        def apply(_generation: int) -> None:
            self._command_registry.replace_with(registry)
            self._command_plugin_names = frozenset(versions)

        return _Candidate(versions, tuple(diagnostics), apply)
