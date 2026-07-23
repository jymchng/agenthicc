"""Exercise the workflow phase tool contracts without an LLM transport."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agenthicc.workflows.code_plan.phase_tools import (
    make_executor_tools,
    make_planner_tools,
    make_questions_tool,
    make_reviewer_tools,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_planner_executor_reviewer_and_questions_tools() -> None:
    event = asyncio.Event()
    exit_event = asyncio.Event()
    data: dict[str, object] = {}
    request_approval, finalize, exit_tool = make_planner_tools(None, event, data, exit_event)
    assert (await finalize("not approved"))["ok"] is False
    assert (await request_approval("plan"))["approved"] is True
    assert (await finalize("approved plan"))["ok"] is True
    assert data["plan"] == "approved plan" and event.is_set()
    assert (await exit_tool())["accepted"] is True and exit_event.is_set()

    execute_event = asyncio.Event()
    execute_data: dict[str, object] = {}
    mark_complete = make_executor_tools(execute_event, execute_data)[0]
    result = await mark_complete("implemented")
    assert result["ok"] is True and execute_data["summary"] == "implemented"

    review_event = asyncio.Event()
    review_data: dict[str, object] = {}
    approve, reject = make_reviewer_tools(review_event, review_data)
    assert (await approve("looks good"))["ok"] is True
    assert review_data["action"] == "approve"
    assert (await reject("needs work"))["ok"] is True
    assert review_data["action"] == "reject"

    assert (await make_questions_tool(None)[0]([]))["cancelled"] is True
    approval = SimpleNamespace(
        request_approval=lambda request: asyncio.sleep(
            0, result=SimpleNamespace(allowed=True, message=json.dumps({"answer": "yes"}))
        )
    )
    ask = make_questions_tool(approval)[0]
    invalid = await ask([])
    assert "problems" in invalid
    valid = await ask([{"id": "answer", "text": "Answer?", "options": ["yes"]}])
    assert valid == {"answer": "yes"}

    denied = SimpleNamespace(
        request_approval=lambda request: asyncio.sleep(
            0, result=SimpleNamespace(allowed=False, message="")
        )
    )
    assert (await make_questions_tool(denied)[0]([{"id": "x", "text": "x", "options": ["y"]}]))[
        "cancelled"
    ] is True

    malformed = SimpleNamespace(
        request_approval=lambda request: asyncio.sleep(
            0, result=SimpleNamespace(allowed=True, message="not-json")
        )
    )
    assert "error" in await make_questions_tool(malformed)[0](
        [{"id": "x", "text": "x", "options": ["y"]}]
    )
