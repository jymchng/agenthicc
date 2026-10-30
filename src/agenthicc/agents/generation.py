"""Provider-neutral generation of reviewed agent-definition drafts (AGX-09)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase

__all__ = ["generate_definition_draft"]

_SYSTEM = """You write concise Markdown agent definitions for agenthicc.
Return exactly one complete definition file with YAML frontmatter, no code fence.
Use only these frontmatter keys: name, description, mode, model, tools,
capabilities, max_steps, ceiling, color, hidden. The name must be supplied by
the user in the request. Use mode: all unless the requested role is clearly
only a primary agent or only a subagent. Do not invent tools, capabilities,
models, or policy grants. Leave tools and capabilities unspecified unless the
user requested restrictions. Keep the body focused on role, responsibilities,
and output expectations. The result is a draft for human review, not a file
write; never claim that it has been saved."""


async def generate_definition_draft(
    runner: AgentRunnerBase, *, model_id: str, name: str, description: str
) -> str:
    """Ask the session's configured model for a Markdown definition draft."""
    from lauren_ai._agents import agent as agent_decorator
    from lauren_ai._agents import use_tools

    draft_agent_class: type[object] = agent_decorator(model=model_id, system=_SYSTEM)(
        use_tools()(type("_DefinitionDraftAgent", (), {}))
    )

    prompt = (
        f"Create an agent definition named {name!r} from this description:\n\n"
        f"{description}\n\n"
        "Use a short, actionable description field and a useful, bounded system prompt."
    )
    stream = await runner.run_stream(
        draft_agent_class(),
        prompt,
        conversation_id=f"agent-definition-{uuid.uuid4().hex}",
    )
    pieces: list[str] = []
    stop_reason = ""
    async for chunk in stream:
        if chunk.delta:
            pieces.append(chunk.delta)
        if chunk.stop_reason is not None:
            stop_reason = str(chunk.stop_reason)
    result = "".join(pieces).strip()
    if stop_reason == "max_tokens":
        raise ValueError("the generated definition was truncated; please try a shorter request")
    if not result:
        raise ValueError("the model returned an empty agent definition")
    if result.startswith("```"):
        lines = result.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            result = "\n".join(lines[1:-1]).strip()
    return result
