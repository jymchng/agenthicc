"""Fixtures for the flat checkpoint format written before workflow stacks."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.workflows.checkpoint import WorkflowCheckpoint


def schema_one_payload(checkpoint: WorkflowCheckpoint) -> dict[str, object]:
    payload = checkpoint.to_dict()
    for field in ("frame_id", "parent_run_id", "origin", "content_hash"):
        del payload[field]
    payload["schema_version"] = 1
    payload["content_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload
