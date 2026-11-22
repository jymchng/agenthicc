"""Shared isolated session setup for the workflow composition scenarios."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from lauren_ai._agents._runner import AgentRunnerBase

from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
from agenthicc.runners import tui_session
from agenthicc.runners.headless import _close_headless_session
from agenthicc.tui.runtime import session_log

from .harness import AuthoredSession, CrashAt, ModelRequest, ScriptedTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_Policy = Literal["auto", "stage_only"]


@pytest.fixture
def policy() -> _Policy:
    return "auto"


@pytest.fixture
def script() -> ScriptedTransport:
    return ScriptedTransport()


@pytest.fixture
def request_log(script: ScriptedTransport) -> list[ModelRequest]:
    return script.requests


@pytest.fixture
def crash_at() -> CrashAt:
    return CrashAt()


@pytest.fixture
async def authored_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: ScriptedTransport,
    policy: _Policy,
) -> AsyncIterator[AuthoredSession]:
    project = tmp_path / "project"
    test_home = tmp_path / "home"
    sessions = test_home / ".agenthicc" / "sessions"
    project.mkdir()
    sessions.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", lambda: test_home)
    monkeypatch.setattr(tui_session, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(session_log, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(session_log, "_SESSION_INDEX", sessions / "index.json")

    config = project / "agenthicc.toml"
    config.write_text(
        "[skills]\ninstall_default_skills = false\n"
        f'[security]\nauthored_extension_policy = "{policy}"\n',
        encoding="utf-8",
    )
    trust = ExtensionTrust(store_path=tmp_path / "trust.json", trusted_roots=())
    with override_extension_trust(trust):
        context = await tui_session._build_session_context(
            None, [], config_path=str(config), headless=True, mode_name="Yolo"
        )
        context.agent_runner = AgentRunnerBase(transport=script)
        task = asyncio.create_task(context.processor.run(), name="wfc-acceptance-processor")
        try:
            yield AuthoredSession(context=context, project=project, trust=trust)
            script.assert_finished()
        finally:
            if context.startup is not None:
                await context.startup.close()
            await _close_headless_session(context, task, None)
