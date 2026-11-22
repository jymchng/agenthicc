"""Delegate eligible workflow tasks without starting refused siblings."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING

import pytest

from agenthicc.subagents.types import SubagentTypeSpec

from .harness import reply, tool_call
from .test_s04_create_tools import _module

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from lauren_ai._transport import Completion, CompletionChunk
    from .harness import AuthoredSession, ScriptedTransport

pytestmark = pytest.mark.e2e


def _committed_project(project: Path) -> None:
    for args in (
        ("init", "-q"),
        ("add", "agenthicc.toml"),
        (
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "fixture",
        ),
    ):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)


async def test_interactive_worker_request_does_not_start_or_refuse_its_sibling(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
) -> None:
    context = authored_session.context
    context.subagent_registry.register(
        SubagentTypeSpec("fixture_reader", frozenset({"read_file"}), "Read the project fixture.")
    )
    script.expect(
        "request a workflow and an ordinary worker in one call",
        lambda request: "spawn_subagents" in request.tools,
        tool_call(
            "spawn_subagents",
            {
                "tasks": [
                    {"type": "fixture_reader", "task": "Run a plan.", "workflow": "code_plan"},
                    {"type": "fixture_reader", "task": "Report the fixture."},
                ],
            },
        ),
    )
    script.expect(
        "run only the non-interactive sibling",
        lambda request: (
            request.contains("Report the fixture.") and "enter_workflow" not in request.tools
        ),
        reply("The sibling finished."),
    )
    script.expect(
        "return the typed refusal and the ordinary worker's result",
        lambda request: (
            request.contains("workflow_requires_human_gate")
            and request.contains("The sibling finished.")
        ),
        reply("The unrelated task succeeded."),
    )
    result = await authored_session.run("default", "Delegate both checks.")
    assert result.status == "complete", result.error
    assert len(script.requests) == 3


@pytest.mark.parametrize("policy", ["stage_only", "auto"])
async def test_workflow_worker_stages_tools_in_its_own_worktree(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
    policy: str,
) -> None:
    project = authored_session.project
    _committed_project(project)
    context = authored_session.context
    context.subagent_registry.register(
        SubagentTypeSpec(
            "fixture_author", frozenset({"read_file", "write_file"}), "Author fixture tools."
        )
    )
    script.expect(
        "spawn an eligible tool author",
        lambda request: "spawn_subagents" in request.tools,
        tool_call(
            "spawn_subagents",
            {
                "tasks": [
                    {
                        "type": "fixture_author",
                        "task": "Create a fixture tool.",
                        "workflow": "create_tools",
                    },
                ],
            },
        ),
    )
    script.expect(
        "the worker submits a plan under its restricted provider schema",
        lambda request: (
            "submit_tools_plan" in request.tools and "enter_workflow" not in request.tools
        ),
        tool_call(
            "submit_tools_plan",
            {
                "tools": [
                    {
                        "name": "fixture_probe",
                        "description": "Report a fixture.",
                        "capabilities": ["read"],
                    }
                ],
            },
        ),
    )
    script.expect(
        "finish planning", lambda request: request.contains("fixture_probe"), reply("Planned.")
    )
    script.expect(
        "write to the worker's scope",
        lambda request: "write_file" in request.tools and "run_bash" not in request.tools,
        tool_call("write_file", {"path": "fixture_probe.py", "content": _module("fixture_probe")}),
    )
    script.expect(
        "observe the file in the worktree and keep the parent tool directory clear",
        lambda request: (
            any(project.glob(".agenthicc/worktrees/*/.agenthicc/.staging/*/tool/fixture_probe.py"))
            and not (project / ".agenthicc/tools/fixture_probe.py").exists()
        ),
        tool_call("confirm_tools_generated", {}),
    )
    script.expect("finish generation", lambda request: request.contains("ok"), reply("Written."))
    script.expect(
        "review the deterministic tool report",
        lambda request: "approve_tools" in request.tools,
        tool_call("approve_tools", {}),
    )
    script.expect("finish the review", lambda request: request.contains("ok"), reply("Approved."))
    script.expect(
        "the parent receives a typed staged artifact and the actual phases",
        lambda request: (
            request.contains("workflow_returns")
            and request.contains("fixture_probe")
            and request.contains("staged")
            and request.contains("validate")
            and ("fixture_probe" in request.tools) == (policy == "auto")
        ),
        tool_call("fixture_probe", {})
        if policy == "auto"
        else reply("The tool is staged for review."),
    )

    if policy == "auto":
        script.expect(
            "call the worker's published tool after the parent reloads",
            lambda request: request.contains("fixture_probe_confirmed"),
            reply("The parent called the new tool."),
        )

    result = await authored_session.run("default", "Run a tool-author worker.")

    assert result.status == "complete", result.error
    if policy == "stage_only":
        (staged,) = context.authored.store().staged_runs()
        assert staged.candidates[0].artifact.name == "fixture_probe"
        assert not (project / ".agenthicc/tools/fixture_probe.py").exists()
        assert not authored_session.trust.records()
    else:
        (record,) = context.authored.store().records()
        assert record.artifact.name == "fixture_probe"
        assert (project / ".agenthicc/tools/fixture_probe.py").is_file()
        assert authored_session.trust.records()


async def test_workflow_authored_by_a_worker_is_published_by_its_parent(
    authored_session: AuthoredSession, script: ScriptedTransport
) -> None:
    from .test_s03_create_workflow import _SOURCE

    project = authored_session.project
    _committed_project(project)
    context = authored_session.context
    context.subagent_registry.register(
        SubagentTypeSpec(
            "fixture_author", frozenset({"read_file", "write_file"}), "Author a workflow."
        )
    )
    script.expect(
        "delegate the workflow author",
        lambda request: "spawn_subagents" in request.tools,
        tool_call(
            "spawn_subagents",
            {
                "tasks": [
                    {
                        "type": "fixture_author",
                        "task": "Create an agent-callable report workflow.",
                        "workflow": "create_workflow",
                    },
                ]
            },
        ),
    )
    script.expect(
        "record the scoped worker design",
        lambda request: (
            "finalize_design" in request.tools and "enter_workflow" not in request.tools
        ),
        tool_call(
            "finalize_design",
            {"workflow_name": "report_fixture", "design": "Report a fixture in one phase."},
        ),
    )
    script.expect(
        "finish design", lambda request: request.contains("Design finalized"), reply("Designed.")
    )
    script.expect(
        "write only the worker's canonical package",
        lambda request: "write_file" in request.tools,
        tool_call("write_file", {"path": "runner.py", "content": _SOURCE}),
    )
    live = project / ".agenthicc/workflows/report_fixture/runner.py"
    script.expect(
        "observe the pending package in its worktree",
        lambda request: (
            not live.exists()
            and any(
                project.glob(".agenthicc/worktrees/*/.agenthicc/workflows/report_fixture/runner.py")
            )
        ),
        tool_call("mark_generation_complete", {"summary": "Wrote the fixture workflow."}),
    )
    script.expect(
        "finish generation", lambda request: not live.exists(), reply("Wrote the package.")
    )
    script.expect(
        "review the sandbox validation result",
        lambda request: request.contains("DETERMINISTIC VALIDATION REPORT"),
        tool_call("approve_workflow", {"summary": "The fixture workflow matches the design."}),
    )
    script.expect("finish validation", lambda request: not live.exists(), reply("Validated."))
    script.expect(
        "return a staged receipt to the parent publisher",
        lambda request: (
            not live.exists() and context.workflow_registry.get("report_fixture") is None
        ),
        reply("The validated package is staged."),
    )
    script.expect(
        "the parent observes the imported workflow",
        lambda request: (
            request.contains("workflow_returns")
            and request.contains("loaded")
            and context.workflow_registry.get("report_fixture") is not None
        ),
        reply("The workflow is available."),
    )

    result = await authored_session.run("default", "Delegate the report workflow author.")

    assert result.status == "complete", result.error
    assert live.is_file()
    assert authored_session.trust.check(live, kind="workflow") is None
    assert context.workflow_registry.get("report_fixture") is not None


async def test_cancelled_tool_author_retains_staged_bytes_in_its_worktree(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = authored_session.project
    _committed_project(project)
    context = authored_session.context
    context.subagent_registry.register(
        SubagentTypeSpec("fixture_author", frozenset({"read_file", "write_file"}), "Author tools.")
    )
    script.expect(
        "delegate the author",
        lambda request: "spawn_subagents" in request.tools,
        tool_call(
            "spawn_subagents",
            {
                "tasks": [
                    {
                        "type": "fixture_author",
                        "task": "Author a probe.",
                        "workflow": "create_tools",
                    },
                ]
            },
        ),
    )
    script.expect(
        "record the plan",
        lambda request: "submit_tools_plan" in request.tools,
        tool_call(
            "submit_tools_plan",
            {
                "tools": [
                    {
                        "name": "staged_probe",
                        "description": "Read a fixture.",
                        "capabilities": ["read"],
                    },
                ]
            },
        ),
    )
    script.expect(
        "finish planning", lambda request: request.contains("staged_probe"), reply("Planned.")
    )
    script.expect(
        "write the staged bytes",
        lambda request: "write_file" in request.tools,
        tool_call("write_file", {"path": "staged_probe.py", "content": _module("staged_probe")}),
    )
    written = asyncio.Event()
    hold = asyncio.Event()
    complete = script.complete
    pattern = ".agenthicc/worktrees/*/.agenthicc/.staging/*/tool/staged_probe.py"

    async def wait_at_write(
        *args: object, **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        if any(project.glob(pattern)):
            written.set()
            await hold.wait()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(script, "complete", wait_at_write)
    task = asyncio.create_task(authored_session.run("default", "Delegate the author."))
    try:
        async with asyncio.timeout(15):
            await written.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        hold.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    (source,) = tuple(project.glob(pattern))
    assert source.read_text(encoding="utf-8") == _module("staged_probe")
    assert not (project / ".agenthicc/tools/staged_probe.py").exists()
    assert not context.authored.store().records()
    assert not authored_session.trust.records()


@pytest.mark.parametrize("policy", ["stage_only"])
async def test_parallel_tool_authors_keep_their_staging_separate(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    from lauren_ai._transport._mock import MockTransport

    assert policy == "stage_only"
    project = authored_session.project
    _committed_project(project)
    context = authored_session.context
    context.subagent_registry.register(
        SubagentTypeSpec("fixture_author", frozenset({"read_file", "write_file"}), "Author a tool.")
    )
    scripts: dict[str, MockTransport] = {}
    for label in ("alpha", "beta"):
        name = f"{label}_probe"
        transport = MockTransport()
        for response in (
            tool_call(
                "submit_tools_plan",
                {
                    "tools": [
                        {"name": name, "description": "Read a fixture.", "capabilities": ["read"]},
                    ]
                },
            ),
            reply("Planned."),
            tool_call("write_file", {"path": f"{name}.py", "content": _module(name)}),
            tool_call("confirm_tools_generated", {}),
            reply("Written."),
            tool_call("approve_tools", {}),
            reply("Approved."),
        ):
            transport.queue_response(response)
        scripts[label] = transport

    script.expect(
        "start both authors in one pool",
        lambda request: "spawn_subagents" in request.tools,
        tool_call(
            "spawn_subagents",
            {
                "max_concurrent": 2,
                "tasks": [
                    {
                        "type": "fixture_author",
                        "task": f"Author the fixture-{label} tool.",
                        "workflow": "create_tools",
                    }
                    for label in scripts
                ],
            },
        ),
    )
    script.expect(
        "observe both staged returns",
        lambda request: (
            request.contains("workflow_returns")
            and request.contains("alpha_probe")
            and request.contains("beta_probe")
            and request.contains("staged")
        ),
        reply("Both tools are staged."),
    )

    barrier = asyncio.Barrier(2)
    complete = script.complete
    roots: set[Path] = set()
    worker_calls: dict[str, MockTransport] = {}

    async def dispatch(
        messages: object, **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        schemas = kwargs.get("tools")
        names = (
            {item.get("name") for item in schemas if isinstance(item, dict)}
            if isinstance(schemas, list)
            else set()
        )
        if not (names & {"submit_tools_plan", "confirm_tools_generated", "approve_tools"}):
            return await complete(messages, **kwargs)
        text = repr(messages) + str(kwargs.get("system", ""))
        label = next(label for label in scripts if f"fixture-{label}" in text)
        transport = scripts[label]
        worker_calls[label] = transport
        if len(transport.calls) == 3:
            async with asyncio.timeout(12):
                await barrier.wait()
            filename = f"{label}_probe.py"
            (candidate,) = tuple(
                project.glob(f".agenthicc/worktrees/*/.agenthicc/.staging/*/tool/{filename}")
            )
            roots.add(candidate.parents[4])
            assert not (project / ".agenthicc/tools" / filename).exists()
        return await transport.complete(messages, **kwargs)

    monkeypatch.setattr(script, "complete", dispatch)
    result = await authored_session.run("default", "Delegate independent tool authoring.")

    assert result.status == "complete", result.error
    assert len(roots) == 2
    assert {label: len(transport.calls) for label, transport in worker_calls.items()} == {
        "alpha": 7,
        "beta": 7,
    }
    assert {run.candidates[0].artifact.name for run in context.authored.store().staged_runs()} == {
        "alpha_probe",
        "beta_probe",
    }


async def test_failed_workflow_worker_reports_the_failure_and_executed_phases(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
) -> None:
    project = authored_session.project
    _committed_project(project)
    context = authored_session.context
    context.cfg.workflows["authoring"] = {"max_repair_cycles": 0}
    context.subagent_registry.register(
        SubagentTypeSpec("fixture_author", frozenset({"read_file", "write_file"}), "Author a tool.")
    )
    steps = [
        (
            "spawn_subagents",
            {
                "tasks": [
                    {
                        "type": "fixture_author",
                        "task": "Create a faulty fixture.",
                        "workflow": "create_tools",
                    },
                ]
            },
        ),
        (
            "submit_tools_plan",
            {
                "tools": [
                    {
                        "name": "broken_probe",
                        "description": "Read a fixture.",
                        "capabilities": ["read"],
                    },
                ]
            },
        ),
    ]
    for name, inputs in steps:
        script.expect(
            name, lambda request, expected=name: expected in request.tools, tool_call(name, inputs)
        )
    script.expect(
        "finish planning", lambda request: request.contains("broken_probe"), reply("Planned.")
    )
    script.expect(
        "write invalid Python in staging",
        lambda request: "write_file" in request.tools,
        tool_call("write_file", {"path": "broken_probe.py", "content": "broken ="}),
    )
    script.expect(
        "confirm generation",
        lambda request: "confirm_tools_generated" in request.tools,
        tool_call("confirm_tools_generated", {}),
    )
    script.expect("finish generation", lambda request: request.contains("ok"), reply("Written."))
    script.expect(
        "return the failure and the phases reached",
        lambda request: (
            request.contains("workflow_failed")
            and request.contains("repair budget exhausted")
            and request.contains("validate")
        ),
        reply("The validator refused the broken tool."),
    )

    result = await authored_session.run("default", "Delegate a failing fixture author.")

    assert result.status == "complete", result.error
    assert not context.authored.store().records()
    assert not list((project / ".agenthicc/tools").glob("*.py"))
