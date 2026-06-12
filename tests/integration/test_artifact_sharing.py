"""Integration tests for multi-agent artifact sharing and namespace isolation."""

from __future__ import annotations

import pytest

from agenthicc.memory.layers import GlobalMemoryLayer, ProjectMemoryLayer, SessionMemoryLayer
from agenthicc.memory.router import MemoryRouter

pytestmark = pytest.mark.integration


def make_router(tmp_path) -> MemoryRouter:
    return MemoryRouter(
        session_layer=SessionMemoryLayer(max_entries=256),
        project_layer=ProjectMemoryLayer(tmp_path / "project.db"),
        global_layer=GlobalMemoryLayer(tmp_path / "global.db"),
    )


async def test_two_agents_share_artifact(tmp_path):
    """Agent-A publishes an artifact; agent-B can read it by artifact_id."""
    router = make_router(tmp_path)

    # Agent-A publishes
    pub = await router.publish_artifact(
        b"shared payload",
        content_type="text/plain",
        published_by="agent-A",
    )
    assert pub["ok"] is True
    artifact_id = pub["artifact_id"]

    # Agent-B reads
    result = await router.read_artifact(artifact_id, agent_id="agent-B")
    assert result["found"] is True
    assert result["content"] == b"shared payload"


async def test_namespace_isolation_for_project_kv(tmp_path):
    """Data written by team-a namespace must not be visible in team-b namespace."""
    router = make_router(tmp_path)

    await router.write("shared_key", "team-a value", tier="project", namespace="team-a")

    read_a = await router.read("shared_key", tier="project", namespace="team-a")
    read_b = await router.read("shared_key", tier="project", namespace="team-b")

    assert read_a["found"] is True
    assert read_a["value"] == "team-a value"
    assert read_b["found"] is False


async def test_artifact_id_is_content_addressed(tmp_path):
    """Two agents publishing the same bytes must get the same artifact_id."""
    router = make_router(tmp_path)

    pub_a = await router.publish_artifact(b"canonical bytes", published_by="agent-A")
    pub_b = await router.publish_artifact(b"canonical bytes", published_by="agent-B")

    assert pub_a["artifact_id"] == pub_b["artifact_id"]
