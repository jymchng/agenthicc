"""BaseWorkflowRunner — ABC for all workflow runners."""
from __future__ import annotations

import abc
from typing import Any


class BaseWorkflowRunner(abc.ABC):
    """Every workflow runner must implement run() and resume().

    ``run()`` returns the runner's typed context object so that subclasses
    can call ``ctx = await super().run(intent)`` and continue with additional
    phases (PRD-114 composite workflow pattern).  Callers that do not need
    the return value may safely ignore it.
    """

    @abc.abstractmethod
    async def run(self, intent: str) -> Any:
        """Start a fresh run for the given user intent.

        Returns the runner's internal context (e.g. ``CodePlanContext`` for
        ``CodePlanRunner``, ``WorkflowContext`` for ``WorkflowRunner``).
        The return value is typed as ``Any`` so that each concrete subclass
        may declare a tighter return type without violating the ABC contract.
        """

    @abc.abstractmethod
    async def resume(self, context: Any) -> None:
        """Resume an interrupted run from a saved context / DataBus."""
