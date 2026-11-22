"""Contracts shared by the authoring gate, manifest, and trust store."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from agenthicc.config import load_config
from agenthicc.plugins.authored_types import PublishedArtifact, content_digest
from agenthicc.plugins.trust import ExtensionTrust

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def test_authored_digest_has_one_path_and_bytes_encoding() -> None:
    entries = {"b.txt": b"b", "a.txt": b"a\n"}
    assert content_digest(entries) == (
        "07fa4195358d6835c7a9f49e1089945b43d91c69588cf4e3dfe1865a749cd852"
    )
    assert content_digest({"a.txt": b"a\n", "b.txt": b"b"}) == content_digest(entries)
    assert content_digest({"a.txt": b"a", "b.txt": b"b"}) != content_digest(entries)
    assert content_digest({"a2.txt": b"a\n", "b.txt": b"b"}) != content_digest(entries)


def test_manifest_artifact_round_trip_carries_validation_and_provenance() -> None:
    artifact = PublishedArtifact(
        kind="tool",
        name="render_summary",
        files=(".agenthicc/tools/summary.py",),
        digest=content_digest({".agenthicc/tools/summary.py": b"source"}),
        state="staged",
        declared_capabilities=("read",),
        advisories=("test.warning",),
        validation="static_only",
    )
    assert PublishedArtifact.from_payload(artifact.to_dict()) == artifact
    with pytest.raises(ValueError, match="metadata"):
        PublishedArtifact.from_payload({**artifact.to_dict(), "state": "invented"})


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/tmp/outside.py", "C:/outside.py", ".agenthicc/tools/../outside.py"],
)
def test_artifact_digest_rejects_noncanonical_file_names(path: str) -> None:
    with pytest.raises(ValueError, match="workspace-relative"):
        content_digest({path: b"source"})


def test_authored_trust_rechecks_bytes_and_records_the_set_digest(tmp_path: Path) -> None:
    trust = ExtensionTrust(store_path=tmp_path / "operator" / "trust.json", trusted_roots=())
    candidate = tmp_path / "example.py"
    digest = content_digest({".agenthicc/tools/example.py": b"approved"})
    expected = hashlib.sha256(b"approved").hexdigest()
    candidate.write_bytes(b"changed")

    with pytest.raises(ValueError, match="changed before"):
        trust.trust(candidate, kind="tool", expected_sha256=expected, artifact_digest=digest)
    assert trust.records() == ()

    candidate.write_bytes(b"approved")
    record = trust.trust(candidate, kind="tool", expected_sha256=expected, artifact_digest=digest)
    assert (record.sha256, record.artifact_digest) == (expected, digest)
    assert trust.records() == (record,)


def test_user_stage_only_policy_cannot_be_relaxed_by_project_config(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "project.toml"
    assert (
        load_config(
            project_path=project, user_path=user, env_overrides=False
        ).security.authored_extension_policy
        == "auto"
    )
    user.write_text('[security]\nauthored_extension_policy = "stage_only"\n', encoding="utf-8")
    project.write_text('[security]\nauthored_extension_policy = "auto"\n', encoding="utf-8")
    config = load_config(project_path=project, user_path=user, env_overrides=False)
    assert config.security.authored_extension_policy == "stage_only"


def test_unknown_publication_policy_fails_configuration_validation(tmp_path: Path) -> None:
    project = tmp_path / "project.toml"
    project.write_text('[security]\nauthored_extension_policy = "unknown"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="authored_extension_policy"):
        load_config(project_path=project, user_path=tmp_path / "user.toml", env_overrides=False)
