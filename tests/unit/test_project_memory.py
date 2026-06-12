"""Unit tests for ProjectMemoryLayer (PRD-05 tier 2)."""

from __future__ import annotations

import pytest

from agenthicc.memory.layers import ProjectMemoryLayer

pytestmark = pytest.mark.unit


class TestProjectMemoryKV:
    async def test_write_read_round_trip(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "test.db")
        await layer.set("greeting", "hello")
        found, value = await layer.get("greeting")
        assert found is True
        assert value == "hello"

    async def test_missing_key_returns_not_found(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "test.db")
        found, value = await layer.get("nonexistent")
        assert found is False
        assert value is None

    async def test_namespace_isolation(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "test.db")
        await layer.set("key", "value-in-ns1", namespace="ns1")
        await layer.set("key", "value-in-ns2", namespace="ns2")

        found1, v1 = await layer.get("key", namespace="ns1")
        found2, v2 = await layer.get("key", namespace="ns2")
        assert found1 and v1 == "value-in-ns1"
        assert found2 and v2 == "value-in-ns2"

    async def test_persistence_across_instances(self, tmp_path):
        db = tmp_path / "persist.db"
        layer_a = ProjectMemoryLayer(db)
        await layer_a.set("persistent_key", {"nested": 42})

        layer_b = ProjectMemoryLayer(db)
        found, value = await layer_b.get("persistent_key")
        assert found is True
        assert value == {"nested": 42}

    async def test_overwrite_updates_value(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "test.db")
        await layer.set("k", "original")
        await layer.set("k", "updated")
        found, value = await layer.get("k")
        assert found and value == "updated"

    async def test_delete_removes_key(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "test.db")
        await layer.set("temp", "exists")
        await layer.delete("temp")
        found, _ = await layer.get("temp")
        assert found is False


class TestProjectMemoryArtifacts:
    async def test_publish_returns_artifact_id(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "artifacts.db")
        record = await layer.put_artifact(b"some bytes")
        assert record.artifact_id  # non-empty string
        assert len(record.artifact_id) == 64  # sha256 hex is 64 chars

    async def test_artifact_content_is_bytes(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "artifacts.db")
        record = await layer.put_artifact(b"raw bytes content")
        assert isinstance(record.content, bytes)
        assert record.content == b"raw bytes content"

    async def test_publish_deterministic(self, tmp_path):
        """Same content always produces the same artifact_id."""
        layer = ProjectMemoryLayer(tmp_path / "artifacts.db")
        rec1 = await layer.put_artifact(b"deterministic content")
        rec2 = await layer.put_artifact(b"deterministic content")
        assert rec1.artifact_id == rec2.artifact_id

    async def test_different_content_different_ids(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "artifacts.db")
        rec1 = await layer.put_artifact(b"content-A")
        rec2 = await layer.put_artifact(b"content-B")
        assert rec1.artifact_id != rec2.artifact_id

    async def test_read_artifact_found(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "artifacts.db")
        record = await layer.put_artifact(b"hello artifact")
        fetched = await layer.get_artifact(record.artifact_id)
        assert fetched is not None
        assert fetched.content == b"hello artifact"
        assert fetched.artifact_id == record.artifact_id

    async def test_read_artifact_not_found_returns_none(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "artifacts.db")
        result = await layer.get_artifact("0" * 64)
        assert result is None

    async def test_string_content_encoded_as_bytes(self, tmp_path):
        layer = ProjectMemoryLayer(tmp_path / "artifacts.db")
        record = await layer.put_artifact("string content")
        assert isinstance(record.content, bytes)
        assert record.content == b"string content"
