"""Typed transition tools for UI/UX Pro Max specialist phases.

These tools validate and record compact summaries. Agents create full reports
and design artifacts through the ordinary workspace tools; transition tools
never fabricate, inspect, or write those files.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from agenthicc.tools.approval import ApprovalService
from agenthicc.workflows.ui_ux_pro_max.state import bounded_list, bounded_text


def _record(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    decision: str,
    summary: str,
    **values: object,
) -> dict[str, object]:
    clean_summary = bounded_text(summary)
    if not clean_summary:
        return {
            "ok": False,
            "error": "A non-empty phase summary is required.",
            "fix": "Finish the phase and submit a concise evidence-based summary.",
        }
    if event.is_set():
        return {
            "ok": False,
            "error": "A successful transition is already recorded for this phase.",
            "fix": "Do not submit another transition; the runner will apply the recorded result.",
        }
    data.clear()
    data.update({"decision": decision, "summary": clean_summary, **values})
    event.set()
    return {"ok": True, "message": f"Recorded {decision}; the runner will apply the transition."}


def make_product_discovery_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_product_discovery(
        summary: str,
        scope: str,
        project_name: str,
        project_type: str,
        platform: str,
        stack: str,
        request_kind: str,
        target_users: list[str],
        user_journeys: list[str],
        required_surfaces: list[str],
        functional_requirements: list[str],
        ux_requirements: list[str],
        accessibility_expectations: list[str],
        technical_constraints: list[str],
        existing_visual_conventions: list[str],
        acceptance_criteria: list[str],
        assumptions: list[str],
    ) -> dict[str, object]:
        """Submit the repository-grounded product and experience brief."""
        normalized_scope = scope.strip().lower()
        if normalized_scope not in {"broad", "targeted", "accessibility_audit"}:
            return {
                "ok": False,
                "error": "scope must be broad, targeted, or accessibility_audit",
            }
        if not bounded_list(acceptance_criteria):
            return {"ok": False, "error": "At least one concrete acceptance criterion is required."}
        return _record(
            event,
            data,
            decision="complete_product_discovery",
            summary=summary,
            scope=normalized_scope,
            project_name=bounded_text(project_name, 256),
            project_type=bounded_text(project_type, 256),
            platform=bounded_text(platform, 128),
            stack=bounded_text(stack, 512),
            request_kind=bounded_text(request_kind, 128),
            target_users=bounded_list(target_users),
            user_journeys=bounded_list(user_journeys),
            required_surfaces=bounded_list(required_surfaces),
            functional_requirements=bounded_list(functional_requirements),
            ux_requirements=bounded_list(ux_requirements),
            accessibility_expectations=bounded_list(accessibility_expectations),
            technical_constraints=bounded_list(technical_constraints),
            existing_visual_conventions=bounded_list(existing_visual_conventions),
            acceptance_criteria=bounded_list(acceptance_criteria),
            assumptions=bounded_list(assumptions),
        )

    return [complete_product_discovery]


def make_design_research_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_design_research(
        summary: str,
        queries: list[str],
        datasets_consulted: list[str],
        findings: list[str],
        source_provenance: list[str],
        applicable_recommendations: list[str],
        rejected_recommendations: list[str],
        conflicts: list[str],
        confidence: str,
        limitations: list[str],
        artifact_refs: list[str],
    ) -> dict[str, object]:
        """Submit actual design research, queries, sources, and limitations."""
        if not bounded_list(queries):
            return {
                "ok": False,
                "error": "Record at least one query or explicit unavailable-capability check.",
            }
        return _record(
            event,
            data,
            decision="submit_design_research",
            summary=summary,
            queries=bounded_list(queries),
            datasets_consulted=bounded_list(datasets_consulted),
            findings=bounded_list(findings),
            source_provenance=bounded_list(source_provenance),
            applicable_recommendations=bounded_list(applicable_recommendations),
            rejected_recommendations=bounded_list(rejected_recommendations),
            conflicts=bounded_list(conflicts),
            confidence=bounded_text(confidence, 128),
            limitations=bounded_list(limitations),
            artifact_refs=bounded_list(artifact_refs),
        )

    return [submit_design_research]


def make_design_system_synthesis_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_design_system(
        summary: str,
        artifact_refs: list[str],
        visual_direction: str,
        rationale: list[str],
        style: str,
        color_tokens: list[str],
        typography_tokens: list[str],
        spacing_rules: list[str],
        layout_rules: list[str],
        component_rules: list[str],
        interaction_states: list[str],
        responsive_behavior: list[str],
        accessibility_rules: list[str],
        motion_rules: list[str],
        anti_patterns: list[str],
    ) -> dict[str, object]:
        """Submit a coherent master design system and its workspace reference."""
        required_values = {
            "visual direction": visual_direction,
            "style": style,
            "color tokens": color_tokens,
            "typography tokens": typography_tokens,
            "spacing rules": spacing_rules,
            "layout rules": layout_rules,
            "component rules": component_rules,
            "interaction states": interaction_states,
            "responsive behavior": responsive_behavior,
            "accessibility rules": accessibility_rules,
            "anti-patterns": anti_patterns,
            "artifact reference": artifact_refs,
        }
        missing = [name for name, value in required_values.items() if not value]
        if missing:
            return {
                "ok": False,
                "error": "The design-system submission is incomplete.",
                "missing": missing,
                "fix": "Synthesize the missing system decisions and persist the master artifact before transition.",
            }
        return _record(
            event,
            data,
            decision="submit_design_system",
            summary=summary,
            artifact_refs=bounded_list(artifact_refs),
            visual_direction=bounded_text(visual_direction, 1_000),
            rationale=bounded_list(rationale),
            style=bounded_text(style, 256),
            color_tokens=bounded_list(color_tokens),
            typography_tokens=bounded_list(typography_tokens),
            spacing_rules=bounded_list(spacing_rules),
            layout_rules=bounded_list(layout_rules),
            component_rules=bounded_list(component_rules),
            interaction_states=bounded_list(interaction_states),
            responsive_behavior=bounded_list(responsive_behavior),
            accessibility_rules=bounded_list(accessibility_rules),
            motion_rules=bounded_list(motion_rules),
            anti_patterns=bounded_list(anti_patterns),
        )

    return [submit_design_system]


def make_design_system_review_tools(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    attempts: int,
    max_attempts: int,
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def approve_design_system(summary: str, findings: list[str]) -> dict[str, object]:
        """Approve a coherent, feasible, evidence-informed design system."""
        return _record(
            event,
            data,
            decision="approve_design_system",
            summary=summary,
            findings=bounded_list(findings),
        )

    @tool_control
    @_tool()
    async def request_design_system_revision(
        summary: str, actionable_findings: list[str]
    ) -> dict[str, object]:
        """Return actionable synthesis findings, with a hard retry limit."""
        if not bounded_list(actionable_findings):
            return {"ok": False, "error": "A revision request requires actionable findings."}
        decision = (
            "request_design_system_revision" if attempts < max_attempts else "block_design_system"
        )
        return _record(
            event,
            data,
            decision=decision,
            summary=summary,
            findings=bounded_list(actionable_findings),
        )

    @tool_control
    @_tool()
    async def block_design_system(summary: str, blockers: list[str]) -> dict[str, object]:
        """Record an unresolved material design-system blocker."""
        return _record(
            event,
            data,
            decision="block_design_system",
            summary=summary,
            findings=bounded_list(blockers),
        )

    return [approve_design_system, request_design_system_revision, block_design_system]


def make_stack_adaptation_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_implementation_guide(
        summary: str,
        detected_stack: str,
        stack_evidence: list[str],
        relevant_guidance: list[str],
        token_mappings: list[str],
        component_strategy: list[str],
        responsive_strategy: list[str],
        state_handling_conventions: list[str],
        accessibility_strategy: list[str],
        test_tools: list[str],
        performance_constraints: list[str],
        anti_patterns: list[str],
        validation_commands: list[str],
        limitations: list[str],
        artifact_refs: list[str],
    ) -> dict[str, object]:
        """Map the approved design to the repository's detected stack."""
        if not bounded_text(detected_stack, 512):
            return {
                "ok": False,
                "error": "Report the detected stack or explicitly state it is unknown.",
            }
        return _record(
            event,
            data,
            decision="submit_implementation_guide",
            summary=summary,
            detected_stack=bounded_text(detected_stack, 512),
            stack_evidence=bounded_list(stack_evidence),
            relevant_guidance=bounded_list(relevant_guidance),
            token_mappings=bounded_list(token_mappings),
            component_strategy=bounded_list(component_strategy),
            responsive_strategy=bounded_list(responsive_strategy),
            state_handling_conventions=bounded_list(state_handling_conventions),
            accessibility_strategy=bounded_list(accessibility_strategy),
            test_tools=bounded_list(test_tools),
            performance_constraints=bounded_list(performance_constraints),
            anti_patterns=bounded_list(anti_patterns),
            validation_commands=bounded_list(validation_commands),
            limitations=bounded_list(limitations),
            artifact_refs=bounded_list(artifact_refs),
        )

    return [submit_implementation_guide]


def make_experience_architecture_tools(
    approval_service: ApprovalService | None,
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    approval: dict[str, object] = {"granted": False, "feedback": ""}

    @_tool()
    async def request_implementation_approval(plan: str) -> dict[str, object]:
        """Request approval for the concrete screen/component implementation plan."""
        clean_plan = bounded_text(plan)
        if not clean_plan:
            return {"ok": False, "error": "The implementation plan must not be empty."}
        if approval_service is None:
            approval["granted"] = True
            approval["feedback"] = "No interactive approval service is configured for this run."
            return {"ok": True, "approved": True, "feedback": approval["feedback"]}
        from agenthicc.tools.approval import ApprovalRequest

        request = ApprovalRequest(
            tool_name="UI/UX Pro Max Implementation Plan",
            tool_use_id=uuid.uuid4().hex,
            tool_input={"plan": clean_plan},
            capabilities=frozenset(),
            event=asyncio.Event(),
            kind="plan_review",
        )
        try:
            response = await approval_service.request_approval(request)
        except Exception as exc:  # noqa: BLE001
            approval["granted"] = False
            return {"ok": False, "error": f"approval failed: {type(exc).__name__}: {exc}"}
        approval["granted"] = bool(response.allowed)
        approval["feedback"] = bounded_text(response.message)
        return {
            "ok": bool(response.allowed),
            "approved": bool(response.allowed),
            "feedback": approval["feedback"],
        }

    @tool_control
    @_tool()
    async def submit_experience_architecture(
        summary: str,
        screen_inventory: list[str],
        interaction_state_matrix: list[str],
        component_dependency_graph: list[str],
        page_component_specs: list[str],
        acceptance_mapping: list[str],
        implementation_tasks: list[str],
        artifact_refs: list[str],
    ) -> dict[str, object]:
        """Submit the page/component model and approved implementation plan."""
        if approval["granted"] is not True:
            return {
                "ok": False,
                "error": "Request and receive implementation-plan approval before submitting this phase.",
            }
        return _record(
            event,
            data,
            decision="submit_experience_architecture",
            summary=summary,
            screen_inventory=bounded_list(screen_inventory),
            interaction_state_matrix=bounded_list(interaction_state_matrix),
            component_dependency_graph=bounded_list(component_dependency_graph),
            page_component_specs=bounded_list(page_component_specs),
            acceptance_mapping=bounded_list(acceptance_mapping),
            implementation_tasks=bounded_list(implementation_tasks),
            artifact_refs=bounded_list(artifact_refs),
            approval_feedback=bounded_text(approval["feedback"]),
        )

    return [request_implementation_approval, submit_experience_architecture]


def make_implementation_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_implementation(
        summary: str,
        modified_files: list[str],
        implemented_components: list[str],
        implemented_states: list[str],
        design_deviations: list[str],
        deviation_reasons: list[str],
        remaining_work: list[str],
    ) -> dict[str, object]:
        """Record actual implementation results and deviations."""
        return _record(
            event,
            data,
            decision="complete_design_implementation",
            summary=summary,
            modified_files=bounded_list(modified_files),
            implemented_components=bounded_list(implemented_components),
            implemented_states=bounded_list(implemented_states),
            design_deviations=bounded_list(design_deviations),
            deviation_reasons=bounded_list(deviation_reasons),
            remaining_work=bounded_list(remaining_work),
        )

    return [complete_design_implementation]


def make_visual_audit_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def submit_visual_ux_audit(
        summary: str,
        findings: list[str],
        visual_inspection_status: str,
        visual_evidence_refs: list[str],
        command_results: list[str],
        acceptance_evidence: list[str],
        limitations: list[str],
    ) -> dict[str, object]:
        """Submit evidence-backed visual, usability, accessibility, and runtime findings."""
        status = visual_inspection_status.strip().lower()
        if status not in {"inspected", "unavailable", "not_applicable", "not_required"}:
            return {
                "ok": False,
                "error": "visual_inspection_status must be inspected, unavailable, not_applicable, or not_required",
            }
        return _record(
            event,
            data,
            decision="submit_visual_ux_audit",
            summary=summary,
            findings=bounded_list(findings),
            visual_inspection_status=status,
            visual_evidence_refs=bounded_list(visual_evidence_refs),
            command_results=bounded_list(command_results),
            acceptance_evidence=bounded_list(acceptance_evidence),
            limitations=bounded_list(limitations),
        )

    return [submit_visual_ux_audit]


def make_remediation_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_remediation(
        summary: str,
        resolved_findings: list[str],
        unresolved_findings: list[str],
        modified_files: list[str],
        verification_results: list[str],
    ) -> dict[str, object]:
        """Record bounded corrections for findings from the latest audit."""
        return _record(
            event,
            data,
            decision="complete_design_remediation",
            summary=summary,
            resolved_findings=bounded_list(resolved_findings),
            unresolved_findings=bounded_list(unresolved_findings),
            modified_files=bounded_list(modified_files),
            verification_results=bounded_list(verification_results),
        )

    return [complete_design_remediation]


_ACCEPTANCE_STATUSES = {"PASS", "FAIL", "UNVERIFIED", "NOT_APPLICABLE"}


def _validate_criterion_results(
    acceptance_criteria: list[str], results: list[str]
) -> tuple[list[str], str]:
    expected = {
        " ".join(item.split()).casefold(): item for item in bounded_list(acceptance_criteria)
    }
    if not expected:
        return [], "The product brief must contain at least one acceptance criterion."
    parsed: dict[str, tuple[str, str]] = {}
    for raw in bounded_list(results):
        parts = [part.strip() for part in raw.split("|", 2)]
        if len(parts) != 3 or parts[0].upper() not in _ACCEPTANCE_STATUSES:
            return [], "Each result must be STATUS | exact criterion | evidence or reason."
        status, criterion, evidence = parts[0].upper(), parts[1], parts[2]
        key = " ".join(criterion.split()).casefold()
        if key not in expected:
            return [], f"Result does not match a product-brief criterion: {criterion[:80]}"
        if key in parsed:
            return [], f"A criterion has more than one result: {expected[key][:80]}"
        if not evidence:
            return [], f"Every criterion result needs evidence or a reason: {expected[key][:80]}"
        parsed[key] = (status, evidence)
    missing = [item for key, item in expected.items() if key not in parsed]
    if missing:
        return [], "Missing explicit status for: " + "; ".join(missing[:5])
    return bounded_list(results), ""


def make_design_acceptance_tools(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    acceptance_criteria: list[str],
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_acceptance(
        summary: str, criterion_results: list[str], limitations: list[str]
    ) -> dict[str, object]:
        """Record explicit PASS/FAIL/UNVERIFIED/NOT_APPLICABLE for every criterion."""
        results, error = _validate_criterion_results(acceptance_criteria, criterion_results)
        if error:
            return {"ok": False, "error": error}
        return _record(
            event,
            data,
            decision="complete_design_acceptance",
            summary=summary,
            criterion_results=results,
            limitations=bounded_list(limitations),
        )

    return [complete_design_acceptance]


def make_delivery_tools(
    event: asyncio.Event, data: dict[str, object]
) -> list[Callable[..., object]]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @_tool()
    async def complete_design_delivery(summary: str) -> dict[str, object]:
        """Submit the final report without upgrading unverified work to success."""
        return _record(event, data, decision="complete_design_delivery", summary=summary)

    return [complete_design_delivery]
