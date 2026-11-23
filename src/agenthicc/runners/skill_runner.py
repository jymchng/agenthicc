"""Apply skill restrictions between provider requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lauren_ai._agents._runner import AgentRunnerBase

from agenthicc.skills.tools import current_skill_use

if TYPE_CHECKING:
    from collections.abc import Callable

    from lauren_ai import AgentContext, AgentMeta, ToolCall, ToolResult


class SkillAwareRunner(AgentRunnerBase):
    """Keep provider schemas and dispatch under the same turn restrictions."""

    _skill_tool_schemas: list[dict[str, object]] | None = None
    _skill_agent_meta: AgentMeta | None = None
    refresh_tool_meta: Callable[[AgentMeta], bool] | None = None
    observe_tool_meta: Callable[[AgentMeta], None] | None = None

    @staticmethod
    def _narrow_skill_tools(meta: AgentMeta) -> bool:
        skill_use = current_skill_use()
        if skill_use is None:
            return False
        filtered = {name: item for name, item in meta.tools.items() if skill_use.permitted(name)}
        if len(filtered) == len(meta.tools):
            return False
        meta.tools = filtered
        return True

    def _get_tool_schemas(self, meta: AgentMeta) -> list[dict[str, object]]:
        refresh = self.refresh_tool_meta
        if refresh is not None:
            refresh(meta)
        self._narrow_skill_tools(meta)
        schemas: list[dict[str, object]] = super()._get_tool_schemas(meta)
        self._skill_tool_schemas = schemas
        self._skill_agent_meta = meta
        if self.observe_tool_meta is not None:
            self.observe_tool_meta(meta)
        return schemas

    async def _execute_tools(
        self,
        tool_calls: list[ToolCall],
        *,
        ctx: AgentContext,
        agent: object,
        model: str,
        run_sinks: tuple[object, ...] = (),
    ) -> list[ToolResult]:
        results = await super()._execute_tools(
            tool_calls, ctx=ctx, agent=agent, model=model, run_sinks=run_sinks
        )
        meta = self._skill_agent_meta
        schemas = self._skill_tool_schemas
        if meta is not None and schemas is not None:
            refresh = self.refresh_tool_meta
            changed = refresh(meta) if refresh is not None else False
            if self._narrow_skill_tools(meta) or changed:
                # Lauren holds this list for the remainder of its provider loop.
                schemas[:] = super()._get_tool_schemas(meta)
                if self.observe_tool_meta is not None:
                    self.observe_tool_meta(meta)
        return results
