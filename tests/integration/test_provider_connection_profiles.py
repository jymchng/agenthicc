"""Integration coverage for profile resolution at the lauren-ai transport boundary."""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from agenthicc.config import build_llm_config, load_config


def test_modal_profile_reaches_openai_transport_without_modal_sdk(tmp_path, monkeypatch) -> None:
    path = tmp_path / "agenthicc.toml"
    path.write_text(
        textwrap.dedent(
            """
            [execution]
            profile = "modal"

            [providers.modal]
            provider = "openai"
            model = "moonshotai/Kimi-K3"
            base_url = "https://modal.example/v1"
            api_key_env = "MODAL_API_KEY"

            [providers.modal.default_headers]
            "Modal-Key" = { env = "MODAL_KEY" }

            [providers.modal.request_options.provider]
            reasoning_effort = "none"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODAL_API_KEY", "api-key")
    monkeypatch.setenv("MODAL_KEY", "modal-key")

    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    config.resolve_provider_profile()
    llm_config = build_llm_config(config.execution)

    # This is the same transport factory used by TUISession.  The integration
    # deliberately inspects construction only; no external endpoint is called.
    from lauren_ai._module import _build_transport

    transport = _build_transport(llm_config)
    assert transport._config.provider == "openai"
    assert transport._config.base_url == "https://modal.example/v1"
    assert transport._config.default_headers["Modal-Key"] == "modal-key"
    assert transport._config.request_options.provider["reasoning_effort"] == "none"


def test_set_secret_legacy_execution_header_reaches_openai_transport(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MODAL_KEY", "header-secret")
    config = load_config(
        project_path=tmp_path / "missing.toml",
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
        cli_overrides=[
            "execution.provider=openai",
            "execution.model=moonshotai/Kimi-K3",
            "execution.base_url=https://modal.example/v1",
        ],
        cli_secret_overrides=["execution.default_headers.Modal-Key=MODAL_KEY"],
    )
    config.resolve_provider_profile()
    llm_config = build_llm_config(config.execution)
    assert llm_config.default_headers["Modal-Key"] == "header-secret"


class _FakeStream:
    def __init__(self) -> None:
        self._chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="hello", tool_calls=[]),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=[]),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
            ),
        ]

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _FakeStream()
        return SimpleNamespace(
            model=kwargs["model"],
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="hello", tool_calls=[]),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )


class _FakeClient:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.mark.asyncio
async def test_modal_profile_forwards_nonstreaming_and_streaming_request_fields(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "agenthicc.toml"
    path.write_text(
        textwrap.dedent(
            """
            [execution]
            profile = "modal"

            [providers.modal]
            provider = "openai"
            model = "moonshotai/Kimi-K3"
            base_url = "https://modal.example/v1"
            temperature = 0.3
            top_p = 0.95
            max_completion_tokens = 0

            [providers.modal.request_options.provider]
            reasoning_effort = "none"

            [providers.modal.request_options.extra_headers]
            X-Request-Mode = "agenthicc"

            [providers.modal.request_options.extra_query]
            tenant = "research"

            [providers.modal.request_options.extra_body]
            vendor_trace = true
            """
        ),
        encoding="utf-8",
    )
    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    config.resolve_provider_profile()
    from lauren_ai import Message
    from lauren_ai._transport._openai import OpenAITransport

    fake = _FakeClient()
    transport = OpenAITransport(build_llm_config(config.execution), client=fake)
    result = await transport.complete(
        [Message.user("hello")],
        model="moonshotai/Kimi-K3",
        temperature=config.execution.temperature,
        top_p=config.execution.top_p,
        max_completion_tokens=config.execution.max_completion_tokens,
        stream=False,
    )
    assert result.content == "hello"
    call = fake.completions.calls[-1]
    assert call["temperature"] == 0.3
    assert call["top_p"] == 0.95
    assert call["max_completion_tokens"] == 0
    assert call["reasoning_effort"] == "none"
    assert call["extra_headers"] == {"X-Request-Mode": "agenthicc"}
    assert call["extra_query"] == {"tenant": "research"}
    assert call["extra_body"] == {"vendor_trace": True}

    stream = await transport.complete(
        [Message.user("hello")],
        model="moonshotai/Kimi-K3",
        temperature=config.execution.temperature,
        top_p=config.execution.top_p,
        max_completion_tokens=config.execution.max_completion_tokens,
        stream=True,
    )
    chunks = [chunk async for chunk in stream]
    assert "hello" == "".join(chunk.delta for chunk in chunks if chunk.delta)
    assert fake.completions.calls[-1]["stream"] is True
