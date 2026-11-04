"""AGI-04: resume and retry relaunch with the session's own launch configuration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import (
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    LaunchSpec,
    SessionStatus,
)
from agenthicc.background.supervisor import BackgroundRequest, LaunchPreflightError

pytestmark = pytest.mark.unit

SECRET = "execution.default_headers.Authorization=AGI_TEST_API_KEY"
LAUNCH = LaunchSpec(
    config_path=None,
    set_overrides=("execution.provider=openai",),
    set_secret_overrides=(SECRET,),
)


def _supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackgroundSupervisor:
    supervisor = BackgroundSupervisor(
        BackgroundStore(tmp_path / "background"), artifact_root=tmp_path / "sessions"
    )
    supervisor.queue_enabled = False
    monkeypatch.setattr(
        "agenthicc.background.supervisor.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=4321, poll=lambda: None),
    )
    return supervisor


def _failed(supervisor: BackgroundSupervisor, tmp_path: Path, launch: LaunchSpec) -> str:
    session = supervisor.submit(intent="build the book", cwd=str(tmp_path), launch=launch)
    supervisor.store.update(session.session_id, status=SessionStatus.FAILED, error="HTTP 401")
    return session.session_id


def _request(supervisor: BackgroundSupervisor, session_id: str) -> BackgroundRequest:
    return BackgroundRequest.from_mapping(
        json.loads(supervisor._request_path(session_id).read_text(encoding="utf-8"))
    )


def test_resume_launches_with_the_recorded_overrides_and_secret_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGI_TEST_API_KEY", "Bearer secret-value")
    supervisor = _supervisor(tmp_path, monkeypatch)
    session_id = _failed(supervisor, tmp_path, LAUNCH)

    supervisor.resume(session_id)

    request = _request(supervisor, session_id)
    assert request.set_overrides == LAUNCH.set_overrides
    assert request.set_secret_overrides == LAUNCH.set_secret_overrides
    stored = json.loads(supervisor._launch_path(session_id).read_text(encoding="utf-8"))
    assert "secret-value" not in json.dumps(stored)  # only the variable's name


def test_a_resumed_worker_sends_the_authorization_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background.worker import WorkerRequest
    from agenthicc.config import load_config

    monkeypatch.setenv("AGI_TEST_API_KEY", "Bearer secret-value")
    monkeypatch.chdir(tmp_path)
    supervisor = _supervisor(tmp_path, monkeypatch)
    session_id = _failed(supervisor, tmp_path, LAUNCH)
    supervisor.resume(session_id)

    raw = json.loads(supervisor._request_path(session_id).read_text(encoding="utf-8"))
    worker_request = WorkerRequest.from_mapping(raw)
    cfg = load_config(
        user_path=tmp_path / "no-user.toml",
        cli_overrides=list(worker_request.set_overrides),
        cli_secret_overrides=list(worker_request.set_secret_overrides),
    )

    header = cfg.execution.default_headers["Authorization"]
    resolved = header if isinstance(header, str) else header.resolve()
    assert resolved == "Bearer secret-value"
    assert cfg.execution.provider == "openai"


def test_a_missing_secret_variable_refuses_the_resume_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGI_TEST_API_KEY", raising=False)
    supervisor = _supervisor(tmp_path, monkeypatch)
    session_id = _failed(supervisor, tmp_path, LAUNCH)
    before = supervisor.store.get(session_id)

    for operation in (supervisor.resume, supervisor.retry):
        with pytest.raises(LaunchPreflightError, match="AGI_TEST_API_KEY"):
            operation(session_id)

    after = supervisor.store.get(session_id)
    assert (after.status, after.attempt, after.retry_count) == (
        before.status,
        before.attempt,
        before.retry_count,
    )
    assert len(supervisor.store.list()) == 1


def test_retry_starts_a_linked_new_session_and_keeps_the_failed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGI_TEST_API_KEY", "Bearer secret-value")
    supervisor = _supervisor(tmp_path, monkeypatch)
    session_id = _failed(supervisor, tmp_path, LAUNCH)
    original = supervisor.store.get(session_id)

    retried = supervisor.retry(session_id)

    assert retried.session_id != session_id
    assert retried.retried_from == session_id and retried.intent == original.intent
    failed = supervisor.store.get(session_id)
    assert failed.retried_as == retried.session_id
    assert (failed.status, failed.attempt, failed.error) == (
        SessionStatus.FAILED,
        original.attempt,
        "HTTP 401",
    )
    assert _request(supervisor, retried.session_id).set_secret_overrides == (SECRET,)


def test_only_a_failed_session_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background.store import InvalidSessionTransition

    supervisor = _supervisor(tmp_path, monkeypatch)
    session = supervisor.submit(intent="x", cwd=str(tmp_path))
    supervisor.store.update(session.session_id, status=SessionStatus.CANCELLED)

    with pytest.raises(InvalidSessionTransition):
        supervisor.retry(session.session_id)


def test_a_session_from_before_launch_records_resumes_with_its_last_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGI_TEST_API_KEY", "Bearer secret-value")
    supervisor = _supervisor(tmp_path, monkeypatch)
    session = BackgroundSession.create(
        "legacy", title="t", cwd=str(tmp_path), workflow_name="", intent="old"
    ).evolve(status=SessionStatus.FAILED)
    supervisor.store.create(session)
    supervisor._write_request(
        BackgroundRequest(
            session_id="legacy",
            workflow_name="",
            intent="old",
            cwd=str(tmp_path),
            set_overrides=LAUNCH.set_overrides,
            set_secret_overrides=LAUNCH.set_secret_overrides,
        )
    )

    supervisor.resume("legacy")

    assert supervisor.recorded_launch("legacy") == LAUNCH
    assert _request(supervisor, "legacy").set_secret_overrides == (SECRET,)


def test_cli_flags_replace_the_recorded_launch_and_a_preflight_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.cli.commands import background
    from agenthicc.cli.context import CLIContext

    supervisor = _supervisor(tmp_path, monkeypatch)
    session_id = _failed(supervisor, tmp_path, LaunchSpec())
    monkeypatch.setattr(
        background, "_store_and_supervisor", lambda ctx: (supervisor.store, supervisor)
    )
    monkeypatch.delenv("AGI_TEST_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        background._mutate(CLIContext(set_secret_overrides=(SECRET,)), session_id, "resume")
    assert exit_info.value.code == 2
    assert "AGI_TEST_API_KEY" in capsys.readouterr().out

    monkeypatch.setenv("AGI_TEST_API_KEY", "Bearer secret-value")
    background._mutate(CLIContext(set_secret_overrides=(SECRET,)), session_id, "resume")
    assert supervisor.recorded_launch(session_id) == LaunchSpec(set_secret_overrides=(SECRET,))


async def test_the_hub_reports_a_failed_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background import AgentsService

    monkeypatch.delenv("AGI_TEST_API_KEY", raising=False)
    supervisor = _supervisor(tmp_path, monkeypatch)
    session_id = _failed(supervisor, tmp_path, LAUNCH)
    service = AgentsService(supervisor.store, supervisor)

    result = await service.resume_async(session_id)

    assert result.ok is False and "AGI_TEST_API_KEY" in result.message


def test_provider_diagnostics_keep_qualified_model_names() -> None:
    from agenthicc.runners.agent_turn import _safe_provider_identifier

    assert _safe_provider_identifier("deepseek/deepseek-v4.1-flash") == (
        "deepseek/deepseek-v4.1-flash"
    )
    assert _safe_provider_identifier("model@2026-01") == "model@2026-01"
    assert _safe_provider_identifier("bad value\n'\"") == "badvalue"


def test_resume_replays_journaled_reasoning_content(tmp_path: Path) -> None:
    from agenthicc.memory.journal import ConversationJournal

    path = tmp_path / "journal.jsonl"
    journal = ConversationJournal(path)
    journal.append_message({"role": "user", "content": "hi"})
    journal.append_message({"role": "assistant", "content": "ok", "reasoning_content": "chain"})
    journal.close()

    messages, _summary = ConversationJournal(path).fold()

    assert messages[-1] == {"role": "assistant", "content": "ok", "reasoning_content": "chain"}
