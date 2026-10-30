"""Provider-driven broad-design workflow and focused-route E2E coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.agents.registry import build_agents_registry
from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.tools.fs.agent_tools import write_file
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager
from agenthicc.workflows.config import WorkflowConfig
from agenthicc.workflows.ui_ux_pro_max import UiUxProMax, UiUxProMaxRunner, UiUxProMaxState

pytestmark = pytest.mark.e2e


def _completion(index: int, response: tuple[str, dict[str, object]] | str) -> Completion:
    if isinstance(response, tuple):
        name, payload = response
        return Completion(
            id=f"ui-ux-{index}",
            model="mock-model",
            content="",
            tool_calls=[ToolCall(tool_use_id=f"tool-{index}", name=name, input=payload)],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=8, output_tokens=4),
        )
    return Completion(
        id=f"ui-ux-{index}",
        model="mock-model",
        content=response,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=8, output_tokens=4),
    )


def _queue(provider: MockTransport, responses: list[tuple[str, dict[str, object]] | str]) -> None:
    for index, response in enumerate(responses):
        provider.queue_response(_completion(index, response))


def _broad_provider(*, new_project: bool = False) -> MockTransport:
    provider = MockTransport()
    _queue(
        provider,
        [
            (
                "complete_product_discovery",
                {
                    "summary": "Greenfield web product request; repository has no established product UI."
                    if new_project
                    else "Existing web application inspected; this is a substantial dashboard redesign.",
                    "details": {
                        "scope": "broad",
                        "project_name": "Ops Console",
                        "project_type": "operations dashboard",
                        "platform": "web",
                        "stack": "React + TypeScript",
                        "request_kind": "new product" if new_project else "redesign",
                        "target_users": ["operations staff"],
                        "user_journeys": ["find and resolve active alerts"],
                        "required_surfaces": ["dashboard", "alert list"],
                        "functional_requirements": ["preserve alert filtering"],
                        "ux_requirements": ["improve status scanability"],
                        "accessibility_expectations": ["keyboard navigation", "visible focus"],
                        "technical_constraints": [
                            "use the repository's existing React and TypeScript stack"
                        ]
                        if new_project
                        else ["preserve existing routes"],
                        "existing_visual_conventions": [
                            "no established product design system; create one from evidence"
                        ]
                        if new_project
                        else ["reuse blue brand token"],
                        "acceptance_criteria": [
                            "alert filtering remains available",
                            "empty and error states are represented",
                        ],
                        "assumptions": ["desktop and mobile are both in scope"],
                    },
                },
            ),
            "Product brief submitted.",
            (
                "submit_design_research",
                {
                    "summary": "Upstream design search is unavailable in this session; findings are repository-grounded.",
                    "details": {
                        "queries": ["operations dashboard alert hierarchy"],
                        "datasets_consulted": [],
                        "findings": [
                            "Existing tokens favor a blue primary and compact data surfaces."
                        ],
                        "source_provenance": ["src/styles/tokens.css"],
                        "applicable_recommendations": ["Reuse the product's token system."],
                        "rejected_recommendations": ["Do not add a separate visual language."],
                        "conflicts": [],
                        "confidence": "medium; upstream dataset search unavailable",
                        "limitations": [
                            "UI/UX Pro Max script/data not loaded in the skill catalog."
                        ],
                        "artifact_refs": ["docs/design/research-report.md"],
                    },
                },
            ),
            "Design research submitted.",
            (
                "write_file",
                {
                    "path": "design-system/ops-console/MASTER.md",
                    "content": "# Ops Console Design System\\n\\nDirection: calm operational clarity.\\n",
                    "create_parents": True,
                },
            ),
            (
                "submit_design_system",
                {
                    "summary": "A coherent token-led master system preserves existing product identity.",
                    "details": {
                        "artifact_refs": ["design-system/ops-console/MASTER.md"],
                        "visual_direction": "calm operational clarity",
                        "rationale": ["Existing visual conventions provide the brand baseline."],
                        "style": "data-dense utility with clear hierarchy",
                        "color_tokens": [
                            "primary = existing brand blue",
                            "error = semantic destructive token",
                        ],
                        "typography_tokens": [
                            "existing sans family; numeric labels use tabular figures"
                        ],
                        "spacing_rules": ["reuse the 4px spacing scale"],
                        "layout_rules": ["responsive grid collapses to one column"],
                        "component_rules": ["reuse existing cards and alert list"],
                        "interaction_states": ["loading", "empty", "success", "error", "disabled"],
                        "responsive_behavior": ["avoid horizontal overflow at narrow widths"],
                        "accessibility_rules": [
                            "visible keyboard focus",
                            "status not conveyed by color alone",
                        ],
                        "motion_rules": [
                            "respect reduced motion; use short state transitions only"
                        ],
                        "anti_patterns": [
                            "do not replace established tokens with generic gradients"
                        ],
                    },
                },
            ),
            "Master design system submitted.",
            (
                "request_design_system_revision",
                {
                    "summary": "Review found a missing contrast rationale.",
                    "details": {
                        "actionable_findings": ["Document contrast pairing for muted status text."]
                    },
                },
            ),
            "Revision requested.",
            (
                "submit_design_system",
                {
                    "summary": "The revised master system documents status contrast and remains token coherent.",
                    "details": {
                        "artifact_refs": ["design-system/ops-console/MASTER.md"],
                        "visual_direction": "calm operational clarity",
                        "rationale": [
                            "Status contrast is checked against existing semantic tokens."
                        ],
                        "style": "data-dense utility with clear hierarchy",
                        "color_tokens": [
                            "brand blue primary",
                            "status labels meet project contrast target",
                        ],
                        "typography_tokens": ["existing sans family with tabular numeric labels"],
                        "spacing_rules": ["reuse the 4px spacing scale"],
                        "layout_rules": ["responsive grid collapses to one column"],
                        "component_rules": ["reuse existing cards and alert list"],
                        "interaction_states": ["loading", "empty", "success", "error", "disabled"],
                        "responsive_behavior": ["avoid horizontal overflow at narrow widths"],
                        "accessibility_rules": [
                            "visible focus",
                            "status has text and icon, not color alone",
                        ],
                        "motion_rules": ["respect reduced motion"],
                        "anti_patterns": ["avoid status conveyed by color alone"],
                    },
                },
            ),
            "Revised system submitted.",
            (
                "approve_design_system",
                {
                    "summary": "Approved: coherent, evidence-based, accessible, and feasible.",
                    "details": {"findings": ["Status contrast rationale is now explicit."]},
                },
            ),
            "Design system approved.",
            (
                "submit_implementation_guide",
                {
                    "summary": "Detected React and TypeScript from package metadata and existing components.",
                    "details": {
                        "detected_stack": "React + TypeScript",
                        "stack_evidence": ["package.json", "src/components/"],
                        "relevant_guidance": [
                            "No local UI/UX Pro Max stack dataset was available."
                        ],
                        "token_mappings": ["map color tokens to existing CSS variables"],
                        "component_strategy": ["extend the current AlertList"],
                        "responsive_strategy": ["use existing CSS grid breakpoints"],
                        "state_handling_conventions": ["retain existing query-state model"],
                        "accessibility_strategy": [
                            "semantic list, keyboard-operable filters, visible focus"
                        ],
                        "test_tools": ["existing component test suite"],
                        "performance_constraints": ["do not add client-side dependencies"],
                        "anti_patterns": ["do not create parallel token sources"],
                        "validation_commands": ["npm test", "npm run build"],
                        "limitations": ["stack-specific upstream guidance unavailable"],
                        "artifact_refs": ["docs/design/implementation-guide.md"],
                    },
                },
            ),
            "Implementation guide submitted.",
            (
                "request_implementation_approval",
                {
                    "plan": "Extend AlertList, preserve filters, add empty/error states and responsive coverage."
                },
            ),
            (
                "submit_experience_architecture",
                {
                    "summary": "Dashboard and alert-list states are mapped to the existing component structure.",
                    "details": {
                        "screen_inventory": ["dashboard", "alert detail"],
                        "interaction_state_matrix": ["AlertList: loading, empty, success, error"],
                        "component_dependency_graph": ["Dashboard → AlertList → AlertRow"],
                        "page_component_specs": ["filters remain keyboard operable"],
                        "acceptance_mapping": ["filter preservation → existing filter tests"],
                        "implementation_tasks": ["extend AlertList", "test empty/error states"],
                        "artifact_refs": ["docs/design/experience-architecture.md"],
                    },
                },
            ),
            "Experience architecture submitted.",
            (
                "write_file",
                {
                    "path": "src/alert-list.tsx",
                    "content": 'export const AlertList = ({ alerts, error }) => { if (error) return <p role="alert">Unable to load alerts</p>; return alerts.length ? <ul>{alerts.map(a => <li key={a.id}>{a.label}</li>)}</ul> : <p>No alerts</p>; };\\n',
                    "create_parents": True,
                },
            ),
            (
                "complete_design_implementation",
                {
                    "summary": "AlertList now implements the approved empty-state behavior while retaining the existing component boundary.",
                    "details": {
                        "modified_files": ["src/alert-list.tsx"],
                        "implemented_components": ["AlertList"],
                        "implemented_states": ["success", "empty"],
                        "design_deviations": [],
                        "deviation_reasons": [],
                        "remaining_work": [],
                    },
                },
            ),
            "Implementation submitted.",
            (
                "submit_visual_ux_audit",
                {
                    "summary": "Automated source and test inspection found an unhandled error state; browser inspection was unavailable.",
                    "details": {
                        "findings": [
                            "High | AlertList | source returns empty state only | error feedback required | error is not represented | add explicit error state | component test"
                        ],
                        "visual_inspection_status": "unavailable",
                        "visual_evidence_refs": [],
                        "command_results": ["npm test: passed"],
                        "acceptance_evidence": ["Existing filter code path is unchanged."],
                        "limitations": [
                            "No browser screenshot tooling was available; visual rendering remains unverified."
                        ],
                    },
                },
            ),
            "Audit submitted.",
            (
                "complete_design_remediation",
                {
                    "summary": "Added explicit error-state handling without changing the filter behavior.",
                    "details": {
                        "resolved_findings": ["AlertList error state is represented."],
                        "unresolved_findings": [],
                        "modified_files": ["src/alert-list.tsx"],
                        "verification_results": ["npm test: passed"],
                    },
                },
            ),
            "Remediation submitted.",
            (
                "submit_visual_ux_audit",
                {
                    "summary": "Source and automated tests confirm required interaction states; screenshot inspection remains unavailable.",
                    "details": {
                        "findings": [],
                        "visual_inspection_status": "unavailable",
                        "visual_evidence_refs": [],
                        "command_results": ["npm test: passed", "npm run build: passed"],
                        "acceptance_evidence": ["Error handling is covered by the component test."],
                        "limitations": ["No screenshot or browser tool was available."],
                    },
                },
            ),
            "Final audit submitted.",
            (
                "complete_design_acceptance",
                {
                    "summary": "Both product criteria are evidenced; visual screenshot inspection was not an acceptance criterion and remains disclosed as a limitation.",
                    "details": {
                        "criterion_results": [
                            "PASS | alert filtering remains available | Existing filter code path unchanged; filter test passed.",
                            "PASS | empty and error states are represented | Source includes empty and error branches; component tests passed.",
                        ],
                        "limitations": ["Rendered visual appearance was not inspected."],
                    },
                },
            ),
            "Acceptance submitted.",
            (
                "complete_design_delivery",
                {
                    "summary": "Redesign implementation completed; tests passed; browser visual inspection was unavailable and is reported as a limitation."
                },
            ),
            "Delivery submitted.",
        ],
    )
    return provider


def _accessibility_provider() -> MockTransport:
    provider = MockTransport()
    _queue(
        provider,
        [
            (
                "complete_product_discovery",
                {
                    "summary": "Repository inspection identifies a terminal UI keyboard-accessibility audit only.",
                    "details": {
                        "scope": "accessibility_audit",
                        "project_name": "sample TUI",
                        "project_type": "terminal application",
                        "platform": "terminal",
                        "stack": "Textual",
                        "request_kind": "accessibility audit only",
                        "target_users": ["keyboard-only users"],
                        "user_journeys": ["navigate the session list"],
                        "required_surfaces": ["session list", "details panel"],
                        "functional_requirements": ["do not modify the application"],
                        "ux_requirements": ["clear focus and key hints"],
                        "accessibility_expectations": ["focus visibility", "keyboard operation"],
                        "technical_constraints": ["terminal rendering"],
                        "existing_visual_conventions": ["use current TUI colors"],
                        "acceptance_criteria": ["keyboard focus is visible"],
                        "assumptions": ["audit only; no code changes requested"],
                    },
                },
            ),
            "Brief submitted.",
            (
                "submit_design_research",
                {
                    "summary": "No UI/UX Pro Max skill was loaded; the audit uses inspected TUI implementation and focused accessibility fallback.",
                    "details": {
                        "queries": ["terminal focus visibility keyboard navigation"],
                        "datasets_consulted": [],
                        "findings": ["Current TUI focus model should be inspected directly."],
                        "source_provenance": ["src/agenthicc/tui/workspace/"],
                        "applicable_recommendations": ["Use non-color focus indicators."],
                        "rejected_recommendations": [
                            "Browser viewport breakpoints do not apply to terminal layout."
                        ],
                        "conflicts": [],
                        "confidence": "fallback; no upstream dataset execution",
                        "limitations": ["Skill not loaded in this session."],
                        "artifact_refs": ["audit/accessibility-research.md"],
                    },
                },
            ),
            "Research submitted.",
            (
                "submit_implementation_guide",
                {
                    "summary": "Textual TUI conventions selected from repository evidence; no web design system is generated.",
                    "details": {
                        "detected_stack": "Textual terminal UI",
                        "stack_evidence": ["pyproject.toml", "src/agenthicc/tui/"],
                        "relevant_guidance": ["Terminal-specific repository patterns only."],
                        "token_mappings": ["existing terminal palette"],
                        "component_strategy": ["audit existing screen widgets without changes"],
                        "responsive_strategy": ["check narrow terminal sizes and wrapping"],
                        "state_handling_conventions": ["preserve current reactive state"],
                        "accessibility_strategy": ["visible focus plus keyboard hints"],
                        "test_tools": ["existing TUI tests"],
                        "performance_constraints": [],
                        "anti_patterns": ["do not apply CSS or browser-specific guidance"],
                        "validation_commands": ["pytest tests/unit/test_tui_*"],
                        "limitations": ["No screen-reader terminal emulator available."],
                        "artifact_refs": ["audit/tui-implementation-guide.md"],
                    },
                },
            ),
            "Guide submitted.",
            (
                "submit_visual_ux_audit",
                {
                    "summary": "Audit inspection reports focus behavior from source and tests; no live terminal visual session was available.",
                    "details": {
                        "findings": [],
                        "visual_inspection_status": "unavailable",
                        "visual_evidence_refs": [],
                        "command_results": ["TUI source review completed"],
                        "acceptance_evidence": [
                            "Focus indicator logic exists in the inspected widget."
                        ],
                        "limitations": [
                            "No live terminal resize or screen-reader run was possible."
                        ],
                    },
                },
            ),
            "Audit submitted.",
            (
                "complete_design_acceptance",
                {
                    "summary": "The source-level criterion is verified; live terminal inspection remains unverified and is disclosed.",
                    "details": {
                        "criterion_results": [
                            "PASS | keyboard focus is visible | Focus indicator branch found in the widget and its unit test passes."
                        ],
                        "limitations": ["Live terminal rendering was unavailable."],
                    },
                },
            ),
            "Acceptance submitted.",
            (
                "complete_design_delivery",
                {
                    "summary": "Accessibility audit completed without modifying application files; upstream skill data was unavailable and terminal visual checks are disclosed as limitations."
                },
            ),
            "Delivery submitted.",
        ],
    )
    return provider


def _targeted_provider() -> MockTransport:
    provider = MockTransport()
    _queue(
        provider,
        [
            (
                "complete_product_discovery",
                {
                    "summary": "Repository inspection identifies a localized dialog focus bug.",
                    "details": {
                        "scope": "targeted",
                        "project_name": "Sample app",
                        "project_type": "web application",
                        "platform": "web",
                        "stack": "React",
                        "request_kind": "targeted component fix",
                        "target_users": ["keyboard users"],
                        "user_journeys": ["open and close the settings dialog"],
                        "required_surfaces": ["settings dialog"],
                        "functional_requirements": ["preserve dialog behavior"],
                        "ux_requirements": ["keep focus inside the open dialog"],
                        "accessibility_expectations": ["keyboard focus management"],
                        "technical_constraints": ["reuse the existing Dialog component"],
                        "existing_visual_conventions": ["keep current dialog styling"],
                        "acceptance_criteria": ["focus remains in dialog until it closes"],
                        "assumptions": [],
                    },
                },
            ),
            "Targeted brief submitted.",
            (
                "submit_design_research",
                {
                    "summary": "The installed search command failed, so no external design findings are claimed.",
                    "details": {
                        "queries": ["dialog keyboard focus behavior"],
                        "datasets_consulted": [],
                        "findings": [],
                        "source_provenance": [],
                        "applicable_recommendations": [],
                        "rejected_recommendations": [],
                        "conflicts": [],
                        "confidence": "low; repository inspection only",
                        "limitations": [
                            "Search command returned non-zero; UX dataset unavailable."
                        ],
                        "artifact_refs": ["docs/design/dialog-research.md"],
                    },
                },
            ),
            "Research limitation recorded.",
            (
                "submit_implementation_guide",
                {
                    "summary": "The repository does not expose enough metadata to identify a supported component framework.",
                    "details": {
                        "detected_stack": "unknown",
                        "stack_evidence": [],
                        "relevant_guidance": [],
                        "token_mappings": [],
                        "component_strategy": ["inspect and preserve the existing Dialog boundary"],
                        "responsive_strategy": ["do not alter current dialog sizing"],
                        "state_handling_conventions": [],
                        "accessibility_strategy": [
                            "preserve focus on close; constrain focus while open"
                        ],
                        "test_tools": [],
                        "performance_constraints": [],
                        "anti_patterns": ["do not add a new UI dependency"],
                        "validation_commands": [],
                        "limitations": [
                            "Stack remains unknown; adapt to local source conventions."
                        ],
                        "artifact_refs": ["docs/design/dialog-stack-guide.md"],
                    },
                },
            ),
            "Stack limitation recorded.",
            (
                "request_implementation_approval",
                {
                    "plan": "Patch the existing Dialog focus trap and add a keyboard regression test."
                },
            ),
            (
                "submit_experience_architecture",
                {
                    "summary": "A single dialog component and its open/close states are in scope.",
                    "details": {
                        "screen_inventory": ["settings dialog"],
                        "interaction_state_matrix": ["closed → open → close"],
                        "component_dependency_graph": ["Settings → Dialog"],
                        "page_component_specs": ["focus stays in dialog while open"],
                        "acceptance_mapping": ["keyboard focus → focus-cycle regression test"],
                        "implementation_tasks": ["correct the existing focus trap"],
                        "artifact_refs": ["docs/design/dialog-spec.md"],
                    },
                },
            ),
            "Targeted experience plan submitted.",
            (
                "complete_design_implementation",
                {
                    "summary": "The existing dialog focus trap was corrected without changing its styling.",
                    "details": {
                        "modified_files": ["src/dialog.tsx", "tests/dialog.test.tsx"],
                        "implemented_components": ["Dialog"],
                        "implemented_states": ["open focus cycle", "close restores trigger focus"],
                        "design_deviations": [],
                        "deviation_reasons": [],
                        "remaining_work": [],
                    },
                },
            ),
            "Targeted implementation submitted.",
            (
                "submit_visual_ux_audit",
                {
                    "summary": "Source and keyboard tests verify focus behavior; live browser inspection was unavailable.",
                    "details": {
                        "findings": [],
                        "visual_inspection_status": "unavailable",
                        "visual_evidence_refs": [],
                        "command_results": ["dialog keyboard test: passed"],
                        "acceptance_evidence": [
                            "Regression test covers focus cycling and restoration."
                        ],
                        "limitations": ["No browser visual inspection tool was available."],
                    },
                },
            ),
            "Targeted audit submitted.",
            (
                "complete_design_acceptance",
                {
                    "summary": "The single targeted criterion is covered by a passing keyboard regression test.",
                    "details": {
                        "criterion_results": [
                            "PASS | focus remains in dialog until it closes | Keyboard regression test covers focus cycle and trigger restoration."
                        ],
                        "limitations": ["Rendered appearance was not inspected."],
                    },
                },
            ),
            "Targeted acceptance submitted.",
            (
                "complete_design_delivery",
                {
                    "summary": "The focused dialog fix is complete; UI search failed and visual inspection limitations are disclosed."
                },
            ),
            "Targeted delivery submitted.",
        ],
    )
    return provider


async def _run_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: MockTransport,
    intent: str,
) -> tuple[object, AppState]:
    monkeypatch.chdir(tmp_path)
    app = AppState.create()
    mode = ModeManager(app_state=app)
    mode.set_by_name("Yolo")
    kernel = KernelAppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    processor = EventProcessor(initial_state=kernel, persist=False)
    processor_task = asyncio.create_task(processor.run())
    try:
        cfg = AgenthiccConfig()
        cfg.execution.model = "mock-model"  # type: ignore[misc]
        cfg.execution.auto_compact = False  # type: ignore[misc]
        cfg.execution.effective_usable_budget = lambda: 12_000  # type: ignore[method-assign]
        memory = ShortTermMemory(max_tokens=12_000)
        config = WorkflowConfig(
            conv_store=app.conversation,
            app_state=app,
            processor=processor,
            agent_runner=AgentRunnerBase(transport=provider, signals=SignalBus()),
            approval_svc=None,
            cfg=cfg,
            skills={},
            plugin_tools=[write_file],
            mcp_registry=None,
            mention_cache=MagicMock(),
            agents_registry=build_agents_registry(),
            params=UiUxProMax.build_params({}),
            session_memory=memory,
            conversation_id="ui-ux-e2e-session",
        )
        runner = UiUxProMaxRunner(config, mode)
        result = await runner.run(intent)
        await processor.drain()
        return result, app
    finally:
        processor_task.cancel()
        await asyncio.gather(processor_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_project", [False, True], ids=["existing-redesign", "new-project"])
async def test_mock_provider_runs_broad_design_review_remediation_and_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_project: bool
) -> None:
    result, app = await _run_provider(
        tmp_path,
        monkeypatch,
        _broad_provider(new_project=new_project),
        (
            "Design a new operations dashboard for desktop and mobile users"
            if new_project
            else "Redesign the operations dashboard for desktop and mobile users"
        ),
    )

    assert result.state is UiUxProMaxState.COMPLETE  # type: ignore[attr-defined]
    assert result.route_profile == "broad"  # type: ignore[attr-defined]
    assert result.design_review_attempts == 2  # type: ignore[attr-defined]
    assert result.remediation_cycles == 1  # type: ignore[attr-defined]
    assert result.passed_criteria == [  # type: ignore[attr-defined]
        "alert filtering remains available",
        "empty and error states are represented",
    ]
    assert result.visual_inspection_status == "unavailable"  # type: ignore[attr-defined]
    assert "No screenshot or browser tool was available." in result.audit_limitations  # type: ignore[attr-defined]
    assert result.changed_files == ["src/alert-list.tsx"]  # type: ignore[attr-defined]
    assert (tmp_path / "src/alert-list.tsx").is_file()
    assert (tmp_path / "design-system/ops-console/MASTER.md").is_file()
    assert result.artifacts["design_system_review"].kind == "approve_design_system"  # type: ignore[attr-defined]
    assert app.workflow_run().status == "complete"


@pytest.mark.asyncio
async def test_mock_provider_accessibility_audit_uses_terminal_route_without_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, app = await _run_provider(
        tmp_path,
        monkeypatch,
        _accessibility_provider(),
        "Audit keyboard accessibility in this TUI without changing code",
    )

    assert result.state is UiUxProMaxState.COMPLETE  # type: ignore[attr-defined]
    assert result.route_profile == "accessibility_audit"  # type: ignore[attr-defined]
    assert result.platform == "terminal"  # type: ignore[attr-defined]
    assert result.changed_files == []  # type: ignore[attr-defined]
    assert result.design_summary == ""  # type: ignore[attr-defined]
    assert result.visual_inspection_status == "unavailable"  # type: ignore[attr-defined]
    assert "design_system_synthesis" not in result.completed_phases  # type: ignore[attr-defined]
    assert "design_implementation" not in result.completed_phases  # type: ignore[attr-defined]
    assert app.workflow_run().status == "complete"


@pytest.mark.asyncio
async def test_mock_provider_runs_targeted_component_fix_with_search_and_stack_limitations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, app = await _run_provider(
        tmp_path,
        monkeypatch,
        _targeted_provider(),
        "Fix keyboard focus behavior in the existing settings dialog",
    )

    assert result.state is UiUxProMaxState.COMPLETE  # type: ignore[attr-defined]
    assert result.route_profile == "targeted"  # type: ignore[attr-defined]
    assert result.detected_stack == "unknown"  # type: ignore[attr-defined]
    assert result.research_datasets == []  # type: ignore[attr-defined]
    assert result.research_limitations == [  # type: ignore[attr-defined]
        "Search command returned non-zero; UX dataset unavailable."
    ]
    assert "design_system_synthesis" not in result.completed_phases  # type: ignore[attr-defined]
    assert "design_system_review" not in result.completed_phases  # type: ignore[attr-defined]
    assert result.passed_criteria == ["focus remains in dialog until it closes"]  # type: ignore[attr-defined]
    assert app.workflow_run().status == "complete"
