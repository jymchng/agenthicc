"""BaseWorkflowRunner — ABC for all workflow runners."""
from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agenthicc.workflows.config import WorkflowConfig


class BaseWorkflowRunner(abc.ABC):
    """Every workflow runner must implement run() and resume()."""

    @abc.abstractmethod
    async def run(self, intent: str) -> None:
        """Start a fresh run for the given user intent."""

    @abc.abstractmethod
    async def resume(self, context: Any) -> None:
        """Resume an interrupted run from a saved context / DataBus."""
