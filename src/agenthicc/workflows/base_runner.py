"""BaseWorkflowRunner — ABC for all workflow runners."""

from __future__ import annotations

import abc


class BaseWorkflowRunner(abc.ABC):
    """Every workflow runner must implement ``run()`` and ``resume()``.

    The session owner creates the durable run identity and bootstrap context;
    a concrete runner must attach its serializable typed context to
    ``config.workflow_handle`` before its first provider or tool call. Runtime
    exceptions should escape to ``WorkflowRunHandle.finalize_failure()`` so the
    TUI and headless owner make one recoverable/terminal disposition. Runners
    may report phase state and artifacts, but must not replace that finalizer
    with an independent terminal-failure checkpoint. ``resume()`` must dispatch
    the supplied context directly and never call ``run(context.intent)``.

    ``run()`` returns the runner's typed context object so that subclasses
    can call ``ctx = await super().run(intent)`` and continue with additional
    phases (PRD-114 composite workflow pattern).  Callers that do not need
    the return value may safely ignore it. Runners must attach that context to
    ``WorkflowConfig.workflow_handle`` before the first provider/tool call and
    leave ordinary exceptions to the session-owned
    ``WorkflowRunHandle.finalize_failure()`` boundary. They must not turn an
    ordinary phase error into a terminal failed checkpoint themselves.
    """

    @abc.abstractmethod
    async def run(self, intent: str) -> object:
        """Start a fresh run for the given user intent.

        Returns the runner's internal context (e.g. ``CodePlanContext`` for
        ``CodePlanRunner``, ``WorkflowContext`` for ``WorkflowRunner``).
        The return value is typed as ``object`` so that each concrete subclass
        may declare a tighter return type without violating the ABC contract.
        """

    @abc.abstractmethod
    async def resume(self, context: object) -> object:
        """Resume an interrupted run from the saved typed context.

        Implementations must dispatch from the restored phase/state and may not
        implement resume as ``run(original_intent)``; doing so would repeat
        earlier phases and side effects.
        """
