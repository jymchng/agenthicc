"""End-to-end session identity journey for OpenCode Go (PRD-187)."""

from __future__ import annotations

import textwrap

import pytest

from agenthicc.config import build_llm_config, load_config
from agenthicc.runners.session_conversation import SessionConversation

pytestmark = pytest.mark.e2e


def test_new_and_resumed_session_keep_one_provider_identity(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text(
        textwrap.dedent(
            """
            [execution]
            profile = "opencode_go"

            [providers.opencode_go]
            provider = "openai"
            protocol = "opencode-go"
            model = "kimi-k3"
            base_url = "https://opencode.ai/zen/go/v1"
            api_key_env = "OPENCODE_API_KEY"
            session_header = "x-opencode-session"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_API_KEY", "api-key")
    config = load_config(project_path=config_path, user_path=tmp_path / "missing.toml")

    journal_path = tmp_path / "conversation.jsonl"
    first = SessionConversation.open(
        "opencode-session-e2e", max_tokens=10_000, journal_path=journal_path
    )
    try:
        config.resolve_provider_profile()
        first_transport_config = build_llm_config(
            config.execution, conversation_id=first.conversation_id
        )
        assert first_transport_config.default_headers["x-opencode-session"] == (
            "opencode-session-e2e"
        )
    finally:
        first.close()

    resumed = SessionConversation.open(
        "opencode-session-e2e", max_tokens=10_000, journal_path=journal_path
    )
    try:
        resumed_transport_config = build_llm_config(
            config.execution, conversation_id=resumed.conversation_id
        )
        assert resumed.conversation_id == first.conversation_id
        assert (
            resumed_transport_config.default_headers["x-opencode-session"]
            == (first_transport_config.default_headers["x-opencode-session"])
        )
    finally:
        resumed.close()
