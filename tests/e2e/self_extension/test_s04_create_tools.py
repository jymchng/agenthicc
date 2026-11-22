"""Build three tools in two modules and call one on the first parent request."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .harness import reply, tool_call

if TYPE_CHECKING:
    from .harness import AuthoredSession, ScriptedTransport

pytestmark = pytest.mark.e2e

_NAMES = ("count_words", "count_letters", "reverse_text")
_PLANS = [
    {
        "name": _NAMES[0],
        "description": "Count words.",
        "capabilities": ["read"],
        "module": "counts",
    },
    {
        "name": _NAMES[1],
        "description": "Count letters.",
        "capabilities": ["read"],
        "module": "counts",
    },
    {"name": _NAMES[2], "description": "Reverse text.", "capabilities": ["read"]},
]
_HEADER = """\
from lauren_ai import tool
from agenthicc.tools.capabilities import tool_read
"""


def _module(*names: str) -> str:
    lines = [
        f'@tool_read\n@tool(name="{name}")\n'
        f"async def {name}() -> dict[str, object]:\n"
        f'    """Report the outcome for {name}."""\n'
        f'    return {{"ok": True, "marker": "{name}_confirmed"}}\n'
        for name in names
    ]
    return _HEADER + "\n" + "\n".join(lines) + f"\nTOOLS = [{', '.join(names)}]\n"


def _enter(script: ScriptedTransport) -> None:
    script.expect(
        "enter the tool workflow",
        lambda request: "enter_workflow" in request.tools,
        tool_call(
            "enter_workflow", {"workflow": "create_tools", "intent": "Create the fixture tools."}
        ),
    )
    script.expect(
        "finish the parent turn",
        lambda request: request.contains("handoff_accepted"),
        reply("I will continue once the tools are created."),
    )


def _submit(script: ScriptedTransport) -> None:
    script.expect(
        "submit the complete plan",
        lambda request: "submit_tools_plan" in request.tools,
        tool_call("submit_tools_plan", {"tools": _PLANS}),
    )
    script.expect(
        "finish planning", lambda request: request.contains("count_words"), reply("Planned.")
    )


def _generate(
    script: ScriptedTransport, session: AuthoredSession, *, broken: bool = False, attempt: int = 1
) -> None:
    staged = session.project / ".agenthicc/.staging"
    counts = session.project / ".agenthicc/tools/counts.py"
    script.expect(
        "write only inside the scoped staging directory",
        lambda request: "write_file" in request.tools and "run_bash" not in request.tools,
        tool_call(
            "write_file",
            {"path": "counts.py", "content": _module(*_NAMES[:2])},
            use_id=f"counts-{attempt}",
        ),
    )
    script.expect(
        "write the second module",
        lambda request: any(staged.glob("*/tool/counts.py")) and not counts.exists(),
        tool_call(
            "write_file",
            {"path": "reverse_text.py", "content": "default =" if broken else _module(_NAMES[2])},
            use_id=f"reverse-{attempt}",
        ),
    )
    script.expect(
        "confirm generation",
        lambda request: any(staged.glob("*/tool/reverse_text.py")),
        tool_call("confirm_generation_complete", {}, use_id=f"confirmed-{attempt}"),
    )
    script.expect(
        "finish generation", lambda request: request.contains("ok"), reply("Wrote both modules.")
    )


async def test_create_tools_publishes_and_dispatches_on_the_first_parent_request(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
) -> None:
    _enter(script)
    _submit(script)
    _generate(script, authored_session)
    script.expect(
        "approve after deterministic validation",
        lambda request: "approve_tools" in request.tools and "write_file" not in request.tools,
        tool_call("approve_tools", {}),
    )
    script.expect(
        "finish tool review",
        lambda request: not authored_session.trust.records(),
        reply("All three tools match the plan."),
    )
    script.expect(
        "the first parent request sees all three tools",
        lambda request: (
            request.contains("[WORKFLOW RETURN]")
            and request.contains("loaded")
            and set(_NAMES) <= set(request.tools)
        ),
        tool_call("count_words", {}),
    )
    script.expect(
        "the newly published tool executes",
        lambda request: request.contains("count_words_confirmed"),
        reply("The tools are ready."),
    )

    result = await authored_session.run("default", "Create the tools and use one.")
    assert result.status == "complete", result.error
    assert authored_session.context.workflow_stack.depth == 0
    assert {
        record.artifact.name for record in authored_session.context.authored.store().records()
    } == set(_NAMES)
    assert len(authored_session.trust.records()) == 2


async def test_one_broken_module_consumes_the_shared_budget_and_publishes_nothing(
    authored_session: AuthoredSession,
    script: ScriptedTransport,
) -> None:
    authored_session.context.cfg.workflows["authoring"] = {"max_repair_cycles": 1}
    _enter(script)
    _submit(script)
    _generate(script, authored_session, broken=True)
    _generate(script, authored_session, broken=True, attempt=2)
    script.expect(
        "return the bounded failure without another review or generation",
        lambda request: (
            request.contains("[WORKFLOW RETURN]")
            and request.contains("repair budget exhausted")
            and "count_words" not in request.tools
        ),
        reply("The entire set was discarded."),
    )

    result = await authored_session.run("default", "Create the tools.")
    assert result.status == "complete", result.error
    assert not list((authored_session.project / ".agenthicc/.staging").iterdir())
    assert not list((authored_session.project / ".agenthicc/tools").glob("*.py"))
    assert not authored_session.trust.records()
