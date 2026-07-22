"""Unit tests for MemoryRouter (PRD-05)."""

from __future__ import annotations

import asyncio

import pytest

from agenthicc.memory.layers import (
    GlobalMemoryLayer,
    MemoryTier,
    ProjectMemoryLayer,
    SessionMemoryLayer,
)
from agenthicc.memory.router import MemoryRouter

pytestmark = pytest.mark.unit


def make_router(tmp_path, permission_checker=None) -> MemoryRouter:
    session = SessionMemoryLayer(max_entries=128)
    project = ProjectMemoryLayer(tmp_path / "project.db")
    global_ = GlobalMemoryLayer(tmp_path / "global.db")
    return MemoryRouter(
        session_layer=session,
        project_layer=project,
        global_layer=global_,
        permission_checker=permission_checker,
    )


class TestSessionTier:
    async def test_write_read_round_trip(self, tmp_path):
        router = make_router(tmp_path)
        result = await router.write("x", 42, tier="session")
        assert result["ok"] is True
        read = await router.read("x", tier="session")
        assert read["found"] is True
        assert read["value"] == 42

    async def test_missing_key_not_found(self, tmp_path):
        router = make_router(tmp_path)
        read = await router.read("nope", tier="session")
        assert read["found"] is False


class TestProjectTier:
    async def test_write_read_round_trip(self, tmp_path):
        router = make_router(tmp_path)
        await router.write("proj_key", {"data": "value"}, tier="project")
        read = await router.read("proj_key", tier="project")
        assert read["found"] is True
        assert read["value"] == {"data": "value"}

    async def test_missing_key_not_found(self, tmp_path):
        router = make_router(tmp_path)
        read = await router.read("ghost", tier="project")
        assert read["found"] is False


class TestPermissionDenial:
    async def test_denied_read_returns_not_found_with_error(self, tmp_path):
        def deny_project(agent_id, tier, operation):
            if tier is MemoryTier.PROJECT:
                return False
            return True

        router = make_router(tmp_path, permission_checker=deny_project)
        result = await router.read("k", tier="project")
        assert result["found"] is False
        assert "permission_denied" in str(result.get("error", ""))

    async def test_denied_write_returns_not_ok_with_error(self, tmp_path):
        def deny_all(agent_id, tier, operation):
            return False

        router = make_router(tmp_path, permission_checker=deny_all)
        result = await router.write("k", "v", tier="session")
        assert result["ok"] is False
        assert "permission_denied" in str(result.get("error", ""))


class TestTTLPassthrough:
    async def test_session_ttl_expires(self, tmp_path):
        router = make_router(tmp_path)
        await router.write("volatile", "ephemeral", tier="session", ttl=0.01)
        # Immediately readable
        read = await router.read("volatile", tier="session")
        assert read["found"] is True
        # After TTL: gone
        await asyncio.sleep(0.02)
        read_after = await router.read("volatile", tier="session")
        assert read_after["found"] is False


class TestArtifacts:
    async def test_publish_and_read_round_trip(self, tmp_path):
        router = make_router(tmp_path)
        pub = await router.publish_artifact(
            b"artifact bytes", content_type="application/octet-stream"
        )
        assert pub["ok"] is True
        artifact_id = pub["artifact_id"]
        assert artifact_id

        read = await router.read_artifact(artifact_id)
        assert read["found"] is True
        assert read["content"] == b"artifact bytes"
        assert read["content_type"] == "application/octet-stream"

    async def test_publish_artifact_deterministic(self, tmp_path):
        router = make_router(tmp_path)
        pub1 = await router.publish_artifact(b"same content")
        pub2 = await router.publish_artifact(b"same content")
        assert pub1["artifact_id"] == pub2["artifact_id"]

    async def test_read_artifact_not_found(self, tmp_path):
        router = make_router(tmp_path)
        result = await router.read_artifact("0" * 64)
        assert result["found"] is False
        assert result["content"] is None

    async def test_denied_publish_returns_not_ok(self, tmp_path):
        def deny_all(agent_id, tier, operation):
            return False

        router = make_router(tmp_path, permission_checker=deny_all)
        result = await router.publish_artifact(b"blocked")
        assert result["ok"] is False
        assert "permission_denied" in str(result.get("error", ""))
