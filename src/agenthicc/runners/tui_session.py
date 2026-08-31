"""TUI session — starts the reactive runtime (PRD-58 to PRD-67, PRD-93)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sized
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, NoReturn, cast

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._config import LLMConfig
    from agenthicc.cli.context import CLIContext, CLIFlags
    from agenthicc.config import AgenthiccConfig
    from agenthicc.config import CloakBrowserSettings, PlaywrightSettings
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.tui.workspace import Workspace
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime import SendMessageCommand, InterruptAgentCommand
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceScope
    from agenthicc.commands.command import Command
    from agenthicc.commands.command import UsageSnapshot
    from agenthicc.commands.busy_policy import BusyDecision
    from agenthicc.commands.registry import UnifiedCommandRegistry
    from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult
    from agenthicc.plugins.discovery import PluginToolSet
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.workflows.registry import WorkflowRegistry
    from agenthicc.workflows.plugin import WorkflowPlugin
    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.runners.workflow_recovery import WorkflowRecoveryRecord
    from agenthicc.runners.startup import StartupCoordinator
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.cloakbrowser.session import BrowserSessionManager
    from agenthicc.background.terminals import TerminalManager


def _make_session_tools(
    approval_svc: ApprovalService | None,
    memory_router: MemoryRouter | None = None,
    semantic_index: SemanticIndex | None = None,
) -> list[ToolLike]:
    """Tools injected into every interactive agent turn (Yolo mode + plan phase)."""
    from agenthicc.workflows.code_plan.phase_tools import make_questions_tool  # noqa: PLC0415
    from agenthicc.workflows.memory_tools import make_memory_tools  # noqa: PLC0415

    return make_questions_tool(approval_svc) + make_memory_tools(memory_router, semantic_index)


def _build_agent_runner(
    llm_cfg: LLMConfig | None, *, cassette_dir: Path | None = None
) -> object | None:
    """Return a lauren-ai runner constructed on the first agent turn.

    Importing and constructing lauren-ai's runner is one of the largest local
    startup costs. A new TUI can render and accept safe commands without it,
    so the proxy keeps that provider boundary off the critical path while
    preserving the private transport/signal contract used by AgentTurnRunner.
    """
    if llm_cfg is None:
        return None
    return _LazyAgentRunner(llm_cfg, cassette_dir=cassette_dir)


def _prepare_startup_dependency(ctx: "SessionContext", phase: str) -> None:
    """Materialize a dependency explicitly declared by a workflow.

    Most readiness phases are already started by session construction or the
    post-frame coordinator. Provider and browser phases are deliberately
    demand-driven, however. A workflow that declares either as required must
    be allowed to opt into that cost before its first agent turn; it must not
    wait forever for a lazy proxy that has not yet been touched.
    """
    if phase == "provider":
        resource = getattr(ctx, "agent_runner", None)
    elif phase == "browser":
        resource = getattr(ctx, "browser_manager", None)
    else:
        return
    prepare = getattr(resource, "prepare", None)
    if callable(prepare):
        prepare()


async def _run_agent_turn(*args: object, **kwargs: object) -> None:
    """Lazy compatibility shim retained for TUI tests and integrations."""
    from agenthicc.runners.agent_turn import _run_agent_turn as run_agent_turn  # noqa: PLC0415

    typed_runner = cast("Callable[..., Awaitable[None]]", run_agent_turn)
    await typed_runner(*args, **kwargs)


def _preserve_interrupted_memory(memory: object | None) -> bool:
    """Lazy compatibility shim for the interruption recovery path."""
    from agenthicc.runners.agent_turn import (  # noqa: PLC0415
        _preserve_interrupted_memory as preserve,
    )

    return preserve(memory)


class _LazyAgentRunner:
    """Small first-use proxy for lauren-ai's provider runner."""

    def __init__(self, llm_cfg: LLMConfig, *, cassette_dir: Path | None) -> None:
        self._llm_cfg = llm_cfg
        self._cassette_dir = cassette_dir
        self._delegate: object | None = None
        self._startup: "StartupCoordinator | None" = None

    def _ensure(self) -> object:
        if self._delegate is not None:
            return self._delegate
        from agenthicc.runners.startup import ReadinessState  # noqa: PLC0415

        if self._startup is not None:
            self._startup.begin("provider", deferred=True)
        try:
            self._delegate = self._create()
        except Exception as exc:
            if self._startup is not None:
                self._startup.finish(
                    "provider",
                    ReadinessState.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if self._startup is not None:
            self._startup.finish("provider")
        return self._delegate

    def _create(self) -> object:
        """Build the real runner, importing lauren-ai only on demand."""
        from lauren_ai._agents._runner import AgentRunnerBase  # noqa: PLC0415
        from lauren_ai._module import _build_transport  # noqa: PLC0415
        from lauren_ai._signals import SignalBus  # noqa: PLC0415

        transport = _build_transport(self._llm_cfg)
        if self._cassette_dir is not None:
            from agenthicc.testing.recording_transport import RecordingTransport  # noqa: PLC0415

            self._cassette_dir.mkdir(parents=True, exist_ok=True)
            transport = RecordingTransport(transport, self._cassette_dir / "cassette.jsonl")
        return AgentRunnerBase(transport=transport, signals=SignalBus())

    def prepare(self) -> object:
        """Explicitly prepare the provider for a declared workflow dependency."""
        return self._ensure()

    def __getattr__(self, name: str) -> object:
        return getattr(self._ensure(), name)


class _LazyBrowserSession:
    """Defer optional browser module imports until browser work is selected."""

    def __init__(
        self,
        backend: str,
        settings: object,
        conversation_id: str,
        workspace_root: Path,
        startup: "StartupCoordinator | None" = None,
    ) -> None:
        self._backend = backend
        self._settings = settings
        self._conversation_id = conversation_id
        self._workspace_root = workspace_root
        self._startup = startup
        self._delegate: object | None = None
        self._pending_checkpoint: object | None = None

    def _ensure(self) -> object:
        if self._delegate is not None:
            return self._delegate
        from agenthicc.runners.startup import ReadinessState  # noqa: PLC0415

        startup = self._startup
        if startup is not None and not startup.closed:
            startup.begin("browser", deferred=True)
        try:
            delegate = self._create_delegate()
            self._delegate = delegate
        except Exception as exc:
            if startup is not None and not startup.closed:
                detail = (
                    f"optional {self._backend} backend is unavailable; install its extra"
                    if isinstance(exc, ImportError)
                    else f"{type(exc).__name__}: {exc}"
                )
                startup.finish("browser", ReadinessState.FAILED, error=detail)
            raise
        if startup is not None and not startup.closed:
            startup.finish("browser")
        return self._delegate

    def _create_delegate(self) -> object:
        """Import and construct the selected backend only on capability use."""
        if self._backend == "playwright":
            from agenthicc.tools.playwright import create_playwright_session  # noqa: PLC0415

            delegate = create_playwright_session(
                cast("PlaywrightSettings", self._settings),
                conversation_id=self._conversation_id,
                workspace_root=self._workspace_root,
            )
        elif self._backend == "cloakbrowser":
            from agenthicc.tools.cloakbrowser import create_browser_session  # noqa: PLC0415

            delegate = create_browser_session(
                cast("CloakBrowserSettings", self._settings),
                conversation_id=self._conversation_id,
                workspace_root=self._workspace_root,
            )
        else:
            raise RuntimeError(f"unsupported browser backend: {self._backend}")
        self._delegate = delegate
        try:
            self._restore_pending_checkpoint()
        except Exception:
            # Do not leave a partially restored backend looking ready. The
            # next explicit browser operation may retry after the checkpoint
            # or dependency issue has been repaired.
            self._delegate = None
            raise
        return delegate

    def prepare(self) -> object:
        """Explicitly prepare the backend for a declared workflow dependency."""
        return self._ensure()

    async def close_session(self) -> None:
        """Do not import or initialize a browser solely during shutdown."""
        if self._delegate is None:
            return
        close = getattr(self._delegate, "close_session", None)
        if callable(close):
            await close()

    def reset_turn_budget(self) -> None:
        """Reset browser accounting without starting an unused backend."""
        if self._delegate is None:
            return
        reset = getattr(self._delegate, "reset_turn_budget", None)
        if callable(reset):
            reset()

    def checkpoint_payload(self) -> dict[str, object]:
        """Return empty browser state until a browser has actually been used."""
        if self._delegate is None:
            return {}
        payload = getattr(self._delegate, "checkpoint_payload", None)
        result = payload() if callable(payload) else {}
        return result if isinstance(result, dict) else {}

    def restore_checkpoint(self, payload: object) -> None:
        """Remember checkpoint state without importing a browser backend."""
        if self._delegate is None:
            self._pending_checkpoint = payload
            return
        restore = getattr(self._delegate, "restore_checkpoint", None)
        if callable(restore):
            restore(payload)

    def _restore_pending_checkpoint(self) -> None:
        if self._pending_checkpoint is None:
            return
        restore = getattr(self._delegate, "restore_checkpoint", None)
        if callable(restore):
            restore(self._pending_checkpoint)
        self._pending_checkpoint = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._ensure(), name)


class _LazyBrowserTools:
    """Iterable tool collection whose backend factory runs at first iteration."""

    def __init__(
        self,
        backend: str,
        manager: _LazyBrowserSession,
        startup: "StartupCoordinator | None" = None,
    ) -> None:
        self._backend = backend
        self._manager = manager
        self._startup = startup
        self._tools: list[object] | None = None

    def _ensure(self) -> list[object]:
        if self._tools is not None:
            return self._tools
        from agenthicc.runners.startup import ReadinessState  # noqa: PLC0415

        startup = self._startup
        if startup is not None and not startup.closed:
            startup.begin("browser", deferred=True)
        try:
            if self._backend == "playwright":
                from agenthicc.tools.playwright import make_playwright_tools  # noqa: PLC0415

                self._tools = list(
                    make_playwright_tools(cast("BrowserSessionManager", self._manager))
                )
            elif self._backend == "cloakbrowser":
                from agenthicc.tools.cloakbrowser import make_cloakbrowser_tools  # noqa: PLC0415

                self._tools = list(
                    make_cloakbrowser_tools(cast("BrowserSessionManager", self._manager))
                )
            else:
                self._tools = []
        except Exception as exc:
            if startup is not None and not startup.closed:
                detail = (
                    f"optional {self._backend} backend is unavailable; install its extra"
                    if isinstance(exc, ImportError)
                    else f"{type(exc).__name__}: {exc}"
                )
                startup.finish("browser", ReadinessState.FAILED, error=detail)
            raise
        if startup is not None and not startup.closed:
            startup.finish("browser")
        return self._tools

    def __iter__(self) -> Iterator[object]:
        return iter(self._ensure())


class _LazySemanticIndex:
    """Keep vector-store imports off startup while preserving memory tooling."""

    def __init__(self, startup: "StartupCoordinator | None" = None) -> None:
        self._delegate: object | None = None
        self._startup = startup

    def _ensure(self) -> object:
        if self._delegate is None:
            from agenthicc.runners.startup import ReadinessState  # noqa: PLC0415

            startup = self._startup
            if startup is not None and not startup.closed:
                startup.begin("semantic_index", deferred=True)
            try:
                from agenthicc.memory.vector import SemanticIndex  # noqa: PLC0415

                self._delegate = SemanticIndex()
            except Exception as exc:
                if startup is not None and not startup.closed:
                    startup.finish(
                        "semantic_index",
                        ReadinessState.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                raise
            if startup is not None and not startup.closed:
                startup.finish("semantic_index")
        return self._delegate

    async def add(self, *args: object, **kwargs: object) -> str:
        add = cast("Callable[..., Awaitable[str]]", getattr(self._ensure(), "add"))
        return await add(*args, **kwargs)

    async def search(self, *args: object, **kwargs: object) -> list[tuple[str, float]]:
        search = cast(
            "Callable[..., Awaitable[list[tuple[str, float]]]]",
            getattr(self._ensure(), "search"),
        )
        return await search(*args, **kwargs)

    def __len__(self) -> int:
        return len(cast("Sized", self._ensure()))


def _fmt_exc(exc: BaseException) -> str:
    """Format an exception as 'ExceptionType: message' for scroll-buffer display.

    Never returns a bare ``str(exc)`` — the exception class name is always
    included so users can identify the failure type (e.g. ``ReadTimeout``).
    """
    name = type(exc).__name__
    msg = str(exc).strip()
    return f"{name}: {msg}" if msg else name


def _workflow_failure_kind(exc: BaseException) -> str:
    """Map common workflow exception classes to stable recovery categories."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(marker in text for marker in ("config", "profile")):
        return "configuration"
    if any(marker in text for marker in ("rate limit", "429", "provider", "transport")):
        return "provider_transient"
    if any(marker in text for marker in ("tool", "mcp", "browser", "subprocess")):
        return "tool_transient"
    return "phase_execution"


def _build_skill_command(slug: str, skill: "SkillDef") -> "Command":
    """Build the dollar-prefixed command owned by one discovered skill."""
    from agenthicc.commands.builtins import _make_skill_handler  # noqa: PLC0415
    from agenthicc.commands.command import Command  # noqa: PLC0415

    return Command(
        name=f"${slug}",
        description=skill.description or skill.name,
        argument_hint="[args…]",
        group="Skills",
        handler=_make_skill_handler(slug, skill),
        aliases=tuple(f"${alias}" for alias in skill.aliases),
        source_id=f"skill:{slug}",
    )


def _register_skill_commands(
    registry: "UnifiedCommandRegistry",
    skills: "dict[str, SkillDef]",
) -> None:
    """Register the current skill commands in the unified command registry."""
    for slug, skill in skills.items():
        try:
            command = _build_skill_command(slug, skill)
            if any(registry.get(name) is not None for name in (command.name, *command.aliases)):
                continue
            registry.register(command)
        except Exception:  # noqa: BLE001
            # A malformed extension must not prevent the TUI from starting.
            pass


def _reset_terminal_on_exit() -> None:
    try:
        sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        import termios as _tm

        settings = _tm.tcgetattr(0)
        settings[3] |= _tm.ECHO | _tm.ICANON | _tm.ISIG
        _tm.tcsetattr(0, _tm.TCSAFLUSH, settings)
    except Exception:  # noqa: BLE001
        pass


from agenthicc.tui.runtime.session_log import (  # noqa: E402
    DEFAULT_RESUME_TRANSCRIPT_TURNS,
    create_session_id,
    register_session,
    update_session_mode,
    load_session_mode,
    touch_session,
    find_latest_session_for_cwd,
    SessionEventLog,
    load_user_message_history,
)
from agenthicc.runners.session_lease import (  # noqa: E402
    SessionAlreadyActiveError,
    SessionOpenCoordinator,
    SessionOwnerLease,
    SessionStorageError,
    format_session_conflict,
)


_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"

# Module-level alias so tests that monkeypatch this name on the module work.
_find_latest_session_for_cwd = find_latest_session_for_cwd


@dataclass(frozen=True)
class _ExtensionDiscovery:
    """Results produced off the event loop by deferred extension loading."""

    skills: dict[str, SkillDef]
    project_plugins: PluginToolSet
    project_commands: list[object]
    agents_registry: AgentsRegistry
    installed_skills: int = 0
    diagnostics: tuple[object, ...] = ()


_EXTENSION_RESULT_CACHE: dict[str, _ExtensionDiscovery] = {}


def _discover_extensions_sync(cfg: "AgenthiccConfig") -> _ExtensionDiscovery:
    """Discover skills, tools, and commands without mutating live state."""
    from agenthicc.agents.registry import build_agents_registry  # noqa: PLC0415
    from agenthicc.commands.plugin_loader import discover_command_plugins  # noqa: PLC0415
    from agenthicc.plugins.discovery import discover_project_tools  # noqa: PLC0415
    from agenthicc.skills.bootstrap import bootstrap_default_skills  # noqa: PLC0415
    from agenthicc.skills.loader import discover_skills_with_diagnostics  # noqa: PLC0415
    from agenthicc.runners.discovery_cache import (  # noqa: PLC0415
        fingerprint_sources,
        read_discovery_cache,
        write_discovery_cache,
    )

    skill_global_dir = (
        Path(cfg.skills.default_skill_directory).expanduser()
        if cfg.skills.default_skill_directory
        else Path.home() / ".agenthicc"
    )
    installed_skills = bootstrap_default_skills(
        global_dir=skill_global_dir,
        enabled=cfg.skills.install_default_skills,
    )
    source_roots = (
        skill_global_dir,
        Path(".agenthicc") / "skills",
        Path.home() / ".agenthicc" / "tools",
        Path(".agenthicc") / "tools",
        Path.home() / ".agenthicc" / "commands",
        Path(".agenthicc") / "commands",
        Path.home() / ".agenthicc" / "agents",
        Path(".agenthicc") / "agents",
        Path.home() / ".agenthicc" / "workflows",
        Path(".agenthicc") / "workflows",
    )
    source_fingerprint = fingerprint_sources(source_roots)
    fingerprint_key = str(source_fingerprint["fingerprint"])
    cache_path = Path(".agenthicc") / "cache" / "extension-discovery.json"
    # The persistent record is intentionally metadata-only.  Reuse live
    # objects only within this process and only after recomputing the source
    # fingerprint, so deleted/changed/unreadable files invalidate naturally.
    previous = read_discovery_cache(cache_path)
    cached = _EXTENSION_RESULT_CACHE.get(fingerprint_key)
    if cached is not None:
        if previous is None or previous.get("fingerprint") != fingerprint_key:
            try:
                write_discovery_cache(cache_path, source_fingerprint)
            except OSError:
                log.debug("could not refresh extension discovery cache", exc_info=True)
        return cached
    skill_discovery = discover_skills_with_diagnostics(
        project_dir=Path(".agenthicc"),
        user_dir=skill_global_dir,
    )
    project_plugins = discover_project_tools(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    project_commands = discover_command_plugins(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    ).all_commands
    agents_registry = build_agents_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
        load_external=True,
    )
    result = _ExtensionDiscovery(
        skills=skill_discovery.skills,
        project_plugins=project_plugins,
        project_commands=list(project_commands),
        agents_registry=agents_registry,
        installed_skills=installed_skills,
        diagnostics=tuple(skill_discovery.diagnostics),
    )
    _EXTENSION_RESULT_CACHE[fingerprint_key] = result
    try:
        write_discovery_cache(cache_path, source_fingerprint)
    except OSError:
        log.debug("could not write extension discovery cache", exc_info=True)
    return result


def _apply_extension_discovery(ctx: "SessionContext", discovery: _ExtensionDiscovery) -> None:
    """Publish a validated discovery result on the event-loop owner."""
    from agenthicc.commands.command import Command as _Cmd  # noqa: PLC0415
    from agenthicc.plugins.discovery import warn_conflicts  # noqa: PLC0415

    ctx.skills.clear()
    ctx.skills.update(discovery.skills)
    ctx.project_plugins.results.clear()
    ctx.project_plugins.results.extend(discovery.project_plugins.results)
    ctx.agents_registry.replace_with(discovery.agents_registry)
    if ctx.project_plugins.all_tools:
        warn_conflicts(ctx.project_plugins)
    for spec in discovery.project_commands:
        try:
            if isinstance(spec, _Cmd):
                command = spec
            else:
                name = getattr(spec, "name", None)
                description = getattr(spec, "description", "")
                if not isinstance(name, str) or not isinstance(description, str):
                    continue
                command = _Cmd(
                    name=name,
                    description=description,
                    aliases=tuple(getattr(spec, "aliases", ())),
                    argument_hint=getattr(spec, "argument_hint", ""),
                    group=getattr(spec, "group", "Project"),
                    source_id="plugin",
                )
            ctx.cmd_registry.register(command)
        except Exception:  # noqa: BLE001
            continue
    ctx.command_plugin_names.update(
        name
        for spec in discovery.project_commands
        if isinstance(name := getattr(spec, "name", None), str)
    )
    _register_skill_commands(ctx.cmd_registry, ctx.skills)


def _discover_workflow_registry_sync() -> "WorkflowRegistry":
    """Load external workflow modules outside the interactive event loop."""
    from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415

    return build_workflow_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
        load_external=True,
    )


# ── session construction ──────────────────────────────────────────────────────


async def _build_session_context(
    resume_id: str | None,
    cli_overrides: list[str] | None,
    record_cassette_dir: Path | None = None,
    config_path: str | None = None,
    headless: bool = False,
    cli_secret_overrides: list[str] | None = None,
    *,
    mode_name: str | None = None,
    workflow_name: str | None = None,
    owner_lease: SessionOwnerLease | None = None,
    config: "AgenthiccConfig | None" = None,
) -> SessionContext:
    """Acquire the session owner before constructing any durable resources."""

    from agenthicc.runners.startup import ReadinessState, StartupCoordinator  # noqa: PLC0415

    session_id = resume_id or (
        owner_lease.session_id if owner_lease is not None else create_session_id()
    )
    startup = StartupCoordinator()
    startup.begin("bootstrap")
    try:
        startup.begin("owner_lease")
        if owner_lease is None:
            coordinator = SessionOpenCoordinator(_SESSIONS_DIR)
            owner_lease = (
                coordinator.acquire_existing(
                    session_id,
                    entrypoint="headless" if headless else "tui",
                )
                if resume_id
                else coordinator.acquire_new(
                    session_id,
                    entrypoint="headless" if headless else "tui",
                )
            )
        elif owner_lease.session_id != session_id:
            raise SessionStorageError("provided session owner does not match resume ID")
        startup.finish("owner_lease")
        startup.finish("bootstrap")
        return await _build_session_context_impl(
            resume_id,
            cli_overrides,
            record_cassette_dir,
            config_path=config_path,
            headless=headless,
            cli_secret_overrides=cli_secret_overrides,
            mode_name=mode_name,
            workflow_name=workflow_name,
            owner_lease=owner_lease,
            startup=startup,
            config=config,
        )
    except BaseException as exc:
        owner_report = startup.report("owner_lease")
        if owner_report.state.value == "loading":
            startup.finish(
                "owner_lease",
                ReadinessState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
        bootstrap_report = startup.report("bootstrap")
        if bootstrap_report.state.value == "loading":
            startup.finish(
                "bootstrap",
                ReadinessState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
        if owner_lease is not None:
            owner_lease.release()
        raise


async def _build_session_context_impl(
    resume_id: str | None,
    cli_overrides: list[str] | None,
    record_cassette_dir: Path | None = None,
    config_path: str | None = None,
    headless: bool = False,
    cli_secret_overrides: list[str] | None = None,
    *,
    mode_name: str | None = None,
    workflow_name: str | None = None,
    owner_lease: SessionOwnerLease,
    startup: StartupCoordinator | None = None,
    config: "AgenthiccConfig | None" = None,
) -> SessionContext:
    """Construct all session-scoped singletons and return a SessionContext."""
    from agenthicc.runners.startup import ReadinessState, StartupCoordinator  # noqa: PLC0415

    startup = startup or StartupCoordinator()
    from rich.console import Console  # noqa: PLC0415
    from agenthicc.kernel import (  # noqa: PLC0415
        AppState as KAppState,
        EventProcessor,
        SecurityPolicy,
        SystemSettings,
    )
    from agenthicc.kernel.reducer import root_reducer  # noqa: PLC0415
    from agenthicc.kernel.processor import restore_from_log  # noqa: PLC0415
    from agenthicc.config import load_config, build_llm_config  # noqa: PLC0415
    from agenthicc.runners.session_context import SessionContext  # noqa: PLC0415
    from agenthicc.tui.conversation_store import AppState  # noqa: PLC0415
    from agenthicc.tui.runtime import (  # noqa: PLC0415
        CommandBus,
        ModeManager,
    )

    # ── session ID ────────────────────────────────────────────────────────────
    session_id = resume_id or owner_lease.session_id
    if session_id != owner_lease.session_id:
        raise SessionStorageError("session owner does not match session context")
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Configuration is the first session-owned decision.  All later
    # integrations consume this immutable-in-practice snapshot; loading it
    # before journals and registries also makes the readiness timeline honest.
    startup.begin("configuration")
    cfg = config or load_config(
        cli_overrides=cli_overrides or [],
        cli_secret_overrides=cli_secret_overrides or [],
        config_path=config_path,
    )
    startup.finish("configuration")

    # PRD-150: every client observes the same session projection.  The TUI
    # remains responsible for Rich/reactive presentation, while the service
    # owns the client-neutral snapshot and event cursor.
    startup.begin("session_service")
    startup.begin("session_index")
    from agenthicc.session_service import SessionService  # noqa: PLC0415

    session_service = SessionService()
    await session_service.ensure_session(
        session_id,
        project_root=Path.cwd(),
        capabilities=frozenset({"read", "control", "workspace"}),
    )
    if session_service.store.index_dirty:
        startup.finish(
            "session_index",
            ReadinessState.DEGRADED,
            error="metadata index repair pending",
        )
    else:
        startup.finish("session_index")
    startup.finish("session_service")

    # ── cassette dir: <base>/<session_id>/ ───────────────────────────────────
    cassette_dir: Path | None = (
        record_cassette_dir / session_id if record_cassette_dir is not None else None
    )
    if cassette_dir is not None:
        cassette_dir.mkdir(parents=True, exist_ok=True)

    # ── kernel ────────────────────────────────────────────────────────────────
    startup.begin("kernel_shell")
    startup.begin("selected_restore")
    log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")
    k_state = KAppState.create(
        settings=SystemSettings(event_log_path=log_path, snapshot_path=".agenthicc/snapshot.json"),
        policy=SecurityPolicy(),
    )
    if resume_id:
        # log_path already points to the kernel event log (sessions/<id>.jsonl).
        # get_session_log_path() returns the TUI conversation log
        # (sessions/<id>/conversation.jsonl) — a completely different file that
        # restore_from_log cannot parse, producing "skipping corrupt event log line".
        kernel_log = Path(log_path)
        if kernel_log.exists():
            k_state = await restore_from_log(log_path, k_state, root_reducer)
        touch_session(resume_id)
    else:
        register_session(session_id, os.getcwd(), "")
    startup.finish("kernel_shell")

    processor = EventProcessor(initial_state=k_state, persist=True)
    if resume_id:
        await session_service.import_kernel_log(session_id, log_path)
    kernel_event_queue = processor.subscribe_events()

    async def _project_kernel_events() -> None:
        while True:
            kernel_event = await kernel_event_queue.get()
            await session_service.publish_kernel_event(session_id, kernel_event)

    kernel_projection_task = asyncio.create_task(
        _project_kernel_events(), name=f"session-kernel-projection-{session_id}"
    )

    # PRD-149: terminal subprocesses are owned by a session-scoped manager.
    # Keep their registry alongside the existing background-session store but
    # in a separate namespace so agent sessions and terminal handles cannot be
    # confused with one another.
    from agenthicc.background.settings import load_background_settings  # noqa: PLC0415
    from agenthicc.background.terminals import TerminalManager  # noqa: PLC0415

    background_settings = load_background_settings(
        config_path=config_path,
        overrides=tuple(cli_overrides or ()),
        cwd=Path.cwd(),
    )
    terminal_root = (
        Path(background_settings.store_path).expanduser() / "terminals"
        if background_settings.store_path
        else None
    )
    terminal_manager = TerminalManager(
        session_id=session_id,
        cwd=Path.cwd(),
        store_root=terminal_root,
        enabled=(
            background_settings.enabled
            and background_settings.terminals_enabled
            and os.environ.get("AGENTHICC_DISABLE_BACKGROUND", "") != "1"
        ),
        max_terminals=background_settings.max_terminals,
        max_terminals_per_project=background_settings.max_terminals_per_project,
        max_output_bytes=background_settings.terminal_max_output_bytes,
        wall_timeout_s=background_settings.terminal_wall_timeout_s,
        cancel_grace_s=background_settings.terminal_cancel_grace_s,
        retention_days=background_settings.terminal_retention_days,
    )

    # PRD-108: configure shared HTTP client timeout from config before any tool runs.
    from agenthicc.tools.http import configure as _configure_http  # noqa: PLC0415

    _configure_http(cfg.tools.http_timeout_s)

    console = Console(
        highlight=False,
        markup=True,
        force_terminal=not headless,
        quiet=headless,
    )
    try:
        # Resolve the selected profile immediately before constructing the
        # transport.  This keeps secrets out of durable session/workflow state
        # and re-reads rotated environment values on every resume.
        cfg.resolve_provider_profile(requires_tools=True)
        llm_cfg = build_llm_config(cfg.execution)
    except ValueError as exc:
        console.print(
            f"[red]LLM config error: {exc}[/red]\n"
            "[dim]Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or --set execution.provider=...[/dim]",
            markup=True,
        )
        llm_cfg = None

    model_label = f"{cfg.execution.provider}/{cfg.execution.effective_model()}"

    # ── reactive state ────────────────────────────────────────────────────────
    app_state = AppState.create()
    app_state.conversation.model_name.set(model_label)
    app_state.conversation.session_id.set(session_id)

    session_log = SessionEventLog(session_id)
    app_state.conversation.on_event(session_log.append)

    def _project_conversation_event(event: object) -> None:
        """Project TUI events without making the reactive store authoritative."""

        try:
            kind = getattr(event, "kind", "")
            payload = getattr(event, "payload", {})
            if not isinstance(kind, str) or not isinstance(payload, dict):
                return
            turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
            task = asyncio.create_task(
                session_service.publish(
                    session_id,
                    source="tui",
                    kind=kind,
                    payload=payload,
                    turn_id=turn_id,
                )
            )

            def _consume_projection_error(done: asyncio.Task[object]) -> None:
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(_consume_projection_error)
        except RuntimeError:
            # Construction-time events can occur before an event loop owns the
            # adapter. The kernel/session journal remains authoritative then.
            return

    app_state.conversation.on_event(_project_conversation_event)

    # ── runtime services ──────────────────────────────────────────────────────
    command_bus = CommandBus()

    from agenthicc.tools.approval import ApprovalService  # noqa: PLC0415

    approval_svc: ApprovalService = ApprovalService(app_state)
    if cassette_dir is not None:
        from agenthicc.testing.recording_approval import RecordingApprovalService  # noqa: PLC0415

        approval_svc = cast(
            "ApprovalService",
            RecordingApprovalService(approval_svc, cassette_dir / "approvals.jsonl"),
        )

    # ── workflow + agents registries ──────────────────────────────────────────
    startup.begin("workflow_registry")
    from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415

    load_external = headless or resume_id is not None or workflow_name is not None

    workflow_registry = build_workflow_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
        # Explicit workflow selection, resume recovery, and headless turns
        # need external implementations before the context is returned.
        load_external=load_external,
    )
    initial_workflow: str | None = None
    if workflow_name is not None:
        requested_workflow = workflow_name.strip()
        if not requested_workflow:
            raise ValueError("Workflow name must not be empty.")
        workflow_cls = workflow_registry.get(requested_workflow)
        if workflow_cls is None:
            available = ", ".join(sorted(workflow_registry.names())) or "none"
            raise ValueError(f"Unknown workflow {requested_workflow!r}. Available: {available}")
        initial_workflow = workflow_cls.name
    startup.finish("workflow_registry")

    startup.begin("agent_registry")
    from agenthicc.agents.registry import build_agents_registry  # noqa: PLC0415

    agents_registry = build_agents_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
        load_external=load_external,
    )
    startup.finish("agent_registry")

    # ── mode manager ──────────────────────────────────────────────────────────
    mode_manager = ModeManager(
        app_state=app_state,
        default_map=workflow_registry.mode_default_map(),
        available_map=workflow_registry.mode_available_map(),
    )
    # Safe is the default posture for every new session. Resume migrates legacy
    # names (Auto/Guard/etc.) through the canonical registry before the callback
    # is installed, so the persisted value is never overwritten prematurely.
    mode_manager.set_by_name("Safe")
    persisted_mode: str | None = None
    if mode_name is not None:
        requested_mode = mode_name.strip()
        selected_mode = mode_manager.set_by_name(requested_mode)
        if selected_mode is None:
            raise ValueError(
                f"Unknown mode {requested_mode!r}. Choose one of: "
                f"{', '.join(mode_manager.registry.selectable_names())}."
            )
    elif resume_id:
        persisted_mode = load_session_mode(resume_id)
        if persisted_mode is not None and mode_manager.set_by_name(persisted_mode) is None:
            raise ValueError(
                f"Cannot resume session {resume_id!r}: unknown persisted mode "
                f"{persisted_mode!r}. Choose one of: "
                f"{', '.join(mode_manager.registry.selectable_names())}."
            )

    def _persist_mode(mode: object) -> None:
        name = getattr(mode, "name", "")
        if isinstance(name, str) and not mode_manager.registry.is_internal(name):
            update_session_mode(session_id, name)

    mode_manager.set_change_callback(_persist_mode)
    if mode_name is not None:
        # Explicit invocation state wins over the persisted mode and is itself
        # persisted canonically for future --continue/--resume invocations.
        update_session_mode(session_id, mode_manager.active_name)
    elif resume_id and persisted_mode is not None:
        # Persist the canonical spelling after a successful alias migration;
        # future resumes never need to carry the legacy identity forward.
        update_session_mode(session_id, mode_manager.active_name)

    # PRD-168: construct one immutable scope for the entire parent session.
    # Every direct turn, workflow phase, mention resolver, and command receives
    # this policy through SessionContext/WorkflowConfig; agent turns bind it
    # task-locally for legacy tool wrappers without leaking across sessions.
    from agenthicc.tools.workspace_access import (  # noqa: PLC0415
        WorkspaceScope,
        WorkspaceAccessPolicy,
    )

    startup.begin("workspace_policy")
    workspace_scope = WorkspaceScope.create(
        Path.cwd(),
        allowed_paths=cfg.security.allowed_paths,
    )
    workspace_access = WorkspaceAccessPolicy(
        workspace_scope,
        mode_provider=app_state.active_mode,
        approval_service=approval_svc,
    )
    startup.finish("workspace_policy")

    # ── skills / plugins ─────────────────────────────────────────────────────
    # A fresh interactive session can render its shell before executing any
    # project extension code. Resume, explicit workflow selection, and
    # headless execution retain eager discovery because their requested
    # operation needs the complete extension contract before returning.
    defer_extensions = not headless and resume_id is None and workflow_name is None
    from agenthicc.plugins.discovery import PluginToolSet  # noqa: PLC0415

    if not defer_extensions:
        startup.begin("extensions")
    if defer_extensions:
        skills: dict[str, SkillDef] = {}
        project_plugins = PluginToolSet()
        project_commands: list[object] = []
        command_plugin_names: set[str] = set()
    else:
        discovered_extensions = _discover_extensions_sync(cfg)
        skills = discovered_extensions.skills
        project_plugins = discovered_extensions.project_plugins
        project_commands = discovered_extensions.project_commands
        command_plugin_names = {
            name
            for command in project_commands
            if isinstance(name := getattr(command, "name", None), str)
        }
        if discovered_extensions.installed_skills:
            console.print(
                f"[dim]Installed {discovered_extensions.installed_skills} default skill(s).[/dim]",
                markup=True,
            )
        for diagnostic in discovered_extensions.diagnostics:
            if getattr(diagnostic, "severity", "info") != "info":
                console.print(f"[yellow]Skill discovery: {diagnostic}[/yellow]", markup=True)
        if project_plugins.all_tools:
            from agenthicc.plugins.discovery import warn_conflicts  # noqa: PLC0415

            warn_conflicts(project_plugins)
            console.print(
                f"[dim]Loaded {len(project_plugins.all_tools)} project tool(s) from .agenthicc/tools/[/dim]"
            )

    # ── MCP (PRD-172) ─────────────────────────────────────────────────────────
    # One manager is shared by normal turns, workflows, subagents, headless
    # callers, and the TUI. ``mcp_registry`` remains an intentional compatibility
    # alias because older runner code consumes its ``all_tools`` method.
    mcp_manager = None
    mcp_registry = None
    if cfg.tools.mcp_servers:
        from agenthicc.tools.mcp_manager import (  # noqa: PLC0415
            McpRequiredServerError,
            McpSessionManager,
        )
        from agenthicc.tools.sandbox import NetworkGuard  # noqa: PLC0415

        try:
            mcp_manager = McpSessionManager(
                (),
                event_processor=processor,
                workspace_root=Path.cwd(),
                network_guard=(
                    NetworkGuard(cfg.security.network_allow_list)
                    if cfg.security.network_allow_list
                    else None
                ),
            )
            for mcp_config in cfg.tools.mcp_servers:
                # Keep malformed optional entries visible in `/mcp` while
                # allowing valid servers to start. Required entries still
                # participate in the manager's fail-closed startup contract.
                mcp_manager.register_server(mcp_config, allow_invalid=True)
            mcp_registry = mcp_manager
            if headless:
                startup.begin("mcp")
                await mcp_manager.start_all(raise_required=True)
                startup.finish("mcp")
            else:
                startup.start_background(
                    "mcp",
                    lambda: mcp_manager.start_all(raise_required=True),
                    degrade_on_error=True,
                )
        except McpRequiredServerError:
            if mcp_manager is not None:
                await mcp_manager.shutdown()
            raise
        except Exception as exc:  # noqa: BLE001
            # Optional MCP failures are isolated and remain visible through
            # the manager when possible; a missing optional dependency must not
            # prevent a normal agenthicc session from starting.
            log.warning("MCP startup unavailable: %s", exc)

    from agenthicc.mentions.cache import MentionCache  # noqa: PLC0415

    mention_cache = MentionCache()

    startup.begin("memory")
    # PRD-129 Phase 2: durable conversation journal.  session_memory is a
    # JournaledShortTermMemory — every transition is fsync'd to a per-session
    # append-only journal, and on resume (session_id == resume_id) the journal
    # is folded straight back into memory.  This supersedes the old SQLite
    # memory-snapshot durability (which only checkpointed at turn boundaries).
    from agenthicc.runners.session_conversation import SessionConversation  # noqa: PLC0415

    session_conversation = SessionConversation.open(
        session_id,
        max_tokens=cfg.execution.effective_usable_budget(),
    )
    session_memory = session_conversation.memory

    # One browser manager and one opaque browser context per stable session
    # conversation.  Both the backend module and its tool closures are lazy so
    # creating a TUI never imports optional browser code.
    browser_tools: Iterable[object]
    if cfg.tools.browser_backend in {"playwright", "cloakbrowser"}:
        browser_settings = (
            cfg.tools.playwright
            if cfg.tools.browser_backend == "playwright"
            else cfg.tools.cloakbrowser
        )
        browser_manager = _LazyBrowserSession(
            cfg.tools.browser_backend,
            browser_settings,
            session_conversation.conversation_id,
            Path.cwd(),
            startup,
        )
        browser_tools = _LazyBrowserTools(cfg.tools.browser_backend, browser_manager, startup)
    else:
        browser_manager = None
        browser_tools = ()

    from agenthicc.runners.usage_ledger import UsageLedger  # noqa: PLC0415

    def _legacy_token_events() -> Iterator[object]:
        """Defer the legacy log scan until the ledger actually needs it."""
        yield from SessionEventLog.load(session_id, kinds={"tokens"})

    usage_ledger = UsageLedger.open(
        session_id,
        journal=session_conversation.journal,
        legacy_token_events=_legacy_token_events(),
        conversation_id=session_conversation.conversation_id,
    )
    usage_ledger.bind_conversation_store(app_state.conversation)

    # PRD-132 L1: install the durable, freshness-validated workspace file cache so
    # read_file resolves unchanged files from a per-project store instead of disk.
    if cfg.execution.file_cache:
        from agenthicc.tools.fs.file_cache import (  # noqa: PLC0415
            WorkspaceFileCache,
            configure_file_cache,
        )

        configure_file_cache(WorkspaceFileCache(Path(".agenthicc") / "cache" / "file-cache.db"))

    # ── three-tier memory (PRD-101) ───────────────────────────────────────────
    from agenthicc.memory.layers import (  # noqa: PLC0415
        ProjectMemoryLayer,
        GlobalMemoryLayer,
        SessionMemoryLayer,
    )
    from agenthicc.memory.router import MemoryRouter  # noqa: PLC0415

    _project_memory = ProjectMemoryLayer(Path(".agenthicc") / "memory" / "project.db")
    _global_memory = GlobalMemoryLayer()
    _session_layer = SessionMemoryLayer()
    _memory_router = MemoryRouter(
        session_layer=_session_layer,
        project_layer=_project_memory,
        global_layer=_global_memory,
    )
    _semantic_index = _LazySemanticIndex(startup)
    startup.finish("memory")

    # ── command registry + trigger registry ──────────────────────────────────
    from agenthicc.tui.trigger import TriggerManager  # noqa: PLC0415
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger  # noqa: PLC0415
    from agenthicc.tui.triggers.slash_command import (  # noqa: PLC0415
        SkillTrigger,
        SlashCommandTrigger,
    )
    from agenthicc.commands import build_builtin_registry  # noqa: PLC0415
    from agenthicc.commands.command import Command as _Cmd  # noqa: PLC0415

    cmd_registry = build_builtin_registry()
    for _spec in project_commands:
        try:
            if isinstance(_spec, _Cmd):
                cmd_registry.register(_spec)
            else:
                cmd_registry.register(
                    _Cmd(
                        name=getattr(_spec, "name"),
                        description=getattr(_spec, "description"),
                        aliases=tuple(getattr(_spec, "aliases", ())),
                        argument_hint=getattr(_spec, "argument_hint", ""),
                        group=getattr(_spec, "group", "Project"),
                        source_id="plugin",
                    )
                )
        except Exception:  # noqa: BLE001
            pass
    if project_commands:
        console.print(
            f"[dim]Loaded {len(project_commands)} project command(s) from .agenthicc/commands/[/dim]"
        )

    if not defer_extensions:
        startup.finish("extensions")

    _register_skill_commands(cmd_registry, skills)

    trigger_registry = TriggerManager()
    trigger_registry.register(AtMentionTrigger())
    trigger_registry.register(SlashCommandTrigger(cmd_registry, workflow_registry))
    trigger_registry.register(SkillTrigger(cmd_registry))

    # ── agent runner ──────────────────────────────────────────────────────────
    agent_runner = _build_agent_runner(
        llm_cfg,
        cassette_dir=cassette_dir,
    )
    if isinstance(agent_runner, _LazyAgentRunner):
        agent_runner._startup = startup

    # ── resume: restore previous context ─────────────────────────────────────
    # PRD-129 Phase 2: prior context is restored by folding the durable journal
    # at construction time (session_id == resume_id), so no explicit load is
    # needed here — only the visual marker.
    #
    # PRD-129 Phase 3: if the prior session died mid-turn (a turn_started with no
    # turn_completed), build a ResumePlan so the session can re-drive that turn
    # from where it left off — replaying already-completed tools.
    pending_resume = None
    if resume_id:
        from rich.rule import Rule  # noqa: PLC0415

        console.print(Rule(f"[dim]resumed session {session_id[:12]}[/dim]"))
        from agenthicc.runners.run_coordinator import RunCoordinator  # noqa: PLC0415

        _incomplete = RunCoordinator.detect_incomplete_turn(session_conversation.journal)
        if _incomplete is not None:
            pending_resume = RunCoordinator.build_resume_plan(
                session_conversation.journal, _incomplete
            )
    startup.finish("selected_restore")

    return SessionContext(
        processor=processor,
        app_state=app_state,
        session_log=session_log,
        approval_svc=approval_svc,
        mode_manager=mode_manager,
        command_bus=command_bus,
        workflow_registry=workflow_registry,
        agents_registry=agents_registry,
        cmd_registry=cmd_registry,
        trigger_registry=trigger_registry,
        agent_runner=cast("AgentRunnerBase", agent_runner),
        session_memory=session_memory,
        session_conversation=session_conversation,
        mention_cache=mention_cache,
        skills=skills,
        project_plugins=project_plugins,
        mcp_registry=mcp_registry,
        terminal_manager=terminal_manager,
        cfg=cfg,
        session_id=session_id,
        model_label=model_label,
        mcp_manager=mcp_manager,
        console=console,
        memory_router=_memory_router,
        semantic_index=cast("SemanticIndex", _semantic_index),
        pending_resume=pending_resume,
        command_plugin_names=command_plugin_names,
        session_service=session_service,
        kernel_projection_task=kernel_projection_task,
        usage_ledger=usage_ledger,
        browser_manager=cast("BrowserSessionManager | None", browser_manager),
        browser_tools=cast("Iterable[ToolLike]", browser_tools),
        cfg_overrides=tuple(cli_overrides or ()),
        cfg_secret_overrides=tuple(cli_secret_overrides or ()),
        initial_workflow=initial_workflow,
        workspace_scope=workspace_scope,
        workspace_access=workspace_access,
        resumed=bool(resume_id),
        owner_lease=owner_lease,
        startup=startup,
    )


# ── TUISession ────────────────────────────────────────────────────────────────


class TUISession:
    """All TUI session behaviour — methods correspond to the former nested closures."""

    def __init__(
        self,
        ctx: "SessionContext",
        workspace: "Workspace",
        input_session: "UnifiedInputSession",
    ) -> None:
        self._ctx = ctx
        self._workspace = workspace
        self._input_session = input_session

        # Mutable session state
        self._pending_skill_body: list[str] = []
        self._msg_queue: list[str] = []
        self._agent_task: asyncio.Task[object] | None = None
        self._deferred_startup_task: asyncio.Task[object] | None = None
        self._last_submitted_text: str = ""
        self._turn_count: int = 0
        self._pending_replay_id: str | None = None
        self._workflow_override: str | None = getattr(
            ctx, "initial_workflow", None
        )  # PRD-114: /workflow command
        if self._workflow_override is not None:
            ctx.app_state.conversation.workflow_override.set(self._workflow_override)
        self._workflow_handle: WorkflowRunHandle | None = None
        from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator  # noqa: PLC0415

        self._workflow_recovery = WorkflowRecoveryCoordinator(ctx.session_id)
        self._workflow_recovery_records: dict[str, WorkflowRecoveryRecord] = {}
        self._workflow_recovery_errors: dict[str, WorkflowRecoveryRecord] = {}
        self._workflow_owner_prefix = f"tui:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        terminal_manager = getattr(ctx, "terminal_manager", None)
        if terminal_manager is not None:
            self._terminal_unsub = terminal_manager.changed.subscribe(self._sync_terminal_status)
        else:
            self._terminal_unsub = lambda: None

        from agenthicc.commands import CommandDispatcher  # noqa: PLC0415
        from agenthicc.workflows.config import WorkflowConfig  # noqa: PLC0415

        self._cmd_dispatcher = CommandDispatcher(ctx.cmd_registry)
        # Built once per session; completed_turns is updated per run via replace().
        self._wf_config_base = WorkflowConfig(
            conv_store=ctx.app_state.conversation,
            app_state=ctx.app_state,
            processor=ctx.processor,
            agent_runner=ctx.agent_runner,
            approval_svc=ctx.approval_svc,
            cfg=ctx.cfg,
            skills=ctx.skills,
            plugin_tools=ctx.project_plugins,
            mcp_registry=ctx.mcp_registry,
            mention_cache=ctx.mention_cache,
            agents_registry=ctx.agents_registry,
            memory_router=ctx.memory_router,
            semantic_index=ctx.semantic_index,
            session_memory=ctx.session_memory,
            conversation_id=ctx.session_id,
            usage_ledger=getattr(ctx, "usage_ledger", None),
            browser_manager=getattr(ctx, "browser_manager", None),
            browser_tools=cast("Iterable[ToolLike]", getattr(ctx, "browser_tools", ())),
            workspace_scope=cast("WorkspaceScope | None", vars(ctx).get("workspace_scope")),
            workspace_access=cast(
                "WorkspaceAccessPolicy | None", vars(ctx).get("workspace_access")
            ),
            startup=getattr(ctx, "startup", None),
            next_queued_message=self._next_queued_message,
        )
        self._restore_paused_workflow()
        self._sync_terminal_status()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set_pending_skill(self, body: str) -> None:
        self._pending_skill_body.clear()
        self._pending_skill_body.append(body)

    def _set_pending_replay(self, session_id: str) -> None:
        self._pending_replay_id = session_id

    def _reset_workflow_to_mode_default(self) -> None:
        """Remove the session-local workflow override.

        Reset is a selection operation: the next ordinary turn must resolve
        its workflow from the active mode, even when recovery inspection has
        found a saved run that still needs an explicit run-id decision.
        """
        self._workflow_override = None
        self._ctx.app_state.conversation.workflow_override.set(None)

    def _activate_streaming_input(self) -> None:
        """Enable interruption before a newly created agent task can await.

        The activity state can render ``Thinking`` before the scheduled task
        gets its first event-loop slice.  Setting the mode at task ownership
        time closes that race, so an immediate ESC is handled as an interrupt.
        """
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        self._input_session.set_mode(InputMode.STREAMING)

    def _finalize_returned_workflow(self) -> None:
        """Close a custom workflow handle whose runner returned normally.

        Built-in runners explicitly mark their handle terminal. Downstream
        runners are allowed to focus on their own state machine, so the session
        owner supplies this final lifecycle boundary as a safety net. A normal
        return cannot leave a run looking active; otherwise the next request
        could accidentally reuse its run id and overwrite its checkpoint.
        """
        handle = self._workflow_handle
        workflow_run = self._ctx.app_state.workflow_run()
        if (
            handle is None
            or handle.lifecycle in {"complete", "discarded"}
            or (handle.lifecycle == "failed" and getattr(workflow_run, "status", None) != "failed")
        ):
            if handle is not None:
                self._release_workflow_claim(handle)
            return
        status = getattr(workflow_run, "status", None)
        if status == "failed":
            # A runner can convert a provider/phase exception into its typed
            # FAILED state and return normally. Keep a valid typed context
            # resumable; only the handle decides whether that is safe.
            context_error = (
                getattr(handle.context, "fail_reason", "") or "workflow returned unsuccessfully"
            )
            finalize = getattr(handle, "finalize_failure", None)
            checkpoint = (
                finalize(context_error, kind="phase_execution", recoverable=True)
                if callable(finalize)
                else None
            )
            if not callable(finalize):
                handle.mark_terminal("failed", error=context_error)
            if checkpoint is not None:
                if workflow_run is not None:
                    import dataclasses as _dc  # noqa: PLC0415

                    if _dc.is_dataclass(workflow_run):
                        self._ctx.app_state.workflow_run.set(
                            _dc.replace(
                                workflow_run,
                                status="paused",
                                current_phase=handle.current_phase,
                            )
                        )
                self._ctx.app_state.conversation.notify_transient(
                    f"⚠ Workflow '{handle.workflow_name}' paused after an error at "
                    f"{handle.current_phase or 'the current phase'}. Saved run "
                    f"{handle.run_id}; use /workflow resume {handle.run_id}."
                )
                self._publish_session_event(
                    "workflow_paused_after_error",
                    {
                        "run_id": handle.run_id,
                        "workflow": handle.workflow_name,
                        "phase": handle.current_phase or "",
                        "failure_kind": handle.failure_kind or "phase_execution",
                        "revision": checkpoint.revision,
                    },
                )
                self._release_workflow_claim(handle)
                return
        else:
            handle.mark_terminal("complete")
            if handle.checkpoint_supported:
                try:
                    handle.save_checkpoint(reason="complete")
                except Exception as exc:  # noqa: BLE001
                    handle.checkpoint_supported = False
                    handle._save_failure_diagnostic(  # noqa: SLF001
                        error=f"completion checkpoint failed: {type(exc).__name__}",
                        kind="checkpoint_storage",
                    )
        self._release_workflow_claim(handle)

    def _fail_workflow_run(
        self,
        error: str,
        *,
        kind: str = "workflow_error",
        recoverable: bool = True,
    ) -> None:
        """Publish and finalize a workflow failure that escaped its runner.

        A generated workflow can fail before ``run_phase()`` opens a
        ``ConversationStore`` turn (for example, while executing a lazy phase
        tool factory).  In that case ``close_turn(error=...)`` has no active
        turn to annotate, and the TUI otherwise appears to return to idle with
        no explanation.  Make the failure visible and ensure the failed handle
        cannot be accidentally reused by the next message.
        """
        conv = self._ctx.app_state.conversation
        if conv.is_turn_active:
            conv.close_turn(error=error)
        else:
            conv.append_event("error", {"message": error})

        handle = self._workflow_handle
        if handle is None:
            workflow_run = self._ctx.app_state.workflow_run()
            if workflow_run is not None:
                import dataclasses as _dc  # noqa: PLC0415

                if _dc.is_dataclass(workflow_run):
                    self._ctx.app_state.workflow_run.set(
                        _dc.replace(workflow_run, status="failed", current_phase=None)
                    )
            return

        finalize = getattr(handle, "finalize_failure", None)
        if callable(finalize):
            checkpoint = finalize(error, kind=kind, recoverable=recoverable)
        else:
            # Compatibility for third-party/in-test handles predating the
            # structured finalizer. They remain terminal rather than being
            # falsely advertised as resumable.
            handle.mark_terminal("failed", error=error)
            checkpoint = None
        workflow_run = self._ctx.app_state.workflow_run()
        if workflow_run is not None:
            import dataclasses as _dc  # noqa: PLC0415

            if _dc.is_dataclass(workflow_run):
                self._ctx.app_state.workflow_run.set(
                    _dc.replace(
                        workflow_run,
                        status="paused" if checkpoint is not None else "failed",
                        current_phase=handle.current_phase if checkpoint is not None else None,
                    )
                )
        if checkpoint is not None:
            self._ctx.app_state.conversation.notify_transient(
                f"⚠ Workflow '{handle.workflow_name}' paused after an error at "
                f"{handle.current_phase or 'the current phase'}. Saved run "
                f"{handle.run_id}; use /workflow resume {handle.run_id}."
            )
            self._publish_session_event(
                "workflow_paused_after_error",
                {
                    "run_id": handle.run_id,
                    "workflow": handle.workflow_name,
                    "phase": handle.current_phase or "",
                    "failure_kind": handle.failure_kind or kind,
                    "revision": checkpoint.revision,
                },
            )
        else:
            self._publish_session_event(
                "workflow_failure_diagnostic",
                {
                    "run_id": getattr(handle, "run_id", ""),
                    "workflow": getattr(handle, "workflow_name", ""),
                    "phase": getattr(handle, "current_phase", None) or "",
                    "failure_kind": getattr(handle, "failure_kind", None) or kind,
                    "resumable": False,
                },
            )

        # A recoverably paused handle remains attached for same-process resume;
        # terminal/diagnostic-only runs must not be reused by the next message.
        if checkpoint is not None:
            # The durable error disposition is complete; a later resume must
            # reacquire the run lease just like a process-restart resume.
            self._release_workflow_claim(handle)
        elif handle.lifecycle in {"complete", "failed", "discarded"}:
            self._release_workflow_claim(handle)
            self._workflow_handle = None

    def _restore_paused_workflow(self) -> None:
        """Discover recoverable workflow checkpoints for this session.

        Startup never claims or invokes a run.  It only validates checkpoints
        and attaches the sole valid candidate so `/workflow resume` can use the
        same path for Esc pauses and process-interrupted runs.
        """
        self._refresh_workflow_recovery_records()
        if not self._workflow_recovery_records and not self._workflow_recovery_errors:
            return

        candidates = list(self._workflow_recovery_records.values())
        self._publish_session_event(
            "workflow_recovery_available",
            {
                "count": len(candidates),
                "run_ids": [record.run_id for record in candidates],
                "invalid_run_ids": list(self._workflow_recovery_errors),
            },
        )
        if not candidates:
            for record in self._workflow_recovery_errors.values():
                self._ctx.app_state.conversation.notify_transient(
                    f"⚠ Workflow recovery unavailable for {record.run_id}: {record.display_error}"
                )
            return
        if len(candidates) != 1:
            # The footer wraps long notifications. Keep the complete IDs here
            # so an operator can copy an exact value into `/workflow resume`.
            choices = ", ".join(record.run_id for record in candidates[:5])
            suffix = " …" if len(candidates) > 5 else ""
            self._ctx.app_state.conversation.notification.set(
                f"{len(candidates)} workflows can be resumed ({choices}{suffix}). "
                "Use /workflow resume <run-id>."
            )
            return

        record = candidates[0]
        try:
            self._workflow_handle = self._rehydrate_workflow_record(record, claim=False)
            checkpoint = record.checkpoint
            assert checkpoint is not None
            disposition = "interrupted" if record.interrupted else "paused"
            self._ctx.app_state.conversation.notification.set(
                f"Workflow '{checkpoint.workflow_name}' is {disposition} at "
                f"{checkpoint.current_phase or 'its saved state'}. "
                f"Use /workflow resume {record.run_id} to continue."
            )
        except Exception as exc:  # noqa: BLE001
            self._ctx.app_state.conversation.notify_transient(
                f"⚠ Cannot restore workflow '{record.run_id}': {type(exc).__name__}: {exc}"
            )

    def _refresh_workflow_recovery_records(self) -> None:
        """Reload durable workflow records before an explicit resume lookup.

        Startup discovery is intentionally non-claiming, but it is only a
        snapshot. A workflow may have written its first checkpoint after the
        TUI was constructed, or another owner may have released a claim since
        the last lookup. Explicit resume commands therefore refresh from the
        checkpoint store before reporting ``run_not_found``.
        """
        conversation = getattr(self._ctx, "session_conversation", None)
        if conversation is None:
            return
        active_profile = getattr(self._ctx.cfg.execution, "profile", "")
        workspace_scope = getattr(self._ctx, "workspace_scope", None)
        workspace_root = str(getattr(workspace_scope, "primary_root", "") or "")
        records = self._workflow_recovery.inspect(
            workflow_registry=self._ctx.workflow_registry,
            conversation=conversation,
            provider_profile=active_profile,
            workspace_root=workspace_root,
        )
        self._workflow_recovery_records = {
            record.run_id: record for record in records if record.recoverable
        }
        self._workflow_recovery_errors = {
            record.run_id: record for record in records if not record.recoverable
        }

    def _list_workflow_runs(self) -> list[WorkflowRecoveryRecord]:
        """Return current recoverable runs in newest-checkpoint-first order."""
        self._refresh_workflow_recovery_records()
        records = list(self._workflow_recovery_records.values())
        return sorted(
            records,
            key=lambda record: (
                -(record.checkpoint.created_at if record.checkpoint is not None else 0.0),
                record.run_id,
            ),
        )

    def _resume_workflow_from_overlay(self, run_id: str) -> bool:
        """Resume a selected run through the canonical guarded resume path."""
        return self._handle_workflow_resume(run_id)

    def _known_workflow_run_ids(self) -> set[str]:
        """Return IDs currently known to this TUI, including an attached run."""
        known = set(self._workflow_recovery_records)
        known.update(self._workflow_recovery_errors)
        if self._workflow_handle is not None:
            known.add(self._workflow_handle.run_id)
        return known

    def _resolve_workflow_run_id(self, value: str) -> str:
        """Resolve a user-copied workflow ID to one canonical stored ID.

        The exact ID always wins. For a unique match only, accept the owner
        token printed in a claim diagnostic and common terminal-font confusions
        (``O``/``0`` and ``l``/``1``). Ambiguous values remain unresolved so
        this convenience can never select the wrong workflow.
        """
        requested = value.strip().strip("`'\"")
        known = self._known_workflow_run_ids()
        if requested in known:
            return requested
        case_matches = [
            candidate for candidate in known if candidate.casefold() == requested.casefold()
        ]
        if len(case_matches) == 1:
            return case_matches[0]

        owner_tail = requested.rsplit(":", 1)[-1] if ":" in requested else ""
        aliases = [owner_tail] if owner_tail else []
        aliases.append(requested)
        visual_aliases = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})
        for alias in aliases:
            normalized = alias.translate(visual_aliases).casefold()
            if normalized == alias.casefold():
                continue
            matches = [candidate for candidate in known if candidate.casefold() == normalized]
            if len(matches) == 1:
                return matches[0]
        return requested

    def _rehydrate_workflow_record(
        self,
        record: "WorkflowRecoveryRecord",
        *,
        claim: bool,
    ) -> "WorkflowRunHandle":
        """Restore one validated record and optionally claim it for this TUI."""
        conversation = getattr(self._ctx, "session_conversation", None)
        if conversation is None or record.checkpoint is None:
            raise ValueError(record.display_error)
        workflow = self._ctx.workflow_registry.get(record.workflow_name)
        if workflow is None:
            raise ValueError(f"workflow {record.workflow_name!r} is not loaded")
        owner_id = f"{self._workflow_owner_prefix}:{record.run_id}" if claim else None
        return self._workflow_recovery.rehydrate(
            record,
            workflow=workflow,
            conversation=conversation,
            browser_manager=getattr(self._ctx, "browser_manager", None),
            owner_id=owner_id,
        )

    def _release_workflow_claim(self, handle: "WorkflowRunHandle | None") -> None:
        """Release a terminal run's durable lease without masking cleanup errors."""
        if handle is None:
            return
        try:
            handle.release_claim()
        except Exception:  # noqa: BLE001 — terminal cleanup must not mask the result
            pass

    def _publish_session_event(
        self,
        kind: str,
        payload: dict[str, object] | None = None,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Send a TUI lifecycle event to the client-neutral projection."""

        service = getattr(self._ctx, "session_service", None)
        if service is None:
            return
        try:
            task = asyncio.create_task(
                service.publish(
                    self._ctx.session_id,
                    source="tui",
                    kind=kind,
                    payload=payload or {},
                    turn_id=turn_id,
                )
            )
        except RuntimeError:
            return

        def _consume_projection_error(done: asyncio.Task[object]) -> None:
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_consume_projection_error)

    def _wire_approval_overlay(self) -> None:
        workspace = self._workspace
        approval_svc = self._ctx.approval_svc
        app_state = self._ctx.app_state

        def _on_approval_change() -> None:
            req = app_state.pending_approval()
            from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay  # noqa: PLC0415
            from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay  # noqa: PLC0415
            from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay  # noqa: PLC0415

            # Registry maps ApprovalRequest.kind → overlay class.
            # Add new overlay kinds by extending this dict — no if/elif needed.
            _overlay_registry = {
                "plan_review": PlanApprovalOverlay,
                "questions": QuestionsOverlay,
            }
            _overlay_default = ApprovalOverlay

            if req is not None:
                kind = getattr(req, "kind", "tool")
                factory = _overlay_registry.get(kind, _overlay_default)
                workspace.overlays.show(factory(req, approval_svc, workspace.overlays.hide))
            else:
                if isinstance(
                    workspace.overlays.widget,
                    tuple(_overlay_registry.values()) + (_overlay_default,),
                ):
                    workspace.overlays.hide()

        app_state.pending_approval.subscribe(_on_approval_change)

    def _sync_terminal_status(self) -> None:
        """Project the current terminal wait into the reactive status bar."""

        terminal_manager = getattr(self._ctx, "terminal_manager", None)
        if terminal_manager is None:
            return
        snapshot = terminal_manager.wait_snapshot()
        conversation = self._ctx.app_state.conversation
        if snapshot is None:
            conversation.clear_terminal_wait()
            redraw = getattr(self._workspace, "_redraw", None)
            if callable(redraw):
                redraw()
            return
        elapsed_value = snapshot.get("elapsed_s", 0.0)
        count_value = snapshot.get("running_count", 0)
        conversation.set_terminal_wait(
            terminal_id=str(snapshot["terminal_id"]),
            label=str(snapshot["label"]),
            elapsed_s=float(elapsed_value) if isinstance(elapsed_value, (int, float)) else 0.0,
            running_count=int(count_value) if isinstance(count_value, int) else 0,
        )
        redraw = getattr(self._workspace, "_redraw", None)
        if callable(redraw):
            redraw()

    def _start_deferred_startup(self) -> None:
        """Start optional discovery only after the first Live frame exists."""
        from agenthicc.runners.startup import ReadinessState  # noqa: PLC0415

        startup = getattr(self._ctx, "startup", None)
        if startup is None or startup.report("extensions").state is not ReadinessState.NOT_STARTED:
            return

        async def load() -> object:
            discovery, workflow_registry = await asyncio.gather(
                asyncio.to_thread(_discover_extensions_sync, self._ctx.cfg),
                asyncio.to_thread(_discover_workflow_registry_sync),
            )
            # Shutdown can win while the worker threads are unwinding. Never
            # publish a discovery result into a closed session's registries.
            if startup.closed:
                return None
            _apply_extension_discovery(self._ctx, discovery)
            self._ctx.workflow_registry.replace_with(workflow_registry)
            self._ctx.mode_manager.refresh_workflow_bindings(
                workflow_registry.mode_default_map(),
                workflow_registry.mode_available_map(),
            )
            return None

        self._deferred_startup_task = startup.start_background(
            "extensions",
            load,
            degrade_on_error=True,
        )

    # ── public routing ────────────────────────────────────────────────────────

    def dispatch_slash(self, text: str) -> bool:
        """Dispatch a registered command or skill trigger."""
        from agenthicc.commands import CommandContext  # noqa: PLC0415
        from agenthicc.plugins.registry import build_registry  # noqa: PLC0415

        ctx = self._ctx
        project_tools: list[ToolLike] = list(ctx.project_plugins.all_tools)
        # Most slash commands are local/read-only and do not need browser
        # tool schemas. Iterating the session proxy here would import an
        # optional browser package merely to run /status or /startup. The
        # explicit /tools view is the user-facing browser-tool selection
        # boundary; agent/workflow turns iterate it at their own dependency
        # boundary below.
        command_name = text.strip().split(None, 1)[0] if text.strip() else ""
        if command_name == "/tools":
            project_tools.extend(getattr(ctx, "browser_tools", ()))
        if ctx.mcp_registry is not None:
            project_tools.extend(ctx.mcp_registry.all_tools())
        tool_registry = build_registry(project_plugin_tools=project_tools)
        context = CommandContext(
            text=text,
            args=" ".join(text.split()[1:]),
            model=ctx.model_label,
            console=ctx.console,
            config=ctx.cfg,
            session_id=ctx.session_id,
            skills=ctx.skills,
            active_agent="default",
            command_registry=ctx.cmd_registry,
            tools=tool_registry.tools,
            tool_sources=tool_registry.sources,
            workflow_registry=ctx.workflow_registry,
            mcp_manager=getattr(ctx, "mcp_manager", None),
            startup=getattr(ctx, "startup", None),
            mode_manager=ctx.mode_manager,
            terminal_manager=getattr(ctx, "terminal_manager", None),
            set_pending_skill=self._set_pending_skill,
            set_pending_menu=self._workspace.overlays.show,
            close_overlay=self._workspace.overlays.hide,
            set_pending_replay=self._set_pending_replay,
            reload_skills=self._reload_skills,
            reload_commands=self._reload_commands,
            reload_tools=self._reload_tools,
            reload_workflows=self._reload_workflows,
            set_input_text=self._set_input_text,
            usage_snapshot=self._usage_snapshot,
            cancel_active=self._cancel_active_task,
            list_workflow_runs=self._list_workflow_runs,
            resume_workflow=self._resume_workflow_from_overlay,
        )
        return bool(self._cmd_dispatcher.dispatch(text, context))

    def _set_input_text(self, text: str) -> None:
        """Put a registry selection in the composer without submitting it."""
        self._input_session.set_text(text)

    def _usage_snapshot(self) -> "UsageSnapshot":
        """Read one local usage snapshot without touching the active runner."""
        from agenthicc.commands.command import UsageSnapshot  # noqa: PLC0415

        conversation = self._ctx.app_state.conversation
        task = self._agent_task
        ledger = getattr(self._ctx, "usage_ledger", None)
        if ledger is not None:
            ledger_snapshot = ledger.snapshot()
            return UsageSnapshot(
                input_tokens=ledger_snapshot.input_tokens,
                output_tokens=ledger_snapshot.output_tokens,
                cost_usd=ledger_snapshot.cost_usd,
                active_run=bool(task is not None and not task.done()),
                queue_depth=len(self._msg_queue),
                usage_status=ledger_snapshot.usage_status,
                cost_status=ledger_snapshot.cost_status,
                calls=ledger_snapshot.calls,
                known_calls=ledger_snapshot.known_calls,
                unavailable_calls=ledger_snapshot.unavailable_calls,
                provisional_calls=ledger_snapshot.provisional_calls,
                durability_status=ledger_snapshot.durability_status,
            )
        return UsageSnapshot(
            input_tokens=int(conversation.tokens_in()),
            output_tokens=int(conversation.tokens_out()),
            cost_usd=float(conversation.cost_usd()),
            active_run=bool(task is not None and not task.done()),
            queue_depth=len(self._msg_queue),
        )

    def _cancel_active_task(self) -> bool:
        """Request cancellation through the same owner used by Ctrl+C."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        task = self._agent_task
        if task is None or task.done():
            # A cancellation request can race the task's finalizer.  Do not
            # leave the input pipeline in STREAMING while the task reference
            # is already finished; otherwise Ctrl+C is treated as another
            # interrupt instead of the idle exit sequence.
            self._input_session.set_mode(InputMode.IDLE)
            return False
        # Esc/Ctrl+C first stops the owned terminal currently awaited by this
        # turn, then cancels the agent task.  The process group therefore does
        # not outlive a cancelled foreground wait.
        terminal_manager = getattr(self._ctx, "terminal_manager", None)
        try:
            if terminal_manager is not None:
                terminal_manager.request_stop_current()
            return task.cancel()
        finally:
            # Switch immediately.  The task's finally block also sets IDLE,
            # but waiting for that callback creates a race with a quick
            # ESC → Ctrl+C exit attempt on Windows.
            self._input_session.set_mode(InputMode.IDLE)

    def _busy_decision(self, text: str) -> "BusyDecision":
        from agenthicc.commands.busy_policy import classify_busy_command  # noqa: PLC0415

        return classify_busy_command(text, self._ctx.cmd_registry)

    def _notify_queued(self, text: str) -> None:
        label = text[:40] + ("…" if len(text) > 40 else "")
        position = len(self._msg_queue)
        self._ctx.app_state.conversation.notification.set(f"⌛ Queued #{position}: {label}")

    def _next_queued_message(self) -> str | None:
        """Claim the next plain-text message at a completed tool boundary.

        The active agent loop owns the shared memory, so a queued message can
        be appended safely only after its tool results have been committed and
        before the loop's next model request. Slash commands and skills stay at
        the head of the FIFO until the turn is idle, where ``advance()`` can
        route them locally instead of sending their command spelling to the
        model.
        """
        if not self._msg_queue:
            return None
        text = self._msg_queue[0].strip()
        if not text or text.startswith(("/", "$")):
            return None
        self._msg_queue.pop(0)
        self._ctx.app_state.conversation.append_event("user_message", {"text": text})
        if self._msg_queue:
            self._notify_queued(self._msg_queue[0])
        else:
            self._ctx.app_state.conversation.notification.set(None)
        return text

    def _notify_busy_rejected(self, text: str, reason: str) -> None:
        command = text.split(None, 1)[0] if text.split(None, 1) else text
        self._ctx.app_state.conversation.notification.set(
            f"⛔ Rejected while busy: {command} — {reason or 'try again after the run'}"
        )

    def _run_busy_immediate(self, text: str, decision: "BusyDecision") -> None:
        """Run one approved local command through the normal route/dispatcher."""
        try:
            handled = self.route(text)
        except Exception as exc:  # noqa: BLE001
            self._ctx.app_state.conversation.notification.set(
                f"⚠ Command failed locally: {type(exc).__name__}: {exc}"
            )
            return
        if not handled:
            # A registry/plugin race cannot fall through to the LPM.
            self._ctx.app_state.conversation.notification.set(
                f"⚠ Command unavailable while busy: {decision.command_name}"
            )
            return
        self._ctx.app_state.conversation.notify_transient(f"▶ Ran now: {decision.command_name}")

    def _reload_skills(self) -> "SkillDiscoveryResult":
        """Rescan skill directories and refresh skill-owned dollar commands."""
        from agenthicc.skills.loader import discover_skills_with_diagnostics  # noqa: PLC0415

        cfg = self._ctx.cfg
        global_dir = (
            Path(cfg.skills.default_skill_directory).expanduser()
            if cfg.skills.default_skill_directory
            else Path.home() / ".agenthicc"
        )
        discovery = discover_skills_with_diagnostics(
            project_dir=Path(".agenthicc"),
            user_dir=global_dir,
        )

        # Build all replacement commands before mutating the live session. If
        # discovery or command construction fails, the current session remains
        # usable and the caller can report the failure.
        replacement_commands = [
            (skill, _build_skill_command(slug, skill)) for slug, skill in discovery.skills.items()
        ]
        registry = self._ctx.cmd_registry
        skill_sources = {
            command.source_id
            for command in registry.all_commands()
            if command.source_id.startswith("skill:")
        }
        for source_id in skill_sources:
            registry.unregister_source(source_id)

        # Preserve the dictionary object because workflow configuration and
        # command contexts keep references to this session-owned mapping.
        self._ctx.skills.clear()
        self._ctx.skills.update(discovery.skills)
        conflicts: list[tuple[Path, str]] = []
        for skill, command in replacement_commands:
            if any(registry.get(name) is not None for name in (command.name, *command.aliases)):
                conflicts.append(
                    (
                        skill.path,
                        f"{command.name}: command name or alias conflicts with an existing command",
                    )
                )
                continue
            registry.register(command)

        if conflicts:
            from agenthicc.skills.loader import SkillDiagnostic, SkillDiscoveryResult  # noqa: PLC0415

            discovery = SkillDiscoveryResult(
                skills=discovery.skills,
                diagnostics=discovery.diagnostics
                + tuple(
                    SkillDiagnostic(
                        path=path,
                        code="command-conflict",
                        message=message,
                        severity="warning",
                    )
                    for path, message in conflicts
                ),
            )
        return discovery

    def _reload_tools(self) -> tuple[bool, str]:
        """Rescan plugins and publish all tools that loaded successfully.

        Tool files are independent load units. A broken or dependency-missing
        file is reported, but must not hide tools from healthy files. If no
        file can be loaded at all because discovery returned failures, retain
        the current registry rather than replacing it with an empty one.
        """
        from agenthicc.plugins.discovery import discover_project_tools, warn_conflicts

        try:
            discovered = discover_project_tools(
                project_dir=Path(".agenthicc"),
                user_dir=Path.home() / ".agenthicc",
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"Tool reload failed; existing tools kept: {type(exc).__name__}: {exc}"

        failures: list[str] = []
        for result in discovered.failed:
            reason = result.error or ("missing dependencies: " + ", ".join(result.missing_deps))
            failures.append(f"{result.path}: {reason}")

        # Publish partial discovery results. The discovery layer attempts
        # every file, so a broken optional plugin must not hide healthy ones.
        if failures and not discovered.all_tools:
            return False, "Tool reload failed; existing tools kept:\n" + "\n".join(failures)

        old_count = len(self._ctx.project_plugins.all_tools)
        warn_conflicts(discovered)
        self._ctx.project_plugins = discovered

        # WorkflowConfig is session-owned and frozen; replace its plugin set so
        # subsequent workflow turns see the same freshly loaded tools.
        import dataclasses

        self._wf_config_base = dataclasses.replace(
            self._wf_config_base,
            plugin_tools=discovered,
        )
        new_count = len(discovered.all_tools)
        message = f"Tools reloaded — {new_count} tool(s) available (was {old_count})."
        if failures:
            message += (
                f" Skipped {len(failures)} plugin(s) that could not be loaded:\n"
                + "\n".join(failures)
            )
        return True, message

    def _reload_workflows(self) -> tuple[bool, str]:
        """Rebuild workflows and replace the live registry in place."""
        from agenthicc.workflows.registry import build_workflow_registry

        try:
            discovered = build_workflow_registry(
                project_dir=Path(".agenthicc"),
                user_dir=Path.home() / ".agenthicc",
            )
        except Exception as exc:  # noqa: BLE001
            return False, (
                f"Workflow reload failed; existing workflows kept: {type(exc).__name__}: {exc}"
            )

        registry = self._ctx.workflow_registry
        old_names = set(registry.names())
        new_names = set(discovered.names())
        registry.replace_with(discovered)

        if self._workflow_override is not None and self._workflow_override not in new_names:
            self._workflow_override = None
            self._ctx.app_state.conversation.workflow_override.set(None)

        changes: list[str] = []
        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        if added:
            changes.append(f"added: {', '.join(added)}")
        if removed:
            changes.append(f"removed: {', '.join(removed)}")
        suffix = "; ".join(changes) if changes else "no workflow changes"
        return True, f"Workflows reloaded — {len(new_names)} workflow(s); {suffix}."

    def _reload_commands(self) -> tuple[bool, str]:
        """Rescan slash-command plugins and publish a valid set atomically."""
        from agenthicc.commands import build_builtin_registry  # noqa: PLC0415
        from agenthicc.commands.plugin_loader import discover_command_plugins  # noqa: PLC0415

        try:
            discovered = discover_command_plugins(
                project_dir=Path(".agenthicc"),
                user_dir=Path.home() / ".agenthicc",
            )
        except Exception as exc:  # noqa: BLE001
            return (
                False,
                f"Command reload failed; existing commands kept: {type(exc).__name__}: {exc}",
            )

        if discovered.failed:
            failures: list[str] = []
            for result in discovered.failed:
                if result.error:
                    reason = result.error
                else:
                    reason = "missing dependencies: " + ", ".join(result.missing_deps)
                failures.append(f"{result.path}: {reason}")
            return (
                False,
                "Command reload failed; existing commands kept:\n" + "\n".join(failures),
            )

        registry = self._ctx.cmd_registry
        old_plugin_names = set(getattr(self._ctx, "command_plugin_names", set()))
        old_plugins = {
            command.name: command
            for command in registry.all_commands()
            if command.name in old_plugin_names
        }
        new_plugins = {command.name: command for command in discovered.all_commands}

        # Preserve commands registered by other extension surfaces (for
        # example MCP) while rebuilding the stable built-in/skill/plugin order.
        preserved = [
            command
            for command in registry.all_commands()
            if command.name not in old_plugin_names
            and command.source_id != "builtin"
            and not command.source_id.startswith("skill:")
        ]

        candidate = build_builtin_registry()
        for command in preserved:
            candidate.register(command)
        candidate.register_many(discovered.all_commands)
        _register_skill_commands(candidate, self._ctx.skills)

        # Only publish after discovery, validation, and candidate construction
        # have all succeeded. Consumers keep the same registry object.
        registry.replace_with(candidate)
        new_plugin_names = set(new_plugins)
        command_plugin_names = getattr(self._ctx, "command_plugin_names", None)
        if command_plugin_names is None:
            self._ctx.command_plugin_names = set(new_plugin_names)
        else:
            command_plugin_names.clear()
            command_plugin_names.update(new_plugin_names)

        added = sorted(new_plugin_names - old_plugin_names)
        removed = sorted(old_plugin_names - new_plugin_names)
        updated = sorted(set(old_plugins) & new_plugin_names)
        summary: list[str] = []
        if added:
            summary.append(f"added: {', '.join(added)}")
        if updated:
            summary.append(f"updated: {', '.join(updated)}")
        if removed:
            summary.append(f"removed: {', '.join(removed)}")
        if not summary:
            summary.append("no command changes")
        return True, "Commands reloaded — " + "; ".join(summary)

    def _handle_workflow_command(self, args: str) -> bool:
        """Handle /workflow <name> | reset | resume [run-id]."""
        name = args.strip()
        conv = self._ctx.app_state.conversation
        if name == "resume" or name.startswith("resume "):
            run_id = name.partition(" ")[2].strip() or None
            return self._handle_workflow_resume(run_id)
        if not name or name == "reset" or name.startswith("reset "):
            requested_reset_id = name.partition(" ")[2].strip() or None
            # `/workflow reset` must always clear the session-local selector.
            # Recovery records are a separate durable-run decision and must
            # not prevent the active workflow from returning to the mode
            # default.
            if requested_reset_id is None:
                self._reset_workflow_to_mode_default()
            handle = self._workflow_handle
            if requested_reset_id is not None and (
                handle is None or handle.run_id != requested_reset_id
            ):
                if handle is not None and handle.lifecycle in {"paused", "pausing"}:
                    conv.notify_transient(
                        f"⚠ '{handle.workflow_name}' is already paused. Reset it first "
                        "before selecting another workflow."
                    )
                    return True
                record = self._workflow_recovery_records.get(requested_reset_id)
                if record is None:
                    record = self._workflow_recovery_errors.get(requested_reset_id)
                if record is None:
                    conv.notify_transient(
                        f"⚠ run_not_found: no recoverable workflow with run id {requested_reset_id!r}"
                    )
                    return True
                if not record.recoverable:
                    owner_id = f"{self._workflow_owner_prefix}:{record.run_id}"
                    try:
                        discarded = self._workflow_recovery.discard(
                            record,
                            owner_id=owner_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        conv.notify_transient(
                            f"⚠ Cannot reset workflow {requested_reset_id!r}: "
                            f"{record.error_code or 'checkpoint_corrupt'}: {exc}"
                        )
                        return True
                    self._workflow_recovery_errors.pop(record.run_id, None)
                    self._publish_session_event(
                        "workflow_discarded",
                        {
                            "run_id": record.run_id,
                            "workflow": discarded.workflow_name,
                            "status": discarded.status,
                        },
                    )
                    conv.notify_transient(
                        f"↩ Workflow '{discarded.workflow_name}' reset; incompatible checkpoint discarded"
                    )
                    return True
                try:
                    handle = self._rehydrate_workflow_record(record, claim=True)
                    self._workflow_handle = handle
                except Exception as exc:  # noqa: BLE001
                    conv.notify_transient(
                        f"⚠ Cannot restore workflow {requested_reset_id!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return True
            if handle is None and self._workflow_recovery_records:
                choices = ", ".join(self._workflow_recovery_records)
                conv.notify_transient(
                    "⚠ Select a recoverable workflow explicitly before resetting: "
                    f"/workflow reset <run-id> ({choices})"
                )
                return True
            if handle is not None and handle.lifecycle in {"paused", "pausing"}:
                handle.mark_terminal("discarded", error="reset by user")
                if handle.checkpoint_supported:
                    try:
                        handle.save_checkpoint(reason="reset")
                    except Exception as exc:  # noqa: BLE001
                        self._release_workflow_claim(handle)
                        conv.notify_transient(f"⚠ Could not persist workflow reset: {exc}")
                        return True
                    from agenthicc.kernel import Event  # noqa: PLC0415

                    asyncio.create_task(
                        self._ctx.processor.emit(
                            Event.create(
                                "WorkflowRunDiscarded",
                                {
                                    "run_id": handle.run_id,
                                    "workflow_name": handle.workflow_name,
                                },
                            )
                        )
                    )
                self._release_workflow_claim(handle)
                self._publish_session_event(
                    "workflow_discarded",
                    {"run_id": handle.run_id, "workflow": handle.workflow_name},
                )
                self._workflow_handle = None
                conv.notify_transient("↩ Paused workflow discarded; workflow reset to mode default")
                self._reset_workflow_to_mode_default()
                return True
            self._reset_workflow_to_mode_default()
            conv.notify_transient("↩ Workflow reset to mode default")
            return True
        defn = self._ctx.workflow_registry.get(name)
        if defn is None:
            available = ", ".join(self._ctx.workflow_registry.names()) or "none"
            conv.notify_transient(f"⚠ Unknown workflow: {name!r}  (available: {available})")
            return True
        if (
            self._workflow_handle is not None
            and self._workflow_handle.lifecycle in {"paused", "pausing"}
            and self._workflow_handle.workflow_name != name
        ):
            conv.notify_transient(
                f"⚠ '{self._workflow_handle.workflow_name}' is paused. Resume or reset it before switching workflows."
            )
            return True
        self._workflow_override = name
        conv.workflow_override.set(name)
        conv.notify_transient(f"⚡ Workflow → {name}")
        return True

    def _handle_workflow_resume(self, run_id: str | None) -> bool:
        """Claim and resume one paused or process-interrupted workflow."""
        conv = self._ctx.app_state.conversation
        if self._agent_task is not None and not self._agent_task.done():
            conv.notify_transient("⚠ Cannot resume a workflow while another run is active")
            return True
        handle = self._workflow_handle
        if run_id is not None:
            # Recovery records are a discovery snapshot.  If this command is
            # selecting a run that is not the live in-memory handle, refresh
            # before resolving the ID so a second pause/resume cycle cannot
            # rehydrate an older context or a checkpoint that has since become
            # terminal.  Keep the attached handle fast-path for same-process
            # Esc pause/resume; it is already the authoritative live context.
            if handle is None or handle.run_id != run_id:
                self._refresh_workflow_recovery_records()
            run_id = self._resolve_workflow_run_id(run_id)
        if run_id is not None and (handle is None or handle.run_id != run_id):
            if handle is not None and handle.lifecycle in {"paused", "pausing"}:
                conv.notify_transient(
                    f"⚠ '{handle.workflow_name}' is already paused. Resume or reset it "
                    "before selecting another workflow."
                )
                return True
            record = self._workflow_recovery_records.get(run_id)
            if record is None:
                # This also covers a run ID copied from a claim diagnostic
                # after a process handoff when the discovery snapshot did not
                # contain the exact spelling.
                self._refresh_workflow_recovery_records()
                run_id = self._resolve_workflow_run_id(run_id)
                record = self._workflow_recovery_records.get(run_id)
            if record is None:
                invalid = self._workflow_recovery_errors.get(run_id)
                if invalid is not None:
                    conv.notify_transient(
                        f"⚠ Cannot resume {run_id!r}: {invalid.error_code}: {invalid.display_error}"
                    )
                else:
                    known = sorted(self._known_workflow_run_ids())
                    available = f" Available IDs: {', '.join(known[:8])}." if known else ""
                    conv.notify_transient(
                        f"⚠ run_not_found: no recoverable workflow with run id {run_id!r}."
                        f"{available}"
                    )
                return True
            try:
                handle = self._rehydrate_workflow_record(record, claim=False)
                self._workflow_handle = handle
            except Exception as exc:  # noqa: BLE001
                conv.notify_transient(
                    f"⚠ Cannot restore workflow {run_id!r}: {type(exc).__name__}: {exc}"
                )
                return True

        if handle is None:
            candidates = list(self._workflow_recovery_records.values())
            if len(candidates) == 1:
                try:
                    handle = self._rehydrate_workflow_record(candidates[0], claim=False)
                    self._workflow_handle = handle
                except Exception as exc:  # noqa: BLE001
                    conv.notify_transient(
                        f"⚠ Cannot restore workflow {candidates[0].run_id!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return True
            elif candidates:
                choices = ", ".join(record.run_id for record in candidates[:8])
                suffix = " …" if len(candidates) > 8 else ""
                conv.notify_transient(
                    "⚠ Multiple workflows can be resumed. Choose one explicitly: "
                    f"/workflow resume <run-id> ({choices}{suffix})"
                )
                return True

        if handle is None or handle.lifecycle not in {"paused", "pausing"}:
            conv.notify_transient(
                "⚠ no_recoverable_workflow: no saved workflow is available to resume. "
                "Use /workflow reset only if you want to discard the saved run."
            )
            return True
        if not handle.checkpoint_supported:
            conv.notify_transient(
                "⚠ custom_context_codec_missing: this workflow cannot safely resume its "
                "saved context; update the workflow or use /workflow reset."
            )
            return True
        definition = self._ctx.workflow_registry.get(handle.workflow_name)
        if definition is None or handle.context is None:
            conv.notify_transient(f"⚠ Workflow '{handle.workflow_name}' is not available to resume")
            return True
        if handle.claim_owner_id is None:
            try:
                handle.claim(f"{self._workflow_owner_prefix}:{handle.run_id}")
            except Exception as exc:  # noqa: BLE001
                from agenthicc.runners.workflow_checkpoint_store import WorkflowClaimError  # noqa: PLC0415

                code = (
                    "run_already_claimed"
                    if isinstance(exc, WorkflowClaimError)
                    else "resume_transition_failed"
                )
                if isinstance(exc, WorkflowClaimError):
                    conv.notify_transient(
                        f"⚠ {code}: cannot claim workflow {handle.run_id!r}: {exc}"
                    )
                else:
                    conv.notify_transient(
                        f"⚠ {code}: cannot claim workflow {handle.run_id!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                return True
        try:
            # Move to RESUMING before creating the task. This closes the small
            # race where Esc arrives after the command claims the run but
            # before the task reaches its first await; request_pause() can
            # then convert RESUMING to PAUSING instead of misclassifying the
            # cancellation as a failure.
            handle.mark_resuming()
            handle.persist_checkpoint(reason="resuming")
        except Exception as exc:  # noqa: BLE001
            self._release_workflow_claim(handle)
            conv.notify_transient(
                f"⚠ resume_transition_failed: cannot start workflow {handle.run_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            return True
        self._activate_streaming_input()
        self._publish_session_event(
            "workflow_resume_started",
            {
                "run_id": handle.run_id,
                "workflow": handle.workflow_name,
                "phase": handle.current_phase or "",
            },
        )
        self._agent_task = asyncio.create_task(
            self._resume_workflow_task(definition, handle.context),
            name=f"resume-workflow-{handle.run_id}",
        )
        conv.notify_transient(f"↻ Resuming workflow '{handle.workflow_name}'…")
        return True

    async def _handle_compact_command(self) -> None:
        """Handle /compact — compact the current session memory (PRD-119)."""
        from agenthicc.memory.compactor import compact_memory  # noqa: PLC0415

        ctx = self._ctx
        conv = ctx.app_state.conversation
        mem = ctx.session_memory

        if mem is None or not mem._messages:
            conv.notify_transient("⎋ Nothing to compact")
            return

        transport = getattr(ctx.agent_runner, "_transport", None)
        if transport is None:
            conv.notify_transient("⚠ No transport available for compaction")
            return

        model = ctx.cfg.execution.effective_model()
        # Bound each summariser call to the model window so compacting a history
        # larger than the window map-reduces instead of overflowing (PRD-135 B).
        await compact_memory(
            mem,
            transport,
            model=model,
            conv_store=conv,
            max_input_tokens=ctx.cfg.execution.effective_context_window(),
            usage_ledger=getattr(ctx, "usage_ledger", None),
            session_id=ctx.session_id,
            run_id=f"compaction:{ctx.session_id}:{uuid.uuid4().hex[:12]}",
            max_completion_tokens=getattr(ctx.cfg.execution, "max_completion_tokens", None),
            request_options=getattr(ctx.cfg.execution, "request_options", None),
        )
        conv.notify_transient("⎋ Compacted")

    def route(self, msg: str) -> bool:
        """Return True if command routing consumes *msg* locally."""
        if not msg.startswith(("/", "$")):
            return False
        # PRD-114: /workflow is handled locally — not via the command registry.
        # PRD-119: /compact likewise — needs access to session memory.
        parts = msg.split(None, 1)
        if parts[0] == "/workflow":
            return self._handle_workflow_command(parts[1] if len(parts) > 1 else "")
        if parts[0] == "/compact":
            asyncio.create_task(self._handle_compact_command(), name="compact")
            return True
        command = self._ctx.cmd_registry.get(parts[0])
        if command is None and parts[0].startswith("$"):
            # Unknown dollar-prefixed text is ordinary user input, not a
            # failed command.  This also keeps literal `$...` prompts usable.
            return False
        if command is not None and command.is_skill != parts[0].startswith("$"):
            # Skills have their own `$` namespace. This guard also keeps
            # stale or manually injected slash-named skill records from
            # reintroducing the removed legacy syntax.
            return False
        if self.dispatch_slash(msg):
            # Check if a replay was requested by the command handler.
            if self._pending_replay_id:
                replay_id = self._pending_replay_id
                self._pending_replay_id = None
                if isinstance(self, TUISession):
                    self._activate_streaming_input()
                self._agent_task = asyncio.create_task(self._run_replay(replay_id), name="replay")
            return True
        cmd_name = msg.split()[0]
        if self._ctx.cmd_registry.get(cmd_name) is not None:
            self._ctx.console.print(
                f"  [dim]Command [bold]{cmd_name}[/bold] has no handler. "
                f"Add a handler in [bold].agenthicc/commands/[/bold][/dim]"
            )
        return True  # never forward slash commands to the agent

    def _start_workflow_continuation(self, text: str) -> bool:
        """Start a paused workflow with one ordinary user continuation."""
        handle = self._workflow_handle
        if handle is None:
            # Recovery discovery normally attaches the sole candidate during
            # startup.  It can still be empty here when the checkpoint was
            # written after construction, when a picker was dismissed, or
            # when this process was handed a session without an in-memory
            # handle.  An ordinary ``continue`` must resolve that durable
            # record before it is allowed to fall through to the new-run path.
            self._refresh_workflow_recovery_records()
            candidates = list(self._workflow_recovery_records.values())
            if len(candidates) == 1:
                try:
                    handle = self._rehydrate_workflow_record(candidates[0], claim=False)
                    self._workflow_handle = handle
                except Exception as exc:  # noqa: BLE001
                    self._ctx.app_state.conversation.notify_transient(
                        f"⚠ Cannot restore workflow {candidates[0].run_id!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return True
            elif len(candidates) > 1:
                choices = ", ".join(record.run_id for record in candidates[:8])
                suffix = " …" if len(candidates) > 8 else ""
                self._ctx.app_state.conversation.notify_transient(
                    "⚠ Multiple workflows can be resumed. Choose one explicitly: "
                    f"/workflow resume <run-id> ({choices}{suffix})"
                )
                return True
            elif self._workflow_recovery_errors:
                run_id, record = next(iter(self._workflow_recovery_errors.items()))
                self._ctx.app_state.conversation.notify_transient(
                    f"⚠ Cannot resume {run_id!r}: {record.error_code}: {record.display_error}"
                )
                return True
            else:
                return False
        if handle is None or handle.lifecycle not in {"paused", "pausing"}:
            return False
        if not handle.checkpoint_supported:
            self._ctx.app_state.conversation.notify_transient(
                "⚠ This workflow does not support safe checkpoints; use /workflow reset or retry after fixing its context codec."
            )
            self._msg_queue.insert(0, text)
            return True
        definition = self._ctx.workflow_registry.get(handle.workflow_name)
        if definition is None or handle.context is None:
            self._ctx.app_state.conversation.notify_transient(
                f"⚠ Workflow '{handle.workflow_name}' is not available to resume"
            )
            return True
        if handle.claim_owner_id is None:
            try:
                handle.claim(f"{self._workflow_owner_prefix}:{handle.run_id}")
            except Exception as exc:  # noqa: BLE001
                from agenthicc.runners.workflow_checkpoint_store import WorkflowClaimError

                code = (
                    "run_already_claimed"
                    if isinstance(exc, WorkflowClaimError)
                    else "resume_transition_failed"
                )
                if isinstance(exc, WorkflowClaimError):
                    message = f"⚠ {code}: cannot continue workflow {handle.run_id!r}: {exc}"
                else:
                    message = (
                        f"⚠ {code}: cannot continue workflow {handle.run_id!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                self._ctx.app_state.conversation.notify_transient(message)
                self._msg_queue.insert(0, text)
                return True
        try:
            handle.mark_resuming()
            handle.persist_checkpoint(reason="resuming")
        except Exception as exc:  # noqa: BLE001
            self._release_workflow_claim(handle)
            self._ctx.app_state.conversation.notify_transient(
                f"⚠ resume_transition_failed: cannot continue workflow {handle.run_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            self._msg_queue.insert(0, text)
            return True
        turn_id = f"turn_{uuid.uuid4().hex}"
        self._publish_session_event(
            "turn_queued", {"text": text, "client_id": "tui"}, turn_id=turn_id
        )
        self._publish_session_event(
            "workflow_resume_started",
            {
                "run_id": handle.run_id,
                "workflow": handle.workflow_name,
                "phase": handle.current_phase or "",
            },
        )
        self._activate_streaming_input()
        self._agent_task = asyncio.create_task(
            self._resume_workflow_task(
                definition,
                handle.context,
                continuation=text,
                turn_id=turn_id,
            ),
            name=f"resume-workflow-{handle.run_id}",
        )
        return True

    def advance(self) -> None:
        """Drain _msg_queue: dispatch slash commands, start next agent task."""
        notice_kept = False
        while self._msg_queue:
            msg = self._msg_queue.pop(0).strip()
            if not msg:
                continue
            # The registry can be reloaded while text is waiting. Reclassify
            # before release so a newly rejected command is not executed from
            # stale metadata, and report removed slash commands explicitly.
            decision = self._busy_decision(msg)
            if decision.policy.value == "reject":
                self._notify_busy_rejected(msg, decision.reason)
                notice_kept = True
                continue
            token = msg.split(None, 1)[0]
            if token.startswith("/") and self._ctx.cmd_registry.get(token) is None:
                self._ctx.app_state.conversation.notify_transient(
                    f"⚠ Queued command no longer exists: {token}"
                )
                notice_kept = True
                continue
            if self.route(msg):
                if self._agent_task is not None and not self._agent_task.done():
                    return
                continue
            self._ctx.app_state.conversation.notification.set(None)
            self._ctx.app_state.conversation.append_event("user_message", {"text": msg})
            if self._start_workflow_continuation(msg):
                return
            turn_id = f"turn_{uuid.uuid4().hex}"
            self._publish_session_event(
                "turn_queued", {"text": msg, "client_id": "tui"}, turn_id=turn_id
            )
            self._activate_streaming_input()
            self._agent_task = asyncio.create_task(
                self.agent_task_body(msg, turn_id=turn_id), name="agent-turn"
            )
            return
        if not notice_kept:
            self._ctx.app_state.conversation.notification.set(None)

    # ── agent turn plumbing ───────────────────────────────────────────────────

    async def run_turn(self, text: str, resume: object | None = None) -> None:
        """Dispatch one user message: workflow or direct agent turn.

        *resume* (a PRD-129 ``ResumePlan``) re-drives an interrupted direct turn
        with its original turn id and a ledger seeded with the tools that already
        ran, so completed side effects are replayed rather than repeated.
        """
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        ctx = self._ctx

        self._input_session.set_mode(InputMode.STREAMING)
        ctx.approval_svc.reset_turn_memory()

        if self._pending_skill_body:
            text = self._pending_skill_body.pop() + "\n\n" + text

        # PRD-114: /workflow override takes priority over mode default.
        _active_wf_name = self._workflow_override or ctx.app_state.active_mode().default_workflow
        _plugin_cls = ctx.workflow_registry.get(_active_wf_name) if _active_wf_name else None

        _timeout = ctx.cfg.execution.turn_timeout_s
        # PRD-126 gap 11: a turn-timeout deadline so retries are not scheduled
        # when there is no meaningful budget left before asyncio.wait_for fires.
        import time as _time  # noqa: PLC0415

        _deadline = (_time.monotonic() + _timeout) if (_timeout and _timeout > 0) else None

        async def _run_inner() -> None:
            if _plugin_cls is not None:
                import dataclasses as _dc  # noqa: PLC0415
                from agenthicc.workflows.plugin import WorkflowContext  # noqa: PLC0415

                required_phases = tuple(
                    phase
                    for phase in getattr(_plugin_cls, "required_startup_phases", ())
                    if isinstance(phase, str) and phase
                )
                if required_phases:
                    startup = getattr(ctx, "startup", None)
                    if startup is None:
                        raise RuntimeError(
                            "workflow declares startup dependencies but readiness is unavailable"
                        )
                    for phase in required_phases:
                        _prepare_startup_dependency(ctx, phase)
                    await startup.wait_for(*required_phases)

                _phase_specs = getattr(_plugin_cls, "phases", ())
                # A missing in-memory handle is not proof that a session has
                # no workflow to recover.  Refresh durable records at this
                # boundary and block the implicit new-run branch whenever a
                # recoverable or diagnostic record exists.  This is the last
                # guard against a transient provider failure becoming a fresh
                # INIT run when the user types an ordinary continuation.
                if (
                    self._workflow_handle is None
                    or self._workflow_handle.workflow_name != _plugin_cls.name
                ):
                    self._refresh_workflow_recovery_records()
                    if self._workflow_recovery_records:
                        records = list(self._workflow_recovery_records.values())
                        if len(records) == 1:
                            record = records[0]
                            self._ctx.app_state.conversation.notify_transient(
                                f"⚠ Workflow '{record.workflow_name}' can be resumed at "
                                f"{record.current_phase or 'its saved state'}; use "
                                f"/workflow resume {record.run_id}."
                            )
                        else:
                            choices = ", ".join(record.run_id for record in records[:8])
                            suffix = " …" if len(records) > 8 else ""
                            self._ctx.app_state.conversation.notify_transient(
                                "⚠ Multiple workflows can be resumed. Choose one explicitly: "
                                f"/workflow resume <run-id> ({choices}{suffix})"
                            )
                        return
                    if self._workflow_recovery_errors:
                        run_id, record = next(iter(self._workflow_recovery_errors.items()))
                        self._ctx.app_state.conversation.notify_transient(
                            f"⚠ Cannot resume {run_id!r}: {record.error_code}: "
                            f"{record.display_error}"
                        )
                        return
                if (
                    self._workflow_handle is None
                    or self._workflow_handle.workflow_name != _plugin_cls.name
                ):
                    from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore  # noqa: PLC0415
                    from agenthicc.runners.workflow_handle import WorkflowRunHandle  # noqa: PLC0415

                    session_conversation = getattr(ctx, "session_conversation", None)
                    if session_conversation is not None:
                        new_handle = WorkflowRunHandle.create(
                            run_id=uuid.uuid4().hex,
                            workflow=_plugin_cls,
                            conversation=session_conversation,
                            intent=text,
                            checkpoint_store=WorkflowCheckpointStore(ctx.session_id),
                            browser_manager=getattr(ctx, "browser_manager", None),
                            provider_profile=ctx.cfg.execution.profile,
                            workspace_root=str(
                                getattr(getattr(ctx, "workspace_scope", None), "primary_root", "")
                                or ""
                            ),
                        )
                        self._workflow_handle = new_handle
                        bootstrap = WorkflowContext(
                            intent=text,
                            run_id=new_handle.run_id,
                            workflow_name=_plugin_cls.name,
                            current_phase=(
                                getattr(_phase_specs[0], "name", None) if _phase_specs else None
                            ),
                        )
                        new_handle.attach_bootstrap_context(bootstrap)
                        new_handle.claim(f"{self._workflow_owner_prefix}:{new_handle.run_id}")
                        if _phase_specs:
                            new_handle.update_phase(_phase_specs[0].name, 0, 0)
                        else:
                            new_handle.save_checkpoint(reason="started")
                        initializer = getattr(_plugin_cls, "create_initial_context", None)
                        if callable(initializer):
                            initial_context = initializer(
                                text,
                                new_handle.run_id,
                                getattr(ctx, "session_memory", None),
                            )
                            if initial_context is not None:
                                new_handle.attach_context(initial_context)
                                if _phase_specs:
                                    new_handle.update_phase(_phase_specs[0].name, 0, 0)
                # PRD-116: build per-workflow params from merged TOML/CLI/env
                # config only after a durable identity exists.
                _wf_params = _plugin_cls.build_params(ctx.cfg.workflows.get(_plugin_cls.name, {}))
                _wf_config = _dc.replace(
                    self._wf_config_base,
                    completed_turns=self._turn_count,
                    params=_wf_params,
                    workflow_handle=self._workflow_handle,
                    terminal_wait_policies={
                        phase.name: phase.terminal_wait_policy
                        for phase in _phase_specs
                        if hasattr(phase, "name") and hasattr(phase, "terminal_wait_policy")
                    },
                    startup=getattr(ctx, "startup", None),
                    required_startup_phases=required_phases,
                )
                # Plugin owns runner construction — no name-based branching.
                _wf_runner = _plugin_cls.build_runner(_wf_config, ctx.mode_manager)
                await _wf_runner.run(text)
                self._finalize_returned_workflow()
                # PRD-155: workflow-bound runs return to Safe after success.
                _wf_result = ctx.app_state.workflow_run()
                if (
                    _wf_result is not None
                    and getattr(_wf_result, "status", None) == "complete"
                    and ctx.app_state.active_mode().default_workflow is not None
                ):
                    ctx.mode_manager.set_by_name("Safe")
                    ctx.app_state.conversation.notification.set(
                        "✓ Workflow complete — switched to Safe mode"
                    )
                if self._workflow_handle is not None and self._workflow_handle.lifecycle in {
                    "complete",
                    "failed",
                    "discarded",
                }:
                    self._workflow_handle = None
            else:
                # PRD-126: direct (non-workflow) turns are retried at the
                # _run_agent_turn boundary inside AgentTurnRunner itself, so no
                # retry wrapper is needed here.
                await _run_agent_turn(
                    text,
                    ctx.agent_runner,
                    ctx.processor,
                    session_memory=ctx.session_memory,
                    conversation_id=ctx.session_id,
                    max_agent_turns=ctx.cfg.execution.max_agent_turns,
                    conv_store=ctx.app_state.conversation,
                    app_state=ctx.app_state,
                    exec_cfg=ctx.cfg.execution,
                    skills=ctx.skills,
                    skill_permissions=ctx.cfg.agents.skill_permissions_for("default"),
                    mention_cache=ctx.mention_cache,
                    project_plugin_tools=(
                        cast("list[ToolLike]", ctx.project_plugins.all_tools)
                        + _make_session_tools(
                            ctx.approval_svc,
                            memory_router=ctx.memory_router,
                            semantic_index=ctx.semantic_index,
                        )
                        + list(getattr(ctx, "browser_tools", []))
                    ),
                    mcp_registry=ctx.mcp_registry,
                    active_agent="default",
                    completed_turns=self._turn_count,
                    approval_svc=ctx.approval_svc,
                    workspace_access=cast(
                        "WorkspaceAccessPolicy | None", vars(ctx).get("workspace_access")
                    ),
                    memory_router=ctx.memory_router,
                    semantic_index=ctx.semantic_index,
                    retry_deadline_monotonic=_deadline,
                    resume_turn_id=getattr(resume, "turn_id", None),
                    resume_ledger=getattr(resume, "ledger", None),
                    next_queued_message=self._next_queued_message,
                    usage_ledger=getattr(ctx, "usage_ledger", None),
                    browser_manager=getattr(ctx, "browser_manager", None),
                )

        try:
            if _timeout and _timeout > 0:
                await asyncio.wait_for(_run_inner(), timeout=_timeout)
            else:
                await _run_inner()
        except asyncio.TimeoutError:
            if self._workflow_handle is not None:
                self._fail_workflow_run(
                    f"TimeoutError: Turn timed out after {_timeout:.0f}s",
                    kind="timeout",
                )
            ctx.app_state.conversation.close_turn(
                error=(
                    f"TimeoutError: Turn timed out after {_timeout:.0f}s — "
                    "the agent or a tool may be stuck on a slow network call."
                )
            )
        finally:
            self._input_session.set_mode(InputMode.IDLE)
            self._turn_count += 1
            # PRD-129 Phase 2: no per-turn snapshot save — the JournaledShortTermMemory
            # already fsync'd every transition durably as it happened.

    async def agent_task_body(
        self,
        text: str,
        resume: object | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Wrap run_turn with error handling; advance queue on completion."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        conv = self._ctx.app_state.conversation
        turn_failed = False
        turn_cancelled = False
        workflow_paused = False
        conversation = getattr(self._ctx, "session_conversation", None)
        owner_id = turn_id or f"activity_{uuid.uuid4().hex}"
        acquired = False
        conv.begin_activity()
        if turn_id is not None:
            self._publish_session_event("turn_started", turn_id=turn_id)
        try:
            # Optional MCP startup is deferred until after the first TUI frame,
            # but an agent turn that can see MCP tools must wait for the
            # catalog readiness boundary before building its tool schema.
            startup = getattr(self._ctx, "startup", None)
            if startup is not None:
                from agenthicc.runners.startup import StartupDependencyError  # noqa: PLC0415

                if startup.report("extensions").state.value != "not_started":
                    try:
                        await startup.wait_for("extensions")
                    except StartupDependencyError:
                        # Project extensions are optional. A failed scan must
                        # not prevent built-in tools and direct local turns;
                        # the failed phase remains visible in /startup.
                        conv.notify_transient(
                            "⚠ Optional extensions unavailable; using built-in tools"
                        )
            mcp_manager = getattr(self._ctx, "mcp_manager", None)
            if startup is not None and mcp_manager is not None:
                # An optional MCP catalog may still be loading or may have
                # failed. Local tools remain usable in both cases. Only the
                # explicitly configured, auto-connected required servers are
                # a dependency of a turn that publishes the MCP catalog.
                required_servers = getattr(mcp_manager, "required_auto_connect_servers", ())
                if required_servers:
                    try:
                        await startup.wait_for("mcp")
                    except StartupDependencyError as exc:
                        # The background boundary intentionally converts
                        # failures to a visible degraded report. Rehydrate
                        # the manager's established typed error at the
                        # operation boundary so callers retain the pre-176
                        # required-MCP contract.
                        failures = getattr(mcp_manager, "required_failures", {})
                        if isinstance(failures, dict) and failures:
                            from agenthicc.tools.mcp_manager import (  # noqa: PLC0415
                                McpRequiredServerError,
                            )

                            raise McpRequiredServerError(failures) from exc
                        raise
            if conversation is not None:
                await conversation.acquire(owner_id)
                acquired = True
            await self.run_turn(text, resume=resume)
        except asyncio.CancelledError:
            turn_cancelled = True
            handle = self._workflow_handle
            if handle is not None and handle.is_pause_requested():
                workflow_paused = True
                await self._finalize_workflow_pause(handle)
            elif handle is not None and handle.lifecycle in {"running", "resuming", "pausing"}:
                self._fail_workflow_run("cancelled", kind="user_cancelled")
            # close_turn() is idempotent — inner layers may have already called it.
            conv.close_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            turn_failed = True
            error = _fmt_exc(exc)
            # Workflow startup can fail before an agent turn exists. Publish
            # that failure explicitly instead of silently returning to idle.
            if self._workflow_handle is not None:
                self._fail_workflow_run(error, kind=_workflow_failure_kind(exc))
            elif conv.is_turn_active:
                conv.close_turn(error=error)
            else:
                conv.append_event("error", {"message": error})
            self._input_session.set_mode(InputMode.IDLE)
        finally:
            conv.end_activity()
            if acquired and conversation is not None:
                conversation.release(owner_id)
            if turn_id is not None:
                turn_event = (
                    "turn_paused"
                    if workflow_paused
                    else "turn_cancelled"
                    if turn_cancelled
                    else "turn_failed"
                    if turn_failed
                    else "turn_completed"
                )
                self._publish_session_event(
                    turn_event,
                    turn_id=turn_id,
                )
            self._agent_task = None
            self.advance()

    async def _finalize_workflow_pause(self, handle: "WorkflowRunHandle") -> None:
        """Persist a paused workflow only after the runner has unwound."""
        from agenthicc.kernel import Event  # noqa: PLC0415

        conv = self._ctx.app_state.conversation
        try:
            # Save while PAUSING first so an unsupported custom context fails
            # closed without falsely advertising a resumable PAUSED state.
            handle.save_checkpoint(reason="escape")
            handle.mark_paused(reason="escape")
            checkpoint = handle.save_checkpoint(reason="escape")
            await self._ctx.processor.emit(
                Event.create(
                    "WorkflowRunPaused",
                    {
                        "run_id": handle.run_id,
                        "workflow_name": handle.workflow_name,
                        "phase_name": handle.current_phase,
                        "conversation_cursor": checkpoint.conversation_cursor,
                    },
                )
            )
            await self._ctx.processor.emit(
                Event.create(
                    "WorkflowCheckpointSaved",
                    {
                        "run_id": handle.run_id,
                        "workflow_name": handle.workflow_name,
                        "revision": checkpoint.revision,
                        "reason": "escape",
                    },
                )
            )
            self._publish_session_event(
                "workflow_resume_paused",
                {
                    "run_id": handle.run_id,
                    "workflow": handle.workflow_name,
                    "phase": handle.current_phase or "",
                    "revision": checkpoint.revision,
                },
            )
            conv.notify_transient(
                f"⏸ Workflow '{handle.workflow_name}' paused at {handle.current_phase or 'current phase'}. "
                "Send a message or use /workflow resume to continue."
            )
            # Keep the picker/explicit-ID index aligned with the checkpoint
            # just written.  The index is intentionally best-effort; the next
            # explicit resume also reloads the store, so a projection failure
            # must not turn a durable pause into a user-visible failure.
            try:
                self._refresh_workflow_recovery_records()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            finalize = getattr(handle, "finalize_failure", None)
            if callable(finalize):
                finalize(
                    f"workflow pause checkpoint failed: {type(exc).__name__}: {exc}",
                    kind="checkpoint_storage",
                    recoverable=False,
                )
            self._publish_session_event(
                "workflow_pause_failed",
                {
                    "run_id": handle.run_id,
                    "workflow": handle.workflow_name,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            conv.notify_transient(f"⚠ Workflow pause could not be checkpointed: {exc}")
            if handle.lifecycle in {"complete", "failed", "discarded"}:
                self._release_workflow_claim(handle)
                if self._workflow_handle is handle:
                    self._workflow_handle = None

    async def handle_send(self, cmd: "SendMessageCommand") -> None:
        """Route user message: slash → command dispatcher, text → agent."""
        text = cmd.text.strip()
        if not text:
            return

        # Keep the latest accepted natural-language intent available to the
        # background control plane even if it was submitted while another run
        # was still active and therefore entered the FIFO queue.
        if not text.startswith(("/", "$")):
            self._last_submitted_text = text

        if self._agent_task and not self._agent_task.done():
            decision = self._busy_decision(text)
            if decision.policy.value in {"immediate-read-only", "immediate-control"}:
                self._run_busy_immediate(text, decision)
                return
            if decision.policy.value == "reject":
                self._notify_busy_rejected(text, decision.reason)
                return
            self._msg_queue.append(text)
            self._notify_queued(text)
            return

        if isinstance(self, TUISession) and not text.startswith(("/", "$")):
            if self._start_workflow_continuation(text):
                self._ctx.app_state.conversation.append_event("user_message", {"text": text})
                return

        if self.route(text):
            if self._pending_skill_body:
                body = self._pending_skill_body.pop()
                self._ctx.app_state.conversation.append_event("user_message", {"text": text})
                turn_id = f"turn_{uuid.uuid4().hex}"
                publish_event = getattr(self, "_publish_session_event", None)
                if callable(publish_event):
                    publish_event(
                        "turn_queued", {"text": body, "client_id": "tui"}, turn_id=turn_id
                    )
                    task = self.agent_task_body(body, turn_id=turn_id)
                else:
                    task = self.agent_task_body(body)
                if isinstance(self, TUISession):
                    self._activate_streaming_input()
                self._agent_task = asyncio.create_task(task, name="agent-turn")
            return
        self._ctx.app_state.conversation.append_event("user_message", {"text": text})
        turn_id = f"turn_{uuid.uuid4().hex}"
        publish_event = getattr(self, "_publish_session_event", None)
        if callable(publish_event):
            publish_event("turn_queued", {"text": text, "client_id": "tui"}, turn_id=turn_id)
            task = self.agent_task_body(text, turn_id=turn_id)
        else:
            task = self.agent_task_body(text)
        if isinstance(self, TUISession):
            self._activate_streaming_input()
        self._agent_task = asyncio.create_task(task, name="agent-turn")

    def handle_interrupt(self, cmd: "InterruptAgentCommand") -> None:
        """Cancel, or cooperatively pause, the current agent task."""
        # Cancellation may happen before the agent's ``turn_complete`` event
        # is delivered.  Flush the appender first so a collapsed tool group is
        # committed to the scroll buffer rather than stranded in the live
        # footer while the task unwinds.
        self._workspace.flush_scroll()
        handle = self._workflow_handle
        if (
            getattr(cmd, "disposition", "cancel") == "pause"
            and handle is not None
            and handle.lifecycle in {"running", "resuming"}
        ):
            handle.request_pause()
            self._ctx.app_state.conversation.notify_transient(
                f"⏸ Pausing workflow '{handle.workflow_name}'…"
            )
        self._cancel_active_task()

    # ── workflow resume (PRD-94) ──────────────────────────────────────────────

    async def _run_replay(self, session_id: str) -> None:
        """Replay a historical session's conversation through the render pipeline."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415
        from agenthicc.tui.runtime.replay import ConversationReplayer  # noqa: PLC0415

        ctx = self._ctx

        # Enter Replay mode — blocks all tool capabilities during replay.
        prior_mode = ctx.app_state.active_mode()
        ctx.mode_manager.set_internal_by_name("Replay")
        self._input_session.set_mode(InputMode.STREAMING)

        try:
            replayer = ConversationReplayer(
                session_id=session_id,
                conv_store=ctx.app_state.conversation,
                mode_manager=ctx.mode_manager,
            )
            await replayer.run()
        except (asyncio.CancelledError, KeyboardInterrupt):
            ctx.app_state.conversation.notification.set("⏮ Replay cancelled.")
            raise
        except Exception as exc:
            ctx.app_state.conversation.notification.set(f"⏮ Replay error: {exc}")
        finally:
            ctx.mode_manager.restore(prior_mode)
            self._input_session.set_mode(InputMode.IDLE)
            self._agent_task = None
            self.advance()

    def _notify_incomplete_workflow(self) -> None:
        """If the kernel state has an unfinished workflow, notify the user.

        Does NOT auto-start the workflow.  A durable workflow recovery record
        takes precedence over the kernel's coarse projection so the user is
        directed to `/workflow resume` instead of accidentally starting a new
        run with the next message.
        """
        if self._workflow_handle is not None and self._workflow_handle.lifecycle in {
            "paused",
            "pausing",
        }:
            return
        recovery_records = getattr(self, "_workflow_recovery_records", {})
        if recovery_records:
            if len(recovery_records) == 1:
                record = next(iter(recovery_records.values()))
                self._ctx.app_state.conversation.notification.set(
                    f"Workflow '{record.workflow_name}' can be resumed at "
                    f"{record.current_phase or 'its saved state'}. "
                    f"Use /workflow resume {record.run_id}."
                )
            else:
                ids = ", ".join(recovery_records)
                self._ctx.app_state.conversation.notification.set(
                    f"{len(recovery_records)} workflows can be resumed. "
                    f"Use /workflow resume <run-id>: {ids}"
                )
            return
        from agenthicc.kernel.state import NodeStatus  # noqa: PLC0415

        k_state = self._ctx.processor.get_state()
        for wf in k_state.workflows.values():
            if wf.status in (NodeStatus.complete, NodeStatus.failed):
                continue
            if not wf.name:
                continue
            self._ctx.app_state.conversation.notification.set(
                f"Session had an in-progress '{wf.name}' workflow. "
                "No durable resume checkpoint is available; use /workflow reset "
                "or start a new run explicitly."
            )
            return

    def _has_incomplete_workflow(self) -> bool:
        from agenthicc.kernel.state import NodeStatus  # noqa: PLC0415

        k_state = self._ctx.processor.get_state()
        return any(
            bool(wf.name) and wf.status not in (NodeStatus.complete, NodeStatus.failed)
            for wf in k_state.workflows.values()
        )

    def _maybe_resume_interrupted_turn(self) -> None:
        """PRD-129 Phase 3: re-drive a turn the prior session left incomplete.

        Fires only for a *direct* turn (no in-progress workflow — those are left
        to the workflow's own resume). Re-submits the user message against the
        journal-rehydrated committed projection with a ledger seeded from the
        tools that already ran, so completed side effects are replayed, not
        repeated; it does not roll memory back to the pre-turn count.
        """
        ctx = self._ctx
        plan = ctx.pending_resume
        if (
            plan is None
            or self._has_incomplete_workflow()
            or self._workflow_recovery_records
            or self._workflow_recovery_errors
        ):
            return
        # PRD-182: the journal/memory projection already contains every
        # provider step committed before the interruption. Rolling back to
        # ``base_count`` here would erase that useful context and was the
        # source of the "fresh conversation" symptom after a late failure.
        # Lauren receives the original message with a resume marker and must
        # continue from the retained safe step instead.
        ctx.app_state.conversation.notification.set(
            "↻ Resuming an interrupted turn — completed tools are replayed, not repeated…"
        )
        self._activate_streaming_input()
        self._agent_task = asyncio.create_task(
            self.agent_task_body(str(getattr(plan, "user_message", "")), resume=plan),
            name="resume-turn",
        )

    async def _resume_workflow_task(
        self,
        wf_defn: type[WorkflowPlugin],
        context: object,
        *,
        continuation: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Resume a WorkflowRunner with the session conversation still attached."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        ctx = self._ctx
        handle = self._workflow_handle
        conversation = getattr(ctx, "session_conversation", None)
        owner_id = turn_id or (
            handle.run_id if handle is not None else f"resume_{uuid.uuid4().hex}"
        )
        acquired = False
        self._input_session.set_mode(InputMode.STREAMING)
        if ctx.approval_svc is not None:
            ctx.approval_svc.reset_turn_memory()
        ctx.app_state.conversation.begin_activity()
        if turn_id is not None:
            self._publish_session_event("turn_started", turn_id=turn_id)
        try:
            import dataclasses as _dc  # noqa: PLC0415

            if conversation is not None:
                await conversation.acquire(owner_id)
                acquired = True
            if handle is not None:
                # The command marks the handle RESUMING before task creation.
                # Esc can change that to PAUSING while the conversation lock is
                # being acquired; never overwrite that request when control
                # returns to this task.
                if handle.lifecycle == "paused":
                    handle.mark_resuming()
                if handle.checkpoint_supported and handle.lifecycle in {"resuming", "pausing"}:
                    handle.persist_checkpoint(
                        reason="resuming" if handle.lifecycle == "resuming" else "pause_requested"
                    )
                from agenthicc.kernel import Event  # noqa: PLC0415

                await self._ctx.processor.emit(
                    Event.create(
                        "WorkflowRunResumed",
                        {
                            "run_id": handle.run_id,
                            "workflow_name": handle.workflow_name,
                        },
                    )
                )
            if ctx.session_memory is not None:
                if _preserve_interrupted_memory(ctx.session_memory):
                    ctx.app_state.conversation.append_event(
                        "system",
                        {
                            "text": (
                                "Tool execution history was repaired before workflow resume; "
                                "completed work was preserved."
                            )
                        },
                    )
                continuation_text = (
                    "[WORKFLOW RESUME]\n"
                    f"Workflow: {wf_defn.name}\n"
                    f"Current phase: {getattr(handle, 'current_phase', None) or 'current'}\n"
                    "The prior agent turn was interrupted safely; preserve completed work "
                    "and continue from the saved phase.\n"
                    f"Original intent: {getattr(handle, 'original_intent', '')}\n"
                    + (f"User continuation: {continuation}" if continuation else "Continue.")
                )
                if handle is not None and continuation:
                    handle.append_continuation(continuation)
                ctx.session_memory.add_user(continuation_text)

            required_phases = tuple(
                phase
                for phase in getattr(wf_defn, "required_startup_phases", ())
                if isinstance(phase, str) and phase
            )
            if required_phases:
                startup = getattr(ctx, "startup", None)
                if startup is None:
                    raise RuntimeError(
                        "workflow declares startup dependencies but readiness is unavailable"
                    )
                for phase in required_phases:
                    _prepare_startup_dependency(ctx, phase)
                await startup.wait_for(*required_phases)
            _wf_params = wf_defn.build_params(ctx.cfg.workflows.get(wf_defn.name, {}))
            _phase_specs = getattr(wf_defn, "phases", ())
            _wf_config = _dc.replace(
                self._wf_config_base,
                params=_wf_params,
                workflow_handle=handle,
                session_memory=ctx.session_memory,
                conversation_id=ctx.session_id,
                terminal_wait_policies={
                    phase.name: phase.terminal_wait_policy
                    for phase in _phase_specs
                    if hasattr(phase, "name") and hasattr(phase, "terminal_wait_policy")
                },
                startup=getattr(ctx, "startup", None),
                required_startup_phases=required_phases,
            )
            runner = wf_defn.build_runner(_wf_config, ctx.mode_manager)
            # The task argument is normally the same object as the handle's
            # context.  Prefer the handle reference when available so a
            # rehydration or a runner-side context attachment cannot leave a
            # later resume cycle using a stale object.
            resume_context = (
                handle.context if handle is not None and handle.context is not None else context
            )
            runner_result = await runner.resume(resume_context)
            if handle is not None and runner_result is not None:
                handle.attach_context(runner_result)

            # A custom runner may catch cancellation internally and return
            # while the session has already moved the handle to PAUSING.  A
            # pause request always wins over the normal-return safety net;
            # otherwise the first such interruption would be terminalized and
            # a second ``/workflow resume`` could never reach the saved run.
            if handle is not None and handle.is_pause_requested():
                await self._finalize_workflow_pause(handle)
            else:
                self._finalize_returned_workflow()
            # PRD-155: completed workflow-bound runs return to Safe.
            _wf_result = ctx.app_state.workflow_run()
            if (
                _wf_result is not None
                and getattr(_wf_result, "status", None) == "complete"
                and ctx.app_state.active_mode().default_workflow is not None
            ):
                ctx.mode_manager.set_by_name("Safe")
                ctx.app_state.conversation.notification.set(
                    "✓ Workflow resumed and complete — switched to Safe mode"
                )
        except asyncio.CancelledError:
            if handle is not None and handle.is_pause_requested():
                await self._finalize_workflow_pause(handle)
            elif handle is not None and handle.lifecycle in {"resuming", "running", "pausing"}:
                self._fail_workflow_run("cancelled", kind="user_cancelled")
            ctx.app_state.conversation.close_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            conv = ctx.app_state.conversation
            error = _fmt_exc(exc)
            if self._workflow_handle is not None:
                self._fail_workflow_run(error, kind=_workflow_failure_kind(exc))
            elif conv.is_turn_active:
                conv.close_turn(error=error)
            else:
                conv.append_event("error", {"message": error})
            self._input_session.set_mode(InputMode.IDLE)
        finally:
            ctx.app_state.conversation.end_activity()
            if acquired and conversation is not None:
                conversation.release(owner_id)
            if turn_id is not None:
                self._publish_session_event(
                    "turn_paused"
                    if handle is not None and handle.lifecycle == "paused"
                    else "turn_failed"
                    if handle is not None and handle.lifecycle == "failed"
                    else "turn_completed",
                    turn_id=turn_id,
                )
            self._agent_task = None
            if handle is not None and handle.lifecycle in {"complete", "failed", "discarded"}:
                self._publish_session_event(
                    "workflow_resume_completed"
                    if handle.lifecycle == "complete"
                    else "workflow_resume_failed"
                    if handle.lifecycle == "failed"
                    else "workflow_discarded",
                    {
                        "run_id": handle.run_id,
                        "workflow": handle.workflow_name,
                        "status": handle.lifecycle,
                        "error": handle.last_error,
                    },
                )
                self._release_workflow_claim(handle)
                self._workflow_handle = None
            self.advance()

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start tasks, run input session, tear down."""
        from agenthicc.tui.runtime import (  # noqa: PLC0415
            SendMessageCommand,
            InterruptAgentCommand,
        )

        ctx = self._ctx

        ctx.command_bus.register(SendMessageCommand, self.handle_send)
        ctx.command_bus.register(InterruptAgentCommand, self.handle_interrupt)
        self._wire_approval_overlay()

        startup = getattr(ctx, "startup", None)
        if startup is not None:
            startup.begin("first_frame")
        try:
            self._workspace.start()
        except Exception as exc:
            if startup is not None:
                from agenthicc.runners.startup import ReadinessState  # noqa: PLC0415

                startup.finish(
                    "first_frame",
                    ReadinessState.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if startup is not None:
            startup.finish("first_frame")
        # The Live shell is now visible. Discovery may execute trusted or
        # project extension code, so it starts only after that first frame and
        # is joined at the agent-operation boundary.
        self._start_deferred_startup()
        if getattr(ctx, "resumed", False):
            from agenthicc.tui.runtime.session_log import SessionEventLog  # noqa: PLC0415

            behaviour = getattr(getattr(ctx, "cfg", None), "behaviour", None)
            replay_turns = getattr(
                behaviour,
                "resume_transcript_turns",
                DEFAULT_RESUME_TRANSCRIPT_TURNS,
            )
            if not isinstance(replay_turns, int):
                replay_turns = DEFAULT_RESUME_TRANSCRIPT_TURNS
            await self._workspace.replay_transcript(
                SessionEventLog.load(
                    ctx.session_id,
                    rendered=False,
                    last_turns=replay_turns or None,
                )
            )
        proc_task = asyncio.create_task(ctx.processor.run())
        # If a previous session had an in-progress workflow, show a notification
        # but do NOT auto-start it — the user decides what to do next.
        self._notify_incomplete_workflow()
        # PRD-129 Phase 3: auto-resume a direct turn the prior session left
        # interrupted (no-op on a clean start or when a workflow was in progress).
        self._maybe_resume_interrupted_turn()
        ad_task: asyncio.Task[object] | None = None
        try:
            from agenthicc.auth import AuthClient  # noqa: PLC0415
            from agenthicc.ads import AdRotator  # noqa: PLC0415

            auth = AuthClient()
            bndl = auth.current_bundle()
            if bndl is not None and not bndl.is_pro:
                ad_task = asyncio.create_task(
                    AdRotator(auth_client=auth, processor=ctx.processor).run()
                )
        except Exception:  # noqa: BLE001
            pass

        async def _tick() -> None:
            while True:
                await asyncio.sleep(0.05)
                # Approval and question requests suspend the LLM turn while
                # the overlay owns the terminal. Freeze animation during that
                # wait so SIGWINCH redraws do not race a changing status bar.
                ctx.app_state.conversation.tick(paused=ctx.app_state.pending_approval() is not None)

        tick_task = asyncio.create_task(_tick())
        try:
            await self._input_session.run()
        finally:
            # Input can exit while an ESC-triggered cancellation is still
            # unwinding. Cancel and await the owned task before closing the
            # session; otherwise Windows may leave its executor/terminal wait
            # alive during asyncio shutdown.
            self._msg_queue.clear()
            agent_task = self._agent_task
            if agent_task is not None and not agent_task.done():
                agent_task.cancel()
            tick_task.cancel()
            proc_task.cancel()
            if ad_task:
                ad_task.cancel()
            startup = getattr(ctx, "startup", None)
            if startup is not None:
                await startup.close()
            await asyncio.gather(
                *([agent_task] if agent_task else []),
                tick_task,
                proc_task,
                *([ad_task] if ad_task else []),
                return_exceptions=True,
            )
            self._workspace.stop()


# ── thin factory (≤60 lines) ──────────────────────────────────────────────────


async def _run_tui_session(
    resume_id: str | None = None,
    cli_overrides: list[str] | None = None,
    record_cassette: str | None = None,
    cli_flags: CLIFlags | None = None,
    config_path: str | None = None,
    cli_secret_overrides: list[str] | None = None,
    cwd: str | None = None,
    mode_name: str | None = None,
    workflow_name: str | None = None,
    owner_lease: SessionOwnerLease | None = None,
    config: "AgenthiccConfig | None" = None,
) -> None:
    """Run the reactive TUI, optionally from a resumed session's project."""
    if cwd is None:
        await _run_tui_session_impl(
            resume_id=resume_id,
            cli_overrides=cli_overrides,
            record_cassette=record_cassette,
            cli_flags=cli_flags,
            config_path=config_path,
            cli_secret_overrides=cli_secret_overrides,
            mode_name=mode_name,
            workflow_name=workflow_name,
            owner_lease=owner_lease,
            config=config,
        )
        return

    previous_cwd = os.getcwd()
    os.chdir(Path(cwd).expanduser().resolve())
    try:
        await _run_tui_session_impl(
            resume_id=resume_id,
            cli_overrides=cli_overrides,
            record_cassette=record_cassette,
            cli_flags=cli_flags,
            config_path=config_path,
            cli_secret_overrides=cli_secret_overrides,
            mode_name=mode_name,
            workflow_name=workflow_name,
            owner_lease=owner_lease,
            config=config,
        )
    finally:
        os.chdir(previous_cwd)


async def _run_tui_session_impl(
    resume_id: str | None = None,
    cli_overrides: list[str] | None = None,
    record_cassette: str | None = None,
    cli_flags: CLIFlags | None = None,
    config_path: str | None = None,
    cli_secret_overrides: list[str] | None = None,
    mode_name: str | None = None,
    workflow_name: str | None = None,
    owner_lease: SessionOwnerLease | None = None,
    config: "AgenthiccConfig | None" = None,
) -> None:
    """Reactive TUI session — single entry point, no legacy branches."""
    from agenthicc.tui.workspace import Workspace  # noqa: PLC0415
    from agenthicc.tui.input.unified_session import UnifiedInputSession  # noqa: PLC0415
    from agenthicc.tui.welcome import (  # noqa: PLC0415
        fetch_changelog,
        load_cached_changelog,
        print_welcome,
    )

    cassette_base: Path | None = Path(record_cassette) if record_cassette else None
    cached_changelog = load_cached_changelog()

    ctx = None
    changelog_task: asyncio.Task[object] | None = None
    try:
        ctx = await _build_session_context(
            resume_id,
            cli_overrides,
            cassette_base,
            config_path=config_path,
            cli_secret_overrides=cli_secret_overrides,
            mode_name=mode_name,
            workflow_name=workflow_name,
            owner_lease=owner_lease,
            config=config,
        )
        # Welcome metadata is non-essential startup UI. It starts after the
        # session coordinator exists and is never awaited before the first
        # frame; the static panel uses the bounded last-known-good cache.
        assert ctx.startup is not None
        changelog_task = ctx.startup.start_background(
            "welcome",
            lambda: fetch_changelog(),
            degrade_on_error=True,
        )
        ctx.startup.begin("tui_shell")
        # PRD-79: stamp CLIFlags onto AppState immediately after creation; frozen for session lifetime.
        if cli_flags is not None:
            ctx.app_state.cli_flags = cli_flags
        replay_turns = ctx.cfg.behaviour.resume_transcript_turns
        workspace = Workspace(
            ctx.app_state,
            ctx.console,
            max_live_tool_calls=ctx.cfg.tools.max_live_tool_calls,
            group_exploratory_calls=ctx.cfg.tools.group_exploratory_calls,
            startup=ctx.startup,
        )
        input_session = UnifiedInputSession(
            app_state=ctx.app_state,
            command_bus=ctx.command_bus,
            trigger_registry=ctx.trigger_registry,
            mode_manager=ctx.mode_manager,
            overlay_host=workspace.overlays,
            cwd=Path(os.getcwd()),
            cfg=ctx.cfg,
            history=(
                load_user_message_history(ctx.session_id, last_turns=replay_turns or None)
                if ctx.resumed
                else None
            ),
        )
        session = TUISession(ctx, workspace, input_session)
        ctx.startup.finish("tui_shell")
        from agenthicc.background.terminals import (  # noqa: PLC0415
            set_current_terminal_manager,
        )

        terminal_context_token = set_current_terminal_manager(ctx.terminal_manager)
    except BaseException:
        # Workspace/input construction can fail after the durable context has
        # opened journals, providers, MCP/browser resources, or projection
        # tasks.  Run the same idempotent close boundary used by a live TUI so
        # partial startup cannot strand the session owner or its handles.
        try:
            if changelog_task is not None and not changelog_task.done():
                changelog_task.cancel()
            if changelog_task is not None:
                await asyncio.gather(changelog_task, return_exceptions=True)
            if ctx is not None:
                await _close_tui_session_resources(ctx, None, None, None)
        except BaseException:
            pass
        raise
    try:
        print_welcome(
            ctx.console,
            model=ctx.model_label,
            cwd=os.getcwd(),
            changelog=cached_changelog,
        )
        await session.run()
    finally:
        if not changelog_task.done():
            changelog_task.cancel()
        await asyncio.gather(changelog_task, return_exceptions=True)
        await _close_tui_session_resources(ctx, session, terminal_context_token, cassette_base)


async def _close_tui_session_resources(
    ctx: "SessionContext",
    session: TUISession | None,
    terminal_context_token: contextvars.Token["TerminalManager | None"] | None,
    cassette_base: Path | None,
) -> None:
    """Close TUI resources and release the outer session lease last."""

    try:
        from agenthicc.background.terminals import reset_current_terminal_manager  # noqa: PLC0415

        if terminal_context_token is not None:
            reset_current_terminal_manager(terminal_context_token)
        # A clean TUI exit releases the workflow claim while leaving a running
        # or paused checkpoint available for the next --resume invocation.
        if session is not None:
            session._release_workflow_claim(session._workflow_handle)
            session._terminal_unsub()
        ctx.session_log.close()
        # PRD-129 Phase 2: close the durable conversation journal handle.
        _close = getattr(ctx.session_memory, "close", None)
        if callable(_close):
            _close()
        # PRD-132 L1: close + clear the workspace file cache.
        from agenthicc.tools.fs.file_cache import (  # noqa: PLC0415
            configure_file_cache,
            get_file_cache,
        )

        _fc = get_file_cache()
        if _fc is not None:
            _fc.close()
            configure_file_cache(None)
        startup = getattr(ctx, "startup", None)
        if startup is not None:
            await startup.close()
        if ctx.mcp_registry:
            await ctx.mcp_registry.shutdown()
        browser_manager = getattr(ctx, "browser_manager", None)
        if browser_manager is not None:
            await browser_manager.close_session()
        await ctx.terminal_manager.close()
        if ctx.kernel_projection_task is not None:
            ctx.kernel_projection_task.cancel()
            await asyncio.gather(ctx.kernel_projection_task, return_exceptions=True)
        session_service = getattr(ctx, "session_service", None)
        if session_service is not None:
            await session_service.close()
        if cassette_base is not None:
            _write_cassette_meta(cassette_base / ctx.session_id, ctx.session_id)
    finally:
        owner_lease = cast(SessionOwnerLease | None, vars(ctx).get("owner_lease"))
        if owner_lease is not None:
            owner_lease.release()


def _write_cassette_meta(cassette_dir: Path, session_id: str) -> None:
    """Write meta.json alongside the cassette files."""
    import json as _json
    from datetime import datetime, timezone

    meta = {
        "session_id": session_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "intent": "",  # filled in manually or from history
    }
    try:
        (cassette_dir / "meta.json").write_text(_json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _select_and_claim_latest_session() -> tuple[str, SessionOwnerLease] | None:
    """Resolve `--continue` and claim the selected session before async startup."""

    coordinator = SessionOpenCoordinator(_SESSIONS_DIR)
    if _find_latest_session_for_cwd is not find_latest_session_for_cwd:
        # Preserve the module-level test seam while keeping production on the
        # atomic coordinator path.  A patched resolver is also useful for
        # embedders that supply their own session index.
        selected = _find_latest_session_for_cwd()
        if selected is None:
            return None
        return selected, coordinator.acquire_existing(selected, entrypoint="tui")
    return coordinator.select_latest_for_cwd(os.getcwd(), entrypoint="tui")


def _exit_for_session_owner_conflict(exc: SessionAlreadyActiveError) -> NoReturn:
    """Render the safe, actionable TUI conflict and terminate the invocation."""

    print(format_session_conflict(exc), file=sys.stderr)
    raise SystemExit(exc.exit_code)


# ── sync entry point (unchanged) ─────────────────────────────────────────────


def _run_tui(ctx: CLIContext) -> None:
    try:
        from rich.console import Console  # noqa: F401
    except ImportError:
        print("error: TUI requires rich — pip install agenthicc", file=sys.stderr)
        sys.exit(1)

    # Crash-safe terminal restore (PRD-107, Layer 5).
    # Cover all exit paths: normal exit (finally below), atexit, SIGTERM, SIGHUP.
    import atexit  # noqa: PLC0415
    import signal as _signal  # noqa: PLC0415

    atexit.register(_reset_terminal_on_exit)

    def _sig_exit(signum: int, frame: object) -> None:
        _reset_terminal_on_exit()
        sys.exit(0)

    try:
        _signal.signal(_signal.SIGTERM, _sig_exit)
        _signal.signal(_signal.SIGHUP, _sig_exit)
    except (AttributeError, OSError):
        pass  # Windows / non-TTY environments

    resume_id: str | None = ctx.resume_id
    owner_lease: SessionOwnerLease | None = None
    if resume_id is None and ctx.continue_session:
        try:
            selected = _select_and_claim_latest_session()
        except SessionAlreadyActiveError as exc:
            _exit_for_session_owner_conflict(exc)
        except SessionStorageError as exc:
            print(f"error: {exc.code}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        if selected is not None:
            resume_id, owner_lease = selected
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    try:
        asyncio.run(
            _run_tui_session(
                resume_id=resume_id,
                cli_overrides=list(ctx.set_overrides),
                record_cassette=ctx.record_cassette,
                cli_flags=ctx.flags,
                config_path=ctx.config_path,
                cli_secret_overrides=list(ctx.set_secret_overrides),
                mode_name=ctx.mode_name,
                workflow_name=ctx.workflow_name,
                owner_lease=owner_lease,
                config=ctx.config,
            )
        )
    except SessionAlreadyActiveError as exc:
        _exit_for_session_owner_conflict(exc)
    except SessionStorageError as exc:
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"TUI error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        # Windows can still deliver a console Ctrl+C as an OS-level interrupt
        # when a host terminal ignores the raw-input mode request. Treat it as
        # a normal TUI exit after asyncio has run its cleanup path; never leave
        # a traceback or a half-reset terminal behind.
        pass
    finally:
        if owner_lease is not None:
            owner_lease.release()
        _reset_terminal_on_exit()
