"""The agents hub: running work, attention, history and the library (AGX-05–07).

One component behind ``agenthicc agents`` (full screen) and ``/agents``,
Ctrl+G or ← on an empty prompt (an overlay over the conversation):

- :mod:`.model` — tabs, rows, groups, trees and search, as pure functions;
- :mod:`.vocabulary` — the one status vocabulary (glyph, label, group);
- :mod:`.layout` — responsive list columns that degrade by priority;
- :mod:`.render` — the list, detail and peek as lines of text;
- :mod:`.detail` — the redacted transcript tail, requests and changes;
- :mod:`.hub` — :class:`AgentsHub`, its engine, keys and operations;
- :mod:`.overlay` — the hub as an overlay inside a session.
"""

from __future__ import annotations

from agenthicc.tui.workspace.agents_hub.hub import (
    AgentsHub,
    DispatchDefaults,
    HubResult,
    run_agents_hub,
)
from agenthicc.tui.workspace.agents_hub.model import Tab

__all__ = ["AgentsHub", "DispatchDefaults", "HubResult", "Tab", "run_agents_hub"]
