"""The agents hub transcript shows a session's subagents and the parent's notice.

A detached session is read through the hub, so what its subagents did and the
notice that woke the parent must be in the transcript the hub reads. They used
to be dropped, and the notice read as the operator's own message (``you``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.text import Text

from agenthicc.background.model import BackgroundSession
from agenthicc.tui.workspace.agents_hub.detail import (
    SUBAGENT_ROLE,
    DetailData,
    TranscriptLine,
    read_transcript,
    transcript_line,
)
from agenthicc.tui.workspace.agents_hub.render import detail_lines

pytestmark = pytest.mark.unit

POOL = "pool-0123456789abcdef"
NOTICE = (
    f"Background subagent pool {POOL} completed (5/5 succeeded, 0 failed). Its results are "
    f'not included here: call collect_subagent_results(pool_id="{POOL}") to read them.'
)
SUMMARY = f"Subagents finished — pool {POOL[:8]} · 5/5 succeeded"


def _record(kind: str, **payload: object) -> str:
    return json.dumps({"kind": kind, "payload": payload})


def test_pool_start_lists_the_workers() -> None:
    line = transcript_line(
        "subagent_pool_started",
        {
            "total": 8,
            "workers": [{"label": f"explorer #{index}", "task": "inspect"} for index in range(8)],
        },
    )
    assert line == TranscriptLine(
        SUBAGENT_ROLE,
        "8 workers started · explorer #0, explorer #1, explorer #2, explorer #3, "
        "explorer #4, explorer #5 +2 more",
        "●",
    )


def test_a_worker_result_names_its_progress_and_outcome() -> None:
    done = transcript_line(
        "subagent_worker_done",
        {
            "label": "explorer #1",
            "ok": True,
            "done": 4,
            "total": 5,
            "text": "Read the guide.\nMore.",
        },
    )
    failed = transcript_line(
        "subagent_worker_failed",
        {"label": "explorer #2", "ok": False, "done": 5, "total": 5, "error": "timed out"},
    )
    assert done == TranscriptLine(SUBAGENT_ROLE, "[4/5] explorer #1 · Read the guide.", "✓")
    assert failed == TranscriptLine(SUBAGENT_ROLE, "[5/5] explorer #2 · timed out", "✗")


def test_a_stop_request_says_who_asked_and_why() -> None:
    line = transcript_line(
        "subagent_worker_stop_requested",
        {
            "worker_id": "0123456789abcdef",
            "mode": "abort",
            "source": "background",
            "reason": "user requested session cancellation",
        },
    )
    assert line is not None and line.role == SUBAGENT_ROLE and line.status == "●"
    assert line.text == (
        "89abcdef abort requested · background · user requested session cancellation"
    )


@pytest.mark.parametrize(
    "payload,glyph,text",
    [
        ({"succeeded": 5, "total": 5, "failed": 0}, "✓", "5/5 done"),
        ({"succeeded": 3, "total": 5, "failed": 2}, "✗", "3/5 done · 2 unsuccessful"),
    ],
)
def test_the_pool_result_summarises_the_outcome(
    payload: dict[str, object], glyph: str, text: str
) -> None:
    assert transcript_line("subagent_pool_done", payload) == TranscriptLine(
        SUBAGENT_ROLE, text, glyph
    )


def test_the_parent_notice_is_not_shown_as_the_operator() -> None:
    notice = transcript_line(
        "user_message", {"text": NOTICE, "source": "subagent_completion", "summary": SUMMARY}
    )
    question = transcript_line(
        "user_message", {"text": "A worker is waiting.", "source": "subagent_question"}
    )
    typed = transcript_line("user_message", {"text": "hello"})
    assert notice == TranscriptLine(SUBAGENT_ROLE, f"parent notified · {SUMMARY}", "✓")
    assert question == TranscriptLine(SUBAGENT_ROLE, "parent notified · A worker is waiting.", "◆")
    assert typed == TranscriptLine("you", "hello")


def test_the_hub_reads_the_whole_subagent_story_in_order(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    path.write_text(
        "\n".join(
            [
                _record("user_message", text="Explore with five explorers."),
                _record(
                    "subagent_pool_started",
                    total=2,
                    workers=[{"label": "explorer #1"}, {"label": "explorer #2"}],
                ),
                _record("subagent_worker_done", label="explorer #1", ok=True, done=1, total=2),
                _record("subagent_worker_done", label="explorer #2", ok=True, done=2, total=2),
                _record("subagent_pool_done", succeeded=2, total=2, failed=0),
                _record("user_message", text=NOTICE, source="subagent_completion", summary=SUMMARY),
                _record("text", text="Both explorers reported."),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    lines = read_transcript(path, tail_bytes=1_000_000)

    assert [(line.role, line.status) for line in lines] == [
        ("you", ""),
        (SUBAGENT_ROLE, "●"),
        (SUBAGENT_ROLE, "✓"),
        (SUBAGENT_ROLE, "✓"),
        (SUBAGENT_ROLE, "✓"),
        (SUBAGENT_ROLE, "✓"),
        ("assistant", ""),
    ]
    assert lines[-2].text == f"parent notified · {SUMMARY}"

    session = BackgroundSession.create(
        "s", title="t", cwd=str(tmp_path), workflow_name="", intent="x"
    )
    view = detail_lines(session, DetailData("s", 1, transcript=lines), None, 100, now=0.0)
    shown = "\n".join(Text.from_markup(str(line)).plain for line in view.lines)
    assert "subagents" in shown and "parent notified" in shown and "[2/2] explorer #2" in shown
