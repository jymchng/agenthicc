"""Check planning, exact static validation, and persisted tool authoring state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lauren_ai._memory import ShortTermMemory

from agenthicc.workflows.authoring.budget import RepairBudget
from agenthicc.workflows.authoring.import_sandbox import ImportCheck
from agenthicc.workflows.authoring.verdict import Finding, ValidationReport
from agenthicc.workflows.create_tools import CreateTools, CreateToolsContext, CreateToolsState
from agenthicc.workflows.create_tools import validation
from agenthicc.workflows.create_tools.plan import ToolPlanError, decode_plan, plan_set
from agenthicc.workflows.loader import builtin_workflow_descriptors

pytestmark = pytest.mark.unit

_PLAN = {
    "name": "describe_name",
    "description": "Describe the caller's name.",
    "capabilities": ["read"],
    "parameters": [
        {"name": "name", "type_hint": "str", "default": "'guest'", "description": "Display name."}
    ],
}
_SOURCE = """\
from lauren_ai import tool
from agenthicc.tools.capabilities import tool_read

@tool_read
@tool(name="describe_name")
async def describe_name(name: str = "guest") -> dict[str, object]:
    \"\"\"Describe the caller's name.

    Args:
        name: Display name.
    \"\"\"
    return {"label": name}

TOOLS = [describe_name]
"""


@pytest.mark.parametrize(
    ("raw", "reserved", "prior", "code"),
    [
        ([], frozenset(), {}, "invalid_plan"),
        (["invalid"], frozenset(), {}, "invalid_plan"),
        ([_PLAN, _PLAN], frozenset(), {}, "name_collision"),
        ([_PLAN], frozenset({"describe_name"}), {}, "name_collision"),
        ([{**_PLAN, "update": True}], frozenset(), {}, "name_collision"),
        ([{**_PLAN, "capabilities": ["unknown"]}], frozenset(), {}, "invalid_capabilities"),
        ([{**_PLAN, "network_hosts": ["local.test"]}], frozenset(), {}, "invalid_capabilities"),
        ([{**_PLAN, "module": "../unsafe"}], frozenset(), {}, "invalid_plan"),
        ([{**_PLAN, "requires_confirmation": "yes"}], frozenset(), {}, "invalid_plan"),
        (
            [{**_PLAN, "parameters": [{"name": "item", "default": "execute()"}]}],
            frozenset(),
            {},
            "invalid_plan",
        ),
        (
            [{**_PLAN, "update": True}],
            frozenset(),
            {
                "describe_name": (".agenthicc/tools/describe_name.py", "prior"),
                "sibling": (".agenthicc/tools/describe_name.py", "prior"),
            },
            "name_collision",
        ),
    ],
)
def test_invalid_plan_refuses_the_entire_set(
    raw: object, reserved: frozenset[str], prior: dict[str, tuple[str, str]], code: str
) -> None:
    with pytest.raises(ToolPlanError) as error:
        plan_set(raw, reserved, prior)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("x = ", "tool.syntax"),
        (_SOURCE.replace("TOOLS = [describe_name]", "TOOLS = []"), "tool.export_mismatch"),
        (_SOURCE.replace('name="describe_name"', 'name="replacement"'), "tool.name_mismatch"),
        (_SOURCE.replace("@tool_read", ""), "tool.capability_mismatch"),
        (_SOURCE.replace('name: str = "guest"', 'name: str = "other"'), "tool.signature_mismatch"),
        (_SOURCE.replace("dict[str, object]", "str"), "tool.return_type"),
        (_SOURCE.replace("dict[str, object]", "dict[str, Any]"), "tool.any_type"),
        (_SOURCE.replace("name: Display name.", "label: Hidden."), "tool.docstring"),
        (_SOURCE.replace("TOOLS =", 'print("unexpected")\nTOOLS ='), "tool.import_side_effect"),
        (
            _SOURCE.replace('return {"label": name}', 'return {"client": httpx.AsyncClient()}'),
            "tool.private_http_client",
        ),
        (
            _SOURCE.replace('return {"label": name}', 'return {"text": open(name).read()}'),
            "tool.capability_mismatch",
        ),
    ],
)
def test_static_validation_reports_specific_errors(source: str, code: str) -> None:
    report = validation._static_checks(source.encode(), "describe_name.py", (decode_plan(_PLAN),))
    assert code in {item.code for item in report if item.severity == "error"}


def test_checkpoint_round_trip_preserves_plans_reports_and_repair_budget() -> None:
    report = ValidationReport(
        "a" * 64, (Finding("tool.example", "warning", "example.py", 2, "Bound the result."),)
    )
    context = CreateToolsContext(
        intent="Describe a name.",
        run_id="tool-run",
        repair=RepairBudget(2, 1),
        state=CreateToolsState.VALIDATE,
        plans=(decode_plan(_PLAN),),
        reports={"describe_name": report},
        static_only=True,
    )
    memory = ShortTermMemory()
    saved = json.loads(json.dumps(CreateTools.checkpoint_context_to_payload(context)))
    restored = CreateTools.checkpoint_context_from_payload(saved, memory)
    assert restored.repair == context.repair
    assert restored.plans == context.plans
    assert restored.reports == context.reports
    assert restored.state is context.state
    assert restored.static_only
    assert restored.shared_memory is memory


def test_preview_fallback_preserves_the_static_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        validation, "inspect_generated_python", lambda *a: ImportCheck("static_only")
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "describe_name.py").write_text(_SOURCE, encoding="utf-8")
    reports, static_only = validation.validate_modules(tmp_path, stage, (decode_plan(_PLAN),))
    assert static_only
    assert reports["describe_name"].errors == 0


def test_module_can_declare_multiple_capabilities_in_one_metadata_decorator() -> None:
    source = _SOURCE.replace(
        "from lauren_ai import tool", "from lauren_ai import set_metadata, tool"
    ).replace(
        "import tool_read\n\n@tool_read",
        "import ToolCapability\n\n"
        '@set_metadata("capabilities", frozenset({ToolCapability.READ, ToolCapability.GIT_READ}))',
    )
    plan = decode_plan({**_PLAN, "capabilities": ["read", "git_read"]})
    assert validation._static_checks(source.encode(), "describe_name.py", (plan,)) == ()


def test_registration_and_models() -> None:
    registered = [descriptor.name for descriptor in builtin_workflow_descriptors()]
    assert registered.count("create_tools") == 1
    assert CreateTools.invocation.agent_callable and CreateTools.invocation.subagent_eligible
    assert CreateTools.build_params({"analyze_model": "plan-model"}).get_phase_models() == {
        "analyze": "plan-model",
        "generate": "",
        "validate": "",
    }
