"""AGI-03: background direct turns get the session tools, including ask_user."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import BackgroundInteractionInbox, BackgroundSession, BackgroundStore
from agenthicc.background.model import SessionStatus
from agenthicc.background.worker import BackgroundApprovalService, WorkerRequest, run_worker

pytestmark = pytest.mark.integration


def _production_builder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner: object) -> Path:
    """The production session builder, with discovery and the provider stubbed."""
    from agenthicc.agents import registry as agents_registry
    from agenthicc.agents.registry import build_agents_registry as build_builtin_agents_registry
    from agenthicc.commands import plugin_loader
    from agenthicc.memory import journal as memory_journal
    from agenthicc.memory import layers
    from agenthicc.plugins import discovery
    from agenthicc.plugins.discovery import PluginToolSet
    from agenthicc.commands.plugin_loader import CommandPluginSet
    from agenthicc.runners import tui_session
    from agenthicc.skills import bootstrap, loader
    from agenthicc.skills.loader import SkillDiscoveryResult
    from agenthicc.session_service import SessionService
    from agenthicc.workflows import registry as workflows_registry
    from agenthicc.workflows.registry import WorkflowRegistry

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = home / ".agenthicc" / "sessions"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(tui_session, "_SESSIONS_DIR", session_root)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", session_root)
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log._SESSION_INDEX", session_root / "index.json"
    )
    monkeypatch.setattr(memory_journal, "_SESSIONS_DIR", session_root)
    monkeypatch.setattr(
        "agenthicc.session_service.SessionService",
        lambda: SessionService(store_root=tmp_path / "service"),
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(tui_session, "_build_agent_runner", lambda *args, **kwargs: runner)
    monkeypatch.setattr(bootstrap, "bootstrap_default_skills", lambda **kwargs: 0)
    monkeypatch.setattr(
        loader, "discover_skills_with_diagnostics", lambda **kwargs: SkillDiscoveryResult({}, ())
    )
    workflow_registry = WorkflowRegistry()
    monkeypatch.setattr(
        workflows_registry, "build_workflow_registry", lambda **kwargs: workflow_registry
    )
    monkeypatch.setattr(
        agents_registry,
        "build_agents_registry",
        lambda **kwargs: build_builtin_agents_registry(load_external=False),
    )
    monkeypatch.setattr(discovery, "discover_project_tools", lambda **kwargs: PluginToolSet())
    monkeypatch.setattr(discovery, "warn_conflicts", lambda tools: None)
    monkeypatch.setattr(
        plugin_loader, "discover_command_plugins", lambda **kwargs: CommandPluginSet()
    )
    monkeypatch.setattr(layers, "GlobalMemoryLayer", lambda: layers.SessionMemoryLayer())
    return workspace


@pytest.mark.asyncio
async def test_a_background_direct_turn_can_ask_the_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._transport import Completion, TokenUsage
    from lauren_ai._transport._mock import MockTransport

    transport = MockTransport()
    transport.queue_tool_use(
        "ask_user",
        {"questions": [{"id": "lang", "text": "Which language?", "options": ["Python", "Go"]}]},
        tool_use_id="ask-1",
        model="mock-model",
    )
    transport.queue_response(
        Completion(
            id="2",
            model="mock-model",
            content="Using Go.",
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            tool_calls=[],
        )
    )
    workspace = _production_builder(monkeypatch, tmp_path, AgentRunnerBase(transport=transport))
    store = BackgroundStore(tmp_path / "background")
    artifact = tmp_path / "sessions" / "ask-session"
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            "ask-session",
            title="asks",
            cwd=str(workspace),
            workflow_name="",
            intent="pick a language",
            artifact_dir=str(artifact),
        )
    )

    async def operator() -> None:
        inbox = BackgroundInteractionInbox(store)
        for _ in range(1_000):
            current = store.get("ask-session")
            if current.status is SessionStatus.WAITING_INPUT and current.input_item_id:
                item = inbox.get(current.input_item_id)
                assert item.kind == "question" and item.target["question_ids"] == ["lang"]
                inbox.respond(
                    item.item_id,
                    {"answers": {"lang": "Go"}},
                    expected_version=item.version,
                    idempotency_key="answer-1",
                )
                return
            await asyncio.sleep(0.02)
        raise AssertionError("the background agent never asked")

    answering = asyncio.create_task(operator())
    exit_code = await run_worker(
        WorkerRequest(
            session_id="ask-session",
            workflow_name="",
            intent="pick a language",
            cwd=str(workspace),
            config_path=None,
            set_overrides=(),
            dangerously_skip_permissions=False,
            mode_name="Yolo",
        ),
        store,
    )
    await asyncio.wait_for(answering, timeout=5)

    assert exit_code == 0, store.get("ask-session").error
    offered = {tool["name"] for tool in transport.calls[0].tools or []}
    assert {"ask_user", "memory_read", "memory_write"} <= offered
    results = [
        block
        for message in transport.calls[1].messages
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    (result,) = results
    assert json.loads(result["content"]) == {"ok": True, "lang": "Go"}


async def test_skipped_permissions_approve_tools_but_still_ask(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "skip", title="t", cwd=str(tmp_path), workflow_name="", intent="i"
    ).evolve(status=SessionStatus.RUNNING, lease_token="lease", attempt=1)
    store.create(session)
    service = BackgroundApprovalService(store, "skip", auto_approve_capabilities=True)

    tool = await service.request_approval(
        SimpleNamespace(kind="tool", tool_name="write_file", workspace_access=None)  # type: ignore[arg-type]
    )
    outside = await service.request_approval(
        SimpleNamespace(kind="tool", tool_name="read_file", workspace_access=[{"path": "/etc"}])  # type: ignore[arg-type]
    )
    assert tool.allowed is True and outside.allowed is False
    assert BackgroundInteractionInbox(store).list_items() == ()

    question = asyncio.create_task(
        service.request_input(
            SimpleNamespace(  # type: ignore[arg-type]
                kind="questions",
                tool_name="ask_user",
                tool_use_id="q",
                request_id="q",
                timeout_s=None,
                tool_input={"questions": [{"id": "x", "text": "?", "options": ["a"]}]},
            )
        )
    )
    for _ in range(200):
        if store.get("skip").status is SessionStatus.WAITING_INPUT:
            break
        await asyncio.sleep(0.01)
    (item,) = BackgroundInteractionInbox(store).list_items()
    BackgroundInteractionInbox(store).respond(
        item.item_id, {"answers": {"x": "a"}}, expected_version=item.version, idempotency_key="k"
    )
    assert (await asyncio.wait_for(question, timeout=5)).message == '{"x": "a"}'


def test_one_builder_for_foreground_and_background() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "agenthicc"
    for path in ("runners/tui_session.py", "background/worker.py"):
        text = (root / path).read_text(encoding="utf-8")
        assert "session_tools(" in text, path
    assert not any("_make_session_tools" in p.read_text() for p in root.rglob("*.py"))
