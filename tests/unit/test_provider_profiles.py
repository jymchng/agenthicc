"""Provider profile and OpenAI-compatible endpoint configuration tests."""

from __future__ import annotations

import textwrap

import pytest

from agenthicc.config import (
    RequestOptionSettings,
    SecretReference,
    build_llm_config,
    load_config,
)

pytestmark = pytest.mark.unit


def _write(path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _profile_toml() -> str:
    return """
    [execution]
    profile = "modal_kimi"
    max_output_tokens = 12000

    [providers.modal_kimi]
    provider = "openai"
    model = "moonshotai/Kimi-K3"
    base_url = "https://example.modal.run/v1"
    api_key_env = "MODAL_API_KEY"
    timeout_s = 120.0
    temperature = 0.3
    top_p = 0.95
    max_completion_tokens = 16384

    [providers.modal_kimi.default_headers]
    "Modal-Key" = { env = "MODAL_KEY" }

    [providers.modal_kimi.request_options.provider]
    reasoning_effort = "none"

    [providers.modal_kimi.request_options.extra_body]
    vendor_trace = true
    """


def test_profile_resolves_current_environment_and_builds_openai_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(path, _profile_toml())
    monkeypatch.setenv("MODAL_API_KEY", "api-secret")
    monkeypatch.setenv("MODAL_KEY", "header-secret")

    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    profile = config.resolve_provider_profile()
    assert profile is not None
    assert profile.api_key == "api-secret"
    assert profile.default_headers == {"Modal-Key": "header-secret"}
    assert profile.protocol == "openai-compatible"
    with pytest.raises(TypeError):
        profile.default_headers["Injected"] = "value"  # type: ignore[index]

    llm = build_llm_config(config.execution)
    assert llm.provider == "openai"
    assert llm.model == "moonshotai/Kimi-K3"
    assert llm.base_url == "https://example.modal.run/v1"
    assert llm.api_key == "api-secret"
    assert llm.default_headers["Modal-Key"] == "header-secret"
    assert llm.temperature == 0.3
    assert llm.top_p == 0.95
    assert llm.max_completion_tokens == 16384
    assert llm.request_options.provider["reasoning_effort"] == "none"
    assert llm.request_options.extra_body["vendor_trace"] is True


def test_profile_is_re_resolved_after_secret_rotation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(path, _profile_toml())
    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")

    monkeypatch.setenv("MODAL_API_KEY", "first")
    monkeypatch.setenv("MODAL_KEY", "first-header")
    assert config.resolve_provider_profile().api_key == "first"
    monkeypatch.setenv("MODAL_API_KEY", "rotated")
    monkeypatch.setenv("MODAL_KEY", "rotated-header")
    profile = config.resolve_provider_profile()
    assert profile.api_key == "rotated"
    assert profile.default_headers["Modal-Key"] == "rotated-header"


def test_nested_cli_override_selects_and_edits_profile(tmp_path) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(path, _profile_toml())
    config = load_config(
        project_path=path,
        user_path=tmp_path / "missing.toml",
        env_overrides=False,
        cli_overrides=[
            "execution.profile=modal_kimi",
            "providers.modal_kimi.model=override-model",
        ],
    )
    assert config.execution.profile == "modal_kimi"
    assert config.providers["modal_kimi"].model == "override-model"


def test_set_secret_override_stores_reference_and_resolves_at_runtime(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(
        project_path=tmp_path / "missing.toml",
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
        cli_overrides=["execution.provider=openai", "execution.model=modal-model"],
        cli_secret_overrides=["execution.default_headers.Modal-Key=MODAL_KEY"],
    )
    reference = config.execution.default_headers["Modal-Key"]
    assert isinstance(reference, SecretReference)
    assert reference.env == "MODAL_KEY"
    assert "MODAL_KEY" in repr(config.redacted_dict())

    monkeypatch.setenv("MODAL_KEY", "header-secret")
    config.resolve_provider_profile()
    llm = build_llm_config(config.execution)
    assert llm.default_headers["Modal-Key"] == "header-secret"
    assert "header-secret" not in repr(config.redacted_dict())


def test_set_secret_override_supports_api_key_and_missing_env_is_clear(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(
        project_path=tmp_path / "missing.toml",
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
        cli_overrides=["execution.provider=openai"],
        cli_secret_overrides=["execution.api_key=MODAL_API_KEY"],
    )
    assert isinstance(config.execution.api_key, SecretReference)
    monkeypatch.delenv("MODAL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MODAL_API_KEY"):
        config.resolve_provider_profile()


@pytest.mark.parametrize(
    "override",
    ["missing_equals", "nodot=MODAL_KEY", "execution.default_headers.X=bad-name"],
)
def test_set_secret_override_rejects_malformed_input(tmp_path, override: str) -> None:
    with pytest.raises(ValueError):
        load_config(
            project_path=tmp_path / "missing.toml",
            user_path=tmp_path / "missing-user.toml",
            env_overrides=False,
            cli_secret_overrides=[override],
        )


def test_user_and_project_profiles_merge_by_field(tmp_path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "project.toml"
    _write(
        user,
        """
        [providers.gateway]
        provider = "openai"
        model = "user-model"
        base_url = "https://user.example/v1"
        [providers.gateway.default_headers]
        X-User = "yes"
        """,
    )
    _write(
        project,
        """
        [execution]
        profile = "gateway"
        [providers.gateway]
        model = "project-model"
        """,
    )
    config = load_config(project_path=project, user_path=user, env_overrides=False)
    resolved = config.resolve_provider_profile()
    assert resolved is not None
    assert resolved.model == "project-model"
    assert resolved.base_url == "https://user.example/v1"
    assert resolved.default_headers["X-User"] == "yes"


def test_missing_secret_fails_without_disclosing_value(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(path, _profile_toml())
    monkeypatch.delenv("MODAL_API_KEY", raising=False)
    monkeypatch.delenv("MODAL_KEY", raising=False)
    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")

    with pytest.raises(ValueError, match="MODAL_API_KEY") as error:
        config.resolve_provider_profile()
    assert "api-secret" not in str(error.value)
    assert "header-secret" not in str(error.value)


def test_config_redaction_keeps_secret_values_out_of_diagnostics(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(path, _profile_toml())
    monkeypatch.setenv("MODAL_API_KEY", "api-secret")
    monkeypatch.setenv("MODAL_KEY", "header-secret")
    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")

    before = repr(config.redacted_dict())
    assert "api-secret" not in before
    assert "header-secret" not in before
    assert "MODAL_API_KEY" in before
    assert "MODAL_KEY" in before

    config.resolve_provider_profile()
    after = repr(config.redacted_dict())
    assert "api-secret" not in after
    assert "header-secret" not in after


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ('base_url = "ftp://example.com"', r"HTTP\(S\) URL"),
        ('base_url = "https://user:pass@example.com/v1"', "credentials"),
        ('[providers.bad.default_headers]\n"X-Test\\n" = "x"', "header name"),
    ],
)
def test_profile_validation_rejects_unsafe_connection_values(tmp_path, fragment, message) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(
        path,
        f"""
        [execution]
        profile = "bad"
        [providers.bad]
        provider = "openai"
        model = "model"
        {fragment}
        """,
    )
    with pytest.raises(ValueError, match=message):
        load_config(project_path=path, user_path=tmp_path / "missing.toml")


def test_request_option_settings_use_symbolic_header_references() -> None:
    settings = RequestOptionSettings.from_mapping(
        {
            "extra_headers": {"X-Token": {"env": "TOKEN"}},
            "provider": {"reasoning_effort": "none"},
            "extra_body": {"trace": True},
            "max_retries": 0,
        },
        path="request_options",
    )
    assert isinstance(settings.extra_headers["X-Token"], SecretReference)
    assert settings.redacted()["extra_headers"] == {"X-Token": {"env": "TOKEN"}}


def test_profile_fields_fall_back_to_legacy_execution_values(tmp_path) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(
        path,
        """
        [execution]
        profile = "gateway"
        model = "legacy-model"
        base_url = "https://legacy.example/v1"
        api_key = "legacy-secret"

        [providers.gateway]
        provider = "openai"
        """,
    )
    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    resolved = config.resolve_provider_profile()
    assert resolved is not None
    assert resolved.model == "legacy-model"
    assert resolved.base_url == "https://legacy.example/v1"
    assert resolved.api_key == "legacy-secret"


def test_request_options_reject_duplicate_canonical_fields() -> None:
    with pytest.raises(ValueError, match="canonical"):
        RequestOptionSettings.from_mapping(
            {"extra_body": {"temperature": 0.2}}, path="providers.gateway.request_options"
        )
    with pytest.raises(ValueError, match="provider and extra_body"):
        RequestOptionSettings.from_mapping(
            {
                "provider": {"vendor_mode": "fast"},
                "extra_body": {"vendor_mode": "safe"},
            },
            path="providers.gateway.request_options",
        )


def test_capabilities_are_known_and_enforced_for_tool_sessions(tmp_path) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(
        path,
        """
        [execution]
        profile = "readonly"
        [providers.readonly]
        provider = "openai"
        model = "model"
        base_url = "https://example.com/v1"
        [providers.readonly.capabilities]
        tools = false
        streaming = true
        """,
    )
    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    config.resolve_provider_profile()  # Configuration-only consumers may inspect it.
    with pytest.raises(ValueError, match="tools=false"):
        config.resolve_provider_profile(requires_tools=True)


def test_unknown_capability_is_rejected(tmp_path) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(
        path,
        """
        [providers.gateway]
        provider = "openai"
        [providers.gateway.capabilities]
        magical_reasoning = true
        """,
    )
    with pytest.raises(ValueError, match="not a known capability"):
        load_config(project_path=path, user_path=tmp_path / "missing.toml")


def test_recursive_option_redaction_hides_sensitive_keys(tmp_path) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(
        path,
        """
        [providers.gateway]
        provider = "openai"
        [providers.gateway.request_options.extra_body]
        api_token = "do-not-print"
        visible = { nested = true }
        """,
    )
    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    rendered = repr(config.redacted_dict())
    assert "do-not-print" not in rendered
    assert "nested" in rendered


def test_checkpoint_carries_profile_identity_but_no_secret() -> None:
    from agenthicc.workflows.checkpoint import WorkflowCheckpoint

    checkpoint = WorkflowCheckpoint(
        run_id="run",
        workflow_name="demo",
        conversation_id="session",
        intent="intent",
        status="paused",
        current_phase="plan",
        phase_index=0,
        phase_iteration=1,
        conversation_cursor=2,
        context={"kind": "generic", "fields": {}},
        plugin_fingerprint="fingerprint",
        provider_profile="modal_kimi",
    )
    raw = checkpoint.to_dict()
    assert raw["provider_profile"] == "modal_kimi"
    assert "api-secret" not in repr(raw)
    restored = WorkflowCheckpoint.from_dict(raw)
    assert restored.provider_profile == "modal_kimi"
