"""E2E tests verifying @mention content reaches AgentRunnerBase.

These tests prove that the _agent_text (prefix + original user text) constructed
by _run_agent_turn() carries injected file/directory content into the actual
message delivered to the LLM transport.  All tests use MockTransport so no real
LLM calls are made.

NOTE: no ``from __future__ import annotations`` at module level — @agent() and
@use_tools() inspect real annotations at decoration time.
"""

import asyncio
from pathlib import Path

import pytest

from lauren_ai._agents import agent
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent

from agenthicc.mentions.cache import MentionCache
from agenthicc.mentions.injector import InjectionConfig, build_context_prefix
from agenthicc.mentions.parser import MentionKind

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completion(content: str = "understood.") -> Completion:
    return Completion(
        id="c1",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _make_runner() -> tuple[AgentRunnerBase, MockTransport]:
    """Build a runner backed by MockTransport with one queued response."""
    mock = MockTransport()
    mock.queue_response(_make_completion())
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())
    return runner, mock


# ---------------------------------------------------------------------------
# 11. Injected file content is present in the text delivered to the runner
# ---------------------------------------------------------------------------


async def test_agent_runner_receives_injected_content(tmp_path: Path) -> None:
    """Verify that the text passed to runner.run() contains the injected file content."""
    secret = tmp_path / "secret.py"
    secret.write_text("SECRET_KEY = 42\n")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "check @secret.py", cwd=tmp_path, cfg=cfg
    )

    agent_text = prefix + "check @secret.py"

    assert "SECRET_KEY" in agent_text
    assert "<file" in agent_text

    # Deliver the agent_text to the runner and confirm the transport received it.
    @agent(model="mock-model", system="You are a test agent.")
    class TestAgent: ...

    mock = MockTransport()
    mock.queue_response(_make_completion("I can see SECRET_KEY = 42."))
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())

    agent_inst = TestAgent()
    response = await runner.run(agent_inst, agent_text)
    assert response.content == "I can see SECRET_KEY = 42."

    # Verify the transport received the full injected text
    assert len(mock.calls) == 1
    delivered = mock.calls[0].messages[0]["content"]
    assert "SECRET_KEY" in delivered
    assert "<file" in delivered
    assert "check @secret.py" in delivered


# ---------------------------------------------------------------------------
# 12. Directory listing is delivered to the runner
# ---------------------------------------------------------------------------


async def test_agent_receives_directory_listing(tmp_path: Path) -> None:
    """build_context_prefix for @src/ puts a <dir> block in agent_text."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("# main entry\n")
    (src / "test.py").write_text("# tests\n")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "look at @src/", cwd=tmp_path, cfg=cfg
    )

    assert "<dir" in prefix
    assert "main.py" in prefix

    agent_text = prefix + "look at @src/"

    @agent(model="mock-model", system="filesystem assistant")
    class DirAgent: ...

    mock = MockTransport()
    mock.queue_response(_make_completion("I see main.py and test.py."))
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())

    agent_inst = DirAgent()
    response = await runner.run(agent_inst, agent_text)
    assert response.content == "I see main.py and test.py."

    delivered = mock.calls[0].messages[0]["content"]
    assert "<dir" in delivered
    assert "main.py" in delivered


# ---------------------------------------------------------------------------
# 13. MentionChip objects are created with correct metadata
# ---------------------------------------------------------------------------


async def test_mention_chips_created_for_resolved_files(tmp_path: Path) -> None:
    """MentionChip is built with ok=True, kind='file', and a human-readable size."""
    from agenthicc.tui.transcript import MentionChip

    f = tmp_path / "schema.py"
    f.write_text("class Schema: ...")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "describe @schema.py", cwd=tmp_path, cfg=cfg
    )

    assert len(resolved) == 1
    r = resolved[0]
    assert r.ok is True
    assert r.mention.kind == MentionKind.FILE

    # Build chips the same way _run_agent_turn() does
    chip = MentionChip(
        raw=r.mention.raw,
        kind="file",
        display_size=f"{r.chars_used / 1024:.1f} KB",
        ok=r.ok,
        error=r.error,
    )

    assert chip.ok is True
    assert chip.kind == "file"
    assert "KB" in chip.display_size
    assert chip.error is None
    assert chip.raw == "@schema.py"


# ---------------------------------------------------------------------------
# 14. Unresolved mention chip has ok=False and kind='unresolved'
# ---------------------------------------------------------------------------


async def test_unresolved_chip_has_error(tmp_path: Path) -> None:
    """An @mention for a missing file produces a chip with ok=False."""
    from agenthicc.tui.transcript import MentionChip

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "@doesnotexist.txt", cwd=tmp_path, cfg=cfg
    )

    assert len(resolved) == 1
    r = resolved[0]
    assert r.ok is False
    assert r.error == "not_found"

    # Build the error chip as _run_agent_turn() does for UNRESOLVED mentions
    chip = MentionChip(
        raw=r.mention.raw,
        kind="unresolved",
        display_size="",
        ok=False,
        error="not found",
    )

    assert chip.ok is False
    assert chip.kind == "unresolved"
    assert chip.error == "not found"


# ---------------------------------------------------------------------------
# 15. Full pipeline: inject → add chips to transcript → render → chip lines appear
# ---------------------------------------------------------------------------


async def test_full_pipeline_transcript_renders_chips(tmp_path: Path) -> None:
    """Full pipeline: inject → add chips to TranscriptModel → render → chip with check-mark."""
    from agenthicc.tui.transcript import MentionChip, TranscriptModel

    service = tmp_path / "service.py"
    service.write_text("class ServiceLayer:\n    pass\n")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "review @service.py", cwd=tmp_path, cfg=cfg
    )

    assert len(resolved) == 1
    r = resolved[0]
    assert r.ok is True

    transcript = TranscriptModel()
    agent_id = "agent-test-001"
    transcript.append_turn(agent_id, "assistant (mock-model)")

    # Build chip exactly as _run_agent_turn() does
    chip = MentionChip(
        raw=r.mention.raw,
        kind="file",
        display_size=f"{r.chars_used / 1024:.1f} KB",
        ok=r.ok,
        error=r.error,
    )
    transcript.add_mention_chips(agent_id, [chip])

    if r.block and r.ok:
        transcript.set_mention_content(agent_id, r.mention.raw, r.block)

    rendered = transcript.render()
    rendered_text = "\n".join(rendered)

    # The rendered output must contain the chip with a green check-mark
    assert "@service.py" in rendered_text
    assert "✓" in rendered_text
    assert "KB" in rendered_text

    # Confirm the chip line has the correct structure
    chip_lines = [ln for ln in rendered if "@service.py" in ln]
    assert len(chip_lines) == 1
    chip_line = chip_lines[0]
    assert "✓" in chip_line


# ---------------------------------------------------------------------------
# Bonus: glob mention chip uses "→ N files" display_size
# ---------------------------------------------------------------------------


async def test_glob_chip_shows_file_count(tmp_path: Path) -> None:
    """Glob @mention chip shows '→ N files' as display_size."""
    from agenthicc.tui.transcript import MentionChip

    (tmp_path / "a.py").write_text("A = 1")
    (tmp_path / "b.py").write_text("B = 2")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "check @*.py", cwd=tmp_path, cfg=cfg
    )

    assert len(resolved) == 1
    r = resolved[0]
    assert r.ok is True
    assert r.mention.kind == MentionKind.GLOB

    count = r.block.count("<file ")
    chip = MentionChip(
        raw=r.mention.raw,
        kind="glob",
        display_size=f"→ {count} file{'s' if count != 1 else ''}",
        ok=r.ok,
    )

    assert "→ 2 files" in chip.display_size


# ---------------------------------------------------------------------------
# Bonus: prefix is prepended before user message, not appended after
# ---------------------------------------------------------------------------


async def test_prefix_prepended_not_appended(tmp_path: Path) -> None:
    """The context prefix appears at the start of agent_text, before the user message."""
    f = tmp_path / "api.py"
    f.write_text("API_BASE = 'https://example.com'\n")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "what is @api.py", cwd=tmp_path, cfg=cfg
    )

    assert prefix != ""
    user_message = "what is @api.py"
    agent_text = prefix + user_message

    # Context block comes before the literal user message
    file_block_pos = agent_text.find("<file")
    user_msg_pos = agent_text.find(user_message)
    assert file_block_pos < user_msg_pos, (
        "injected file block must precede the user's original message"
    )

    # The delivered content to the transport captures this ordering
    @agent(model="mock-model", system="api assistant")
    class ApiAgent: ...

    mock = MockTransport()
    mock.queue_response(_make_completion("API_BASE is https://example.com"))
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())

    agent_inst = ApiAgent()
    await runner.run(agent_inst, agent_text)

    delivered = mock.calls[0].messages[0]["content"]
    delivered_file_pos = delivered.find("<file")
    delivered_msg_pos = delivered.find(user_message)
    assert delivered_file_pos < delivered_msg_pos


# ---------------------------------------------------------------------------
# Bonus: multiple resolved mentions all reach the transport
# ---------------------------------------------------------------------------


async def test_multiple_mentions_all_reach_transport(tmp_path: Path) -> None:
    """Three separate file @mentions all appear in the text the runner receives."""
    (tmp_path / "models.py").write_text("MODELS_MARKER = True\n")
    (tmp_path / "routes.py").write_text("ROUTES_MARKER = True\n")
    (tmp_path / "middleware.py").write_text("MIDDLEWARE_MARKER = True\n")

    cfg = InjectionConfig(cwd=tmp_path)
    user_msg = "review @models.py, @routes.py and @middleware.py"
    prefix, resolved = await build_context_prefix(user_msg, cwd=tmp_path, cfg=cfg)

    assert len(resolved) == 3
    assert all(r.ok for r in resolved)

    agent_text = prefix + user_msg

    @agent(model="mock-model", system="code reviewer")
    class ReviewerAgent: ...

    mock = MockTransport()
    mock.queue_response(_make_completion("I reviewed all three files."))
    runner = AgentRunnerBase(transport=mock, signals=SignalBus())

    agent_inst = ReviewerAgent()
    response = await runner.run(agent_inst, agent_text)
    assert response.content == "I reviewed all three files."

    delivered = mock.calls[0].messages[0]["content"]
    assert "MODELS_MARKER" in delivered
    assert "ROUTES_MARKER" in delivered
    assert "MIDDLEWARE_MARKER" in delivered
    assert delivered.count("<file") == 3
