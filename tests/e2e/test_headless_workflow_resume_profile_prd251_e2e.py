"""Headless workflow resume uses the checkpoint's resolved profile."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from agenthicc.config import load_config
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_profile import provider_profile_fingerprint
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin, WorkflowRun
from tests.unit.test_workflow_cli import _make_session

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_resuming_checkpoint", [False, True])
async def test_headless_resume_uses_explicit_profile_on_same_run_and_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_resuming_checkpoint: bool,
) -> None:
    monkeypatch.setenv("SAVED_KEY", "saved-test-key")
    monkeypatch.setenv("CURRENT_KEY", "current-test-key")
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text(
        textwrap.dedent(
            """
            [execution]
            provider = "openai"
            model = "legacy-model"
            profile = "current"

            [providers.saved]
            provider = "openai"
            model = "saved-model"
            base_url = "https://saved.example/v1"
            api_key_env = "SAVED_KEY"

            [providers.current]
            provider = "openai"
            model = "current-model"
            base_url = "https://current.example/v1"
            api_key_env = "CURRENT_KEY"
            """
        ),
        encoding="utf-8",
    )
    config = load_config(
        project_path=config_path,
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
    )
    calls: list[tuple[str, str]] = []

    class ResumeWorkflow(WorkflowPlugin):
        name = "headless_profile_resume"
        phases = [PhaseSpec(name="implement")]

        @classmethod
        def build_runner(cls, workflow_config, _mode):
            calls.append(("workflow-config", workflow_config.cfg.execution.profile))

            class Runner:
                async def run(self, _intent: str) -> object:
                    pytest.fail("resume must not start a new workflow")

                async def resume(self, context: object) -> object:
                    calls.append(("resume", context.current_phase))
                    workflow_config.app_state.workflow_run.set(
                        WorkflowRun(
                            run_id="headless-saved-run",
                            workflow_name=cls.name,
                            intent="original request",
                            current_phase=None,
                            status="complete",
                        )
                    )
                    return context

            return Runner()

    from agenthicc.runners.headless import execute_workflow

    session = _make_session(ResumeWorkflow)
    session.cfg = config
    session.cfg_overrides = ()
    conversation = SessionConversation.open(
        session.session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    session.session_conversation = conversation
    store = WorkflowCheckpointStore(session.session_id, root=tmp_path / "checkpoints")
    handle = WorkflowRunHandle.create(
        run_id="headless-saved-run",
        workflow=ResumeWorkflow,
        conversation=conversation,
        intent="original request",
        checkpoint_store=store,
        provider_profile="saved",
        provider_profile_fingerprint=provider_profile_fingerprint(config, "saved"),
    )
    context = WorkflowContext(
        intent="original request",
        run_id=handle.run_id,
        workflow_name=ResumeWorkflow.name,
        current_phase="implement",
        phase_iteration=2,
    )
    handle.attach_context(context)
    handle.update_phase("implement", 0, 2)
    handle.request_pause()
    handle.mark_paused(reason="e2e setup")
    handle.save_checkpoint(reason="e2e setup")
    if fail_resuming_checkpoint:
        persist_checkpoint = WorkflowRunHandle.persist_checkpoint

        def fail_resuming_write(self, *, reason: str):
            if reason == "resuming":
                raise OSError("simulated checkpoint write failure")
            return persist_checkpoint(self, reason=reason)

        monkeypatch.setattr(WorkflowRunHandle, "persist_checkpoint", fail_resuming_write)
    monkeypatch.setattr(
        "agenthicc.runners.workflow_checkpoint_store.WorkflowCheckpointStore",
        lambda _session_id: store,
    )
    processor_task = asyncio.create_task(session.processor.run())
    try:
        await asyncio.sleep(0)
        result = await execute_workflow(
            session,
            ResumeWorkflow.name,
            "ignored resume intent",
            resume_run_id=handle.run_id,
            profile_override="current",
        )
    finally:
        await session.processor.stop()
        await processor_task
        conversation.close()

    saved = store.load(handle.run_id)
    assert saved is not None
    assert result.run_id == "headless-saved-run"
    if fail_resuming_checkpoint:
        assert result.status == "paused"
        assert result.failure_category == "workflow_resume_transition_failed"
        assert "could not persist the resuming checkpoint" in (result.error or "")
        assert calls == []
        assert saved.status == "paused"
        assert saved.provider_profile == "saved"
        assert saved.current_phase == "implement"
        assert store.claim_owner(handle.run_id) is None
    else:
        assert result.status == "complete"
        assert calls == [("workflow-config", "current"), ("resume", "implement")]
        assert saved.provider_profile == "current"
        assert saved.provider_profile_origin == "saved"
        assert saved.provider_profile_history == ("saved", "current")
