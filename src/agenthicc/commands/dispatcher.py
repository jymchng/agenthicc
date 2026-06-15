"""CommandDispatcher — executes commands via the unified registry (PRD-44)."""
from __future__ import annotations

from .command import CommandContext
from .registry import UnifiedCommandRegistry

__all__ = ["CommandDispatcher"]


class CommandDispatcher:
    """Executes a command given its text and context.

    Usage::

        handled = dispatcher.dispatch("/config", ctx)
    """

    def __init__(self, registry: UnifiedCommandRegistry) -> None:
        self._registry = registry

    def dispatch(self, text: str, ctx: CommandContext) -> bool:
        """Look up and execute the command for *text*.

        Returns True if the command was handled (either via handler or by
        setting ``ctx.renderer._pending_menu``), False if unknown.
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
            renderer=ctx.renderer,
            config=ctx.config,
            session_id=ctx.session_id,
        )

        # Menu factory takes precedence when no args are given.
        if cmd.menu_factory is not None and not args.strip():
            widget = cmd.menu_factory(ctx_with_args)
            if ctx.renderer is not None:
                ctx.renderer._pending_menu = widget
            return True

        # Handler (can also be triggered alongside a menu factory when args exist)
        if cmd.handler is not None:
            return cmd.handler(ctx_with_args)

        return False
