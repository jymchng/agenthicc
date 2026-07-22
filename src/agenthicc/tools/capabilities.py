# No "from __future__ import annotations" — set_metadata runs at import time.
"""Tool capability taxonomy and pre-built capability decorators (PRD-76).

Usage::

    from agenthicc.tools.capabilities import tool_read, tool_write

    @tool_write
    @tool()
    async def write_file(path: str, content: str) -> dict:
        ...

Decorator order does not matter — @set_metadata and @tool() write to different
attributes and never interfere.  Conventional style is @set_metadata above @tool().
"""

from enum import Enum

from lauren_ai._tools import TOOL_METADATA as _TOOL_METADATA, set_metadata

__all__ = [
    "CAPABILITIES_KEY",
    "ToolCapability",
    "get_tool_capabilities",
    "tool_read",
    "tool_write",
    "tool_execute",
    "tool_git_read",
    "tool_git_write",
    "tool_network",
    "tool_search",
    "tool_read_search",
    "tool_network_read",
    "tool_network_write",
    "tool_network_search",
]

#: Metadata key used by ToolCapabilityGate to look up capabilities.
CAPABILITIES_KEY = "capabilities"


class ToolCapability(str, Enum):
    """Named capability tags attached to @tool()-decorated functions.

    Inherits str so frozenset members serialise as plain strings and compare
    correctly against RuntimeMode.blocked_capabilities.
    """

    READ = "read"  # reads files / data — no persistent side effects
    WRITE = "write"  # creates, modifies, or deletes files / data
    EXECUTE = "execute"  # runs shell commands or arbitrary code
    GIT_READ = "git_read"  # reads git history, diffs, status, blame
    GIT_WRITE = "git_write"  # modifies git state (add, commit, checkout, stash)
    NETWORK = "network"  # makes outbound network calls (email, REST API, etc.)
    SEARCH = "search"  # searches content without state changes


# ── Single-capability decorators ──────────────────────────────────────────────

tool_read = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.READ}))
tool_write = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.WRITE}))
tool_execute = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.EXECUTE}))
tool_git_read = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.GIT_READ}))
tool_git_write = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.GIT_WRITE}))
tool_network = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.NETWORK}))
tool_search = set_metadata(CAPABILITIES_KEY, frozenset({ToolCapability.SEARCH}))

# ── Common multi-capability combinations ─────────────────────────────────────

tool_read_search = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.READ, ToolCapability.SEARCH}),
)
tool_network_read = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.NETWORK, ToolCapability.READ}),
)
tool_network_write = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.NETWORK, ToolCapability.WRITE}),
)
tool_network_search = set_metadata(
    CAPABILITIES_KEY,
    frozenset({ToolCapability.NETWORK, ToolCapability.SEARCH}),
)


def get_tool_capabilities(tool: object) -> frozenset:
    """Return the ToolCapability frozenset stored on a @tool()-decorated function.

    Reads from __lauren_ai_tool_metadata__[CAPABILITIES_KEY], written by
    set_metadata(CAPABILITIES_KEY, ...).  Returns an empty frozenset for
    unannotated tools (open-by-default semantics).
    """
    meta_dict: dict = getattr(tool, _TOOL_METADATA, None) or {}
    return meta_dict.get(CAPABILITIES_KEY) or frozenset()
