"""WorkflowRunner — executes phase-based workflows (PRD-87, PRD-94, PRD-95, PRD-101).

PRD-101 adds graph-based execution via WorkflowGraph + PhaseNode.  The runner
dispatches on definition type at construction time (isinstance check):
  WorkflowGraph      → _run_graph / _run_node / _follow_edge
  WorkflowDefinition → _run_phase_loop / _run_phase  (legacy)
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._config import AgentConfig
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.workflow.config import WorkflowConfig
    from agenthicc.workflow.plugin import (
        DataBus,
        NodeResult,
        PhaseNode,
        PhaseOutput,
        PhaseRunRecord,
        PhaseSpec,
        WorkflowContext,
        WorkflowDefinition,
        WorkflowGraph,
        WorkflowRun,
    )
    from agenthicc.tui.runtime.mode_manager import ModeManager

log = logging.getLogger(__name__)


class WorkflowRunner:
    """Executes a phase-based workflow.

    Accepts both ``WorkflowGraph`` (PRD-101 graph model) and the legacy
    ``WorkflowDefinition`` (PRD-87 phase list).  The definition type is
    detected once at construction via isinstance; all graph-specific methods
    are regular methods of this class.

    Parameters
    ----------
    definition:
        ``WorkflowGraph`` or ``WorkflowDefinition``.
    config:
        ``WorkflowConfig`` holding all session-scoped singletons.
    mode_manager:
        Optional ``ModeManager`` for per-node/per-phase mode overrides.
    """

    def __init__(
        self,
        definition:   WorkflowGraph | WorkflowDefinition,
        config:       WorkflowConfig,
        mode_manager: ModeManager | None = None,
    ) -> None:
        from agenthicc.workflow.plugin import WorkflowGraph as _WG  # noqa: PLC0415
        self._def          = definition
        self._cfg          = config
        self._mode_manager = mode_manager
        self._is_graph     = isinstance(definition, _WG)

        # Gap 1 (lauren-ai): no public model_id accessor on AgentRunnerBase.
        # Until AgentRunnerBase exposes a public property we reach into the
        # transport's private _config.model as the least-bad option.
        _transport_cfg   = getattr(
            getattr(config.agent_runner, "_transport", None), "_config", None
        )
        _transport_model = getattr(_transport_cfg, "model", None)
        self._model_id   = _transport_model or config.cfg.execution.effective_model()

        self._run_id:        str                  = ""
        self._shared_memory: ShortTermMemory | None = None

    # ── public entry points ───────────────────────────────────────────────────

    async def run(self, intent: str) -> None:
        """Start a fresh workflow run for the given intent."""
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
        from agenthicc.kernel import Event             # noqa: PLC0415

        self._shared_memory = ShortTermMemory(max_tokens=32_000)
        run_id       = uuid.uuid4().hex
        self._run_id = run_id

        if self._is_graph:
            from agenthicc.workflow.plugin import DataBus, WorkflowRun  # noqa: PLC0415
            data_bus = DataBus(intent=intent, run_id=run_id)
            wf_run   = WorkflowRun(
                run_id=run_id,
                workflow_name=self._def.name,
                intent=intent,
                current_phase=self._def.entry,          # type: ignore[union-attr]
                total_phases=len(self._def.nodes),      # type: ignore[union-attr]
            )
            self._cfg.app_state.workflow_run.set(wf_run)
            await self._cfg.processor.emit(Event.create("WorkflowRunStarted", {
                "run_id":        run_id,
                "workflow_name": self._def.name,
                "intent":        intent,
                "phase_names":   self._def.node_names(),  # type: ignore[union-attr]
            }))
            await self._run_graph(intent, data_bus, wf_run, run_id, self._def.entry)  # type: ignore[union-attr]
        else:
            from agenthicc.workflow.plugin import WorkflowContext, WorkflowRun  # noqa: PLC0415
            context = WorkflowContext(intent=intent, run_id=run_id, workflow_name=self._def.name)
            first   = self._def.first_phase()           # type: ignore[union-attr]
            wf_run  = WorkflowRun(
                run_id=run_id,
                workflow_name=self._def.name,
                intent=intent,
                current_phase=first.name if first else None,
                total_phases=len(self._def.phases),     # type: ignore[union-attr]
            )
            self._cfg.app_state.workflow_run.set(wf_run)
            await self._cfg.processor.emit(Event.create("WorkflowRunStarted", {
                "run_id":        run_id,
                "workflow_name": self._def.name,
                "intent":        intent,
                "phase_names":   self._def.phase_names(),  # type: ignore[union-attr]
            }))
            await self._run_phase_loop(intent, context, wf_run, run_id, first.name if first else None)

    async def resume(self, context: DataBus | WorkflowContext) -> None:
        """Resume a workflow from a pre-populated context.

        For WorkflowGraph: *context* is a ``DataBus``.
        For WorkflowDefinition: *context* is a ``WorkflowContext`` (legacy).
        """
        from agenthicc.workflow.plugin import WorkflowRun, PhaseRunRecord  # noqa: PLC0415
        from lauren_ai._memory import ShortTermMemory                      # noqa: PLC0415

        run_id              = context.run_id
        self._run_id        = run_id
        self._shared_memory = ShortTermMemory(max_tokens=32_000)

        if self._is_graph:
            wf_run = WorkflowRun(
                run_id=run_id,
                workflow_name=self._def.name,
                intent=context.intent,
                current_phase=None,
                total_phases=len(self._def.nodes),  # type: ignore[union-attr]
            )
            self._cfg.app_state.workflow_run.set(wf_run)
            start_node = self._find_resume_node(context)  # type: ignore[arg-type]
            if start_node is None:
                wf_run = dataclasses.replace(wf_run, status="complete")
                self._cfg.app_state.workflow_run.set(wf_run)
                return
            await self._run_graph(context.intent, context, wf_run, run_id, start_node)  # type: ignore[arg-type]
        else:
            phase_history = [
                PhaseRunRecord(
                    phase_name=name,
                    role=output.role,
                    approved=output.approved,
                    output_summary=output.full_text[:200],
                    iteration=1,
                    duration_s=output.duration_s,
                )
                for name, output in context.phase_outputs.items()  # type: ignore[union-attr]
            ]
            wf_run = WorkflowRun(
                run_id=run_id,
                workflow_name=self._def.name,
                intent=context.intent,
                current_phase=None,
                total_phases=len(self._def.phases),  # type: ignore[union-attr]
                phase_history=phase_history,
            )
            self._cfg.app_state.workflow_run.set(wf_run)
            start_phase = self._find_resume_phase(context)  # type: ignore[arg-type]
            if start_phase is None:
                wf_run = dataclasses.replace(wf_run, status="complete", current_phase=None)
                self._cfg.app_state.workflow_run.set(wf_run)
                return
            await self._run_phase_loop(context.intent, context, wf_run, run_id, start_phase)  # type: ignore[arg-type]

    # ── graph execution (PRD-101) ─────────────────────────────────────────────

    async def _run_graph(
        self,
        intent:     str,
        data_bus:   DataBus,
        wf_run:     WorkflowRun,
        run_id:     str,
        start_node: str | None,
    ) -> None:
        """Walk the WorkflowGraph following edges until a terminal node or error."""
        from agenthicc.workflow.plugin import PhaseRunRecord  # noqa: PLC0415
        from agenthicc.kernel import Event                    # noqa: PLC0415

        node_name  = start_node
        total_runs = 0

        try:
            while node_name is not None:
                node = self._def.get_node(node_name)  # type: ignore[union-attr]
                if node is None:
                    log.error("Workflow %s: unknown node %r", self._def.name, node_name)
                    wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                    self._cfg.app_state.workflow_run.set(wf_run)
                    return

                total_runs += 1
                _max_total  = self._def.max_total_phase_runs  # type: ignore[union-attr]
                if _max_total > 0 and total_runs > _max_total:
                    log.warning(
                        "Workflow %s: global cap reached (%d/%d runs).",
                        self._def.name, total_runs, _max_total,
                    )
                    wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                    self._cfg.app_state.workflow_run.set(wf_run)
                    self._cfg.conv_store.append_event("error", {
                        "message": (
                            f"Workflow '{self._def.name}' stopped after {total_runs} node "
                            f"runs (limit: {_max_total}). Use /auto to continue manually."
                        )
                    })
                    return

                if node.parallel_with:
                    sibling_names = [node_name] + list(node.parallel_with)
                    siblings      = [
                        self._def.get_node(n)  # type: ignore[union-attr]
                        for n in sibling_names
                        if self._def.get_node(n) is not None  # type: ignore[union-attr]
                    ]
                    outputs = await asyncio.gather(
                        *[self._run_node(s, intent, data_bus) for s in siblings],
                        return_exceptions=True,
                    )
                    for sib, result in zip(siblings, outputs):
                        if isinstance(result, Exception):
                            log.error("Parallel node %r failed: %s", sib.name, result)
                            continue
                        data_bus.set(sib.name, result.output)
                        if result.edge_label:
                            data_bus.record_edge(sib.name, result.edge_label)
                        record = PhaseRunRecord(
                            phase_name=sib.name, role=sib.agent_type, approved=None,
                            output_summary=str(result.output)[:200],
                            iteration=1, duration_s=result.duration_s,
                        )
                        wf_run = dataclasses.replace(
                            wf_run, phase_history=wf_run.phase_history + [record],
                        )
                    self._cfg.app_state.workflow_run.set(wf_run)
                    lead = next((r for r in outputs if not isinstance(r, Exception)), None)
                    node_name = self._follow_edge(node, lead.edge_label if lead else None)
                    continue

                node_idx = self._def.node_index(node_name)  # type: ignore[union-attr]
                wf_run = dataclasses.replace(
                    wf_run,
                    current_phase=node_name,
                    current_phase_index=node_idx,
                )
                self._cfg.app_state.workflow_run.set(wf_run)

                await self._cfg.processor.emit(Event.create("WorkflowPhaseStarted", {
                    "run_id": run_id, "phase_name": node_name,
                    "workflow_name": self._def.name,
                }))

                result = await self._run_node(node, intent, data_bus)
                data_bus.set(node_name, result.output)
                if result.edge_label is not None:
                    data_bus.record_edge(node_name, result.edge_label)

                record = PhaseRunRecord(
                    phase_name=node_name, role=node.agent_type, approved=None,
                    output_summary=str(result.output)[:200],
                    iteration=1, duration_s=result.duration_s,
                )
                wf_run = dataclasses.replace(wf_run, phase_history=wf_run.phase_history + [record])
                self._cfg.app_state.workflow_run.set(wf_run)

                await self._cfg.processor.emit(Event.create("WorkflowPhaseCompleted", {
                    "run_id":      run_id,
                    "phase_name":  node_name,
                    "role":        node.agent_type,
                    "full_text":   str(result.output.get("summary", "")),
                    "approved":    None,
                    "structured":  result.output,
                    "edge_label":  result.edge_label,
                }))

                node_name = self._follow_edge(node, result.edge_label)

            wf_run = dataclasses.replace(wf_run, status="complete", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            await self._cfg.processor.emit(Event.create("WorkflowRunCompleted", {
                "run_id":        run_id,
                "workflow_name": self._def.name,
                "phases_run":    len(wf_run.phase_history),
                "status":        "complete",
            }))

        except (asyncio.CancelledError, KeyboardInterrupt):
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            raise
        except Exception as exc:
            log.error("WorkflowRunner (graph) error: %s", exc, exc_info=True)
            wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
            self._cfg.app_state.workflow_run.set(wf_run)
            self._cfg.conv_store.append_event("error", {
                "message": f"Workflow '{self._def.name}' failed: {exc}"
            })

    def _follow_edge(self, node: PhaseNode, edge_label: str | None) -> str | None:
        """Return the target node name for *edge_label*, or None for terminal."""
        for edge in node.edges:
            if edge.label == edge_label:
                return edge.target
        return None

    async def _run_node(self, node: PhaseNode, intent: str, data_bus: DataBus) -> NodeResult:
        """Run one PhaseNode in a continuation loop until complete_phase fires."""
        from agenthicc.workflow.plugin import NodeResult                # noqa: PLC0415
        from agenthicc.runners.agent_turn import _run_agent_turn        # noqa: PLC0415
        from agenthicc.workflow.phase_tools import make_completion_tool  # noqa: PLC0415
        from lauren_ai._config import AgentConfig                        # noqa: PLC0415

        if node.agent_type == "human":
            return await self._run_human_node(node, intent, data_bus)

        filtered         = self._filter_node_tools(node)
        transition_event = asyncio.Event()
        transition_data: dict = {}
        complete_tool    = make_completion_tool(
            node, data_bus, transition_event, transition_data, self._cfg.approval_svc,
        )
        filtered = filtered + [complete_tool]

        _original_mode = self._cfg.app_state.active_mode()
        if node.mode_override and self._mode_manager is not None:
            if self._mode_manager.set_by_name(node.mode_override) is None:
                log.warning(
                    "Node %r: mode_override %r not found — using current mode",
                    node.name, node.mode_override,
                )

        agent_cfg   = node.agent_config or AgentConfig()
        role_prompt = (
            agent_cfg.system_prompt
            or self._cfg.agents_registry.get_role_system_prompt(node.agent_type)
        )
        output_buf: list[str] = []
        t0 = time.monotonic()

        _turn_kwargs = dict(
            runner=self._cfg.agent_runner,
            processor=self._cfg.processor,
            session_memory=self._shared_memory,
            max_agent_turns=agent_cfg.max_turns,
            conv_store=self._cfg.conv_store,
            app_state=self._cfg.app_state,
            exec_cfg=self._cfg.cfg.execution,
            skills=self._cfg.skills,
            mention_cache=self._cfg.mention_cache,
            project_plugin_tools=filtered,
            mcp_registry=self._cfg.mcp_registry,
            active_agent=node.agent_type,
            completed_turns=self._cfg.completed_turns,
            approval_svc=self._cfg.approval_svc,
            output_collector=output_buf,
            system_prompt_suffix=role_prompt,
        )

        try:
            max_cont   = max(node.max_continuations, 1)
            phase_text = self._build_node_prompt(node, intent, data_bus)

            for attempt in range(1, max_cont + 1):
                text = (
                    phase_text if attempt == 1
                    else (
                        "Continue — you have not yet called complete_phase(). "
                        "Resume from where you left off. "
                        "Call complete_phase() once all tasks are done."
                    )
                )
                try:
                    await _run_agent_turn(text, **_turn_kwargs)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    log.error("Node %r attempt %d error: %s", node.name, attempt, exc)
                    break
                if transition_event.is_set():
                    break
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            log.error("Node %r fatal error: %s", node.name, exc, exc_info=True)
            return NodeResult(node_name=node.name, edge_label=None, output={},
                              duration_s=time.monotonic() - t0)
        finally:
            if node.mode_override and self._mode_manager is not None:
                self._cfg.app_state.active_mode.set(_original_mode)

        return NodeResult(
            node_name=node.name,
            edge_label=transition_data.get("edge_label"),
            output=transition_data.get("output", {}),
            duration_s=time.monotonic() - t0,
        )

    async def _run_human_node(
        self, node: PhaseNode, intent: str, data_bus: DataBus
    ) -> NodeResult:
        """Run a human-review node: call ApprovalService directly, no LLM."""
        from agenthicc.workflow.plugin import NodeResult       # noqa: PLC0415
        from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415

        t0 = time.monotonic()

        if self._cfg.approval_svc is None:
            log.warning("Human node %r: headless mode — auto-approving", node.name)
            edge_label = next(
                (e.label for e in node.edges if e.label in ("approve", "approved", "next")),
                None,
            )
            return NodeResult(node_name=node.name, edge_label=edge_label,
                              output={"auto_approved": True}, duration_s=time.monotonic() - t0)

        prior = list(data_bus.outputs.values())[-1] if data_bus.outputs else {}

        req = ApprovalRequest(
            tool_name=f"Review: {node.name}",
            tool_use_id=uuid.uuid4().hex,
            tool_input=prior,
            capabilities=frozenset(),
            event=asyncio.Event(),
            kind="plan_review",
        )
        response  = await self._cfg.approval_svc.request_approval(req)
        edge_label = next(
            (e.label for e in node.edges
             if e.label in (("approve", "approved", "next") if response.allowed
                            else ("reject", "rejected"))),
            None,
        )
        output = {"approved": response.allowed}
        if response.message:
            output["feedback"] = response.message

        return NodeResult(node_name=node.name, edge_label=edge_label,
                          output=output, duration_s=time.monotonic() - t0)

    def _filter_node_tools(self, node: PhaseNode) -> list:
        """Return capability-filtered tools for this node."""
        from agenthicc.tools.capabilities import get_tool_capabilities  # noqa: PLC0415
        from agenthicc.agents.plugin import ROLE_DEFAULT_ALLOWED          # noqa: PLC0415

        mode_blocked = self._cfg.app_state.active_mode().blocked_capabilities
        node_allowed = (
            node.allowed_capabilities
            if node.allowed_capabilities is not None
            else ROLE_DEFAULT_ALLOWED.get(node.agent_type)
        )

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
            if node_allowed is not None and not (caps <= node_allowed):
                continue
            result.append(tool)
        return result

    def _build_node_prompt(self, node: PhaseNode, intent: str, data_bus: DataBus) -> str:
        """Build the opening user message for a node's first continuation attempt."""
        ctx_block = data_bus.as_context_block()
        edge_info = ""
        if node.edges:
            labels    = ", ".join(f'"{e.label}"' for e in node.edges)
            edge_info = (
                f"\n\nWhen your task is complete, call complete_phase(next=...) "
                f"with one of these edge labels: {labels}"
            )
        return f"{ctx_block}\n\nTask: {intent}{edge_info}"

    def _find_resume_node(self, data_bus: DataBus) -> str | None:
        """Walk the edge history to find the first incomplete node."""
        completed  = set(data_bus.outputs.keys())
        node_name  = self._def.entry  # type: ignore[union-attr]
        seen: set[str] = set()

        while node_name is not None:
            if node_name in seen:
                # Cycle reached via a rejection edge — this node needs re-execution.
                return node_name
            seen.add(node_name)
            if node_name not in completed:
                return node_name
            node = self._def.get_node(node_name)  # type: ignore[union-attr]
            if node is None:
                return None
            edge_label = data_bus.edge_history.get(node_name)
            node_name  = self._follow_edge(node, edge_label)

        return None

    # ── legacy phase loop (PRD-87) ────────────────────────────────────────────

    async def _run_phase_loop(
        self,
        intent:      str,
        context:     WorkflowContext,
        wf_run:      WorkflowRun,
        run_id:      str,
        start_phase: str | None,
    ) -> None:
        from agenthicc.workflow.plugin import PhaseRunRecord  # noqa: PLC0415
        from agenthicc.kernel import Event                    # noqa: PLC0415

        iteration_counts: dict[str, int] = {}
        _processed_parallel: set[str]    = set()
        phase_name = start_phase

        try:
            while phase_name is not None:
                spec = self._def.get_phase(phase_name)  # type: ignore[union-attr]
                if spec is None:
                    log.error("Workflow %s: unknown phase %r", self._def.name, phase_name)
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
                        self._def.name, phase_name, spec.max_iterations,
                    )
                    wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                    self._cfg.app_state.workflow_run.set(wf_run)
                    self._cfg.conv_store.append_event("error", {
                        "message": (
                            f"Workflow '{self._def.name}': phase '{phase_name}' exceeded "
                            f"max_iterations={spec.max_iterations}. Stopping."
                        )
                    })
                    return
                iteration_counts[phase_name] = count + 1

                _max_total = self._def.max_total_phase_runs  # type: ignore[union-attr]
                if _max_total > 0:
                    _total_runs = sum(iteration_counts.values())
                    if _total_runs >= _max_total:
                        log.warning(
                            "Workflow %s: global cap reached (%d/%d phase runs).",
                            self._def.name, _total_runs, _max_total,
                        )
                        wf_run = dataclasses.replace(wf_run, status="failed", current_phase=None)
                        self._cfg.app_state.workflow_run.set(wf_run)
                        self._cfg.conv_store.append_event("error", {
                            "message": (
                                f"Workflow '{self._def.name}' stopped after {_total_runs} phase "
                                f"runs (limit: {_max_total}). Use /auto to continue manually."
                            )
                        })
                        return

                phase_idx = next(
                    (i for i, p in enumerate(self._def.phases)  # type: ignore[union-attr]
                     if p.name == phase_name), 0,
                )
                wf_run = dataclasses.replace(
                    wf_run,
                    current_phase=phase_name,
                    current_phase_index=phase_idx,
                )
                self._cfg.app_state.workflow_run.set(wf_run)

                if spec.parallel_with:
                    peer_specs = [spec] + [
                        self._def.get_phase(n)  # type: ignore[union-attr]
                        for n in spec.parallel_with
                        if self._def.get_phase(n) is not None  # type: ignore[union-attr]
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
                "workflow_name": self._def.name,
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
                "message": f"Workflow '{self._def.name}' failed: {exc}"
            })

    def _find_resume_phase(self, context: WorkflowContext) -> str | None:
        completed  = set(context.phase_outputs.keys())
        phase_name = self._def.first_phase().name if self._def.first_phase() else None  # type: ignore[union-attr]
        seen: set[str] = set()

        while phase_name is not None:
            if phase_name in seen:
                break
            seen.add(phase_name)
            if phase_name not in completed:
                return phase_name
            spec = self._def.get_phase(phase_name)  # type: ignore[union-attr]
            if spec is None:
                return None
            output    = context.phase_outputs[phase_name]
            phase_name = self._determine_transition(spec, output)

        return None

    async def _run_phase(
        self, spec: PhaseSpec, intent: str, context: WorkflowContext
    ) -> PhaseOutput:
        from agenthicc.workflow.plugin import PhaseOutput, _parse_output_schema  # noqa: PLC0415
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
        if self._cfg.approval_svc is not None:
            from agenthicc.workflow.phase_tools import (   # noqa: PLC0415
                make_planner_tools, make_executor_tools,
            )
            plan_event = asyncio.Event()
            filtered   = list(filtered) + make_planner_tools(
                self._cfg.approval_svc, plan_event, plan_data,
            )
            execute_event = asyncio.Event()
            filtered      = filtered + make_executor_tools(execute_event, execute_data)

        _original_mode = self._cfg.app_state.active_mode()
        if spec.mode_override and self._mode_manager is not None:
            if self._mode_manager.set_by_name(spec.mode_override) is None:
                log.warning(
                    "Phase %r: mode_override %r not found — using current mode",
                    spec.name, spec.mode_override,
                )

        _turn_kwargs = dict(
            runner=self._cfg.agent_runner,
            processor=self._cfg.processor,
            session_memory=self._shared_memory,
            max_agent_turns=spec.max_turns,
            conv_store=self._cfg.conv_store,
            app_state=self._cfg.app_state,
            exec_cfg=self._cfg.cfg.execution,
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
                max_cont = spec.max_iterations if spec.max_iterations > 0 else 10
                for attempt in range(1, max_cont + 1):
                    text = (
                        phase_text if attempt == 1
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
                        log.error("Phase %r continuation %d error: %s", spec.name, attempt, exc)
                        break
                    if execute_event.is_set():
                        break
            else:
                await _run_agent_turn(phase_text, **_turn_kwargs)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            log.error("Phase %r agent error: %s", spec.name, exc)
            return PhaseOutput(
                phase_name=spec.name, role=spec.agent_type,
                full_text=f"[Phase error: {exc}]",
                approved=False, agent_id="error",
                duration_s=time.monotonic() - t0,
            )
        finally:
            if spec.mode_override and self._mode_manager is not None:
                self._cfg.app_state.active_mode.set(_original_mode)

        if spec.require_plan_finalization and plan_event is not None:
            if plan_event.is_set() and "plan" in plan_data:
                full_text = plan_data["plan"]
            else:
                return PhaseOutput(
                    phase_name=spec.name, role=spec.agent_type,
                    full_text="".join(output_buf),
                    approved=False, agent_id=uuid.uuid4().hex[:8],
                    duration_s=time.monotonic() - t0,
                )
        elif spec.require_explicit_completion and execute_event is not None:
            if execute_event.is_set():
                full_text = execute_data.get("summary", "".join(output_buf))
            else:
                return PhaseOutput(
                    phase_name=spec.name, role=spec.agent_type,
                    full_text="".join(output_buf),
                    approved=False, agent_id=uuid.uuid4().hex[:8],
                    duration_s=time.monotonic() - t0,
                )
        else:
            full_text = "".join(output_buf)

        structured = _parse_output_schema(full_text, spec.output_schema)

        if structured and structured.get("incomplete"):
            return PhaseOutput(
                phase_name=spec.name, role=spec.agent_type,
                full_text=full_text, approved=False,
                metadata={"__next_phase__": spec.name},
                agent_id=uuid.uuid4().hex[:8],
                duration_s=time.monotonic() - t0,
            )

        approved: bool | None = None
        if structured and "approved" in structured:
            approved = bool(structured["approved"]) if structured["approved"] is not None else None

        return PhaseOutput(
            phase_name=spec.name, role=spec.agent_type,
            full_text=full_text, structured=structured,
            approved=approved, agent_id=uuid.uuid4().hex[:8],
            duration_s=time.monotonic() - t0,
        )

    async def _run_human_phase(
        self, spec: PhaseSpec, context: WorkflowContext
    ) -> PhaseOutput:
        from agenthicc.workflow.plugin import PhaseOutput  # noqa: PLC0415

        if self._cfg.approval_svc is None:
            log.warning("Human phase %r in headless mode — auto-approving", spec.name)
            return PhaseOutput(
                phase_name=spec.name, role="human",
                full_text="[auto-approved: no approval service]",
                approved=True, agent_id="headless",
            )

        prior_text = ""
        if context.phase_outputs:
            last_name  = list(context.phase_outputs)[-1]
            prior_text = context.phase_outputs[last_name].full_text[:2000]

        from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415
        req = ApprovalRequest(
            tool_name=f"Review: {spec.name}",
            tool_use_id=uuid.uuid4().hex,
            tool_input={"plan": prior_text} if prior_text else {},
            capabilities=frozenset(),
            event=asyncio.Event(),
            kind="plan_review",
        )
        response  = await self._cfg.approval_svc.request_approval(req)
        full_text = response.message if response.message else "[human review]"

        return PhaseOutput(
            phase_name=spec.name, role="human",
            full_text=full_text, approved=response.allowed, agent_id="human",
        )

    def _filter_tools(self, spec: PhaseSpec) -> list:
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
        return f"{context.as_system_block()}\n\nTask: {intent}"


def build_workflow_runner(
    definition:   WorkflowGraph | WorkflowDefinition,
    *,
    config:       WorkflowConfig,
    mode_manager: ModeManager | None = None,
) -> WorkflowRunner:
    return WorkflowRunner(definition, config, mode_manager)
