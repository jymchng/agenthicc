"""Unit tests for storage configuration (StorageS3Settings, StorageSettings)."""
from __future__ import annotations

import pytest

from agenthicc.config import AgenthiccConfig, StorageS3Settings, load_config

pytestmark = pytest.mark.unit


def test_default_storage_not_configured():
    cfg = AgenthiccConfig()
    assert cfg.storage.s3.configured is False


def test_storage_s3_configured_when_bucket_set():
    s3 = StorageS3Settings(bucket="x")
    assert s3.configured is True


def test_load_config_parses_s3_toml(tmp_path):
    toml_file = tmp_path / "agenthicc.toml"
    toml_file.write_text(
        '[storage.s3]\nbucket = "test"\nregion = "eu-west-1"\n',
        encoding="utf-8",
    )
    cfg = load_config(
        project_path=toml_file,
        user_path=tmp_path / "missing.toml",
        env_overrides=False,
    )
    assert cfg.storage.s3.bucket == "test"
    assert cfg.storage.s3.region == "eu-west-1"


def test_load_config_s3_defaults_when_no_section(tmp_path):
    toml_file = tmp_path / "agenthicc.toml"
    toml_file.write_text('[execution]\nmax_parallel_tasks = 2\n', encoding="utf-8")
    cfg = load_config(
        project_path=toml_file,
        user_path=tmp_path / "missing.toml",
        env_overrides=False,
    )
    assert cfg.storage.s3.configured is False


def test_storage_s3_mounts_parsed(tmp_path):
    toml_file = tmp_path / "agenthicc.toml"
    toml_file.write_text(
        '[storage.s3.mounts]\narchive = {bucket = "b2", prefix = "p/"}\n',
        encoding="utf-8",
    )
    cfg = load_config(
        project_path=toml_file,
        user_path=tmp_path / "missing.toml",
        env_overrides=False,
    )
    assert cfg.storage.s3.mounts["archive"]["bucket"] == "b2"
