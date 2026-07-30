"""Bounded browser artifact persistence through the workspace boundary."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from agenthicc.tools.sandbox import WorkspaceView

__all__ = ["BrowserArtifact", "BrowserArtifactStore"]

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True, slots=True)
class BrowserArtifact:
    """Metadata returned after a screenshot is stored."""

    artifact_id: str
    relative_path: str
    mime_type: str
    byte_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.relative_path,
            "mime_type": self.mime_type,
            "byte_count": self.byte_count,
        }


class BrowserArtifactStore:
    """Write screenshots below ``.agenthicc/browser-artifacts`` only."""

    def __init__(
        self, workspace: WorkspaceView, root: str = ".agenthicc/browser-artifacts"
    ) -> None:
        self._workspace = workspace
        self._root = root.strip("/")

    def write_screenshot(
        self,
        session_id: str,
        content: bytes,
        *,
        mime_type: str = "image/png",
    ) -> BrowserArtifact:
        if not _SAFE_ID.fullmatch(session_id):
            raise ValueError("browser session id is not safe for artifact storage")
        if mime_type not in {"image/png", "image/jpeg"}:
            raise ValueError("unsupported screenshot MIME type")
        suffix = ".jpg" if mime_type == "image/jpeg" else ".png"
        artifact_id = uuid.uuid4().hex
        relative = str(Path(self._root) / session_id / f"{artifact_id}{suffix}")
        self._workspace.resolve(relative).parent.mkdir(parents=True, exist_ok=True)
        self._workspace.resolve(relative).write_bytes(content)
        return BrowserArtifact(artifact_id, relative, mime_type, len(content))
