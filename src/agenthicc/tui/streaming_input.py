"""Backward-compatibility re-export — canonical location is input/streaming.py."""
from __future__ import annotations

from agenthicc.tui.input.streaming import StreamingSession as StreamingInput  # noqa: F401

__all__ = ["StreamingInput"]
