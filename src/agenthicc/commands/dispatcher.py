"""CommandDispatcher — executes commands via the unified registry (PRD-44)."""
from __future__ import annotations

from .command import CommandContext
from .registry import UnifiedCommandRegistry

__all__ = ["CommandDispatcher"]


class CommandDispatcher:
    """Executes a command given its text and context."""

    def __init__(self, registry: UnifiedCommandRegistry) -> None:
        self._registry = registry

    def dispatch(self, text: str, ctx: CommandContext) -> bool:
        """Look up and execute the command for *text*.

        Returns True if the command was handled, False if unknown.
        """
        parts = text.strip().split(None, 1)
        name = parts[0] if parts else text.strip()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self._registry.get(name)
        if cmd is None:
            return False

        ctx_with_args = CommandContext(
            text=text,
            args=args,
            model=ctx.model,
            console=ctx.console,
            config=ctx.config,
            session_id=ctx.session_id,
            skills=ctx.skills,
            command_registry=ctx.command_registry,
            mode_manager=ctx.mode_manager,
            set_pending_skill=ctx.set_pending_skill,
            set_pending_menu=ctx.set_pending_menu,
            close_overlay=ctx.close_overlay,
        )

        # Menu factory takes precedence when no args are given.
        if cmd.menu_factory is not None and not args.strip():
            widget = cmd.menu_factory(ctx_with_args)
            if ctx.set_pending_menu is not None:
                ctx.set_pending_menu(widget)
            return True

        if cmd.handler is not None:
            return cmd.handler(ctx_with_args)

        return False
