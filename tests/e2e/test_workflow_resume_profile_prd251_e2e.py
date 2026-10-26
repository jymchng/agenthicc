"""TUI end-to-end coverage for provider-profile-aware workflow recovery."""

from __future__ import annotations

import asyncio
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agenthicc.config import load_config
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.runners.workflow_profile import provider_profile_fingerprint
from agenthicc.runners.workflow_recovery import WorkflowRecoveryCoordinator
from agenthicc.workflows.plugin import PhaseSpec, WorkflowContext, WorkflowPlugin

from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.e2e


def _write_profile_config(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            [execution]
            provider = "openai"
            model = "legacy-model"
            profile = "current"
            api_key_env = "LEGACY_KEY"

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


@pytest.mark.asyncio
async def test_resume_override_keeps_run_phase_and_persists_provider_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAVED_KEY", "saved-test-key")
    monkeypatch.setenv("CURRENT_KEY", "current-test-key")
    monkeypatch.setenv("LEGACY_KEY", "legacy-test-key")
    config_path = tmp_path / "agenthicc.toml"
    _write_profile_config(config_path)
    config = load_config(
        project_path=config_path,
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
    )
    saved_profile_fingerprint = provider_profile_fingerprint(config, "saved")
    # Model the reported case: the checkpoint was created while this profile
    # existed, but it was later removed from the current configuration. The
    # user's explicit recovery choice is the current legacy execution config.
    config = replace(config, providers={})
    session, ctx, _workspace, _input = _make_session(initial_workflow="stale")
    ctx.cfg = config
    ctx.cfg_overrides = ()
    conversation = SessionConversation.open(
        ctx.session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    ctx.session_conversation = conversation
    ctx.session_memory = conversation.memory
    store = WorkflowCheckpointStore(conversation.conversation_id, root=tmp_path / "checkpoints")
    session._workflow_recovery = WorkflowRecoveryCoordinator(
        conversation.conversation_id,
        checkpoint_store=store,
    )
    observed: dict[str, object] = {}
    handle: WorkflowRunHandle

    class ProviderRunner:
        def prepare(self) -> None:
            observed["prepared"] = True

    class SessionRunner:
        def for_execution(self, execution, *, conversation_id: str) -> ProviderRunner:
            observed["execution_profile"] = execution.profile
            observed["conversation_id"] = conversation_id
            return ProviderRunner()

    ctx.agent_runner = SessionRunner()

    class Recoverable(WorkflowPlugin):
        name = "profile_resume_workflow"
        phases = [PhaseSpec(name="implement")]

        @classmethod
        def build_runner(cls, workflow_config: object, _mode: object) -> object:
            from agenthicc.workflows.config import WorkflowConfig

            cfg = cast(WorkflowConfig, workflow_config).cfg
            observed["workflow_profile"] = cfg.execution.profile

            class Runner:
                async def resume(self, context: object) -> object:
                    observed["resume_context"] = context
                    handle.mark_terminal("complete")
                    handle.save_checkpoint(reason="profile-resume e2e completion")
                    ctx.app_state.workflow_run.set(SimpleNamespace(status="complete"))
                    return context

                async def run(self, _intent: str) -> None:
                    pytest.fail("resume must not restart the workflow from its first phase")

            return Runner()

    handle = WorkflowRunHandle.create(
        run_id="saved-profile-run",
        workflow=Recoverable,
        conversation=conversation,
        intent="continue the existing implementation",
        checkpoint_store=store,
        provider_profile="saved",
        provider_profile_fingerprint=saved_profile_fingerprint,
    )
    context = WorkflowContext(
        intent="continue the existing implementation",
        run_id=handle.run_id,
        workflow_name=Recoverable.name,
        current_phase="implement",
        phase_iteration=3,
    )
    handle.attach_context(context)
    handle.update_phase("implement", 0, 3)
    handle.request_pause()
    handle.mark_paused(reason="e2e setup")
    handle.save_checkpoint(reason="e2e setup")
    session._workflow_handle = handle
    ctx.workflow_registry.register(Recoverable)

    async def emit(*_args: object, **_kwargs: object) -> None:
        return None

    ctx.processor.emit = emit
    try:
        checkpoint_before = store.path_for(handle.run_id).read_bytes()
        assert session.route("/workflow resume saved-profile-run")
        assert "provider_profile_unavailable" in (ctx.app_state.conversation.notification() or "")
        assert "/workflow resume [run-id] --profile <name|none>" in (
            ctx.app_state.conversation.notification() or ""
        )
        assert handle.lifecycle == "paused"
        assert store.path_for(handle.run_id).read_bytes() == checkpoint_before
        assert store.claim_owner(handle.run_id) is None
        assert "execution_profile" not in observed

        assert session.route("/workflow resume saved-profile-run --profile none")
        task = session._agent_task
        assert task is not None
        await asyncio.wait_for(task, timeout=2)

        assert observed["execution_profile"] == ""
        assert observed["conversation_id"] == conversation.conversation_id
        assert observed["workflow_profile"] == ""
        assert observed["prepared"] is True
        assert observed["resume_context"] is context
        assert handle.current_phase == "implement"
        saved = store.load(handle.run_id)
        assert saved is not None
        assert saved.current_phase == "implement"
        assert saved.provider_profile == ""
        assert saved.provider_profile_origin == "saved"
        assert saved.provider_profile_history == (
            "saved",
            "<legacy execution settings>",
        )
    finally:
        session._release_workflow_claim(handle)
        conversation.close()


@pytest.mark.asyncio
async def test_missing_saved_profile_preserves_checkpoint_without_provider_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SAVED_KEY", raising=False)
    monkeypatch.setenv("CURRENT_KEY", "current-test-key")
    config_path = tmp_path / "agenthicc.toml"
    _write_profile_config(config_path)
    config = load_config(
        project_path=config_path,
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
    )
    session, ctx, _workspace, _input = _make_session()
    ctx.cfg = config
    conversation = SessionConversation.open(
        ctx.session_id,
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    ctx.session_conversation = conversation
    ctx.session_memory = conversation.memory
    store = WorkflowCheckpointStore(conversation.conversation_id, root=tmp_path / "checkpoints")
    calls: list[str] = []

    class SavedWorkflow(WorkflowPlugin):
        name = "missing_profile_workflow"
        phases = [PhaseSpec(name="implement")]

        @classmethod
        def build_runner(cls, _workflow_config: object, _mode: object) -> object:
            pytest.fail("profile resolution must fail before workflow dispatch")

    class SessionRunner:
        def for_execution(self, _execution: object, *, conversation_id: str) -> object:
            calls.append(conversation_id)
            pytest.fail("profile resolution must fail before runner construction")

    ctx.agent_runner = SessionRunner()
    handle = WorkflowRunHandle.create(
        run_id="missing-profile-run",
        workflow=SavedWorkflow,
        conversation=conversation,
        intent="keep going",
        checkpoint_store=store,
        provider_profile="saved",
        provider_profile_fingerprint=provider_profile_fingerprint(config, "saved"),
    )
    context = WorkflowContext(
        intent="keep going",
        run_id=handle.run_id,
        workflow_name=SavedWorkflow.name,
        current_phase="implement",
        phase_iteration=4,
    )
    handle.attach_context(context)
    handle.update_phase("implement", 0, 4)
    handle.request_pause()
    handle.mark_paused(reason="e2e setup")
    handle.save_checkpoint(reason="e2e setup")
    checkpoint_path = store.path_for(handle.run_id)
    checkpoint_bytes = checkpoint_path.read_bytes()
    session._workflow_handle = handle
    session._workflow_recovery = WorkflowRecoveryCoordinator(
        conversation.conversation_id,
        checkpoint_store=store,
    )
    ctx.workflow_registry.register(SavedWorkflow)

    try:
        assert session.route("/workflow resume missing-profile-run") is True

        assert "provider_profile_invalid" in (ctx.app_state.conversation.notification() or "")
        assert handle.lifecycle == "paused"
        assert handle.current_phase == "implement"
        assert store.path_for(handle.run_id).read_bytes() == checkpoint_bytes
        assert store.claim_owner(handle.run_id) is None
        assert calls == []
    finally:
        session._release_workflow_claim(handle)
        conversation.close()
