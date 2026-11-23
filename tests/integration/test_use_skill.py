"""Model skill calls through the real session turn and worker runners."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import Completion, CompletionChunk, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.commands.builtins import build_builtin_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.kernel.processor import EventProcessor
from agenthicc.plugins.authored_types import (
    AuthoredRun,
    PublishedArtifact,
    StagedArtifact,
    content_digest,
)
from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.runners.authored import AuthoredPublisher
from agenthicc.runners.reload import ReloadService
from agenthicc.skills.tools import SkillUseState
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.testing.recording_transport import RecordingTransport
from agenthicc.tools.base import tool_name
from agenthicc.tools.capabilities import get_tool_capabilities
from agenthicc.workflows.authoring.verdict import ValidationReport
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_operator(tmp_path: Path) -> Iterator[None]:
    trust = ExtensionTrust(store_path=tmp_path / "operator" / "trust.json", trusted_roots=())
    with override_extension_trust(trust):
        yield


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


@pytest.fixture
def reload_service(workspace: Path, running_processor: EventProcessor) -> ReloadService:
    return ReloadService(
        processor=running_processor,
        project_dir=workspace / ".agenthicc",
        user_dir=workspace.parent / "operator",
        workflow_registry=WorkflowRegistry(),
        command_registry=build_builtin_registry(),
        subagent_registry=DEFAULT_REGISTRY.copy(),
    )


def _text(identifier: str) -> Completion:
    return Completion(
        id=identifier,
        model="mock-model",
        content=identifier,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


@pytest.mark.usefixtures("isolated_operator")
async def test_model_uses_authored_skill_and_worker_inherits_remaining_tools(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload_service: ReloadService,
    running_processor: EventProcessor,
) -> None:
    monkeypatch.chdir(workspace)
    publisher = AuthoredPublisher(workspace, reload_service, running_processor, policy="auto")
    stage = publisher.store().stage_directory("release-skill", "skill") / "release-notes"
    stage.mkdir()
    disabled = sorted(
        tool_name(tool)
        for tool in AGENT_TOOLS
        if get_tool_capabilities(tool) & {"write", "execute", "git_write"}
    )
    markdown = (
        b"---\nname: Release Notes\ndescription: Prepare a release summary\nsuggestedTopics: [release]\n"
        + f"disabledTools: [{', '.join(disabled)}]\n".encode()
        + b"maxTurnDepth: 1\n---\n"
        b"Summarize release {0}. See {reference}"
    )
    template = b"Use the changes section."
    reference = b"Read the changelog and !`printf guideline`."
    files = {"SKILL.md": markdown, "template.md": template, "reference.md": reference}
    for name, data in files.items():
        (stage / name).write_bytes(data)
    inventory = {f".agenthicc/skills/release-notes/{name}": data for name, data in files.items()}
    digest = content_digest(inventory)
    artifact = PublishedArtifact(
        "skill", "release-notes", tuple(sorted(inventory)), digest, "staged"
    )
    run = AuthoredRun(
        "release-skill",
        "test-session",
        "offline",
        (StagedArtifact(artifact, ValidationReport(digest, ())),),
    )

    spawn_descriptions: list[str] = []

    class PublishOnFirstRequest(MockTransport):
        async def complete(
            self, *args: object, **kwargs: object
        ) -> Completion | AsyncIterator[CompletionChunk]:
            if not self.calls:
                assert not reload_service.skills.current().value
                published = await publisher.publish(run)
                assert published.error_code is None, published
            tools = kwargs.get("tools")
            if isinstance(tools, list):
                spawn_descriptions.extend(
                    str(tool["description"])
                    for tool in tools
                    if isinstance(tool, dict) and tool["name"] == "spawn_subagents"
                )
            return await super().complete(*args, **kwargs)

    transport = PublishOnFirstRequest()
    transport.queue_tool_use("list_skills", {"query": "release"})
    transport.queue_tool_use("use_skill", {"name": "release-notes", "args": ["v2"]})
    transport.queue_tool_use(
        "spawn_subagents", {"tasks": [{"type": "researcher", "task": "Read the skill."}]}
    )
    transport.queue_tool_use("list_skills", {})
    transport.queue_tool_use("use_skill", {"name": "release-notes", "args": ["worker"]})
    transport.queue_response(_text("worker-finished"))
    transport.queue_response(_text("agent-finished"))
    events = running_processor.subscribe_events()
    cassette = workspace.parent / "requests.jsonl"
    config = AgenthiccConfig()
    await _run_agent_turn(
        "Prepare release notes.",
        runner=AgentRunnerBase(transport=RecordingTransport(transport, cassette)),
        processor=running_processor,
        session_memory=ShortTermMemory(),
        max_agent_turns=8,
        exec_cfg=config.execution,
        skills=reload_service.skills,
        active_agent="auto",
    )
    await running_processor.drain()

    requests = [json.loads(line) for line in cassette.read_text(encoding="utf-8").splitlines()]
    schemas = [request["tool_names_available"] for request in requests]
    assert len(schemas) == 7, schemas
    assert "run_bash" in schemas[0]
    assert "run_bash" in schemas[1]
    assert all("run_bash" not in names for names in schemas[2:])
    builtins = set(map(tool_name, AGENT_TOOLS))
    assert set(schemas[2]) & builtins == set(schemas[3]) & builtins
    assert all({"list_skills", "use_skill"} <= set(names) for names in schemas)
    assert "researcher [edits files, runs commands]" in spawn_descriptions[0]
    assert "researcher [read-only]" in spawn_descriptions[2]
    listing = transport.calls[1].messages[-1]["content"]
    assert listing[-1]["content"][0]["slug"] == "release-notes"
    parent_results = transport.calls[2].messages[-1]["content"]
    parent_context = json.loads(parent_results[-1]["content"])["body"]
    assert 'authored="true"' in parent_context
    assert 'source="project"' in parent_context
    assert "Summarize release v2." in parent_context
    assert "Use the changes section." in parent_context
    assert (
        "[run `printf guideline` with your command tool and use its output here]" in parent_context
    )
    worker_results = transport.calls[5].messages[-1]["content"]
    assert "Summarize release worker." in json.loads(worker_results[-1]["content"])["body"]

    receipts = []
    while not events.empty():
        event = events.get_nowait()
        if event.event_type == "SkillUsed":
            receipts.append(event.payload)
    assert len(receipts) == 2, receipts
    assert receipts[0]["slug"] == "release-notes"
    assert "run_bash" not in receipts[0]["narrowed_tools"]
    assert "pool_id" in receipts[1]
    assert all("body" not in receipt for receipt in receipts)


async def test_reload_during_skill_calls_preserves_complete_snapshots(
    workspace: Path, reload_service: ReloadService
) -> None:
    directory = workspace / ".agenthicc" / "skills" / "alternating"
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"

    async def publish(version: int) -> None:
        path.write_text(
            f"---\nname: Alternating\ndescription: version-{version}\n---\nbody-{version} {{reference}}",
            encoding="utf-8",
        )
        (directory / "reference.md").write_text(f"reference-{version}", encoding="utf-8")
        (directory / "template.md").write_text(f"template-{version}", encoding="utf-8")
        report = await reload_service.reload(frozenset({"skills"}), reason="command")
        assert report.ok

    await publish(0)

    async def read() -> None:
        for _ in range(20):
            state = SkillUseState(reload_service.skills, agent="reader")
            assert [entry["slug"] for entry in state.inventory(None)] == ["alternating"]
            result = await state.use("alternating", [])
            assert result["ok"] is True
            body = result["body"]
            assert isinstance(body, str)
            rendered = body.splitlines()
            assert any(
                rendered[1] == f"body-{version} reference-{version}"
                and rendered[-2] == f"template-{version}"
                for version in range(11)
            )

    async def change() -> None:
        for version in range(1, 11):
            await publish(version)

    await asyncio.gather(change(), read(), read(), read())
