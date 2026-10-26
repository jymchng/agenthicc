"""TUI session — starts the reactive runtime (PRD-58 to PRD-67, PRD-93)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import os
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Literal, NoReturn, cast

from agenthicc.supervised_tasks import SupervisedTasks

log = logging.getLogger(__name__)


def _optional_field(value: object, name: str, default: object = None) -> object:
    """Read optional fields from lightweight UI test doubles and adapters."""
    try:
        return object.__getattribute__(value, name)
    except AttributeError:
        return default


if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._config import LLMConfig
    from lauren_ai._signals import SignalBus
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.cli.context import CLIContext, CLIFlags
    from agenthicc.config import AgenthiccConfig
    from agenthicc.config import ExecutionSettings
    from agenthicc.config import CloakBrowserSettings, PlaywrightSettings
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.tui.workspace import Workspace
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime import (
        InterruptAgentCommand,
        OpenAgentsHubCommand,
        OpenSubagentsCommand,
        SendMessageCommand,
    )
    from agenthicc.tools.approval import ApprovalBroker, ApprovalRequest, ApprovalRequester
    from agenthicc.tui.workspace.overlay import Overlay
    from agenthicc.tui.workspace.overlays.subagents import TranscriptTarget
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
    from agenthicc.background.agents_service import AgentsService
    from agenthicc.background.settings import BackgroundSettings
    from agenthicc.background.supervisor import BackgroundSupervisor
    from agenthicc.tui.workspace.agents_hub import HubResult
    from agenthicc.runners.workflow_recovery import WorkflowRecoveryRecord
    from agenthicc.runners.workflow_profile import ResumeProviderProfile
    from agenthicc.runners.startup import StartupCoordinator
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.cloakbrowser.session import BrowserSessionManager
    from agenthicc.background.terminals import TerminalManager
    from agenthicc.memory.tool_history import ToolHistoryRecoveryState


def _make_session_tools(
    approval_svc: ApprovalBroker | None,
    memory_router: MemoryRouter | None = None,
    semantic_index: SemanticIndex | None = None,
) -> list[ToolLike]:
    """Tools injected into every interactive agent turn (Yolo mode + plan phase)."""
    from agenthicc.workflows.code_plan.phase_tools import make_questions_tool
    from agenthicc.workflows.memory_tools import make_memory_tools

    return make_questions_tool(approval_svc) + make_memory_tools(memory_router, semantic_index)


def _build_agent_runner(
    llm_config: Callable[[], LLMConfig] | None,
    *,
    cassette_dir: Path | None = None,
    startup: StartupCoordinator | None = None,
) -> _LazyAgentRunner | None:
    """Return a proxy that constructs lauren-ai's runner on demand.

    Building the provider config, transport and runner imports lauren-ai, one
    of the largest local startup costs. The TUI warms the proxy up on a worker
    thread after its first frame (AGX-02); otherwise the first agent turn
    constructs it. The proxy preserves the private transport/signal contract
    used by AgentTurnRunner.
    """
    if llm_config is None:
        return None
    return _LazyAgentRunner(llm_config, cassette_dir=cassette_dir, startup=startup)


def _prepare_startup_dependency(ctx: "SessionContext", phase: str) -> None:
    """Materialize a dependency explicitly declared by a workflow.

    Most readiness phases are already started by session construction or the
    post-frame coordinator. Provider and browser phases are deliberately
    demand-driven, however. A workflow that declares either as required must
    be allowed to opt into that cost before its first agent turn; it must not
    wait forever for a lazy proxy that has not yet been touched.
    """
    resource: object
    if phase == "provider":
        resource = ctx.agent_runner
    elif phase == "browser":
        resource = ctx.browser_manager
    else:
        return
    # Only the lazy proxies defer construction; a real runner or browser
    # manager is already prepared.
    if isinstance(resource, (_LazyAgentRunner, _LazyBrowserSession)):
        resource.prepare()


async def _run_agent_turn(*args: object, **kwargs: object) -> None:
    """Lazy compatibility shim retained for TUI tests and integrations."""
    from agenthicc.runners.agent_turn import _run_agent_turn as run_agent_turn

    typed_runner = cast("Callable[..., Awaitable[None]]", run_agent_turn)
    await typed_runner(*args, **kwargs)


def _preserve_interrupted_memory(memory: ShortTermMemory | None) -> bool:
    """Lazy compatibility shim for the interruption recovery path."""
    from agenthicc.runners.agent_turn import (
        _preserve_interrupted_memory as preserve,
    )

    return preserve(memory)


class _LazyAgentRunner:
    """First-use proxy for lauren-ai's provider runner."""

    def __init__(
        self,
        llm_config: Callable[[], LLMConfig],
        *,
        cassette_dir: Path | None,
        startup: StartupCoordinator | None = None,
        signals: "SignalBus | None" = None,
    ) -> None:
        self._llm_config = llm_config
        self._cassette_dir = cassette_dir
        self._startup = startup
        self._delegate: AgentRunnerBase | None = None
        self._signals = signals
        # The warm-up thread and the event loop may race to build the runner;
        # whichever loses waits for the same instance.
        self._lock = threading.Lock()

    def _ensure(self) -> AgentRunnerBase:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        from agenthicc.runners.startup import ReadinessState

        startup = self._startup
        if startup is None or startup.report("provider").state is ReadinessState.LOADING:
            # Unobserved, or a warm-up thread owns the phase report.
            return self._materialize()
        startup.begin("provider", deferred=True)
        try:
            delegate = self._materialize()
        except Exception as exc:
            startup.finish(
                "provider",
                ReadinessState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        startup.finish("provider")
        return delegate

    def _materialize(self) -> AgentRunnerBase:
        """Build the runner exactly once; safe to call from a worker thread."""
        with self._lock:
            if self._delegate is None:
                self._delegate = self._create()
            return self._delegate

    def _create(self) -> AgentRunnerBase:
        """Build the real runner, importing lauren-ai only on demand."""
        from lauren_ai._agents._runner import AgentRunnerBase
        from lauren_ai._module import _build_transport
        from lauren_ai._signals import SignalBus

        transport = _build_transport(self._llm_config())
        if self._cassette_dir is not None:
            from agenthicc.testing.recording_transport import RecordingTransport

            self._cassette_dir.mkdir(parents=True, exist_ok=True)
            transport = RecordingTransport(transport, self._cassette_dir / "cassette.jsonl")
        if self._signals is None:
            self._signals = SignalBus()
        return AgentRunnerBase(transport=transport, signals=self._signals)

    def for_execution(
        self,
        execution: "ExecutionSettings",
        *,
        conversation_id: str,
    ) -> "_LazyAgentRunner":
        """Create a workflow-scoped runner with this session's signal bus.

        The regular session runner stays bound to its normal configuration;
        only the resumed workflow receives the selected provider profile.
        """

        from agenthicc.config import build_llm_config
        from lauren_ai._signals import SignalBus

        # Do not materialize the session's default provider here: it may be
        # precisely the invalid or unavailable profile the user is switching
        # away from. Reuse its bus if already built; otherwise initialize the
        # session bus without constructing any provider transport.
        with self._lock:
            signals = self._signals
            if signals is None:
                signals = SignalBus()
                self._signals = signals
        return _LazyAgentRunner(
            partial(build_llm_config, execution, conversation_id=conversation_id),
            cassette_dir=self._cassette_dir,
            startup=None,
            signals=signals,
        )

    async def warm_up(self) -> None:
        """Build the runner on a worker thread, recording the provider phase.

        A failure is recorded as a failed ``provider`` phase and re-raised; the
        agent-operation boundary reports it when a turn needs the provider.
        """
        if self._delegate is not None:
            return
        startup = self._startup
        if startup is None:
            await asyncio.to_thread(self._materialize)
            return
        await startup.run(
            "provider",
            lambda: asyncio.to_thread(self._materialize),
            deferred=True,
        )

    def prepare(self) -> AgentRunnerBase:
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
        self._delegate: BrowserSessionManager | None = None
        self._pending_checkpoint: dict[str, object] | None = None

    def _ensure(self) -> BrowserSessionManager:
        if self._delegate is not None:
            return self._delegate
        from agenthicc.runners.startup import ReadinessState

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
        return delegate

    def _create_delegate(self) -> BrowserSessionManager:
        """Import and construct the selected backend only on capability use."""
        if self._backend == "playwright":
            from agenthicc.tools.playwright import create_playwright_session

            delegate = create_playwright_session(
                cast("PlaywrightSettings", self._settings),
                conversation_id=self._conversation_id,
                workspace_root=self._workspace_root,
            )
        elif self._backend == "cloakbrowser":
            from agenthicc.tools.cloakbrowser import create_browser_session

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
        if self._delegate is not None:
            await self._delegate.close_session()

    def reset_turn_budget(self) -> None:
        """Reset browser accounting without starting an unused backend."""
        if self._delegate is not None:
            self._delegate.reset_turn_budget()

    def checkpoint_payload(self) -> dict[str, object]:
        """Return empty browser state until a browser has actually been used."""
        return self._delegate.checkpoint_payload() if self._delegate is not None else {}

    def restore_checkpoint(self, payload: dict[str, object]) -> None:
        """Remember checkpoint state without importing a browser backend."""
        if self._delegate is None:
            self._pending_checkpoint = payload
            return
        self._delegate.restore_checkpoint(payload)

    def _restore_pending_checkpoint(self) -> None:
        if self._pending_checkpoint is None or self._delegate is None:
            return
        self._delegate.restore_checkpoint(self._pending_checkpoint)
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
        from agenthicc.runners.startup import ReadinessState

        startup = self._startup
        if startup is not None and not startup.closed:
            startup.begin("browser", deferred=True)
        try:
            if self._backend == "playwright":
                from agenthicc.tools.playwright import make_playwright_tools

                self._tools = list(
                    make_playwright_tools(cast("BrowserSessionManager", self._manager))
                )
            elif self._backend == "cloakbrowser":
                from agenthicc.tools.cloakbrowser import make_cloakbrowser_tools

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
        self._delegate: SemanticIndex | None = None
        self._startup = startup

    def _ensure(self) -> "SemanticIndex":
        if self._delegate is None:
            from agenthicc.runners.startup import ReadinessState

            startup = self._startup
            if startup is not None and not startup.closed:
                startup.begin("semantic_index", deferred=True)
            try:
                from agenthicc.memory.vector import SemanticIndex

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

    async def add(self, doc_id: str, text: str, embedding: list[float] | None = None) -> str:
        return await self._ensure().add(doc_id, text, embedding)

    async def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        return await self._ensure().search(query, top_k)

    def index_in_background(self, doc_id: str, text: str) -> None:
        self._ensure().index_in_background(doc_id, text)

    async def aclose(self) -> None:
        """Finish background indexing; an index that was never built has none."""
        if self._delegate is not None:
            await self._delegate.aclose()

    def __len__(self) -> int:
        return len(self._ensure())


def _fmt_exc(exc: BaseException) -> str:
    """Format an exception as 'ExceptionType: message' for scroll-buffer display.

    Never returns a bare ``str(exc)`` — the exception class name is always
    included so users can identify the failure type (e.g. ``ReadTimeout``).
    """
    name = type(exc).__name__
    msg = str(exc).strip()
    return f"{name}: {msg}" if msg else name


def _workflow_failure_kind(exc: object) -> str:
    """Map common workflow exception classes to stable recovery categories."""
    from agenthicc.runners.workflow_handle import classify_workflow_failure

    disposition = classify_workflow_failure(exc)
    text = str(exc).lower()
    if disposition.kind == "workflow_error" and any(
        marker in text for marker in ("config", "profile")
    ):
        return "configuration"
    return disposition.kind


def _build_skill_command(slug: str, skill: "SkillDef") -> "Command":
    """Build the dollar-prefixed command owned by one discovered skill."""
    from agenthicc.commands.builtins import _make_skill_handler
    from agenthicc.commands.command import Command

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
    create_session_id,
    register_session,
    update_session_mode,
    load_session_mode,
    touch_session,
    find_latest_session_for_cwd,
    SessionEventLog,
)
from agenthicc.runners.session_lease import (  # noqa: E402
    SessionAlreadyActiveError,
    SessionOpenCoordinator,
    SessionOwnerLease,
    SessionStorageError,
    format_session_conflict,
)


_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
# Slash commands that hand the foreground session to the background service.
_BACKGROUND_HANDOFF_COMMANDS = frozenset({"/bg", "/background", "/detach"})

# Module-level alias so tests that monkeypatch this name on the module work.
_find_latest_session_for_cwd = find_latest_session_for_cwd


@dataclass(frozen=True)
class _ExtensionDiscovery:
    """Results produced off the event loop by deferred extension loading."""

    skills: dict[str, SkillDef]
    project_plugins: PluginToolSet
    project_commands: list[Command]
    agents_registry: AgentsRegistry
    installed_skills: int = 0
    diagnostics: tuple[object, ...] = ()


_EXTENSION_RESULT_CACHE: dict[str, _ExtensionDiscovery] = {}


def _discover_extensions_sync(cfg: "AgenthiccConfig") -> _ExtensionDiscovery:
    """Discover skills, tools, and commands without mutating live state."""
    from agenthicc.agents.registry import build_agents_registry
    from agenthicc.commands.plugin_loader import discover_command_plugins
    from agenthicc.plugins.discovery import discover_project_tools
    from agenthicc.skills.bootstrap import bootstrap_default_skills
    from agenthicc.skills.loader import discover_skills_with_diagnostics
    from agenthicc.runners.discovery_cache import (
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
    # Skills live in ``<global dir>/skills``; fingerprinting the global
    # directory itself would walk every session journal and log under it.
    source_roots = (
        skill_global_dir / "skills",
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
    from agenthicc.plugins.discovery import warn_conflicts

    ctx.skills.clear()
    ctx.skills.update(discovery.skills)
    ctx.project_plugins.results.clear()
    ctx.project_plugins.results.extend(discovery.project_plugins.results)
    ctx.agents_registry.replace_with(discovery.agents_registry)
    if ctx.project_plugins.all_tools:
        warn_conflicts(ctx.project_plugins)
    for command in discovery.project_commands:
        try:
            ctx.cmd_registry.register(command)
        except Exception:  # noqa: BLE001
            log.warning("Command plugin %r could not be registered", command.name, exc_info=True)
    ctx.command_plugin_names.update(command.name for command in discovery.project_commands)
    _register_skill_commands(ctx.cmd_registry, ctx.skills)


def _discover_workflow_registry_sync() -> "WorkflowRegistry":
    """Load external workflow modules outside the interactive event loop."""
    from agenthicc.workflows.registry import build_workflow_registry

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

    from agenthicc import STARTED_AT
    from agenthicc.runners.startup import ReadinessState, StartupCoordinator

    session_id = resume_id or (
        owner_lease.session_id if owner_lease is not None else create_session_id()
    )
    startup = StartupCoordinator(process_started_at=STARTED_AT)
    startup.begin("bootstrap")
    if not headless:
        startup.begin("time_to_interactive_input")
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
        startup.begin("session_context")
        context = await _build_session_context_impl(
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
        startup.finish("session_context")
        return context
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
        for phase in ("session_context", "time_to_interactive_input"):
            report = startup.report(phase)
            if report.state.value == "loading":
                startup.finish(
                    phase,
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
    from agenthicc.runners.startup import ReadinessState, StartupCoordinator

    startup = startup or StartupCoordinator()
    from rich.console import Console
    from agenthicc.kernel import (
        AppState as KAppState,
        EventProcessor,
        SecurityPolicy,
        SystemSettings,
    )
    from agenthicc.kernel.reducer import root_reducer
    from agenthicc.kernel.processor import restore_from_log
    from agenthicc.config import load_config, build_llm_config
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime import (
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
    from agenthicc.session_service import SessionService

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
        await asyncio.to_thread(cassette_dir.mkdir, parents=True, exist_ok=True)

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
        # A session with no kernel log yet restores to the initial state.
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
    from agenthicc.background.settings import load_background_settings
    from agenthicc.background.terminals import TerminalManager

    background_settings = load_background_settings(
        config_path=config_path,
        overrides=tuple(cli_overrides or ()),
        cwd=Path.cwd(),
    )
    from agenthicc.background.inbox import BackgroundInteractionInbox
    from agenthicc.background.store import BackgroundStore

    background_root = background_settings.store_root
    session_service.set_background_inbox(
        BackgroundInteractionInbox(BackgroundStore(background_root))
    )
    # Reconcile a durable notification outbox even when this invocation is
    # not opening the agents hub; this recovers delivery after a restart.
    try:
        from agenthicc.background.store import BackgroundStore

        BackgroundStore(background_root).configure_notifications(background_settings.notifications)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "background notification recovery unavailable (%s)", type(exc).__name__
        )
    terminal_root = background_root / "terminals" if background_root is not None else None
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
    from agenthicc.tools.http import configure as _configure_http

    _configure_http(cfg.tools.http_timeout_s)

    console = Console(
        highlight=False,
        markup=True,
        # agenthicc never writes emoji shortcodes, and replacing them would
        # alter literal ``:name:`` text and load Rich's emoji table at startup.
        emoji=False,
        force_terminal=not headless,
        quiet=headless,
    )
    llm_config_error: str | None = None
    llm_config: Callable[[], LLMConfig] | None
    try:
        # Resolve the selected profile immediately before constructing the
        # transport.  This keeps secrets out of durable session/workflow state
        # and re-reads rotated environment values on every resume.
        cfg.resolve_provider_profile(requires_tools=True)
        # ``session_id`` is the same validated identity later used by
        # SessionConversation.conversation_id.  Pass it at transport
        # construction so dynamic provider headers (for example
        # x-opencode-session) are stable across every turn and retry.
        llm_config = partial(build_llm_config, cfg.execution, conversation_id=session_id)
        if headless:
            # Headless callers report a configuration error before any turn.
            # Building the config imports lauren-ai, so the TUI builds it with
            # its agent runtime after the first frame instead (AGX-02).
            llm_config()
    except ValueError as exc:
        llm_config_error = str(exc)
        console.print(
            f"[red]LLM config error: {exc}[/red]\n"
            "[dim]Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or --set execution.provider=...[/dim]",
            markup=True,
        )
        llm_config = None

    model_label = f"{cfg.execution.provider}/{cfg.execution.effective_model()}"

    # ── reactive state ────────────────────────────────────────────────────────
    app_state = AppState.create(defer_keymap=not headless)
    app_state.conversation.model_name.set(model_label)
    app_state.conversation.session_id.set(session_id)
    app_state.ui.set(cfg.ui)

    session_log = SessionEventLog(session_id)
    pending_question_waits: dict[str, dict[str, object]] = {}
    if resume_id:
        from agenthicc.tui.runtime.session_log import load_pending_question_waits

        pending_question_waits = load_pending_question_waits(session_id)
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

    from agenthicc.tools.approval import ApprovalService, InteractiveApprovals

    approval_svc: InteractiveApprovals = ApprovalService(
        app_state,
        question_timeout_s=cfg.tools.question_timeout_s,
        question_wait_records=pending_question_waits,
    )
    if cassette_dir is not None:
        from agenthicc.testing.recording_approval import RecordingApprovalService

        approval_svc = RecordingApprovalService(approval_svc, cassette_dir / "approvals.jsonl")

    # ── workflow + agents registries ──────────────────────────────────────────
    startup.begin("workflow_registry")
    from agenthicc.workflows.registry import build_workflow_registry

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
    from agenthicc.agents.registry import build_agents_registry

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
    from agenthicc.tools.workspace_access import (
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
    from agenthicc.plugins.discovery import PluginToolSet

    if not defer_extensions:
        startup.begin("extensions")
    if defer_extensions:
        skills: dict[str, SkillDef] = {}
        project_plugins = PluginToolSet()
        project_commands: list[Command] = []
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
            from agenthicc.plugins.discovery import warn_conflicts

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
        from agenthicc.tools.mcp_manager import (
            McpRequiredServerError,
            McpSessionManager,
        )
        from agenthicc.tools.sandbox import NetworkGuard

        try:
            mcp_manager = McpSessionManager(
                (),
                event_processor=processor,
                conversation_store=app_state.conversation,
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

    from agenthicc.mentions.cache import MentionCache

    mention_cache = MentionCache()

    startup.begin("memory")
    # PRD-129 Phase 2: durable conversation journal.  session_memory is a
    # JournaledShortTermMemory — every transition is fsync'd to a per-session
    # append-only journal, and on resume (session_id == resume_id) the journal
    # is folded straight back into memory.  This supersedes the old SQLite
    # memory-snapshot durability (which only checkpointed at turn boundaries).
    # AGX-02: the TUI folds the journal on a worker thread after the first
    # frame (TUISession._start_agent_runtime); headless callers need the
    # restored memory before they return.
    from agenthicc.runners.session_conversation import SessionConversation

    session_conversation = SessionConversation.open_deferred(
        session_id,
        max_tokens=cfg.execution.effective_usable_budget,
    )
    if headless:
        startup.begin("provider_journal_restore")
        try:
            session_conversation.load_memory()
        except BaseException as exc:
            startup.finish(
                "provider_journal_restore",
                ReadinessState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            session_conversation.close()
            raise
        startup.finish("provider_journal_restore")

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

    from agenthicc.runners.usage_ledger import UsageLedger

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
        from agenthicc.tools.fs.file_cache import (
            WorkspaceFileCache,
            configure_file_cache,
        )

        configure_file_cache(WorkspaceFileCache(Path(".agenthicc") / "cache" / "file-cache.db"))

    # ── three-tier memory (PRD-101) ───────────────────────────────────────────
    from agenthicc.memory.layers import (
        ProjectMemoryLayer,
        GlobalMemoryLayer,
        SessionMemoryLayer,
    )
    from agenthicc.memory.router import MemoryRouter

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
    from agenthicc.tui.trigger import TriggerManager
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger
    from agenthicc.tui.triggers.slash_command import (
        SkillTrigger,
        SlashCommandTrigger,
    )
    from agenthicc.commands import build_builtin_registry

    cmd_registry = build_builtin_registry()
    for command in project_commands:
        try:
            cmd_registry.register(command)
        except Exception:  # noqa: BLE001
            log.warning("Command plugin %r could not be registered", command.name, exc_info=True)
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
        llm_config,
        cassette_dir=cassette_dir,
        startup=startup,
    )

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
        from rich.rule import Rule

        console.print(Rule(f"[dim]resumed session {session_id[:12]}[/dim]"))
        from agenthicc.runners.run_coordinator import RunCoordinator

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
        session_conversation=session_conversation,
        mention_cache=mention_cache,
        skills=skills,
        project_plugins=project_plugins,
        mcp_registry=mcp_registry,
        terminal_manager=terminal_manager,
        cfg=cfg,
        session_id=session_id,
        model_label=model_label,
        llm_config_error=llm_config_error,
        config_path=config_path,
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


@dataclass(frozen=True)
class SessionSwitch:
    """Where a session that ended goes next (AGX-05 §5.2).

    ``attach`` continues a background agent in the foreground and
    ``open_saved`` resumes a saved session; when that session ends, the
    *origin* session reopens. ``hub`` follows ``/background``: the agents hub
    opens with the new background agent selected.
    """

    action: Literal["attach", "open_saved", "hub"]
    target: str
    origin: str
    origin_cwd: str


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
        # Foreground /background handoff state (agenthicc.background.integration).
        self._background_intent: str = ""
        self._background_handoff_state: str = "idle"
        # A switch to another agent chosen in the agents hub (AGX-05 §5.2).
        self._switch: SessionSwitch | None = None
        # The agents hub's store and executor, kept from its first opening to
        # the end of the session so reopening it costs no setup.
        self._agents_service: AgentsService | None = None
        self._agents_settings: BackgroundSettings | None = None
        self._background_prepared_supervisor: BackgroundSupervisor | None = None
        self._background_prepared_session_id: str | None = None
        self._turn_count: int = 0
        self._pending_replay_id: str | None = None
        self._workflow_override: str | None = ctx.initial_workflow  # PRD-114: /workflow command
        if self._workflow_override is not None:
            self._set_workflow_override(self._workflow_override)
        self._workflow_handle: WorkflowRunHandle | None = None
        # Commands that run in the background (for example /compact).
        self._tasks = SupervisedTasks("tui session")
        from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator

        self._workflow_recovery = WorkflowRecoveryCoordinator(ctx.session_id)
        self._workflow_recovery_records: dict[str, WorkflowRecoveryRecord] = {}
        self._workflow_recovery_errors: dict[str, WorkflowRecoveryRecord] = {}
        self._workflow_owner_prefix = f"tui:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        terminal_manager = ctx.terminal_manager
        if terminal_manager is not None:
            self._terminal_unsub = terminal_manager.changed.subscribe(self._sync_terminal_status)
        else:
            self._terminal_unsub = lambda: None

        from agenthicc.commands import CommandDispatcher
        from agenthicc.workflows.config import WorkflowConfig
        from agenthicc.runners.loop_scheduler import LoopManager, LoopStore

        self._cmd_dispatcher = CommandDispatcher(ctx.cmd_registry)
        self._loop_manager = LoopManager(
            session_id=ctx.session_id,
            settings=ctx.cfg.loops,
            store=LoopStore(_SESSIONS_DIR, ctx.session_id),
            is_idle=self._loop_is_idle,
            dispatch=self._dispatch_loop_iteration,
            event_sink=self._record_loop_event,
            payload_validator=self._validate_loop_payload,
        )
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
            # Each run binds the session memory when it starts; the journal is
            # folded only after the first frame (AGX-02).
            conversation_id=ctx.session_id,
            usage_ledger=ctx.usage_ledger,
            browser_manager=ctx.browser_manager,
            browser_tools=ctx.browser_tools,
            workspace_scope=ctx.workspace_scope,
            workspace_access=ctx.workspace_access,
            startup=ctx.startup,
            next_queued_message=self._next_queued_message,
        )
        # Loads the conversation memory and provider after the first frame;
        # input that needs them is queued until it finishes (AGX-02).
        self._runtime_pending = False
        self._runtime_task: asyncio.Task[object] | None = None
        self._account_task: asyncio.Task[None] | None = None
        self._ad_task: asyncio.Task[None] | None = None
        self._sync_terminal_status()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set_pending_skill(self, body: str) -> None:
        self._pending_skill_body.clear()
        self._pending_skill_body.append(body)

    def _tool_history_state(self) -> ToolHistoryRecoveryState | None:
        """Read the session conversation's redacted provider-readiness state."""
        from agenthicc.runners.session_conversation import (
            SessionConversation,
        )

        conversation = self._ctx.session_conversation
        return (
            conversation.tool_history_recovery
            if isinstance(conversation, SessionConversation)
            else None
        )

    def _tool_history_blocked(self) -> bool:
        state = self._tool_history_state()
        return state is not None and not state.provider_ready

    def _notify_tool_history_recovery(self) -> None:
        """Keep an unready session inspectable and show the safe repair action."""
        state = self._tool_history_state()
        if state is None or state.provider_ready:
            return
        summary = (
            ", ".join(code[:64] for code in state.issue_codes[:4]) or "history validation failed"
        )
        if state.status == "journal_corrupt":
            message = (
                "⚠ Session history is preserved and can be inspected, but agent/workflow "
                f"requests are blocked ({summary}). Automatic repair is disabled because "
                "the journal tail may contain skipped records; preserve the journal and "
                "restore only from a known-good backup."
            )
        else:
            message = (
                "⚠ Session history is preserved and can be inspected, but agent/workflow "
                f"requests are blocked ({summary}). Run /history repair to retry safe local repair."
            )
        self._ctx.app_state.conversation.notify_transient(message)

    def _repair_session_history(
        self,
        *,
        success_message: str = (
            "✓ Conversation history is provider-ready. Resume the saved workflow explicitly if needed."
        ),
    ) -> ToolHistoryRecoveryState:
        """Retry deterministic local repair without sending a provider request."""
        conversation = self._ctx.session_conversation
        if conversation is None:
            raise RuntimeError("session conversation recovery is unavailable")
        state = conversation.retry_tool_history_recovery()
        self._publish_session_readiness()
        if state.provider_ready:
            self._ctx.app_state.conversation.notify_transient(success_message)
        else:
            self._notify_tool_history_recovery()
        return state

    def _publish_session_readiness(self) -> None:
        """Publish only bounded recovery readiness, never transcript contents."""
        state = self._tool_history_state()
        if state is None:
            provider_status = "unknown"
            provider_ready: bool | None = None
        else:
            provider_status = state.status
            provider_ready = state.provider_ready

        candidates = self._workflow_recovery_records
        errors = self._workflow_recovery_errors
        if candidates:
            workflow_status = "resumable"
            workflow_resumable: bool | None = True
        elif errors:
            workflow_status = "blocked"
            workflow_resumable = False
        else:
            workflow_status = "none"
            workflow_resumable = False
        self._publish_session_event(
            "session_readiness_changed",
            {
                "session_viewable": True,
                "provider_history_status": provider_status,
                "provider_history_ready": provider_ready,
                "workflow_recovery_status": workflow_status,
                "workflow_resumable": workflow_resumable,
            },
        )

    def _loop_is_idle(self) -> bool:
        """Return whether a loop may enqueue one normal user turn."""

        task = self._agent_task
        if task is not None and not task.done():
            return False
        if self._runtime_loading():
            return False
        return self._ctx.app_state.pending_approval() is None

    def _record_loop_event(self, kind: str, payload: dict[str, object]) -> None:
        """Project scheduler lifecycle events through the normal transcript log."""

        self._ctx.app_state.conversation.append_event(kind, payload)
        manager = vars(self).get("_loop_manager")
        from agenthicc.runners.loop_scheduler import LoopLifecycle, LoopManager

        if isinstance(manager, LoopManager):
            status = manager.status_text()
            record = manager.record
            terminal = record is not None and record.state in {
                LoopLifecycle.STOPPED,
                LoopLifecycle.EXPIRED,
                LoopLifecycle.FAILED,
            }
            self._ctx.app_state.conversation.loop_status.set(
                None if status == "No active loop." or terminal else status
            )

    def _validate_loop_payload(self, payload: str, kind: object) -> str | None:
        """Validate scheduled command payloads without executing them."""

        del kind
        if not payload.startswith("/"):
            return None
        command_name = payload.split(None, 1)[0]
        if command_name == "/loop":
            return "A loop payload cannot schedule another loop."
        command = self._ctx.cmd_registry.get(command_name)
        if command is None:
            return f"Unknown scheduled command {command_name!r}. Use /help to inspect commands."
        if command_name in {"/workflow", "/compact"}:
            return f"Scheduled command {command_name} is not supported by /loop."
        return None

    async def _dispatch_loop_iteration(self, record: object) -> None:
        """Submit one loop payload through ``handle_send`` and await its turn."""

        from agenthicc.runners.loop_scheduler import LoopRecord
        from agenthicc.tui.runtime.commands import SendMessageCommand

        if not isinstance(record, LoopRecord):
            raise TypeError("loop dispatch received an invalid record")
        if not self._loop_is_idle():
            raise RuntimeError("session became busy before loop dispatch")
        await self.handle_send(SendMessageCommand(text=record.payload, source="loop"))
        task = self._agent_task
        if task is not None and not task.done():
            await asyncio.shield(task)

    def _set_pending_replay(self, session_id: str) -> None:
        self._pending_replay_id = session_id

    def _reset_workflow_to_mode_default(self) -> None:
        """Remove the session-local workflow override.

        Reset is a selection operation: the next ordinary turn must resolve
        its workflow from the active mode, even when recovery inspection has
        found a saved run that still needs an explicit run-id decision.
        """
        self._set_workflow_override(None)

    def _set_workflow_override(self, name: str | None) -> None:
        """Keep the session's workflow selector and its reactive projection in sync."""
        self._workflow_override = name
        self._ctx.app_state.conversation.workflow_override.set(name)

    def _dispatch_workflow_resume(
        self,
        definition: type[WorkflowPlugin],
        handle: "WorkflowRunHandle",
        context: object,
        *,
        continuation: str | None = None,
        turn_id: str | None = None,
        profile_selection: "ResumeProviderProfile | None" = None,
    ) -> None:
        """Publish and schedule an already accepted resume transition.

        Callers must validate the checkpoint, acquire its claim, and durably
        persist ``resuming`` before reaching this boundary. Keeping selection
        here ensures every interactive resume path updates dispatch state and
        the footer projection before the runner task can yield.
        """
        self._set_workflow_override(handle.workflow_name)
        if continuation is not None and turn_id is not None:
            self._publish_session_event(
                "turn_queued", {"text": continuation, "client_id": "tui"}, turn_id=turn_id
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
                context,
                continuation=continuation,
                turn_id=turn_id,
                profile_selection=profile_selection,
            ),
            name=f"resume-workflow-{handle.run_id}",
        )

    def _activate_streaming_input(self) -> None:
        """Enable interruption before a newly created agent task can await.

        The activity state can render ``Thinking`` before the scheduled task
        gets its first event-loop slice.  Setting the mode at task ownership
        time closes that race, so an immediate ESC is handled as an interrupt.
        """
        from agenthicc.tui.input.unified_session import InputMode

        self._input_session.set_mode(InputMode.STREAMING)

    def _record_returned_workflow_result(self, result: object | None) -> None:
        """Project a typed runner failure before normal-return finalization.

        Several specialized runners intentionally return their context with a
        terminal ``FAILED`` enum after a bounded phase loop. Without this
        projection the session owner cannot distinguish that result from a
        successful normal return and may mark the handle complete. The
        durable failure decision still belongs to ``_finalize_returned_workflow``.
        """
        state_value = _optional_field(result, "state", "")
        state_name = _optional_field(state_value, "name", "")
        if not isinstance(state_name, str) or state_name.lower() not in {"failed", "failure"}:
            return
        workflow_run = self._ctx.app_state.workflow_run()
        if workflow_run is None:
            return
        import dataclasses as _dc

        if not _dc.is_dataclass(workflow_run):
            return
        handle = self._workflow_handle
        current_phase = _optional_field(handle, "current_phase")
        current_phase = current_phase if isinstance(current_phase, str) else None
        self._ctx.app_state.workflow_run.set(
            _dc.replace(
                workflow_run,
                status="failed",
                current_phase=current_phase,
            )
        )

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
        context_value = _optional_field(handle, "context")
        context_state = _optional_field(context_value, "state", "")
        returned_failed = str(_optional_field(context_state, "name", "")).lower() in {
            "failed",
            "failure",
        }
        if (
            handle is None
            or handle.lifecycle in {"complete", "discarded"}
            or (
                handle.lifecycle == "failed"
                and (workflow_run.status if workflow_run is not None else None) != "failed"
            )
        ):
            if handle is not None:
                self._release_workflow_claim(handle)
            return
        status = workflow_run.status if workflow_run is not None else None
        if status == "failed" or returned_failed:
            # A runner can convert a provider/phase exception into its typed
            # FAILED state and return normally. Keep a valid typed context
            # resumable; only the handle decides whether that is safe.
            context_error = (
                getattr(handle.context, "fail_reason", "") or "workflow returned unsuccessfully"
            )
            checkpoint = handle.finalize_failure(
                context_error, kind="phase_execution", recoverable=True
            )
            if checkpoint is not None:
                if workflow_run is not None:
                    import dataclasses as _dc

                    if _dc.is_dataclass(workflow_run):
                        self._ctx.app_state.workflow_run.set(
                            _dc.replace(
                                workflow_run,
                                status="paused",
                                current_phase=handle.current_phase,
                            )
                        )
                self._ctx.app_state.conversation.notify_transient(
                    self._workflow_pause_notice(handle)
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
                    handle._save_failure_diagnostic(
                        error=f"completion checkpoint failed: {type(exc).__name__}",
                        kind="checkpoint_storage",
                    )
        self._release_workflow_claim(handle)

    @staticmethod
    def _workflow_pause_notice(handle: "WorkflowRunHandle") -> str:
        """Build a bounded, actionable pause message without secrets."""
        model = _optional_field(handle, "failure_model")
        provider = _optional_field(handle, "failure_provider")
        status_code = _optional_field(handle, "failure_status_code")
        if _optional_field(handle, "failure_retryable") is False:
            subject = "non-retryable provider error"
            details = ""
            if model:
                details = f" for model '{model}'"
            if provider:
                details += f" via {provider}"
            if status_code is not None:
                details += f" (HTTP {status_code})"
            subject += details
        else:
            subject = "an error"
        return (
            f"⚠ Workflow '{handle.workflow_name}' paused in phase "
            f"'{handle.current_phase or 'the current phase'}' after {subject}. "
            f"Saved run {handle.run_id}; fix the provider/model configuration if needed, "
            f"then use /workflow resume {handle.run_id}."
        )

    def _fail_workflow_run(
        self,
        error: str,
        *,
        kind: str = "workflow_error",
        recoverable: bool = True,
        error_event_id: str | None = None,
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
            conv.append_event("error", {"message": error}, event_id=error_event_id)

        handle = self._workflow_handle
        if handle is None:
            workflow_run = self._ctx.app_state.workflow_run()
            if workflow_run is not None:
                import dataclasses as _dc

                if _dc.is_dataclass(workflow_run):
                    self._ctx.app_state.workflow_run.set(
                        _dc.replace(workflow_run, status="failed", current_phase=None)
                    )
            return

        checkpoint = handle.finalize_failure(error, kind=kind, recoverable=recoverable)
        workflow_run = self._ctx.app_state.workflow_run()
        if workflow_run is not None:
            import dataclasses as _dc

            if _dc.is_dataclass(workflow_run):
                self._ctx.app_state.workflow_run.set(
                    _dc.replace(
                        workflow_run,
                        status="paused" if checkpoint is not None else "failed",
                        current_phase=handle.current_phase if checkpoint is not None else None,
                    )
                )
        if checkpoint is not None:
            self._ctx.app_state.conversation.notify_transient(self._workflow_pause_notice(handle))
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
                    "run_id": handle.run_id,
                    "workflow": handle.workflow_name,
                    "phase": handle.current_phase or "",
                    "failure_kind": handle.failure_kind or kind,
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
                "Use /workflow resume for the latest, or /workflow resume <run-id> for a specific run."
            )
            return

        record = candidates[0]
        try:
            self._workflow_handle = self._rehydrate_workflow_record(record, claim=False)
            checkpoint = record.checkpoint
            assert checkpoint is not None
            disposition = "interrupted" if record.interrupted else "paused"
            saved_profile = checkpoint.provider_profile
            profile_notice = (
                f" Saved provider profile '{saved_profile}' is inherited on resume; "
                "use --profile <name|none> to override."
                if saved_profile
                else " This older run uses the current provider configuration."
            )
            self._ctx.app_state.conversation.notification.set(
                f"Workflow '{checkpoint.workflow_name}' is {disposition} at "
                f"{checkpoint.current_phase or 'its saved state'}. "
                f"Use /workflow resume {record.run_id} to continue.{profile_notice}"
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
        conversation = self._ctx.session_conversation
        if conversation is None:
            return
        workspace_scope = self._ctx.workspace_scope
        workspace_root = str(workspace_scope.primary_root) if workspace_scope is not None else ""
        records = self._workflow_recovery.inspect(
            workflow_registry=self._ctx.workflow_registry,
            conversation=conversation,
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
                -self._workflow_record_activity_timestamp(record),
                -self._workflow_record_revision(record),
                record.run_id,
            ),
        )

    @staticmethod
    def _workflow_record_activity_timestamp(record: object) -> float:
        """Read latest-run ordering metadata with a test/plugin-safe fallback."""
        value = getattr(record, "activity_timestamp", None)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            checkpoint = getattr(record, "checkpoint", None)
            value = getattr(checkpoint, "updated_at", None)
            if value is None:
                value = getattr(checkpoint, "created_at", 0.0)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return 0.0
        timestamp = float(value)
        return timestamp if math.isfinite(timestamp) else 0.0

    @staticmethod
    def _workflow_record_revision(record: object) -> int:
        """Read the checkpoint revision for deterministic list ordering."""
        value = getattr(record, "checkpoint_revision", None)
        if not isinstance(value, int) or isinstance(value, bool):
            checkpoint = getattr(record, "checkpoint", None)
            value = getattr(checkpoint, "revision", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _select_latest_workflow_record(self) -> WorkflowRecoveryRecord | None:
        """Select the newest durable recoverable run for omitted-ID resume."""
        conversation = self._ctx.session_conversation
        if conversation is None:
            return None
        workspace_scope = self._ctx.workspace_scope
        workspace_root = str(workspace_scope.primary_root) if workspace_scope is not None else ""
        return self._workflow_recovery.select_latest_for_resume(
            workflow_registry=self._ctx.workflow_registry,
            conversation=conversation,
            workspace_root=workspace_root,
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
        conversation = self._ctx.session_conversation
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
            browser_manager=self._ctx.browser_manager,
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

        service = self._ctx.session_service
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

    def _approval_overlay(self, req: ApprovalRequest) -> Overlay:
        """The overlay that answers *req*: a plan review, questions or a tool approval."""
        from agenthicc.tools.approval import InteractiveApprovals
        from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
        from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay
        from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

        approval_svc = self._ctx.approval_svc
        if not isinstance(approval_svc, InteractiveApprovals):
            raise TypeError("the TUI needs an approval service its overlays can answer")
        hide = self._workspace.overlays.hide
        if req.kind == "plan_review":
            return PlanApprovalOverlay(req, approval_svc, hide)
        if req.kind == "questions":
            return QuestionsOverlay(req, approval_svc, hide)
        return ApprovalOverlay(
            req,
            approval_svc,
            hide,
            open_requester=lambda requester: self._open_requester_transcript(requester, req),
        )

    def _wire_approval_overlay(self) -> None:
        from agenthicc.tools.approval import InteractiveApprovals
        from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
        from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay
        from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay

        workspace = self._workspace
        if not isinstance(self._ctx.approval_svc, InteractiveApprovals):
            raise TypeError("the TUI needs an approval service its overlays can answer")
        app_state = self._ctx.app_state

        def _on_approval_change() -> None:
            req = app_state.pending_approval()
            if req is not None:
                workspace.overlays.show(self._approval_overlay(req))
            elif isinstance(
                workspace.overlays.widget,
                (ApprovalOverlay, PlanApprovalOverlay, QuestionsOverlay),
            ):
                workspace.overlays.hide()

        app_state.pending_approval.subscribe(_on_approval_change)

    def _sync_terminal_status(self) -> None:
        """Project the current terminal wait into the reactive status bar."""

        terminal_manager = self._ctx.terminal_manager
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
        from agenthicc.runners.startup import ReadinessState

        startup = self._ctx.startup
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

    def _start_agent_runtime(self) -> None:
        """Load the conversation memory and provider after the first frame (AGX-02)."""
        self._runtime_pending = True
        startup = self._ctx.startup
        self._runtime_task = (
            startup.start_background(
                "agent_runtime", self._load_agent_runtime, degrade_on_error=True
            )
            if startup is not None
            else asyncio.create_task(self._load_agent_runtime(), name="agent-runtime")
        )

    def _runtime_loading(self) -> bool:
        """Whether input that needs the agent runtime must still wait for it."""
        return bool(vars(self).get("_runtime_pending", False))

    async def _load_agent_runtime(self) -> object:
        """Load the runtime, then release input queued while it loaded.

        Queued input drains even when loading failed, so each queued turn
        reports the failed dependency instead of waiting forever.
        """
        failure: Exception | None = None
        try:
            await self._prepare_agent_runtime()
        except Exception as exc:  # noqa: BLE001 - recorded by the startup phase
            failure = exc
        finally:
            self._runtime_pending = False
        if self._msg_queue and (self._agent_task is None or self._agent_task.done()):
            self.advance()
        if failure is not None:
            raise failure
        self._tasks.start(self._prewarm_agents_hub(), name="agents-hub-prewarm")
        return None

    async def _prewarm_agents_hub(self) -> None:
        """Import the agents hub on a worker thread once the session is idle.

        ``/agents``, Ctrl+G and ← open the hub at once (AGX-05 §6) instead of
        paying for its first import on that keypress.
        """
        import importlib

        await asyncio.to_thread(
            importlib.import_module, "agenthicc.tui.workspace.agents_hub.overlay"
        )

    async def _prepare_agent_runtime(self) -> None:
        """Fold the conversation journal and build the provider off the event loop.

        Startup recovery that reads the conversation — paused workflows,
        provider-history readiness and an interrupted turn — runs once the
        memory exists.
        """
        ctx = self._ctx
        startup = ctx.startup
        conversation = ctx.session_conversation
        try:
            if startup is None:
                await conversation.load()
            else:
                await startup.run("provider_journal_restore", conversation.load, deferred=True)
        except Exception as exc:
            ctx.app_state.conversation.notify_transient(
                f"⚠ Conversation history could not be loaded ({type(exc).__name__}); "
                "agent requests are unavailable. See /startup."
            )
            raise
        self._restore_paused_workflow()
        self._publish_session_readiness()
        self._notify_tool_history_recovery()
        # A previous session's in-progress workflow is announced, never
        # started automatically: the user decides what to do next.
        self._notify_incomplete_workflow()
        runner = ctx.agent_runner
        if isinstance(runner, _LazyAgentRunner):
            try:
                await runner.warm_up()
            except Exception as exc:  # noqa: BLE001 - the failed phase blocks turns
                ctx.llm_config_error = str(exc)
                ctx.app_state.conversation.notify_transient(f"⚠ LLM config error: {exc}")
        # PRD-129 Phase 3: auto-resume a direct turn the prior session left
        # interrupted (no-op on a clean start or when a workflow was in progress).
        self._maybe_resume_interrupted_turn()

    async def _await_agent_runtime(self) -> None:
        """Join the post-frame conversation and provider phases before a turn.

        Raises :class:`StartupDependencyError` when either failed.
        """
        startup = self._ctx.startup
        if startup is None:
            return
        from agenthicc.runners.startup import ReadinessState

        for phase in ("provider_journal_restore", "provider"):
            state = startup.report(phase).state
            if state is ReadinessState.NOT_STARTED:
                continue
            if state is ReadinessState.LOADING:
                self._ctx.app_state.conversation.notify_transient("⏳ Loading agent runtime…")
            await startup.wait_for(phase)

    def _start_account_check(self) -> None:
        """Decide whether to show sponsored messages without blocking input."""
        self._account_task = asyncio.create_task(self._check_account(), name="account-check")

    async def _check_account(self) -> None:
        """Start sponsored messages for a free-tier account (best effort).

        The tier comes from a cache that stays fresh for a day, so an ordinary
        start does not touch the OS keyring; a stale cache is refreshed on a
        worker thread (AGX-02).
        """
        from agenthicc.auth import AuthClient

        auth = AuthClient()
        try:
            tier = await asyncio.to_thread(auth.account_tier)
        except Exception:  # noqa: BLE001 - sponsored messages never affect the session
            log.debug("account tier unavailable", exc_info=True)
            return
        if tier != "free":
            return
        from agenthicc.ads import AdRotator

        self._ad_task = asyncio.create_task(
            AdRotator(auth_client=auth, processor=self._ctx.processor).run(),
            name="sponsored-messages",
        )

    # ── agents hub (AGX-05) ───────────────────────────────────────────────────

    def open_agents_hub(self, tab: str = "", run_id: str = "") -> None:
        """Show the agents hub over the conversation (``/agents``, Ctrl+G, ←)."""
        from agenthicc.tui.workspace.agents_hub import AgentsHub, DispatchDefaults, Tab
        from agenthicc.tui.workspace.agents_hub.overlay import AgentsHubOverlay
        from agenthicc.tui.workspace.agents_hub.sources import HubSources

        ctx = self._ctx
        overlays = self._workspace.overlays
        current = overlays.widget
        if isinstance(current, AgentsHubOverlay):
            if tab:
                current.hub.switch_tab(Tab(tab))
            return
        service, settings = self._agents_hub_service()
        if service is None or settings is None:
            ctx.app_state.conversation.notify_transient(
                "Background agents are disabled by configuration"
            )
            return
        hub = AgentsHub(
            ctx.console,
            store=service.store,
            supervisor=service.supervisor,
            settings=settings.manager,
            service=service,
            sources=HubSources.default(Path.cwd()),
            app_state=ctx.app_state,
            tab=Tab(tab) if tab else Tab.RUNNING,
            run_id=run_id,
            in_session=True,
            dispatch=DispatchDefaults(
                config_path=ctx.config_path,
                set_overrides=ctx.cfg_overrides,
                set_secret_overrides=ctx.cfg_secret_overrides,
                dangerously_skip_permissions=ctx.app_state.cli_flags.dangerously_skip_permissions,
            ),
        )
        overlays.show(
            AgentsHubOverlay(
                hub,
                on_close=overlays.hide,
                on_result=self._switch_to,
                request_redraw=self._workspace._redraw,
            )
        )

    def _agents_hub_service(self) -> tuple[AgentsService | None, BackgroundSettings | None]:
        """The session's agents service, created when the hub first opens."""
        if self._agents_service is not None:
            return self._agents_service, self._agents_settings
        from agenthicc.background import (
            AgentsService,
            BackgroundStore,
            BackgroundSupervisor,
            background_enabled,
            load_background_settings,
        )

        ctx = self._ctx
        settings = load_background_settings(
            config_path=ctx.config_path, overrides=ctx.cfg_overrides
        )
        if not background_enabled(settings):
            return None, None
        store = BackgroundStore(settings.store_root)
        self._agents_service = AgentsService(
            store,
            BackgroundSupervisor.from_settings(store, settings),
            max_workers=settings.manager.max_in_flight_operations,
            poll_interval_s=settings.manager.maintenance_interval_s,
        )
        self._agents_settings = settings
        return self._agents_service, settings

    def _switch_to(self, result: HubResult) -> None:
        """Leave this session for another; this one reopens when that one ends."""
        overlays = self._workspace.overlays
        if result.action == "subagent" and result.session_id:
            # A worker of this session's turn: its transcript, over the conversation.
            pool_id, _, worker_id = result.session_id.partition(":")
            overlays.hide()
            target = self._transcript_target(pool_id, worker_id)
            if target is not None:
                self._open_subagent_transcript(target)
            return
        if result.session_id is None or result.action not in ("attach", "open_saved"):
            overlays.hide()
            return
        task = self._agent_task
        if task is not None and not task.done():
            self._ctx.app_state.conversation.notify_transient(
                "Finish or stop the current turn before switching to another agent"
            )
            return
        self._switch = SessionSwitch(
            "attach" if result.action == "attach" else "open_saved",
            result.session_id,
            origin=self._ctx.session_id,
            origin_cwd=os.getcwd(),
        )
        overlays.hide()
        self._input_session.request_exit()

    @property
    def switch(self) -> SessionSwitch | None:
        """Where to go when this session ends: another agent, or the hub."""
        if self._switch is not None:
            return self._switch
        if self._background_handoff_state == "started":
            return SessionSwitch(
                "hub", self._ctx.session_id, origin=self._ctx.session_id, origin_cwd=os.getcwd()
            )
        return None

    async def _open_agents_hub_command(self, command: OpenAgentsHubCommand) -> None:
        self.open_agents_hub(command.tab, command.run_id)

    # ── subagents (AGX-08) ───────────────────────────────────────────────────

    async def _open_subagents_command(self, _command: OpenSubagentsCommand) -> None:
        self.open_subagents()

    def open_subagents(self) -> None:
        """Ctrl+O, ``/subagents``: the subagent panel with focus (AGX-08 §5.2)."""
        self._tasks.start(self._show_subagents(), name="subagents")

    def _subagents_root(self) -> Path:
        """Where this session's pools keep their worker transcripts."""
        from agenthicc.memory.journal import journal_path_for

        return journal_path_for(self._ctx.session_id).parent / "subagents"

    async def _show_subagents(self) -> None:
        from agenthicc.tui.workspace.overlays.subagents import SubagentsOverlay, load_earlier

        earlier = await asyncio.to_thread(load_earlier, self._subagents_root())
        overlays = self._workspace.overlays
        if overlays.active and not isinstance(overlays.widget, SubagentsOverlay):
            return  # an approval or another screen took the terminal meanwhile
        overlays.show(
            SubagentsOverlay(
                self._ctx.app_state,
                earlier=earlier,
                open_transcript=self._open_subagent_transcript,
                stop=self._stop_subagent,
                dismiss=self._dismiss_subagent,
                close=overlays.hide,
            )
        )

    def _open_subagent_transcript(
        self, target: TranscriptTarget, *, back: Callable[[], None] | None = None
    ) -> None:
        from agenthicc.tui.workspace.overlays.subagents import SubagentTranscriptOverlay

        overlays = self._workspace.overlays

        def close() -> None:
            overlays.hide()
            if back is not None:
                back()

        overlays.show(
            SubagentTranscriptOverlay(
                self._ctx.app_state, target, stop=self._stop_subagent, close=close
            )
        )

    def _stop_subagent(self, pool_id: str, worker_id: str) -> bool:
        """``x``: stop one worker; the others and the turn go on (AGX-08 §5.4)."""
        from agenthicc.subagents.pool import active_pool

        pool = active_pool(pool_id)
        if pool is None or not pool.stop_worker(worker_id):
            return False
        worker = pool.state.worker(worker_id)
        label = worker.label if worker is not None else worker_id
        self._ctx.app_state.conversation.notify_transient(f"Stopping {label}")
        return True

    def _dismiss_subagent(self, pool_id: str, worker_id: str) -> bool:
        """``x`` on a failed row: remove it from the panel."""
        from dataclasses import replace as dataclass_replace

        from agenthicc.subagents.pool import active_pool

        pool = active_pool(pool_id)
        if pool is not None:
            return pool.dismiss_worker(worker_id)
        conversation = self._ctx.app_state.conversation
        state = conversation.subagent_pool_state()
        worker = state.worker(worker_id) if state is not None and state.pool_id == pool_id else None
        if state is None or worker is None or worker.status != "failed":
            return False
        conversation.subagent_pool_state.set(
            state.with_worker(dataclass_replace(worker, dismissed=True))
        )
        return True

    def _transcript_target(self, pool_id: str, worker_id: str) -> TranscriptTarget | None:
        """The current pool's transcript of *worker_id*, with its siblings."""
        from agenthicc.tui.workspace.overlays.subagents import TranscriptTarget

        state = self._ctx.app_state.conversation.subagent_pool_state()
        if state is None or state.pool_id != pool_id or state.transcripts is None:
            return None
        ids = [worker.worker_id for worker in state.workers]
        index = ids.index(worker_id) if worker_id in ids else 0
        return TranscriptTarget(state.pool_id, state.workers, state.transcripts, index)

    def _open_requester_transcript(
        self, requester: ApprovalRequester, request: ApprovalRequest
    ) -> None:
        """``o`` in a worker's approval prompt: its transcript, then the prompt."""
        app_state = self._ctx.app_state
        target = self._transcript_target(requester.pool_id, requester.worker_id)
        if target is None:
            app_state.conversation.notify_transient(f"No transcript for {requester.label}")
            return

        def back() -> None:
            if app_state.pending_approval() is request:
                self._workspace.overlays.show(self._approval_overlay(request))

        self._open_subagent_transcript(target, back=back)

    # ── public routing ────────────────────────────────────────────────────────

    def dispatch_slash(self, text: str) -> bool:
        """Dispatch a registered command or skill trigger."""
        command_name = text.strip().split(None, 1)[0] if text.strip() else ""
        if command_name in _BACKGROUND_HANDOFF_COMMANDS:
            # The background service owns the lifecycle; the session only
            # hands its conversation over (PRD-141).
            from agenthicc.background import integration

            return integration._handoff(self)

        from agenthicc.commands import CommandContext
        from agenthicc.plugins.registry import build_registry

        ctx = self._ctx
        project_tools: list[ToolLike] = list(ctx.project_plugins.all_tools)
        # Most slash commands are local/read-only and do not need browser
        # tool schemas. Iterating the session proxy here would import an
        # optional browser package merely to run /status or /startup. The
        # explicit /tools view is the user-facing browser-tool selection
        # boundary; agent/workflow turns iterate it at their own dependency
        # boundary below.
        schedule_manager = None
        schedule_repository_root = ""
        if command_name in {"/schedule", "/schedules"}:
            from agenthicc.schedules import ScheduleManager, ScheduleStore

            schedule_manager = ScheduleManager(ScheduleStore())
            schedule_repository_root = str(
                ctx.workspace_scope.primary_root if ctx.workspace_scope is not None else Path.cwd()
            )
        if command_name == "/tools":
            project_tools.extend(ctx.browser_tools)
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
            mcp_manager=ctx.mcp_manager,
            startup=ctx.startup,
            frame_stats=self._workspace.frame_stats,
            keymap_service=vars(self).get("_keymap_service"),
            mode_manager=ctx.mode_manager,
            terminal_manager=ctx.terminal_manager,
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
            repair_session_history=self._repair_session_history,
            loop_manager=vars(self).get("_loop_manager"),
            schedule_manager=schedule_manager,
            schedule_repository_root=schedule_repository_root,
            schedule_config_path=ctx.config_path or "",
            open_agents_hub=self.open_agents_hub,
            open_subagents=self.open_subagents,
        )
        return bool(self._cmd_dispatcher.dispatch(text, context))

    def _set_input_text(self, text: str) -> None:
        """Put a registry selection in the composer without submitting it."""
        self._input_session.set_text(text)

    def _usage_snapshot(self) -> "UsageSnapshot":
        """Read one local usage snapshot without touching the active runner."""
        from agenthicc.commands.command import UsageSnapshot

        conversation = self._ctx.app_state.conversation
        task = self._agent_task
        ledger = self._ctx.usage_ledger
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
        from agenthicc.tui.input.unified_session import InputMode

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
        terminal_manager = self._ctx.terminal_manager
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
        from agenthicc.commands.busy_policy import classify_busy_command

        return classify_busy_command(text, self._ctx.cmd_registry)

    def _notify_queued(self, text: str) -> None:
        label = text[:40] + ("…" if len(text) > 40 else "")
        position = len(self._msg_queue)
        waiting = " — loading agent runtime…" if self._runtime_loading() else ""
        self._ctx.app_state.conversation.notification.set(
            f"⌛ Queued #{position}: {label}{waiting}"
        )

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
        from agenthicc.skills.loader import discover_skills_with_diagnostics

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
            from agenthicc.skills.loader import SkillDiagnostic, SkillDiscoveryResult

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
            self._set_workflow_override(None)

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
        from agenthicc.commands import build_builtin_registry
        from agenthicc.commands.plugin_loader import discover_command_plugins

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
        old_plugin_names = set(self._ctx.command_plugin_names)
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
        command_plugin_names = self._ctx.command_plugin_names
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
        """Handle workflow selection and durable resume controls."""
        name = args.strip()
        conv = self._ctx.app_state.conversation
        resume_parts = name.split()
        if resume_parts and resume_parts[0] == "resume":
            run_id: str | None = None
            profile_override: str | None = None
            index = 1
            while index < len(resume_parts):
                part = resume_parts[index]
                if part == "--profile":
                    if profile_override is not None or index + 1 >= len(resume_parts):
                        conv.notify_transient(
                            "⚠ Usage: /workflow resume [run-id] [--profile <name|none>]"
                        )
                        return True
                    raw_profile = resume_parts[index + 1]
                    profile_override = "" if raw_profile.casefold() == "none" else raw_profile
                    index += 2
                    continue
                if part.startswith("--profile="):
                    if profile_override is not None:
                        conv.notify_transient(
                            "⚠ Usage: /workflow resume [run-id] [--profile <name|none>]"
                        )
                        return True
                    raw_profile = part.partition("=")[2]
                    if not raw_profile:
                        conv.notify_transient(
                            "⚠ Usage: /workflow resume [run-id] [--profile <name|none>]"
                        )
                        return True
                    profile_override = "" if raw_profile.casefold() == "none" else raw_profile
                    index += 1
                    continue
                if part.startswith("-") or run_id is not None:
                    conv.notify_transient(
                        "⚠ Usage: /workflow resume [run-id] [--profile <name|none>]"
                    )
                    return True
                run_id = part
                index += 1
            return self._handle_workflow_resume(run_id, profile_override=profile_override)
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
                    from agenthicc.kernel import Event

                    self._ctx.processor.emit_nowait(
                        Event.create(
                            "WorkflowRunDiscarded",
                            {
                                "run_id": handle.run_id,
                                "workflow_name": handle.workflow_name,
                            },
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
        self._set_workflow_override(name)
        conv.notify_transient(f"⚡ Workflow → {name}")
        return True

    def _handle_workflow_resume(
        self,
        run_id: str | None,
        *,
        profile_override: str | None = None,
    ) -> bool:
        """Claim and resume one paused or process-interrupted workflow.

        An omitted ID selects the newest eligible durable checkpoint, then
        follows the same explicit-ID path so claims and revalidation cannot
        drift between the two command forms.
        """
        conv = self._ctx.app_state.conversation
        if self._tool_history_blocked():
            # A persisted history can be blocked at startup because a previous
            # process stopped between a tool call and its durable result. Retry
            # Agenthicc's deterministic, journal-backed repair before refusing
            # an explicit workflow resume. This never contacts the provider.
            recovery = self._repair_session_history(
                success_message="✓ Conversation history repaired; continuing workflow resume."
            )
            if not recovery.provider_ready:
                return True
        if self._agent_task is not None and not self._agent_task.done():
            conv.notify_transient("⚠ Cannot resume a workflow while another run is active")
            return True
        handle = self._workflow_handle
        if run_id is None:
            try:
                selected = self._select_latest_workflow_record()
            except Exception as exc:  # noqa: BLE001
                conv.notify_transient(
                    f"⚠ Cannot resume the latest workflow run: {type(exc).__name__}: {exc}"
                )
                return True
            if selected is not None:
                run_id = selected.run_id
                conv.notify_transient(
                    f"↻ Selected latest recoverable run '{selected.run_id}' "
                    f"({selected.workflow_name or 'workflow'} at "
                    f"{selected.current_phase or 'saved state'})"
                )
            elif self._ctx.session_conversation is not None:
                # A fresh durable scan is authoritative. Do not fall back to
                # a stale startup projection when it reports no candidate.
                self._workflow_recovery_records = {}
                handle = None

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
                    "⚠ Multiple workflows can be resumed. "
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
        claimed_here = False
        if handle.claim_owner_id is None:
            try:
                handle.claim(f"{self._workflow_owner_prefix}:{handle.run_id}")
                claimed_here = True
            except Exception as exc:  # noqa: BLE001
                from agenthicc.runners.workflow_checkpoint_store import WorkflowClaimError

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

        # Resolve the provider only after ownership is established. This keeps
        # claim-conflict diagnostics independent of local provider config and
        # ensures a rejected profile can never modify an unowned checkpoint.
        from agenthicc.runners.workflow_profile import (
            WorkflowProfileResolutionError,
            execution_profile_override,
            resolve_resume_provider_profile,
        )

        explicit_profile = profile_override
        if explicit_profile is None:
            explicit_profile = execution_profile_override(self._ctx.cfg_overrides)
        try:
            profile_selection = resolve_resume_provider_profile(
                self._ctx.cfg,
                saved_profile=handle.provider_profile,
                saved_binding_fingerprint=handle.provider_profile_fingerprint,
                explicit_profile=explicit_profile,
            )
        except WorkflowProfileResolutionError as exc:
            if claimed_here:
                self._release_workflow_claim(handle)
            conv.notify_transient(f"⚠ Cannot resume {handle.run_id!r}: {exc}")
            return True
        old_binding = handle.provider_profile_fingerprint
        record_switch = profile_selection.source == "explicit override" and (
            profile_selection.switched
            or (bool(old_binding) and old_binding != profile_selection.binding_fingerprint)
        )
        previous_profile_state = (
            handle.provider_profile,
            handle.provider_profile_fingerprint,
            handle.provider_profile_origin,
            handle.provider_profile_history,
            handle.lifecycle,
            handle.pause_requested,
        )
        handle.apply_provider_profile(
            profile_selection.profile_name,
            profile_selection.binding_fingerprint,
            record_switch=record_switch,
        )
        try:
            # Move to RESUMING before creating the task. This closes the small
            # race where Esc arrives after the command claims the run but
            # before the task reaches its first await; request_pause() can
            # then convert RESUMING to PAUSING instead of misclassifying the
            # cancellation as a failure.
            handle.mark_resuming()
            handle.persist_checkpoint(reason="resuming")
        except Exception as exc:  # noqa: BLE001
            (
                handle.provider_profile,
                handle.provider_profile_fingerprint,
                handle.provider_profile_origin,
                handle.provider_profile_history,
                handle.lifecycle,
                handle.pause_requested,
            ) = previous_profile_state
            self._release_workflow_claim(handle)
            conv.notify_transient(
                f"⚠ resume_transition_failed: cannot start workflow {handle.run_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            return True
        self._publish_session_event(
            "workflow_resume_profile_selected",
            {
                "run_id": handle.run_id,
                "saved_profile": profile_selection.saved_profile,
                "selected_profile": profile_selection.profile_name,
                "source": profile_selection.source,
                "switched": record_switch,
                "binding_fingerprint": profile_selection.binding_fingerprint,
            },
        )
        context = handle.context
        assert context is not None  # validated above; narrows the runner input
        self._dispatch_workflow_resume(
            definition,
            handle,
            context,
            profile_selection=profile_selection,
        )
        conv.notify_transient(
            f"↻ Resuming workflow '{handle.workflow_name}' with profile "
            f"{profile_selection.profile_name or 'legacy execution settings'} "
            f"({profile_selection.source})…"
        )
        return True

    async def _handle_compact_command(self) -> None:
        """Handle /compact — compact the current session memory (PRD-119)."""
        from agenthicc.runners.session_conversation import (
            ConversationBusyError,
        )

        from agenthicc.runners.startup import StartupDependencyError

        ctx = self._ctx
        try:
            await self._await_agent_runtime()
        except StartupDependencyError as exc:
            ctx.app_state.conversation.notify_transient(f"⚠ Cannot compact: {exc}")
            return
        conversation = ctx.session_conversation
        owner_id = f"manual_compaction:{uuid.uuid4().hex}"
        acquired = False
        if conversation is not None:
            try:
                await conversation.acquire(owner_id)
                acquired = True
            except ConversationBusyError:
                ctx.app_state.conversation.notify_transient(
                    "⚠ Cannot compact while an agent turn is using the conversation"
                )
                return
        try:
            await self._compact_session_memory()
        finally:
            if acquired and conversation is not None:
                conversation.release(owner_id)

    async def _compact_session_memory(self) -> None:
        """Run compaction while owning the session conversation boundary."""
        from agenthicc.memory.compactor import compact_memory_with_result

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
        outcome = await compact_memory_with_result(
            mem,
            transport,
            model=model,
            conv_store=conv,
            max_input_tokens=ctx.cfg.execution.effective_context_window(),
            usage_ledger=ctx.usage_ledger,
            session_id=ctx.session_id,
            run_id=f"compaction:{ctx.session_id}:{uuid.uuid4().hex[:12]}",
            max_completion_tokens=ctx.cfg.execution.max_completion_tokens,
            request_options=ctx.cfg.execution.request_options,
        )
        if outcome.applied:
            conv.notify_transient(
                "⎋ Compacted"
                if outcome.status == "compacted_summary"
                else "⎋ Compaction fallback: retained recent complete exchanges"
            )
        elif outcome.status == "blocked_durability_error":
            conv.notify_transient(
                "⚠ Compaction was not committed; conversation history is preserved"
            )
        elif outcome.status == "skipped_concurrent_update":
            conv.notify_transient(
                "⎋ Compaction skipped because the conversation changed; newer history was kept"
            )
        else:
            conv.notify_transient(
                "⚠ Compaction did not change the conversation; history is preserved"
            )

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
            self._tasks.start(self._handle_compact_command(), name="compact")
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
                    f"/workflow resume for the latest, or /workflow resume <run-id> ({choices}{suffix})"
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
        context = handle.context
        assert context is not None  # validated above; narrows the runner input
        turn_id = f"turn_{uuid.uuid4().hex}"
        self._dispatch_workflow_resume(
            definition,
            handle,
            context,
            continuation=text,
            turn_id=turn_id,
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
            if self._tool_history_blocked():
                self._notify_tool_history_recovery()
                notice_kept = True
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
        from agenthicc.tui.input.unified_session import InputMode

        ctx = self._ctx

        if self._tool_history_blocked():
            self._notify_tool_history_recovery()
            return

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
        import time as _time

        _deadline = (_time.monotonic() + _timeout) if (_timeout and _timeout > 0) else None

        async def _run_inner() -> None:
            if _plugin_cls is not None:
                import dataclasses as _dc
                from agenthicc.workflows.plugin import WorkflowContext

                required_phases = tuple(
                    phase
                    for phase in _plugin_cls.required_startup_phases
                    if isinstance(phase, str) and phase
                )
                if required_phases:
                    startup = ctx.startup
                    if startup is None:
                        raise RuntimeError(
                            "workflow declares startup dependencies but readiness is unavailable"
                        )
                    for phase in required_phases:
                        _prepare_startup_dependency(ctx, phase)
                    await startup.wait_for(*required_phases)

                _phase_specs = _plugin_cls.phases
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
                                "⚠ Multiple workflows can be resumed. "
                                f"/workflow resume for the latest, or /workflow resume <run-id> ({choices}{suffix})"
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
                    from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
                    from agenthicc.runners.workflow_handle import WorkflowRunHandle
                    from agenthicc.runners.workflow_profile import provider_profile_fingerprint

                    session_conversation = ctx.session_conversation
                    if session_conversation is not None:
                        new_handle = WorkflowRunHandle.create(
                            run_id=uuid.uuid4().hex,
                            workflow=_plugin_cls,
                            conversation=session_conversation,
                            intent=text,
                            checkpoint_store=WorkflowCheckpointStore(ctx.session_id),
                            browser_manager=ctx.browser_manager,
                            provider_profile=ctx.cfg.execution.profile,
                            provider_profile_fingerprint=provider_profile_fingerprint(
                                ctx.cfg, ctx.cfg.execution.profile
                            ),
                            workspace_root=str(
                                getattr(ctx.workspace_scope, "primary_root", "") or ""
                            ),
                        )
                        self._workflow_handle = new_handle
                        bootstrap = WorkflowContext(
                            intent=text,
                            run_id=new_handle.run_id,
                            workflow_name=_plugin_cls.name,
                            current_phase=(_phase_specs[0].name if _phase_specs else None),
                        )
                        new_handle.attach_bootstrap_context(bootstrap)
                        new_handle.claim(f"{self._workflow_owner_prefix}:{new_handle.run_id}")
                        if _phase_specs:
                            new_handle.update_phase(_phase_specs[0].name, 0, 0)
                        else:
                            new_handle.save_checkpoint(reason="started")
                        initializer = _plugin_cls.create_initial_context
                        if callable(initializer):
                            initial_context = initializer(
                                text,
                                new_handle.run_id,
                                ctx.session_memory,
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
                    session_memory=ctx.session_memory,
                    terminal_wait_policies={
                        phase.name: phase.terminal_wait_policy
                        for phase in _phase_specs
                        if hasattr(phase, "name") and hasattr(phase, "terminal_wait_policy")
                    },
                    startup=ctx.startup,
                    required_startup_phases=required_phases,
                )
                # Plugin owns runner construction — no name-based branching.
                _wf_runner = _plugin_cls.build_runner(_wf_config, ctx.mode_manager)
                if self._workflow_handle is not None:
                    from agenthicc.runners.workflow_execution_context import (
                        bind_workflow_execution,
                    )

                    with bind_workflow_execution(self._workflow_handle.run_id):
                        _runner_result = await _wf_runner.run(text)
                else:
                    _runner_result = await _wf_runner.run(text)
                self._record_returned_workflow_result(_runner_result)
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
                        + list(ctx.browser_tools)
                    ),
                    mcp_registry=ctx.mcp_registry,
                    active_agent="default",
                    completed_turns=self._turn_count,
                    approval_svc=ctx.approval_svc,
                    workspace_access=ctx.workspace_access,
                    memory_router=ctx.memory_router,
                    semantic_index=ctx.semantic_index,
                    retry_deadline_monotonic=_deadline,
                    resume_turn_id=getattr(resume, "turn_id", None),
                    resume_ledger=getattr(resume, "ledger", None),
                    next_queued_message=self._next_queued_message,
                    usage_ledger=ctx.usage_ledger,
                    browser_manager=ctx.browser_manager,
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
        from agenthicc.tui.input.unified_session import InputMode

        conv = self._ctx.app_state.conversation
        turn_failed = False
        turn_cancelled = False
        workflow_paused = False
        conversation = self._ctx.session_conversation
        owner_id = turn_id or f"activity_{uuid.uuid4().hex}"
        acquired = False
        conv.begin_activity()
        if turn_id is not None:
            self._publish_session_event("turn_started", turn_id=turn_id)
        try:
            # Optional MCP startup is deferred until after the first TUI frame,
            # but an agent turn that can see MCP tools must wait for the
            # catalog readiness boundary before building its tool schema.
            startup = self._ctx.startup
            if startup is not None:
                from agenthicc.runners.startup import StartupDependencyError

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
            mcp_manager = self._ctx.mcp_manager
            if startup is not None and mcp_manager is not None:
                # An optional MCP catalog may still be loading or may have
                # failed. Local tools remain usable in both cases. Only the
                # explicitly configured, auto-connected required servers are
                # a dependency of a turn that publishes the MCP catalog.
                required_servers = mcp_manager.required_auto_connect_servers
                if required_servers:
                    try:
                        await startup.wait_for("mcp")
                    except StartupDependencyError as exc:
                        # The background boundary intentionally converts
                        # failures to a visible degraded report. Rehydrate
                        # the manager's established typed error at the
                        # operation boundary so callers retain the pre-176
                        # required-MCP contract.
                        failures = mcp_manager.required_failures
                        if isinstance(failures, dict) and failures:
                            from agenthicc.tools.mcp_manager import (
                                McpRequiredServerError,
                            )

                            raise McpRequiredServerError(failures) from exc
                        raise
            await self._await_agent_runtime()
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
            error_event_id = getattr(exc, "_agenthicc_error_event_id", None)
            # Workflow startup can fail before an agent turn exists. Publish
            # that failure explicitly instead of silently returning to idle.
            if self._workflow_handle is not None:
                self._fail_workflow_run(
                    error,
                    kind=_workflow_failure_kind(exc),
                    error_event_id=error_event_id,
                )
            elif conv.is_turn_active:
                conv.close_turn(error=error)
            else:
                conv.append_event("error", {"message": error}, event_id=error_event_id)
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
        from agenthicc.kernel import Event

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
            handle.finalize_failure(
                f"workflow pause checkpoint failed: {type(exc).__name__}: {exc}",
                kind="checkpoint_storage",
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
        source_value = vars(cmd).get("source", "user")
        source = source_value if isinstance(source_value, str) and source_value else "user"
        if not text:
            return
        if text.startswith("/") and text.split(maxsplit=1)[0] in _BACKGROUND_HANDOFF_COMMANDS:
            # A handoff must run even while a turn is active.
            self.dispatch_slash(text)
            return
        # TUISession emits user_message before opening the agent turn, so the
        # event is not owned by a ConversationTurn; remember the accepted input
        # for /background.
        self._background_intent = text

        # Keep the latest accepted natural-language intent available to the
        # background control plane even if it was submitted while another run
        # was still active and therefore entered the FIFO queue.
        if source == "user" and not text.startswith(("/", "$")):
            self._last_submitted_text = text

        if self._runtime_loading() or (self._agent_task and not self._agent_task.done()):
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
                self._ctx.app_state.conversation.append_event(
                    "user_message", {"text": text, "source": source}
                )
                return

        if self.route(text):
            if self._pending_skill_body:
                body = self._pending_skill_body.pop()
                self._ctx.app_state.conversation.append_event(
                    "user_message", {"text": text, "source": source}
                )
                turn_id = f"turn_{uuid.uuid4().hex}"
                publish_event = getattr(self, "_publish_session_event", None)
                if callable(publish_event):
                    publish_event(
                        "turn_queued",
                        {"text": body, "client_id": "tui", "source": source},
                        turn_id=turn_id,
                    )
                    task = self.agent_task_body(body, turn_id=turn_id)
                else:
                    task = self.agent_task_body(body)
                if isinstance(self, TUISession):
                    self._activate_streaming_input()
                self._agent_task = asyncio.create_task(task, name="agent-turn")
            return
        self._ctx.app_state.conversation.append_event(
            "user_message", {"text": text, "source": source}
        )
        turn_id = f"turn_{uuid.uuid4().hex}"
        publish_event = getattr(self, "_publish_session_event", None)
        if callable(publish_event):
            publish_event(
                "turn_queued",
                {"text": text, "client_id": "tui", "source": source},
                turn_id=turn_id,
            )
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
        from agenthicc.tui.input.unified_session import InputMode
        from agenthicc.tui.runtime.replay import ConversationReplayer

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
        recovery_records = self._workflow_recovery_records
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
                    f"Use /workflow resume for the latest, or /workflow resume <run-id>: {ids}"
                )
            return
        from agenthicc.kernel.state import NodeStatus

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
        from agenthicc.kernel.state import NodeStatus

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
        profile_selection: "ResumeProviderProfile | None" = None,
    ) -> None:
        """Resume a WorkflowRunner with the session conversation still attached."""
        from agenthicc.tui.input.unified_session import InputMode

        ctx = self._ctx
        handle = self._workflow_handle
        conversation = ctx.session_conversation
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
            import dataclasses as _dc
            from agenthicc.runners.workflow_profile import (
                resolve_resume_provider_profile,
                workflow_resume_agent_runner,
            )

            startup = ctx.startup
            if startup is not None:
                await startup.wait_for("provider_journal_restore")
            if profile_selection is None:
                if handle is None:
                    raise RuntimeError("workflow resume has no durable run handle")
                profile_selection = resolve_resume_provider_profile(
                    ctx.cfg,
                    saved_profile=handle.provider_profile,
                    saved_binding_fingerprint=handle.provider_profile_fingerprint,
                )
            resume_agent_runner = workflow_resume_agent_runner(
                ctx.agent_runner,
                profile_selection,
                conversation_id=ctx.session_id,
            )
            prepare_provider = getattr(resume_agent_runner, "prepare", None)
            if callable(prepare_provider):
                await asyncio.to_thread(prepare_provider)
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
                from agenthicc.kernel import Event

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
                    f"Current phase: {(handle.current_phase if handle else None) or 'current'}\n"
                    "The prior agent turn was interrupted safely; preserve completed work "
                    "and continue from the saved phase.\n"
                    f"Original intent: {handle.original_intent if handle else ''}\n"
                    + (f"User continuation: {continuation}" if continuation else "Continue.")
                )
                if handle is not None and continuation:
                    handle.append_continuation(continuation)
                ctx.session_memory.add_user(continuation_text)

            required_phases = tuple(
                phase
                for phase in wf_defn.required_startup_phases
                if isinstance(phase, str) and phase
            )
            if required_phases:
                startup = ctx.startup
                if startup is None:
                    raise RuntimeError(
                        "workflow declares startup dependencies but readiness is unavailable"
                    )
                for phase in required_phases:
                    if phase != "provider":
                        _prepare_startup_dependency(ctx, phase)
                wait_phases = tuple(phase for phase in required_phases if phase != "provider")
                if wait_phases:
                    await startup.wait_for(*wait_phases)
            _wf_params = wf_defn.build_params(ctx.cfg.workflows.get(wf_defn.name, {}))
            _phase_specs = wf_defn.phases
            _wf_config = _dc.replace(
                self._wf_config_base,
                agent_runner=cast("AgentRunnerBase", resume_agent_runner),
                cfg=profile_selection.config,
                params=_wf_params,
                workflow_handle=handle,
                session_memory=ctx.session_memory,
                conversation_id=ctx.session_id,
                terminal_wait_policies={
                    phase.name: phase.terminal_wait_policy
                    for phase in _phase_specs
                    if hasattr(phase, "name") and hasattr(phase, "terminal_wait_policy")
                },
                startup=ctx.startup,
                required_startup_phases=tuple(
                    phase for phase in required_phases if phase != "provider"
                ),
            )
            runner = wf_defn.build_runner(_wf_config, ctx.mode_manager)
            # The task argument is normally the same object as the handle's
            # context.  Prefer the handle reference when available so a
            # rehydration or a runner-side context attachment cannot leave a
            # later resume cycle using a stale object.
            resume_context = (
                handle.context if handle is not None and handle.context is not None else context
            )
            if handle is not None:
                from agenthicc.runners.workflow_execution_context import (
                    bind_workflow_execution,
                )

                pending_resume = ctx.pending_resume
                resume_plan = (
                    pending_resume
                    if pending_resume is not None
                    and pending_resume.workflow_run_id == handle.run_id
                    else None
                )
                with bind_workflow_execution(handle.run_id, resume_plan=resume_plan):
                    runner_result = await runner.resume(resume_context)
            else:
                runner_result = await runner.resume(resume_context)
            if handle is not None and runner_result is not None:
                handle.attach_context(runner_result)
            self._record_returned_workflow_result(runner_result)

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
            error_event_id = getattr(exc, "_agenthicc_error_event_id", None)
            if self._workflow_handle is not None:
                self._fail_workflow_run(
                    error,
                    kind=_workflow_failure_kind(exc),
                    error_event_id=error_event_id,
                )
            elif conv.is_turn_active:
                conv.close_turn(error=error)
            else:
                conv.append_event("error", {"message": error}, event_id=error_event_id)
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
        from agenthicc.tui.runtime import (
            InterruptAgentCommand,
            OpenAgentsHubCommand,
            OpenSubagentsCommand,
            SendMessageCommand,
        )

        ctx = self._ctx

        ctx.command_bus.register(SendMessageCommand, self.handle_send)
        ctx.command_bus.register(InterruptAgentCommand, self.handle_interrupt)
        ctx.command_bus.register(OpenAgentsHubCommand, self._open_agents_hub_command)
        ctx.command_bus.register(OpenSubagentsCommand, self._open_subagents_command)
        self._wire_approval_overlay()

        startup = ctx.startup
        if startup is not None:
            startup.begin("first_frame")
        try:
            self._workspace.start()
        except Exception as exc:
            if startup is not None:
                from agenthicc.runners.startup import ReadinessState

                startup.finish(
                    "first_frame",
                    ReadinessState.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        if startup is not None:
            startup.finish("first_frame")
            startup.record_first_frame_modules(len(sys.modules))
        # AGX-13: the user's key bindings, hot-reloaded while the session runs.
        # They load right after the first frame (whose hints use the defaults)
        # and before any key is read, which keeps the module off startup.
        from agenthicc.tui.keymap_service import KeymapService

        self._keymap_service = KeymapService(ctx.app_state)
        # The Live shell is now visible. Discovery may execute trusted or
        # project extension code, so it starts only after that first frame and
        # is joined at the agent-operation boundary.
        self._start_deferred_startup()
        self._start_agent_runtime()
        if ctx.resumed:
            from agenthicc.runners.startup import ReadinessState
            from agenthicc.tui.runtime.session_log import (
                load_recent_transcript_page,
                user_message_history_from_events,
            )

            behaviour = ctx.cfg.behaviour
            replay_turns = behaviour.resume_transcript_turns
            max_events = behaviour.resume_transcript_max_events
            max_bytes = behaviour.resume_transcript_max_bytes
            max_event_bytes = behaviour.resume_transcript_max_event_bytes
            if startup is not None:
                startup.begin("transcript_preview_read")
            try:
                preview = await asyncio.to_thread(
                    load_recent_transcript_page,
                    ctx.session_id,
                    last_turns=replay_turns,
                    max_events=max_events,
                    max_bytes=max_bytes,
                    max_event_bytes=max_event_bytes,
                )
            except asyncio.CancelledError:
                if startup is not None:
                    startup.finish(
                        "transcript_preview_read",
                        ReadinessState.CANCELLED,
                        error="cancelled",
                    )
                raise
            except Exception as exc:  # noqa: BLE001 - transcript is presentation-only
                preview = None
                if startup is not None:
                    startup.finish(
                        "transcript_preview_read",
                        ReadinessState.DEGRADED,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            else:
                if startup is not None:
                    startup.finish("transcript_preview_read")

            page_events = preview.events if preview is not None else []
            if startup is not None:
                startup.begin("input_history_projection")
            self._input_session.set_history(user_message_history_from_events(page_events))
            if startup is not None:
                startup.finish("input_history_projection")

            if startup is not None:
                startup.begin("transcript_preview_render")
            try:
                await self._workspace.replay_transcript(page_events)
            except asyncio.CancelledError:
                if startup is not None:
                    startup.finish(
                        "transcript_preview_render",
                        ReadinessState.CANCELLED,
                        error="cancelled",
                    )
                raise
            except Exception as exc:  # noqa: BLE001 - input must remain usable
                if startup is not None:
                    startup.finish(
                        "transcript_preview_render",
                        ReadinessState.DEGRADED,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            else:
                if startup is not None:
                    startup.finish("transcript_preview_render")
            if preview is not None and preview.notice:
                ctx.app_state.conversation.notify_transient(preview.notice, duration=8.0)
        proc_task = asyncio.create_task(ctx.processor.run())
        loop_manager = vars(self).get("_loop_manager")
        if loop_manager is not None:
            await loop_manager.start(rehydrate=bool(ctx.resumed))
        self._start_account_check()

        async def _tick() -> None:
            question_second: int | None = None
            while True:
                # 10 Hz: spinner frames and the elapsed-time clock (AGX-11).
                await asyncio.sleep(0.1)
                # Approval and question requests suspend the LLM turn while
                # the overlay owns the terminal. Freeze animation during that
                # wait so SIGWINCH redraws do not race a changing status bar.
                pending = ctx.app_state.pending_approval()
                ctx.app_state.conversation.tick(paused=pending is not None)
                if _optional_field(pending, "kind") == "questions":
                    deadline = _optional_field(pending, "deadline_at")
                    if isinstance(deadline, (int, float)):
                        import time as _time

                        remaining = max(0, int(deadline - _time.time() + 0.999))
                        if remaining != question_second:
                            question_second = remaining
                            self._workspace._redraw()
                else:
                    question_second = None

        tick_task = asyncio.create_task(_tick())
        keymap_service = vars(self).get("_keymap_service")
        keymap_task = asyncio.create_task(
            keymap_service.watch() if keymap_service is not None else asyncio.sleep(0)
        )
        try:
            await self._input_session.run()
        finally:
            # Input can exit while an ESC-triggered cancellation is still
            # unwinding. Cancel and await the owned task before closing the
            # session; otherwise Windows may leave its executor/terminal wait
            # alive during asyncio shutdown.
            self._msg_queue.clear()
            await self._tasks.aclose()
            if loop_manager is not None:
                await loop_manager.shutdown()
            agent_task = self._agent_task
            if agent_task is not None and not agent_task.done():
                agent_task.cancel()
            tick_task.cancel()
            keymap_task.cancel()
            proc_task.cancel()
            owned: list[asyncio.Task[object] | asyncio.Task[None]] = [
                task
                for task in (self._runtime_task, self._account_task, self._ad_task)
                if task is not None
            ]
            for task in owned:
                task.cancel()
            startup = ctx.startup
            if startup is not None:
                await startup.close()
            await asyncio.gather(
                *([agent_task] if agent_task else []),
                tick_task,
                keymap_task,
                proc_task,
                *owned,
                return_exceptions=True,
            )
            self._workspace.stop()
            agents_service, self._agents_service = self._agents_service, None
            if agents_service is not None:
                await agents_service.close()


# ── thin factory (≤60 lines) ──────────────────────────────────────────────────


def _resolved_directory(path: str) -> Path:
    """Expand ``~`` and resolve *path* (touches the filesystem)."""
    return Path(path).expanduser().resolve()


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
    """Run the reactive TUI, optionally from a resumed session's project.

    A session can hand over to another (AGX-05 §5.2): switching to an agent
    from the agents hub runs that agent's session, and the original one
    reopens when it ends. After ``/background`` the agents hub opens with the
    new background agent selected.
    """

    async def run_once(
        directory: str | None,
        *,
        session_id: str | None,
        first: bool = False,
    ) -> SessionSwitch | None:
        return await _run_tui_session_in(
            directory,
            resume_id=session_id,
            cli_overrides=cli_overrides,
            record_cassette=record_cassette,
            cli_flags=cli_flags,
            config_path=config_path,
            cli_secret_overrides=cli_secret_overrides,
            mode_name=mode_name if first else None,
            workflow_name=workflow_name if first else None,
            owner_lease=owner_lease if first else None,
            config=config,
        )

    async def run_target(action: str, target: str) -> SessionSwitch | None:
        directory = await _switch_target_directory(
            action, target, config_path=config_path, overrides=tuple(cli_overrides or ())
        )
        if directory is None:
            return None
        return await run_once(directory, session_id=target)

    switch = await run_once(cwd, session_id=resume_id, first=True)
    returns: list[tuple[str, str]] = []
    while True:
        if switch is None:
            if not returns:
                return
            origin, origin_cwd = returns.pop()
            switch = await run_once(origin_cwd, session_id=origin)
        elif switch.action == "hub":
            result = await _agents_hub_after_background(
                switch.target,
                config_path=config_path,
                overrides=tuple(cli_overrides or ()),
                secret_overrides=tuple(cli_secret_overrides or ()),
                config=config,
            )
            switch = None
            if result is not None and result.session_id is not None:
                if result.action in ("attach", "open_saved"):
                    switch = await run_target(result.action, result.session_id)
        else:
            returns.append((switch.origin, switch.origin_cwd))
            switch = await run_target(switch.action, switch.target)


async def _switch_target_directory(
    action: str, target: str, *, config_path: str | None, overrides: tuple[str, ...]
) -> str | None:
    """Prepare the session to switch to; return its project directory.

    A background agent is first stopped and released to the foreground
    (``attach_foreground``); a saved session opens in the directory it
    recorded.
    """
    if action == "open_saved":
        from agenthicc.sessions import _load_all_session_indexes

        record = _load_all_session_indexes().get(target, {})
        directory = record.get("cwd")
        if isinstance(directory, str) and await asyncio.to_thread(os.path.isdir, directory):
            return directory
        return os.getcwd()
    from agenthicc.background import (
        BackgroundStore,
        BackgroundSupervisor,
        load_background_settings,
    )

    settings = load_background_settings(config_path=config_path, overrides=overrides)
    store = BackgroundStore(settings.store_root)
    supervisor = BackgroundSupervisor.from_settings(store, settings)
    try:
        foreground = await asyncio.to_thread(supervisor.attach_foreground, target)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to attach background agent {target}: {exc}", file=sys.stderr)
        return None
    return foreground.cwd


async def _agents_hub_after_background(
    session_id: str,
    *,
    config_path: str | None,
    overrides: tuple[str, ...],
    secret_overrides: tuple[str, ...],
    config: "AgenthiccConfig | None",
) -> HubResult | None:
    """After ``/background``: the agents hub with the new background agent selected."""
    from rich.console import Console

    from agenthicc.background import (
        BackgroundStore,
        BackgroundSupervisor,
        background_enabled,
        load_background_settings,
    )
    from agenthicc.tui.terminal.backend import get_backend
    from agenthicc.tui.workspace.agents_hub import DispatchDefaults, run_agents_hub
    from agenthicc.tui.workspace.agents_hub.sources import HubSources

    if not get_backend().is_interactive():
        return None
    settings = load_background_settings(config_path=config_path, overrides=overrides)
    if not background_enabled(settings):
        return None
    store = BackgroundStore(settings.store_root)
    return await run_agents_hub(
        Console(highlight=False),
        store=store,
        supervisor=BackgroundSupervisor.from_settings(store, settings),
        settings=settings.manager,
        sources=HubSources.default(Path.cwd()),
        select=session_id,
        dispatch=DispatchDefaults(
            config_path=config_path,
            set_overrides=overrides,
            set_secret_overrides=secret_overrides,
        ),
        ui=config.ui if config is not None else None,
    )


async def _run_tui_session_in(
    cwd: str | None,
    *,
    resume_id: str | None,
    cli_overrides: list[str] | None,
    record_cassette: str | None,
    cli_flags: CLIFlags | None,
    config_path: str | None,
    cli_secret_overrides: list[str] | None,
    mode_name: str | None,
    workflow_name: str | None,
    owner_lease: SessionOwnerLease | None,
    config: "AgenthiccConfig | None",
) -> SessionSwitch | None:
    """Run one session, in *cwd* when given; return where to go next."""
    if cwd is None:
        return await _run_tui_session_impl(
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

    previous_cwd = os.getcwd()
    os.chdir(await asyncio.to_thread(_resolved_directory, cwd))
    try:
        return await _run_tui_session_impl(
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
) -> SessionSwitch | None:
    """Reactive TUI session — single entry point, no legacy branches.

    Returns where to go when the session ends (:class:`SessionSwitch`).
    """
    from agenthicc.tui.workspace import Workspace
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.welcome import (
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
        session_startup = ctx.startup
        assert session_startup is not None
        workspace = Workspace(
            ctx.app_state,
            ctx.console,
            max_live_tool_calls=ctx.cfg.tools.max_live_tool_calls,
            group_exploratory_calls=ctx.cfg.tools.group_exploratory_calls,
            startup=session_startup,
            composer_max_rows=ctx.cfg.behaviour.composer_max_rows,
            frame_interval_s=ctx.cfg.behaviour.frame_interval_ms / 1_000.0,
        )

        def _input_ready(interactive: bool) -> None:
            from agenthicc.runners.startup import ReadinessState

            report = session_startup.report("time_to_interactive_input")
            if report.state.value == "loading":
                session_startup.finish(
                    "time_to_interactive_input",
                    ReadinessState.READY if interactive else ReadinessState.DEGRADED,
                    error=None if interactive else "non-interactive terminal",
                )

        input_session = UnifiedInputSession(
            app_state=ctx.app_state,
            command_bus=ctx.command_bus,
            trigger_registry=ctx.trigger_registry,
            mode_manager=ctx.mode_manager,
            overlay_host=workspace.overlays,
            cwd=Path(os.getcwd()),
            cfg=ctx.cfg,
            history=[] if ctx.resumed else None,
            on_ready=_input_ready,
        )
        session = TUISession(ctx, workspace, input_session)
        ctx.startup.finish("tui_shell")
        from agenthicc.background.terminals import (
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
    return session.switch


async def _close_tui_session_resources(
    ctx: "SessionContext",
    session: TUISession | None,
    terminal_context_token: contextvars.Token["TerminalManager | None"] | None,
    cassette_base: Path | None,
) -> None:
    """Close TUI resources and release the outer session lease last."""

    try:
        from agenthicc.background.terminals import reset_current_terminal_manager

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
        from agenthicc.tools.fs.file_cache import (
            configure_file_cache,
            get_file_cache,
        )

        _fc = get_file_cache()
        if _fc is not None:
            _fc.close()
            configure_file_cache(None)
        startup = ctx.startup
        if startup is not None:
            await startup.close()
        if ctx.mcp_registry:
            await ctx.mcp_registry.shutdown()
        if ctx.semantic_index is not None:
            await ctx.semantic_index.aclose()
        browser_manager = ctx.browser_manager
        if browser_manager is not None:
            await browser_manager.close_session()
        await ctx.terminal_manager.close()
        if ctx.kernel_projection_task is not None:
            ctx.kernel_projection_task.cancel()
            await asyncio.gather(ctx.kernel_projection_task, return_exceptions=True)
        session_service = ctx.session_service
        if session_service is not None:
            await session_service.close()
        if cassette_base is not None:
            _write_cassette_meta(cassette_base / ctx.session_id, ctx.session_id)
    finally:
        owner_lease = ctx.owner_lease
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
    import atexit
    import signal as _signal

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
