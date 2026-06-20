"""WorkflowRunner — executes phase-based workflows (PRD-87, PRD-94, PRD-95)."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.plugin import (
        PhaseOutput,
        PhaseSpec,
        WorkflowContext,
        WorkflowPlugin,
        WorkflowRun,
    )
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from lauren_ai._memory import ShortTermMemory

log = logging.getLogger(__name__)


class WorkflowRunner:
    """Executes a phase-based workflow (PRD-116).

    Parameters
    ----------
    plugin_cls:
        The ``WorkflowPlugin`` subclass describing phases and transitions.
    config:
        A ``WorkflowConfig`` holding all session-scoped singletons.
    mode_manager:
        Optional ``ModeManager`` for per-phase mode overrides (PRD-91).
    """

    def __init__(
        self,
        plugin_cls:   type[WorkflowPlugin],
        config:       WorkflowConfig,
        mode_manager: ModeManager | None = None,
    ) -> None:
        self._plugin       = plugin_cls
        self._cfg          = config          # WorkflowConfig; AgenthiccConfig via self._cfg.cfg
        self._mode_manager = mode_manager

        # Derive the raw API model string from the transport config (primary)
        # falling back to cfg.execution.effective_model() (secondary).
        _transport_cfg   = getattr(
            getattr(config.agent_runner, "_transport", None), "_config", None
        )
        _transport_model = getattr(_transport_cfg, "model", None)
        self._model_id   = _transport_model or config.cfg.execution.effective_model()

        # Set during run() / resume() — not constructor state.
        self._run_id:        str                    = ""
        self._shared_memory: ShortTermMemory | None = None

    # ── public entry points ───────────────────────────────────────────────────

    async def run(self, intent: str) -> WorkflowContext:
        """Start a fresh workflow run for the given intent.

        Returns the populated ``WorkflowContext`` so that subclasses can call
        ``ctx = await super().run(intent)`` and continue with additional phases
        (PRD-114 composite workflow pattern).
        """
        from agenthicc.workflows.plugin import WorkflowContext, WorkflowRun  # noqa: PLC0415
        from lauren_ai._memory import ShortTermMemory                       # noqa: PLC0415
        from agenthicc.kernel import Event                                  # noqa: PLC0415

        self._shared_memory = ShortTermMemory(max_tokens=32_000)
        run_id       = uuid.uuid4().hex
        self._run_id = run_id

        context = WorkflowContext(intent=intent, run_id=run_id, workflow_name=self._plugin.name)
        wf_run  = WorkflowRun(
            run_id=run_id,
            workflow_name=self._plugin.name,
            intent=intent,
            current_phase=self._plugin.first_phase().name if self._plugin.first_phase() else None,
            total_phases=len(self._plugin.phases),
        )
        self._cfg.app_state.workflow_run.set(wf_run)

        await self._cfg.processor.emit(Event.create("WorkflowRunStarted", {
            "run_id":        run_id,
            "workflow_name": self._plugin.name,
            "intent":        intent,
            "phase_names":   self._plugin.phase_names(),
        }))

        start_phase = self._plugin.first_phase().name if self._plugin.first_phase() else None
        await self._run_phase_loop(intent, context, wf_run, run_id, start_phase)
        return context

    async def resume(self, context: WorkflowContext) -> None:
        """Resume a workflow from a pre-populated WorkflowContext.

        Phases already present in ``context.phase_outputs`` are skipped;
        execution continues from the first incomplete phase.
        """
        from agenthicc.workflows.plugin import WorkflowRun, PhaseRunRecord  # noqa: PLC0415
        from lauren_ai._memory import ShortTermMemory                      # noqa: PLC0415

        run_id       = context.run_id
        self._run_id = run_id
        self._shared_memory = ShortTermMemory(max_tokens=32_000)

        phase_history = [
            PhaseRunRecord(
                phase_name=name,
                role=output.role,
                approved=output.approved,
                output_summary=output.full_text[:200],
                iteration=1,
                duration_s=output.duration_s,
            )
            for name, output in context.phase_outputs.items()
        ]
        wf_run = WorkflowRun(
            run_id=run_id,
            workflow_name=self._plugin.name,
            intent=context.intent,
            current_phase=None,
            total_phases=len(self._plugin.phases),
            phase_history=phase_history,
        )
        self._cfg.app_state.workflow_run.set(wf_run)

        start_phase = self._find_resume_phase(context)
        if start_phase is None:
            wf_run = dataclasses.replace(wf_run, status="complete", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            return

        await self._run_phase_loop(context.intent, context, wf_run, run_id, start_phase)

    # ── phase loop ────────────────────────────────────────────────────────────

    async def _run_phase_loop(
        self,
        intent:      str,
        context:     WorkflowContext,
        wf_run:      WorkflowRun,
        run_id:      str,
        start_phase: str | None,
    ) -> None:
        from agenthicc.workflows.plugin import PhaseRunRecord  # noqa: PLC0415
        from agenthicc.kernel import Event                    # noqa: PLC0415

        iteration_counts: dict[str, int] = {}
        _processed_parallel: set[str]    = set()
        phase_name = start_phase

        try:
            while phase_name is not None:
                spec = self._plugin.get_phase(phase_name)
                if spec is None:
                    log.error("Workflow %s: unknown phase %r", self._plugin.name, phase_name)
                    wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                    self._cfg.app_state.workflow_run.set(wf_run)
                    return

                if phase_name in _processed_parallel:
                    phase_name = spec.next
                    continue

                count = iteration_counts.get(phase_name, 0)
                if spec.max_iterations != -1 and count >= spec.max_iterations:
                    log.warning(
                        "Workflow %s: phase %r hit max_iterations=%d",
                        self._plugin.name, phase_name, spec.max_iterations,
                    )
                    wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                    self._cfg.app_state.workflow_run.set(wf_run)
                    self._cfg.conv_store.append_event("error", {
                        "message": (
                            f"Workflow '{self._plugin.name}': phase '{phase_name}' exceeded "
                            f"max_iterations={spec.max_iterations}. Stopping."
                        )
                    })
                    return
                iteration_counts[phase_name] = count + 1

                # Opt-in global cap — only enforced when max_total_phase_runs > 0.
                # Default is 0 (unlimited) so the execute↔review loop can iterate freely.
                _max_total = self._plugin.max_total_phase_runs
                if _max_total > 0:
                    _total_runs = sum(iteration_counts.values())
                    if _total_runs >= _max_total:
                        log.warning(
                            "Workflow %s: global cap reached (%d/%d phase runs). Stopping.",
                            self._plugin.name, _total_runs, _max_total,
                        )
                        wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                        self._cfg.app_state.workflow_run.set(wf_run)
                        self._cfg.conv_store.append_event("error", {
                            "message": (
                                f"Workflow '{self._plugin.name}' stopped after {_total_runs} phase "
                                f"runs (limit: {_max_total}). Use /auto to continue manually."
                            )
                        })
                        return

                phase_idx = next(
                    (i for i, p in enumerate(self._plugin.phases) if p.name == phase_name), 0
                )
                wf_run = dataclasses.replace(
                    wf_run,
                    current_phase=phase_name,
                    current_phase_index=phase_idx,
                )
                self._cfg.app_state.workflow_run.set(wf_run)

                if spec.parallel_with:
                    peer_specs = [spec] + [
                        self._plugin.get_phase(n)
                        for n in spec.parallel_with
                        if self._plugin.get_phase(n) is not None
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

                await self._cfg.processor.emit(Event.create("WorkflowPhaseStarted", {
                    "run_id": run_id, "phase_name": phase_name,
                    "workflow_name": self._plugin.name,
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
                self._cfg.app_state.workflow_run.set(wf_run)

                await self._cfg.processor.emit(Event.create("WorkflowPhaseCompleted", {
                    "run_id":     run_id,
                    "phase_name": phase_name,
                    "role":       spec.agent_type,
                    "full_text":  output.full_text,
                    "approved":   output.approved,
                    "structured": output.structured or {},
                }))

                phase_name = self._determine_transition(spec, output)

            wf_run = dataclasses.replace(wf_run, status="complete", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            await self._cfg.processor.emit(Event.create("WorkflowRunCompleted", {
                "run_id":        run_id,
                "workflow_name": self._plugin.name,
                "phases_run":    len(wf_run.phase_history),
                "status":        "complete",
            }))

        except (asyncio.CancelledError, KeyboardInterrupt):
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            raise
        except Exception as exc:
            log.error("WorkflowRunner error: %s", exc, exc_info=True)
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            self._cfg.conv_store.append_event("error", {
                "message": f"Workflow '{self._plugin.name}' failed: {exc}"
            })

    # ── resume helpers ────────────────────────────────────────────────────────

    def _find_resume_phase(self, context: WorkflowContext) -> str | None:
        """Walk the phase-transition graph to find the first incomplete phase."""
        completed  = set(context.phase_outputs.keys())
        phase_name = self._plugin.first_phase().name if self._plugin.first_phase() else None
        seen: set[str] = set()

        while phase_name is not None:
            if phase_name in seen:
                break
            seen.add(phase_name)
            if phase_name not in completed:
                return phase_name
            spec = self._plugin.get_phase(phase_name)
            if spec is None:
                return None
            output = context.phase_outputs[phase_name]
            phase_name = self._determine_transition(spec, output)

        return None

    # ── phase execution ───────────────────────────────────────────────────────

    async def _run_phase(self, spec: PhaseSpec, intent: str, context: WorkflowContext) -> PhaseOutput:
        from agenthicc.workflows.plugin import PhaseOutput, _parse_output_schema  # noqa: PLC0415
        from agenthicc.runners.agent_turn import _run_agent_turn                 # noqa: PLC0415

        if spec.agent_type == "human":
            return await self._run_human_phase(spec, context)

        phase_text  = self._build_phase_prompt(spec, intent, context)
        filtered    = self._filter_tools(spec)
        role_prompt = (
            spec.system_prompt_override
            or self._cfg.agents_registry.get_role_system_prompt(spec.agent_type)
        )
        output_buf: list[str] = []
        t0 = time.monotonic()

        plan_event:    asyncio.Event | None = None
        plan_data:     dict                 = {}
        execute_event: asyncio.Event | None = None
        execute_data:  dict                 = {}
        review_event:  asyncio.Event | None = None
        review_data:   dict                 = {}
        if self._cfg.approval_svc is not None:
            from agenthicc.workflows.code_plan.phase_tools import (   # noqa: PLC0415
                make_planner_tools, make_executor_tools, make_reviewer_tools,
            )
            plan_event = asyncio.Event()
            filtered   = list(filtered) + make_planner_tools(
                self._cfg.approval_svc, plan_event, plan_data,
            )
            execute_event = asyncio.Event()
            filtered = filtered + make_executor_tools(execute_event, execute_data)
            if spec.require_explicit_review:
                review_event = asyncio.Event()
                filtered = filtered + make_reviewer_tools(review_event, review_data)

        _original_mode = self._cfg.app_state.active_mode()
        if spec.mode_override and self._mode_manager is not None:
            _override = self._mode_manager.set_by_name(spec.mode_override)
            if _override is None:
                log.warning(
                    "Phase %r: mode_override %r not found — using current mode",
                    spec.name, spec.mode_override,
                )

        # PRD-111: per-phase model override from WorkflowParams.
        # Empty model_for_phase() falls back to the global model_id.
        import dataclasses as _dataclasses  # noqa: PLC0415
        _phase_model = (
            self._cfg.params.model_for_phase(spec.name, self._model_id)
            if self._cfg.params is not None
            else self._model_id
        )
        _base_exec = self._cfg.cfg.execution
        _exec_cfg = (
            _dataclasses.replace(_base_exec, model=_phase_model)
            if _phase_model != getattr(_base_exec, "model", _phase_model)
            and _dataclasses.is_dataclass(_base_exec)
            else _base_exec
        )

        # Shared kwargs for all _run_agent_turn calls in this phase.
        _turn_kwargs = dict(
            runner=self._cfg.agent_runner,
            processor=self._cfg.processor,
            session_memory=self._shared_memory,
            max_agent_turns=spec.max_turns,
            conv_store=self._cfg.conv_store,
            app_state=self._cfg.app_state,
            exec_cfg=_exec_cfg,
            skills=self._cfg.skills,
            mention_cache=self._cfg.mention_cache,
            project_plugin_tools=filtered,
            mcp_registry=self._cfg.mcp_registry,
            active_agent=spec.agent_type,
            completed_turns=self._cfg.completed_turns,
            approval_svc=self._cfg.approval_svc,
            output_collector=output_buf,
            system_prompt_suffix=role_prompt,
        )

        try:
            if spec.require_explicit_completion and execute_event is not None:
                # Continuation loop — one agent, many turns, until the completion
                # tool fires.  The shared ShortTermMemory carries full context so
                # each continuation is a genuine resume, not a restart.
                max_cont = spec.max_iterations if spec.max_iterations > 0 else 10
                for attempt in range(1, max_cont + 1):
                    text = (
                        phase_text
                        if attempt == 1
                        else (
                            "Continue implementing — you have not yet called "
                            "mark_execute_complete(). Resume from where you left off "
                            "and complete all remaining tasks. Call "
                            "mark_execute_complete() once everything is done."
                        )
                    )
                    try:
                        await _run_agent_turn(text, **_turn_kwargs)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except Exception as exc:
                        log.error(
                            "Phase %r continuation %d error: %s",
                            spec.name, attempt, exc,
                        )
                        break
                    if execute_event.is_set():
                        break

            elif spec.require_explicit_review and review_event is not None:
                # Continuation loop — loops until approve_review() or reject_review()
                # is called.  If the agent ends its turn without calling either tool,
                # a reminder prompt is sent so it gets another attempt.
                max_cont = spec.max_iterations if spec.max_iterations > 0 else 10
                for attempt in range(1, max_cont + 1):
                    text = (
                        phase_text
                        if attempt == 1
                        else (
                            "You have not yet called approve_review() or reject_review(). "
                            "Review the implementation now and call one of these tools: "
                            "approve_review(summary) if all tests pass and the code is correct, "
                            "or reject_review(reason) if there are issues that need fixing."
                        )
                    )
                    try:
                        await _run_agent_turn(text, **_turn_kwargs)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except Exception as exc:
                        log.error(
                            "Phase %r continuation %d error: %s",
                            spec.name, attempt, exc,
                        )
                        break
                    if review_event.is_set():
                        break

            elif spec.require_plan_finalization and plan_event is not None:
                # Continuation loop — loops until finalize_plan() is called.
                # If the agent ends its turn without calling finalize_plan(),
                # a reminder prompt is sent that re-states the user's task so
                # the agent stays focused on producing and approving the plan.
                max_cont = spec.max_iterations if spec.max_iterations > 0 else 10
                for attempt in range(1, max_cont + 1):
                    text = (
                        phase_text
                        if attempt == 1
                        else (
                            "You have not yet finalized the plan. "
                            "Return to the task described above, develop or refine your "
                            "implementation plan, present it with request_plan_approval(plan), "
                            "and once it is approved call finalize_plan(plan)."
                        )
                    )
                    try:
                        await _run_agent_turn(text, **_turn_kwargs)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        raise
                    except Exception as exc:
                        log.error(
                            "Phase %r continuation %d error: %s",
                            spec.name, attempt, exc,
                        )
                        break
                    if plan_event.is_set():
                        break

            else:
                await _run_agent_turn(phase_text, **_turn_kwargs)
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
            if spec.mode_override and self._mode_manager is not None:
                self._cfg.app_state.active_mode.set(_original_mode)

        # Phase-specific flags are checked first — they must take priority over
        # the generic event-presence checks below, because plan_event / execute_event
        # are injected into EVERY phase and would otherwise short-circuit all others.
        if spec.require_explicit_review and review_event is not None:
            if review_event.is_set():
                action = review_data.get("action", "reject")
                if action == "approve":
                    return PhaseOutput(
                        phase_name=spec.name,
                        role=spec.agent_type,
                        full_text=review_data.get("summary", "".join(output_buf)),
                        approved=True,
                        agent_id=uuid.uuid4().hex[:8],
                        duration_s=time.monotonic() - t0,
                    )
                else:
                    return PhaseOutput(
                        phase_name=spec.name,
                        role=spec.agent_type,
                        full_text=review_data.get("reason", "".join(output_buf)),
                        approved=False,
                        agent_id=uuid.uuid4().hex[:8],
                        duration_s=time.monotonic() - t0,
                    )
            else:
                # Agent didn't call either review tool — retry the review phase.
                return PhaseOutput(
                    phase_name=spec.name,
                    role=spec.agent_type,
                    full_text="".join(output_buf),
                    approved=False,
                    metadata={"__next_phase__": spec.name},
                    agent_id=uuid.uuid4().hex[:8],
                    duration_s=time.monotonic() - t0,
                )

        elif spec.require_plan_finalization and plan_event is not None:
            if plan_event.is_set() and "plan" in plan_data:
                full_text = plan_data["plan"]
            else:
                return PhaseOutput(
                    phase_name=spec.name,
                    role=spec.agent_type,
                    full_text="".join(output_buf),
                    approved=False,
                    agent_id=uuid.uuid4().hex[:8],
                    duration_s=time.monotonic() - t0,
                )

        elif spec.require_explicit_completion and execute_event is not None:
            if execute_event.is_set():
                full_text = execute_data.get("summary", "".join(output_buf))
            else:
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

        # Review phase: if <review> tag missing the turn ended without a decision —
        # retry the review phase itself rather than routing back through execute.
        if structured and structured.get("incomplete"):
            return PhaseOutput(
                phase_name=spec.name,
                role=spec.agent_type,
                full_text=full_text,
                approved=False,
                metadata={"__next_phase__": spec.name},  # retry same phase
                agent_id=uuid.uuid4().hex[:8],
                duration_s=time.monotonic() - t0,
            )

        approved: bool | None = None
        if structured and "approved" in structured:
            approved = bool(structured["approved"]) if structured["approved"] is not None else None

        return PhaseOutput(
            phase_name=spec.name,
            role=spec.agent_type,
            full_text=full_text,
            structured=structured,
            approved=approved,
            agent_id=uuid.uuid4().hex[:8],
            duration_s=time.monotonic() - t0,
        )

    async def _run_human_phase(self, spec: PhaseSpec, context: WorkflowContext) -> PhaseOutput:
        from agenthicc.workflows.plugin import PhaseOutput  # noqa: PLC0415

        if self._cfg.approval_svc is None:
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
        response = await self._cfg.approval_svc.request_approval(req)
        full_text = response.message if response.message else "[human review]"

        return PhaseOutput(
            phase_name=spec.name, role="human",
            full_text=full_text, approved=response.allowed, agent_id="human",
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _filter_tools(self, spec: PhaseSpec) -> list[object]:
        from agenthicc.tools.capabilities import get_tool_capabilities  # noqa: PLC0415

        mode_blocked  = self._cfg.app_state.active_mode().blocked_capabilities
        phase_allowed = spec.resolved_allowed_caps

        all_tools = list(self._cfg.plugin_tools)
        if self._cfg.mcp_registry is not None:
            try:
                all_tools += self._cfg.mcp_registry.all_tools()
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

    def _determine_transition(self, spec: PhaseSpec, output: PhaseOutput) -> str | None:
        if output.metadata and "__next_phase__" in output.metadata:
            return output.metadata["__next_phase__"] or None
        if output.approved is False and spec.on_reject:
            return spec.on_reject
        return spec.next

    def _build_phase_prompt(self, spec: PhaseSpec, intent: str, context: WorkflowContext) -> str:
        ctx_block = context.as_system_block()
        return f"{ctx_block}\n\nTask: {intent}"


# build_workflow_runner() removed in PRD-116 — use plugin_cls.build_runner() instead.
