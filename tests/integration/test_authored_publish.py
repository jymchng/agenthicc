"""Publication exercises real workspace files, provenance, and extension trust."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rich.console import Console

from agenthicc.commands import CommandContext, CommandDispatcher, build_builtin_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.plugins.authored import AuthoredArtifactStore, AuthoredError
from agenthicc.plugins.authored_types import (
    AuthoredRun,
    PublishedArtifact,
    StagedArtifact,
    content_digest,
)
from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
from agenthicc.runners.authored import AuthoredPublisher
from agenthicc.runners.reload import ReloadService
from agenthicc.skills.loader import discover_skills
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tools.base import tool_name
from agenthicc.tui.trigger import TriggerContext
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.workflows.authoring.verdict import Finding, ValidationReport
from agenthicc.workflows.registry import WorkflowRegistry

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

    from agenthicc.kernel.processor import EventProcessor

pytestmark = pytest.mark.integration


@pytest.fixture
def store(tmp_path: Path) -> AuthoredArtifactStore:
    workspace = tmp_path / "project"
    workspace.mkdir()
    trust = ExtensionTrust(store_path=tmp_path / "operator" / "trust.json", trusted_roots=())
    return AuthoredArtifactStore(workspace, trust=trust)


def _run(store: AuthoredArtifactStore, run_id: str, sources: Mapping[str, bytes]) -> AuthoredRun:
    directory = store.stage_directory(run_id, "tool")
    candidates: list[StagedArtifact] = []
    for name, source in sources.items():
        (directory / f"{name}.py").write_bytes(source)
        relative = f".agenthicc/tools/{name}.py"
        digest = content_digest({relative: source})
        candidates.append(
            StagedArtifact(
                PublishedArtifact("tool", name, (relative,), digest, "staged"),
                ValidationReport(digest, ()),
            )
        )
    return AuthoredRun(run_id, "session-1", "offline", tuple(candidates))


def test_authored_set_migrates_validated_bytes_and_exact_trust(
    store: AuthoredArtifactStore,
) -> None:
    source = b"# Exact approved source bytes\n"
    run = _run(store, "candidate", {"version_probe": source})
    artifact = run.candidates[0].artifact
    path = store.workspace / artifact.files[0]
    assert not path.exists()

    with store.commit(run) as installed:
        assert installed == (replace(artifact, state="loaded"),)
        assert path.read_bytes() == source
        assert not store.trust.permits(path, kind="tool")

    (record,) = store.records()
    assert record.artifact == installed[0]
    assert record.approved_by == "policy"
    assert record.run_id == run.run_id
    assert store.trust.permits(path, kind="tool")
    assert store.trust.records()[0].artifact_digest == artifact.digest


def test_failed_runner_verdict_discards_entire_set(store: AuthoredArtifactStore) -> None:
    run = _run(store, "bad-set", {"good": b"# good", "bad": b"# bad"})
    good, bad = run.candidates
    bad = replace(
        bad,
        verdict=ValidationReport(
            bad.artifact.digest,
            (Finding("tool.invalid", "error", "bad.py", None, "Invalid source."),),
        ),
    )
    invalid = replace(run, candidates=(good, bad))
    with pytest.raises(AuthoredError, match="runner verdict"):
        with store.commit(invalid):
            pytest.fail("invalid artifacts entered publication")
    assert not (store.workspace / ".agenthicc" / ".staging" / run.run_id).exists()
    assert store.trust.records() == ()
    assert store.records() == ()


def test_moving_second_file_failure_restores_first_and_trust(
    store: AuthoredArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(store, "atomic", {"first": b"# first", "second": b"# second", "third": b"# third"})
    install = store._install_file

    def fail_second(source: Path, target: Path) -> None:
        if target.name == "second.py":
            raise OSError("injected move failure")
        install(source, target)

    monkeypatch.setattr(store, "_install_file", fail_second)

    with pytest.raises(OSError, match="injected move failure"):
        with store.commit(run):
            pytest.fail("a partial installation reached reload")
    assert store.trust.records() == ()
    assert store.records() == ()
    for candidate in run.candidates:
        assert not (store.workspace / candidate.artifact.files[0]).exists()


def test_changed_staged_bytes_fail_closed(store: AuthoredArtifactStore) -> None:
    run = _run(store, "drifted", {"freshness_probe": b"# approved"})
    changed = store.stage_directory(run.run_id, "tool") / "freshness_probe.py"
    changed.write_bytes(b"# changed after validation")

    with pytest.raises(AuthoredError, match="changed after validation"):
        with store.commit(run):
            pytest.fail("changed bytes entered publication")
    assert store.trust.records() == ()
    assert not changed.exists()


def test_stage_only_keeps_files_hidden_until_operator_approval(
    store: AuthoredArtifactStore,
) -> None:
    run = _run(store, "awaiting-approval", {"operator_probe": b"# awaiting approval"})
    artifact = run.candidates[0].artifact
    assert store.stage(run) == (artifact,)
    assert store.trust.records() == ()
    assert not (store.workspace / artifact.files[0]).exists()
    assert tuple(item.run_id for item in store.staged_runs()) == (run.run_id,)

    reopened = AuthoredArtifactStore(store.workspace, trust=store.trust)
    with reopened.commit(reopened.staged(run.run_id), approved_by="user"):
        pass
    assert reopened.staged_runs() == ()
    assert reopened.records()[0].approved_by == "user"
    assert reopened.trust.permits(reopened.workspace / artifact.files[0], kind="tool")


def test_reject_stage_removes_the_ticket_and_the_candidate(
    store: AuthoredArtifactStore,
) -> None:
    run = _run(store, "awaiting-rejection", {"rejected_probe": b"# awaiting rejection"})
    store.stage(run)
    assert store.reject_staged(run.run_id).run_id == run.run_id
    assert store.staged_runs() == ()
    assert store.trust.records() == ()
    assert not (store.workspace / ".agenthicc" / ".staging" / run.run_id).exists()


def test_two_publishers_allow_only_one_owner_of_the_same_name(
    store: AuthoredArtifactStore,
) -> None:
    first = _run(store, "first-worker", {"collision_probe": b"# first"})
    second = _run(store, "second-worker", {"collision_probe": b"# second"})
    competitor = AuthoredArtifactStore(store.workspace, trust=store.trust)

    def attempt_second() -> str:
        try:
            with competitor.commit(second):
                return "published"
        except AuthoredError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.commit(first):
            competing = workers.submit(attempt_second)
        assert competing.result(timeout=5) == "name_conflict"
    assert (store.workspace / first.candidates[0].artifact.files[0]).read_bytes() == b"# first"
    assert len(store.trust.records()) == 1


def test_project_provenance_cannot_authorize_replacement(store: AuthoredArtifactStore) -> None:
    run = _run(store, "forged", {"owner_file": b"# candidate"})
    artifact = run.candidates[0].artifact
    target = store.workspace / artifact.files[0]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# user file")
    (store.workspace / ".agenthicc" / "authored.json").write_text(
        json.dumps([artifact.to_dict()]), encoding="utf-8"
    )
    with pytest.raises(AuthoredError) as captured:
        with store.commit(run):
            pytest.fail("repository data authorized overwrite")
    assert captured.value.code == "not_authored"
    assert target.read_bytes() == b"# user file"
    assert store.trust.records() == ()


def test_store_refuses_authoring_below_the_user_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    with pytest.raises(AuthoredError, match="User-global"):
        AuthoredArtifactStore(home / ".agenthicc" / "project")
    assert not home.exists()


@pytest.fixture
def reload_service(
    store: AuthoredArtifactStore, running_processor: EventProcessor
) -> ReloadService:
    return ReloadService(
        processor=running_processor,
        project_dir=store.workspace / ".agenthicc",
        user_dir=store.workspace.parent / "operator",
        workflow_registry=WorkflowRegistry(),
        command_registry=build_builtin_registry(),
        subagent_registry=DEFAULT_REGISTRY.copy(),
    )


def _tool(name: str, result: str) -> bytes:
    return (
        "from lauren_ai._tools import tool\n"
        "from agenthicc.tools.capabilities import tool_read\n"
        "@tool_read\n"
        "@tool()\n"
        f"def {name}() -> str:\n"
        '    """Return the fixture result."""\n'
        f"    return {result!r}\n"
        f"TOOLS = [{name}]\n"
    ).encode()


async def test_session_publisher_loads_validated_tools_and_emits_one_notice(
    store: AuthoredArtifactStore,
    reload_service: ReloadService,
    running_processor: EventProcessor,
) -> None:
    publisher = AuthoredPublisher(store.workspace, reload_service, running_processor)
    events = running_processor.subscribe_events()
    run = _run(
        store,
        "auto-session",
        {"first_tool": _tool("first_tool", "first"), "second_tool": _tool("second_tool", "second")},
    )
    with override_extension_trust(store.trust):
        result = await publisher.publish(run, policy="auto")
    await running_processor.drain()

    assert result.error_code is None
    assert {item.state for item in result.artifacts} == {"loaded"}
    assert {tool_name(item) for item in reload_service.tools.current().value} == {
        "first_tool",
        "second_tool",
    }
    events_seen: list[str] = []
    while not events.empty():
        events_seen.append(events.get_nowait().event_type)
    assert events_seen.count("AuthoredPublished") == 1
    assert events_seen.count("AuthoredArtifactStateChanged") == 2
    await reload_service.close()


async def test_load_failure_keeps_the_prior_surface_and_reverts_the_set(
    store: AuthoredArtifactStore,
    reload_service: ReloadService,
    running_processor: EventProcessor,
) -> None:
    publisher = AuthoredPublisher(store.workspace, reload_service, running_processor)
    snapshot = reload_service.tools.current()
    run = _run(
        store,
        "failed-loader",
        {
            "good_tool": _tool("good_tool", "good"),
            "bad_tool": b"raise RuntimeError('runtime rejected this module')\n",
        },
    )
    with override_extension_trust(store.trust):
        result = await publisher.publish(run, policy="auto")

    assert result.error_code == "import_error"
    assert {item.state for item in result.artifacts} == {"load_failed"}
    assert reload_service.tools.current() is snapshot
    assert store.records() == ()
    assert store.trust.records() == ()
    assert all(not (store.workspace / c.artifact.files[0]).exists() for c in run.candidates)
    await reload_service.close()


def _update(store: AuthoredArtifactStore, run_id: str, name: str, contents: bytes) -> AuthoredRun:
    run = _run(store, run_id, {name: contents})
    current = next(item.artifact for item in store.records() if item.artifact.name == name)
    staged = run.candidates[0]
    return replace(
        run,
        candidates=(replace(staged, artifact=replace(staged.artifact, supersedes=current.digest)),),
    )


def test_rollback_restores_the_prior_version_and_its_exact_digest(
    store: AuthoredArtifactStore,
) -> None:
    first = _run(store, "version-1", {"rollback_probe": b"# first behavior\n"})
    with store.commit(first) as loaded:
        old = loaded[0]
    with store.commit(_update(store, "version-2", old.name, b"# second behavior\n")):
        pass
    diff = store.diff(old.name)
    assert "-# first behavior" in diff
    assert "+# second behavior" in diff

    with store.rollback(old.name) as restored:
        assert restored[0].digest == old.digest
        assert (store.workspace / old.files[0]).read_bytes() == b"# first behavior\n"
    assert store.trust.records()[0].artifact_digest == old.digest
    assert store.records()[0].artifact.digest == old.digest

    with store.remove(old.name):
        assert not (store.workspace / old.files[0]).exists()
    assert store.records() == ()
    assert store.trust.records() == ()


def test_rollback_refuses_changed_archive_content(store: AuthoredArtifactStore) -> None:
    first = _run(store, "version-1", {"rollback_probe": b"# first"})
    with store.commit(first) as loaded:
        old = loaded[0]
    with store.commit(_update(store, "version-2", old.name, b"# second")) as loaded:
        current = loaded[0]
    archive = store.workspace / ".agenthicc" / "authored" / "versions" / old.name / old.digest
    (archive / old.files[0]).write_bytes(b"# tampered")

    with pytest.raises(AuthoredError, match="Archived bytes"):
        with store.rollback(old.name):
            pytest.fail("changed historical bytes were installed")
    assert (store.workspace / current.files[0]).read_bytes() == b"# second"
    assert store.trust.records()[0].artifact_digest == current.digest


async def test_authored_picker_and_operator_commands_restore_previous_behavior(
    store: AuthoredArtifactStore,
    reload_service: ReloadService,
    running_processor: EventProcessor,
) -> None:
    registry = build_builtin_registry()
    trigger = SlashCommandTrigger(registry)
    picker = TriggerContext(cwd=store.workspace)
    assert "/authored" in {item.value for item in trigger.get_matches("authored", picker)}
    assert "/authored rollback" in {item.value for item in trigger.get_matches("authored ", picker)}
    publisher = AuthoredPublisher(store.workspace, reload_service, running_processor)
    dispatcher = CommandDispatcher(registry)
    output = io.StringIO()
    pending: list[asyncio.Task[None]] = []

    def start(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        pending.append(task)
        return task

    async def submit(arguments: str) -> str:
        text = f"/authored {arguments}"
        context = CommandContext(
            text,
            arguments,
            "offline",
            Console(file=output),
            AgenthiccConfig(),
            authored=publisher,
            start_task=start,
        )
        assert dispatcher.dispatch(text, context)
        await pending.pop()
        result = output.getvalue()
        output.seek(0)
        output.truncate()
        return result

    run = _run(store, "operator", {"restore_demo": _tool("restore_demo", "first")})
    with override_extension_trust(store.trust):
        staged = await publisher.publish(run, policy="stage_only")
        assert staged.artifacts[0].state == "staged"
        assert "staged" in await submit("list")
        assert "loaded" in await submit("approve operator")
        updated = _update(store, "operator-2", "restore_demo", _tool("restore_demo", "second"))
        assert (await publisher.publish(updated, policy="auto")).error_code is None
        assert "second" in await submit("diff restore_demo")
        assert "Restored restore_demo" in await submit("rollback restore_demo")
        (tool,) = reload_service.tools.current().value
        assert callable(tool) and tool() == "first"
        assert "Removed restore_demo" in await submit("remove restore_demo")
    assert reload_service.tools.current().value == ()
    await reload_service.close()


def test_authored_cli_lists_approves_and_rejects_staged_runs(
    store: AuthoredArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agenthicc.__main__ import main

    monkeypatch.chdir(store.workspace)
    approved = _run(store, "approve-cli", {"accepted_cli": _tool("accepted_cli", "accepted")})
    rejected = _run(store, "reject-cli", {"refused_cli": _tool("refused_cli", "discarded")})
    store.stage(approved)
    store.stage(rejected)

    def command(*arguments: str) -> str:
        monkeypatch.setattr(sys, "argv", ["agenthicc", "authored", *arguments])
        main()
        return capsys.readouterr().out

    with override_extension_trust(store.trust):
        inventory = command("list")
        assert "approve-cli" in inventory and "reject-cli" in inventory
        assert "loaded" in command("approve", "approve-cli")
        assert "rejected" in command("reject", "reject-cli")
    assert [record.artifact.name for record in store.records()] == ["accepted_cli"]
    assert store.staged_runs() == ()


def test_private_recovery_rolls_back_a_process_killed_during_publication(
    store: AuthoredArtifactStore,
) -> None:
    run = _run(store, "interrupted", {"interrupted_tool": _tool("interrupted_tool", "first")})
    store.stage(run)
    artifact = run.candidates[0].artifact
    target = store.workspace / artifact.files[0]
    source = """
import os
import sys
from pathlib import Path
from agenthicc.plugins.authored import AuthoredArtifactStore
from agenthicc.plugins.trust import ExtensionTrust

workspace, owner, run_id = sys.argv[1:]
trust = ExtensionTrust(store_path=Path(owner), trusted_roots=())
store = AuthoredArtifactStore(Path(workspace), trust=trust)
with store.commit(store.staged(run_id), approved_by="user"):
    os._exit(74)
"""
    interrupted = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(store.workspace),
            str(store.trust.store_path),
            run.run_id,
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert interrupted.returncode == 74, interrupted.stderr.decode()
    assert target.is_file()
    assert not store.trust.permits(target, kind="tool")

    reopened = AuthoredArtifactStore(store.workspace, trust=store.trust)
    reopened.recover()
    assert not target.exists()
    assert reopened.trust.records() == ()
    assert reopened.records() == ()
    assert reopened.staged(run.run_id).candidates[0].artifact == artifact


def test_skill_discovery_hides_provisional_set_without_recording_python_trust(
    store: AuthoredArtifactStore,
) -> None:
    stage = store.stage_directory("skill-run", "skill") / "pending-skill"
    stage.mkdir()
    body = b"---\nname: pending-skill\ndescription: A test skill\n---\nRead the guide.\n"
    (stage / "SKILL.md").write_bytes(body)
    relative = ".agenthicc/skills/pending-skill/SKILL.md"
    digest = content_digest({relative: body})
    artifact = PublishedArtifact("skill", "pending-skill", (relative,), digest, "staged")
    run = AuthoredRun(
        "skill-run",
        "session-1",
        "offline",
        (StagedArtifact(artifact, ValidationReport(digest, ())),),
    )
    project = store.workspace / ".agenthicc"
    user = store.workspace.parent / "operator"
    with override_extension_trust(store.trust):
        with store.commit(run):
            assert "pending-skill" not in discover_skills(project, user)
        assert "pending-skill" in discover_skills(project, user)
    assert store.trust.records() == ()
