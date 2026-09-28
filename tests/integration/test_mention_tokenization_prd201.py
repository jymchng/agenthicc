"""Integration coverage for PRD-201 mention candidate boundaries."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agenthicc.mentions import injector
from agenthicc.mentions.injector import build_context_prefix
from agenthicc.mentions.parser import MentionKind, parse_mentions
from agenthicc.tools.workspace_access import WorkspaceScope

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_literal_identifiers_stop_before_resolution_pipeline(tmp_path: Path) -> None:
    resolver = AsyncMock()

    with patch.object(injector, "resolve_mention", resolver):
        prefix, resolved = await build_context_prefix(
            "Subscribe to @bookTicker @depth20 @100ms and contact@example.com",
            cwd=tmp_path,
        )

    assert prefix == ""
    assert resolved == []
    resolver.assert_not_awaited()


def test_scope_denied_explicit_path_remains_a_denied_mention(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("must not be read", encoding="utf-8")

    mentions = parse_mentions(
        "Inspect @../outside/secret.txt",
        cwd=workspace,
        workspace_scope=WorkspaceScope.create(workspace),
    )

    assert len(mentions) == 1
    assert mentions[0].kind == MentionKind.OUT_OF_SCOPE
    assert mentions[0].scope_status == "outside_workspace"
