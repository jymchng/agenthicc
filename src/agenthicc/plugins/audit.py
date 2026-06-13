from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_AUDIT_FILE = Path(".agenthicc/plugin_audit.jsonl")


def record_call(
    agent_name: str,
    tool_name: str,
    args: dict[str, Any],
    ok: bool,
    duration_ms: float,
    error: str | None = None,
    audit_file: Path | None = None,
) -> None:
    """Append one audit record for a completed plugin tool call."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent_name,
        "tool": tool_name,
        "args": args,
        "ok": ok,
        "duration_ms": round(duration_ms, 1),
    }
    if error:
        entry["error"] = error

    target = audit_file or _AUDIT_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:
        log.warning("Failed to write plugin audit log: %s", exc)
