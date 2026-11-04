"""MCP tool results and schemas at the provider boundary (TOOLR-06).

:func:`tool_result` turns an MCP ``CallToolResult`` into the TOOLR-04
envelope the model receives:

- ``structuredContent`` is used whole whenever it is present;
- text blocks are joined (one JSON text block is decoded);
- image, audio and binary resource blocks are written to the artifact
  directory and returned as ``{type, mime_type, path, bytes}`` — never as
  base64 text in the model's context;
- embedded text resources return their text, and resource links return
  ``{uri, name, mime_type}``;
- an ``isError`` result is ``ok: false, error_code: "internal",
  reason: "remote_error"``;
- text is cut at :data:`MAX_RESULT_CHARS` with ``truncated`` and
  ``original_chars``.

:func:`provider_safe_schema` rewrites an MCP ``inputSchema`` into a shape
every provider accepts: local ``$ref`` inlined, a top-level
``oneOf``/``anyOf``/``allOf`` flattened into one object, a non-object root
wrapped as ``{value: …}``, and unsupported meta keywords dropped.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_RESULT_CHARS",
    "SafeSchema",
    "configure_artifact_root",
    "provider_safe_schema",
    "tool_result",
]

#: Text an MCP result may put in the model's context.
MAX_RESULT_CHARS: Final = 50_000
#: A single binary block written as an artifact.
MAX_ARTIFACT_BYTES: Final = 20 * 1024 * 1024
#: How deep local ``$ref`` chains are inlined before giving up.
_MAX_REF_DEPTH: Final = 8
_DROPPED_KEYWORDS: Final = frozenset({"$schema", "$id", "$comment", "$defs", "definitions"})

_artifact_root = Path(tempfile.gettempdir()) / "agenthicc-mcp-artifacts"


def configure_artifact_root(path: Path) -> None:
    """Where binary MCP content is written (the session directory, at startup)."""
    global _artifact_root
    _artifact_root = path


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    try:
        return object.__getattribute__(value, name)
    except AttributeError:
        return None


def _decode_json_text(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _artifact(data: object, mime_type: str, server: str, kind: str) -> dict[str, object]:
    raw: bytes
    if isinstance(data, bytes):
        raw = data
    elif isinstance(data, str):
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raw = data.encode()
    else:
        return {"type": kind, "mime_type": mime_type, "error": "no binary payload"}
    if len(raw) > MAX_ARTIFACT_BYTES:
        return {
            "type": kind,
            "mime_type": mime_type,
            "bytes": len(raw),
            "error": f"larger than {MAX_ARTIFACT_BYTES} bytes; not saved",
        }
    import mimetypes

    digest = hashlib.sha256(raw).hexdigest()[:24]
    extension = mimetypes.guess_extension(mime_type) or ".bin"
    safe_server = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in server) or "mcp"
    directory = _artifact_root / safe_server
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}{extension}"
    if not path.exists():
        path.write_bytes(raw)
    return {"type": kind, "mime_type": mime_type, "path": str(path), "bytes": len(raw)}


def _content_value(result: object, server: str) -> object:
    structured = _field(result, "structuredContent")
    if isinstance(structured, Mapping):
        if set(structured) == {"result"}:
            return structured["result"]
        return dict(structured)
    content = _field(result, "content")
    if not content:
        return None
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return content
    texts: list[str] = []
    artifacts: list[dict[str, object]] = []
    links: list[dict[str, object]] = []
    for block in content:
        kind = str(_field(block, "type") or "")
        text = _field(block, "text")
        if kind in {"", "text"} and isinstance(text, str):
            texts.append(text)
        elif kind in {"image", "audio"} or (not kind and _field(block, "data") is not None):
            mime = str(_field(block, "mimeType") or "application/octet-stream")
            artifacts.append(_artifact(_field(block, "data"), mime, server, kind or "binary"))
        elif kind == "resource":
            resource = _field(block, "resource")
            resource_text = _field(resource, "text")
            if isinstance(resource_text, str):
                texts.append(resource_text)
            else:
                mime = str(_field(resource, "mimeType") or "application/octet-stream")
                artifacts.append(_artifact(_field(resource, "blob"), mime, server, "resource"))
        elif kind == "resource_link":
            links.append(
                {
                    "uri": str(_field(block, "uri") or ""),
                    "name": str(_field(block, "name") or ""),
                    "mime_type": str(_field(block, "mimeType") or ""),
                }
            )
        else:
            texts.append(str(text if isinstance(text, str) else block))
    if not artifacts and not links:
        if len(texts) == 1:
            return _decode_json_text(texts[0])
        return "\n".join(texts)
    value: dict[str, object] = {}
    if texts:
        value["text"] = "\n".join(texts)
    if artifacts:
        value["artifacts"] = artifacts
    if links:
        value["resource_links"] = links
    return value


def _error_text(result: object) -> str:
    content = _field(result, "content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts = [_field(block, "text") for block in content]
        text = " ".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    return "the server reported an error"


def tool_result(result: object, *, server: str) -> dict[str, object]:
    """The envelope for one MCP ``CallToolResult``."""
    if _field(result, "isError") is True:
        return {
            "ok": False,
            "error_code": "internal",
            "reason": "remote_error",
            "error": f"MCP server {server!r}: {_error_text(result)}"[:1_000],
            "retryable": False,
        }
    value = _content_value(result, server)
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(rendered) <= MAX_RESULT_CHARS:
        return {"ok": True, "content": value}
    return {
        "ok": True,
        "content": rendered[:MAX_RESULT_CHARS],
        "truncated": True,
        "original_chars": len(rendered),
    }


@dataclass(frozen=True, slots=True)
class SafeSchema:
    """A provider-safe input schema, and whether arguments arrive wrapped."""

    schema: dict[str, object]
    wrapped: bool = False


def _inline(node: object, defs: Mapping[str, object], depth: int) -> object:
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if not ref.startswith(("#/$defs/", "#/definitions/")) or name not in defs:
                return {}
            if depth >= _MAX_REF_DEPTH:
                return {}
            return _inline(defs[name], defs, depth + 1)
        return {
            key: _inline(value, defs, depth)
            for key, value in node.items()
            if key not in _DROPPED_KEYWORDS
        }
    if isinstance(node, list):
        return [_inline(item, defs, depth) for item in node]
    return node


def provider_safe_schema(schema: Mapping[str, object]) -> SafeSchema | None:
    """Rewrite *schema* for providers, or ``None`` when it cannot be made safe."""
    raw = copy.deepcopy(dict(schema))
    defs: dict[str, object] = {}
    for key in ("$defs", "definitions"):
        table = raw.get(key)
        if isinstance(table, Mapping):
            defs.update({str(name): value for name, value in table.items()})
    inlined = _inline(raw, defs, 0)
    if not isinstance(inlined, dict):
        return None
    for combinator in ("oneOf", "anyOf", "allOf"):
        branches = inlined.get(combinator)
        if inlined.get("type") in (None, "object") and isinstance(branches, list):
            existing = inlined.get("properties")
            merged: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
            required: list[str] = []
            for branch in branches:
                if isinstance(branch, dict) and isinstance(branch.get("properties"), dict):
                    merged.update(branch["properties"])
                    if combinator == "allOf" and isinstance(branch.get("required"), list):
                        required.extend(str(item) for item in branch["required"])
            inlined = {key: value for key, value in inlined.items() if key not in {combinator}}
            inlined["type"] = "object"
            inlined["properties"] = merged
            if required:
                inlined["required"] = sorted(set(required))
    if inlined.get("type") not in (None, "object"):
        return SafeSchema(
            {"type": "object", "properties": {"value": inlined}, "required": ["value"]},
            wrapped=True,
        )
    inlined["type"] = "object"
    root_properties = inlined.get("properties")
    if root_properties is None:
        inlined["properties"] = {}
    elif not isinstance(root_properties, dict):
        return None
    return SafeSchema(inlined)
