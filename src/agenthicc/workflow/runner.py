"""WorkflowRunner — executes phase-based workflows using AgentsRegistry (PRD-87)."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from typing import Any

log = logging.getLogger(__name__)


class WorkflowRunner:

    def __init__(
        self,
        definition:      Any,           # WorkflowDefinition
        conv_store:      Any,
        app_state:       Any,
        processor:       Any,           # EventProcessor
        agent_runner:    Any,           # lauren-ai AgentRunnerBase (for transport/signals)
        session_mem:     Any,
        approval_svc:    Any | None,
        cfg:             Any,
        skills:          dict,
        plugin_tools:    list,
        mcp_registry:    Any | None,
        mention_cache:   Any,
        agents_registry: Any,           # AgentsRegistry
        mode_manager:    Any = None,    # ModeManager — used for mode_override per phase
        completed_turns: int = 0,
    ) -> None:
        self._def             = definition
        self._conv_store      = conv_store
        self._app_state       = app_state
        self._processor       = processor
        self._runner          = agent_runner
        self._session_mem     = session_mem
        self._approval_svc    = approval_svc
        self._cfg             = cfg
        self._skills          = skills
        self._plugin_tools    = plugin_tools
        self._mcp_registry    = mcp_registry
        self._mention_cache   = mention_cache
        self._agents_registry = agents_registry
        self._mode_manager    = mode_manager
        self._completed_turns = completed_turns
        # Derive the raw API model string from the transport config (primary)
        # falling back to cfg.execution.effective_model() (secondary).
        # Never use the display label (provider/model) — the API only accepts
        # the model portion without the provider prefix.
        _transport_cfg  = getattr(
            getattr(agent_runner, "_transport", None), "_config", None
        )
        _transport_model = getattr(_transport_cfg, "model", None)
        self._model_id   = _transport_model or cfg.execution.effective_model()

    # ── public entry point ────────────────────────────────────────────────────

    async def run(self, intent: str) -> None:
        from agenthicc.workflow.plugin import WorkflowContext, WorkflowRun, PhaseRunRecord  # noqa: PLC0415
        from lauren_ai._memory import ShortTermMemory                                       # noqa: PLC0415

        # Shared memory for all phases — the agent carries full context forward.
        self._shared_memory = ShortTermMemory(max_tokens=32_000)

        run_id  = uuid.uuid4().hex
        context = WorkflowContext(intent=intent, run_id=run_id, workflow_name=self._def.name)
        wf_run  = WorkflowRun(
            run_id=run_id,
            workflow_name=self._def.name,
            intent=intent,
            current_phase=self._def.first_phase().name if self._def.first_phase() else None,
            total_phases=len(self._def.phases),
        )
        self._app_state.workflow_run.set(wf_run)

        iteration_counts: dict[str, int] = {}
        _processed_parallel: set[str]    = set()
        phase_name = self._def.first_phase().name if self._def.first_phase() else None

        try:
            while phase_name is not None:
                spec = self._def.get_phase(phase_name)
                if spec is None:
                    log.error("Workflow %s: unknown phase %r", self._def.name, phase_name)
                    wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                    self._app_state.workflow_run.set(wf_run)
                    return

                if phase_name in _processed_parallel:
                    phase_name = spec.next
                    continue

                count = iteration_counts.get(phase_name, 0)
                if spec.max_iterations > 0 and count >= spec.max_iterations:
                    log.warning(
                        "Workflow %s: phase %r hit max_iterations=%d",
                        self._def.name, phase_name, spec.max_iterations,
                    )
                    wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                    self._app_state.workflow_run.set(wf_run)
                    self._conv_store.append_event("error", {
                        "message": (
                            f"Workflow '{self._def.name}': phase '{phase_name}' exceeded "
                            f"max_iterations={spec.max_iterations}. Stopping."
                        )
                    })
                    return
                iteration_counts[phase_name] = count + 1

                wf_run = dataclasses.replace(wf_run, current_phase=phase_name)
                self._app_state.workflow_run.set(wf_run)

                if spec.parallel_with:
                    peer_specs = [spec] + [
                        self._def.get_phase(n)
                        for n in spec.parallel_with
                        if self._def.get_phase(n) is not None
                    ]
                    outputs = await asyncio.gather(
                        *[self._run_phase(ps, intent, context) for ps in peer_specs],
                        return_exceptions=True,
                    )
                    for ps, output in zip(peer_specs, outputs):
                        if isinstance(output, Exception):
                            log.error("Parallel phase %r failed: %s", ps.name, output)
                        else:
                            context.add_output(output)
                            record = PhaseRunRecord(
                                phase_name=ps.name,
                                role=ps.agent_type,
                                approved=output.approved,
                                output_summary=output.full_text[:200],
                                iteration=iteration_counts.get(ps.name, 1),
                                duration_s=output.duration_s,
                            )
                            wf_run = dataclasses.replace(
                                wf_run, phase_history=wf_run.phase_history + [record],
                            )
                    _processed_parallel.update(ps.name for ps in peer_specs)
                    phase_name = spec.next
                    continue

                from agenthicc.kernel import Event  # noqa: PLC0415
                await self._processor.emit(Event.create("WorkflowPhaseStarted", {
                    "run_id": run_id, "phase_name": phase_name,
                    "workflow_name": self._def.name,
                }))

                output = await self._run_phase(spec, intent, context)
                context.add_output(output)

                record = PhaseRunRecord(
                    phase_name=spec.name,
                    role=spec.agent_type,
                    approved=output.approved,
                    output_summary=output.full_text[:200],
                    iteration=count + 1,
                    duration_s=output.duration_s,
                )
                wf_run = dataclasses.replace(
                    wf_run, phase_history=wf_run.phase_history + [record],
                )
                self._app_state.workflow_run.set(wf_run)

                await self._processor.emit(Event.create("WorkflowPhaseCompleted", {
                    "run_id": run_id, "phase_name": phase_name, "approved": output.approved,
                }))

                phase_name = self._determine_transition(spec, output)

            wf_run = dataclasses.replace(wf_run, status="complete", current_phase=None)
            self._app_state.workflow_run.set(wf_run)
            await self._processor.emit(Event.create("WorkflowRunCompleted", {
                "run_id": run_id, "workflow_name": self._def.name,
                "phases_run": len(wf_run.phase_history), "status": "complete",
            }))

        except (asyncio.CancelledError, KeyboardInterrupt):
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._app_state.workflow_run.set(wf_run)
            raise
        except Exception as exc:
            log.error("WorkflowRunner error: %s", exc, exc_info=True)
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._app_state.workflow_run.set(wf_run)
            self._conv_store.append_event("error", {
                "message": f"Workflow '{self._def.name}' failed: {exc}"
            })

    # ── phase execution ───────────────────────────────────────────────────────

    async def _run_phase(self, spec: Any, intent: str, context: Any) -> Any:
        """Execute one phase by delegating to _run_agent_turn.

        _run_agent_turn owns all conv_store lifecycle (begin_turn / end_turn),
        real-time text streaming, token accounting, and tool signal routing.
        _run_phase provides phase-specific configuration:
          - project_plugin_tools = capability-filtered tool list
          - system_prompt_suffix = role-specific instructions from AgentsRegistry
          - output_collector = captures text for PhaseOutput
        """
        from agenthicc.workflow.plugin import PhaseOutput, _parse_output_schema  # noqa: PLC0415
        from agenthicc.runners.agent_turn import _run_agent_turn                 # noqa: PLC0415

        if spec.agent_type == "human":
            return await self._run_human_phase(spec, context)

        phase_text = self._build_phase_prompt(spec, intent, context)
        filtered   = self._filter_tools(spec)
        # system_prompt_override on PhaseSpec takes priority over the registry.
        role_prompt = (
            spec.system_prompt_override
            or self._agents_registry.get_role_system_prompt(spec.agent_type)
        )
        output_buf: list[str] = []
        t0 = time.monotonic()

        # ── Approval tool injection ───────────────────────────────────────────
        # request_plan_approval() and finalize_plan() are injected into every
        # phase when approval_svc is available.  Only the plan phase will
        # actually call them (guided by system_prompt_override); other phases
        # simply ignore them.  plan_event tells _run_phase whether this phase
        # called finalize_plan().
        plan_event: asyncio.Event | None = None
        plan_data:  dict                 = {}
        if self._approval_svc is not None:
            from agenthicc.workflow.phase_tools import make_planner_tools  # noqa: PLC0415
            plan_event = asyncio.Event()
            filtered   = list(filtered) + make_planner_tools(
                self._approval_svc, plan_event, plan_data,
            )

        # ── Mode override ─────────────────────────────────────────────────────
        # Some phases need a different active mode than the session mode.
        # e.g. the execute phase in code_plan switches to "Auto" so that
        # ToolCapabilityGate permits write/execute tools.
        _original_mode = self._app_state.active_mode()
        if spec.mode_override and self._mode_manager is not None:
            _override = self._mode_manager.set_by_name(spec.mode_override)
            if _override is None:
                log.warning(
                    "Phase %r: mode_override %r not found — using current mode",
                    spec.name, spec.mode_override,
                )

        try:
            await _run_agent_turn(
                phase_text,
                self._runner,
                self._processor,
                session_memory=self._shared_memory,   # shared across all phases
                max_agent_turns=spec.max_turns,
                conv_store=self._conv_store,
                app_state=self._app_state,
                exec_cfg=self._cfg.execution,
                skills=self._skills,
                mention_cache=self._mention_cache,
                project_plugin_tools=filtered,        # phase-filtered + approval tools
                mcp_registry=self._mcp_registry,
                active_agent=spec.agent_type,
                completed_turns=self._completed_turns,
                approval_svc=self._approval_svc,
                output_collector=output_buf,
                system_prompt_suffix=role_prompt,     # per-phase focus instructions
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            log.error("Phase %r agent error: %s", spec.name, exc)
            return PhaseOutput(
                phase_name=spec.name,
                role=spec.agent_type,
                full_text=f"[Phase error: {exc}]",
                approved=False,
                agent_id="error",
                duration_s=time.monotonic() - t0,
            )
        finally:
            # Always restore the session mode after the phase, even on error.
            if spec.mode_override and self._mode_manager is not None:
                self._app_state.active_mode.set(_original_mode)

        # ── Resolve output ────────────────────────────────────────────────────
        if plan_event is not None:
            if plan_event.is_set() and "plan" in plan_data:
                # finalize_plan() was called after approval — use the submitted plan.
                full_text = plan_data["plan"]
            else:
                # Approval tools were injected but finalize_plan() was never called.
                # The agent either ignored rejections or ran out of turns.
                # Return approved=False so on_reject fires (loops back to plan phase).
                return PhaseOutput(
                    phase_name=spec.name,
                    role=spec.agent_type,
                    full_text="".join(output_buf),
                    approved=False,
                    agent_id=uuid.uuid4().hex[:8],
                    duration_s=time.monotonic() - t0,
                )
        else:
            full_text = "".join(output_buf)

        structured = _parse_output_schema(full_text, spec.output_schema)

        approved: bool | None = None
        if structured and "approved" in structured:
            approved = bool(structured["approved"])

        return PhaseOutput(
            phase_name=spec.name,
            role=spec.agent_type,
            full_text=full_text,
            structured=structured,
            approved=approved,
            agent_id=uuid.uuid4().hex[:8],
            duration_s=time.monotonic() - t0,
        )

    async def _run_human_phase(self, spec: Any, context: Any) -> Any:
        from agenthicc.workflow.plugin import PhaseOutput  # noqa: PLC0415

        if self._approval_svc is None:
            log.warning("Human phase %r in headless mode — auto-approving", spec.name)
            return PhaseOutput(
                phase_name=spec.name, role="human",
                full_text="[auto-approved: no approval service]",
                approved=True, agent_id="headless",
            )

        prior_text = ""
        if context.phase_outputs:
            last_name   = list(context.phase_outputs)[-1]
            last_output = context.phase_outputs[last_name]
            prior_text  = last_output.full_text[:2000]

        import asyncio as _asyncio                             # noqa: PLC0415
        from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415

        req = ApprovalRequest(
            tool_name=f"Review: {spec.name}",
            tool_use_id=uuid.uuid4().hex,
            tool_input={"plan": prior_text} if prior_text else {},
            capabilities=frozenset(),
            event=_asyncio.Event(),
            kind="plan_review",
        )
        response = await self._approval_svc.request_approval(req)
        full_text = response.message if response.message else "[human review]"

        return PhaseOutput(
            phase_name=spec.name, role="human",
            full_text=full_text, approved=response.allowed, agent_id="human",
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _filter_tools(self, spec: Any) -> list:
        """Return tools whose capabilities fit within phase allowed + mode ceiling.

        Capabilities are read via get_tool_capabilities(), which reads from
        __lauren_ai_tool_metadata__[CAPABILITIES_KEY] — the dict written by
        set_metadata().  Unannotated tools pass through (open-by-default).
        """
        from agenthicc.tools.capabilities import get_tool_capabilities  # noqa: PLC0415

        mode_blocked  = self._app_state.active_mode().blocked_capabilities
        phase_allowed = spec.resolved_allowed_caps  # frozenset | None

        all_tools = list(self._plugin_tools)
        if self._mcp_registry is not None:
            try:
                all_tools += self._mcp_registry.all_tools()
            except Exception:  # noqa: BLE001
                pass

        result = []
        for tool in all_tools:
            caps = get_tool_capabilities(tool)
            if caps & mode_blocked:
                continue
            if phase_allowed is not None and not (caps <= phase_allowed):
                continue
            result.append(tool)
        return result

    def _determine_transition(self, spec: Any, output: Any) -> str | None:
        if output.metadata and "__next_phase__" in output.metadata:
            return output.metadata["__next_phase__"] or None
        if output.approved is False and spec.on_reject:
            return spec.on_reject
        return spec.next

    def _build_phase_prompt(self, spec: Any, intent: str, context: Any) -> str:
        ctx_block = context.as_system_block()
        return f"{ctx_block}\n\nTask: {intent}"


def build_workflow_runner(
    definition:      Any,
    *,
    conv_store:      Any,
    app_state:       Any,
    processor:       Any,
    agent_runner:    Any,
    session_mem:     Any,
    approval_svc:    Any | None = None,
    cfg:             Any,
    skills:          dict | None = None,
    plugin_tools:    list | None = None,
    mcp_registry:    Any | None = None,
    mention_cache:   Any | None = None,
    agents_registry: Any | None = None,
    mode_manager:    Any | None = None,
    completed_turns: int = 0,
) -> WorkflowRunner:
    return WorkflowRunner(
        definition=definition,
        conv_store=conv_store,
        app_state=app_state,
        processor=processor,
        agent_runner=agent_runner,
        session_mem=session_mem,
        approval_svc=approval_svc,
        cfg=cfg,
        skills=skills or {},
        plugin_tools=plugin_tools or [],
        mcp_registry=mcp_registry,
        mention_cache=mention_cache,
        agents_registry=agents_registry,
        mode_manager=mode_manager,
        completed_turns=completed_turns,
    )
