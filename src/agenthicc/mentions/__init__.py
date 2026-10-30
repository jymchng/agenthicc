"""@mention parsing and file content injection (PRD-32, PRD-33)."""

from .parser import (
    AGENT_PREFIX,
    MentionKind,
    Mention,
    agent_mentions,
    parse_mentions,
    strip_mentions,
)

__all__ = [
    "AGENT_PREFIX",
    "MentionKind",
    "Mention",
    "agent_mentions",
    "parse_mentions",
    "strip_mentions",
]
