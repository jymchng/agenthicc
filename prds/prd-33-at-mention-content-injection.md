---
title: "PRD-33: @Mention Content Injection — File Reading, Truncation, and Context Building"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-32-at-mention-parser.md
---

# PRD-33: @Mention Content Injection

## Context

PRD-32 produces a list of `Mention` objects from submitted text.  This PRD
defines how each mention type is **resolved to content**, how that content is
**truncated** to fit within a token budget, and how it is **injected into the
agent's context** before the message is sent to the LLM.

The result is that typing `"Review @src/auth.py and list issues"` causes the
agent to receive the full file contents alongside the instruction, without
needing to call `read_file` itself.

---

## Goals

| ID | Goal |
|----|------|
| G1 | File mentions inject the full file content (up to token budget) into the message context |
| G2 | Directory mentions inject a file listing (name, size, modified) not full contents |
| G3 | Glob patterns expand to at most `max_glob_files` files, each injected with G1 rules |
| G4 | URL mentions fetch the page and inject plain-text extracted content |
| G5 | Large files are truncated with a `[… truncated N bytes]` marker; no silent loss |
| G6 | A per-message token budget limits total injected content across all mentions |
| G7 | Binary files get a `[binary file — N bytes]` placeholder, not raw bytes |
| G8 | Injected content is presented as a structured prefix block so the LLM sees it clearly |
| G9 | Unresolved mentions emit a warning line; they do not block the message |
| G10 | The injection step is async (I/O via `asyncio.to_thread`) and testable with mocks |

## Non-Goals
- Indexing / embedding files for semantic search (separate PRD)
- Persisting injected content across turns (each turn re-reads)
- Watching files for changes

---

## Token Budget

The default budget is **32 000 tokens ≈ 128 000 characters** (4 chars/token
approximation).  It is configurable via:

```toml
[execution]
mention_token_budget = 32000   # characters budget for injected @mention content
mention_max_glob_files = 20    # max files expanded from a single glob
mention_max_file_chars = 16000 # per-file character cap before truncation
```

---

## Content Block Format

Injected content is prepended to the user message as a markdown fence block:

```
<file path="src/auth.py" chars="1234">
... file content ...
</file>

<dir path="src/">
README.md  1.2 KB  2026-06-12
auth.py    3.4 KB  2026-06-11
tests/     dir
</dir>

<url href="https://example.com/doc">
... page text ...
</url>

[⚠ @nonexistent.txt not found]
```

The original mention tokens remain in the user message so the LLM knows what
the user was referring to.

---

## Data Structures

```python
# src/agenthicc/mentions/injector.py

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .parser import Mention, MentionKind

__all__ = ["InjectionConfig", "InjectedContent", "build_context_prefix", "resolve_mention"]


@dataclass
class InjectionConfig:
    mention_token_budget: int = 32_000     # total chars across all mentions
    max_file_chars: int = 16_000           # per-file truncation threshold
    max_glob_files: int = 20               # max files from one glob
    url_timeout_seconds: float = 10.0
    cwd: Path = field(default_factory=Path.cwd)


@dataclass
class InjectedContent:
    mention: Mention
    block: str             # formatted content block (empty string on error)
    chars_used: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
```

---

## Resolver Functions

```python
# src/agenthicc/mentions/injector.py  (continued)

import fnmatch
import mimetypes

_BINARY_MIMES = frozenset({"image/", "audio/", "video/", "application/pdf",
                           "application/zip", "application/octet-stream"})


def _is_binary(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and any(mime.startswith(m) for m in _BINARY_MIMES):
        return True
    # Heuristic: sample first 512 bytes for null bytes
    try:
        sample = path.read_bytes()[:512]
        return b"\x00" in sample
    except OSError:
        return False


def _read_file_sync(path: Path, max_chars: int) -> tuple[str, int, bool]:
    """Read file; return (content, total_chars, was_truncated)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OSError(str(exc)) from exc
    total = len(raw)
    if total > max_chars:
        return raw[:max_chars], total, True
    return raw, total, False


def _format_file_block(path_str: str, content: str, total_chars: int,
                       truncated: bool) -> str:
    trunc_note = f"\n[… truncated {total_chars - len(content):,} chars]" if truncated else ""
    return f'<file path="{path_str}" chars="{total_chars:,}">\n{content}{trunc_note}\n</file>'


def _format_dir_block(path: Path, path_str: str) -> str:
    lines = []
    try:
        for entry in sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name)):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                lines.append(f"{entry.name}/  dir")
            else:
                size_kb = entry.stat().st_size / 1024
                mtime = time.strftime(
                    "%Y-%m-%d", time.localtime(entry.stat().st_mtime)
                )
                lines.append(f"{entry.name}  {size_kb:.1f} KB  {mtime}")
    except PermissionError:
        lines.append("[permission denied]")
    body = "\n".join(lines) or "(empty)"
    return f'<dir path="{path_str}">\n{body}\n</dir>'


async def _format_url_block(url: str, timeout: float) -> str:
    try:
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "agenthicc/1.0"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text" in content_type or "json" in content_type:
                text = resp.text[:16_000]
                # Strip HTML tags for readability
                import re  # noqa: PLC0415
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
            else:
                text = f"[non-text content: {content_type}]"
    except Exception as exc:  # noqa: BLE001
        text = f"[fetch failed: {exc}]"
    return f'<url href="{url}">\n{text}\n</url>'
```

---

## Main Entry Point

```python
# src/agenthicc/mentions/injector.py  (continued)

async def resolve_mention(
    mention: Mention,
    cfg: InjectionConfig,
) -> InjectedContent:
    """Resolve a single Mention to an InjectedContent block."""

    if mention.kind == MentionKind.UNRESOLVED:
        return InjectedContent(
            mention=mention,
            block=f"[⚠ {mention.raw} not found]",
            chars_used=0,
            error="not_found",
        )

    if mention.kind == MentionKind.URL:
        block = await _format_url_block(mention.path, cfg.url_timeout_seconds)
        return InjectedContent(mention=mention, block=block, chars_used=len(block))

    if mention.kind == MentionKind.DIRECTORY:
        block = _format_dir_block(mention.resolved, mention.path)
        return InjectedContent(mention=mention, block=block, chars_used=len(block))

    if mention.kind == MentionKind.GLOB:
        return await _resolve_glob(mention, cfg)

    # FILE
    if mention.resolved and _is_binary(mention.resolved):
        size = mention.resolved.stat().st_size
        block = f'<file path="{mention.path}" binary="true" bytes="{size:,}"/>'
        return InjectedContent(mention=mention, block=block, chars_used=len(block))

    try:
        content, total, truncated = await asyncio.to_thread(
            _read_file_sync, mention.resolved, cfg.max_file_chars
        )
    except OSError as exc:
        block = f"[⚠ could not read {mention.raw}: {exc}]"
        return InjectedContent(mention=mention, block=block, error=str(exc))

    block = _format_file_block(mention.path, content, total, truncated)
    return InjectedContent(mention=mention, block=block, chars_used=len(block))


async def _resolve_glob(mention: Mention, cfg: InjectionConfig) -> InjectedContent:
    import glob as _glob  # noqa: PLC0415
    pattern = str(cfg.cwd / mention.path)
    matches = sorted(_glob.glob(pattern, recursive=True))[:cfg.max_glob_files]
    blocks: list[str] = []
    total_chars = 0
    for match_path in matches:
        p = Path(match_path)
        if not p.is_file() or _is_binary(p):
            continue
        try:
            content, chars, truncated = await asyncio.to_thread(
                _read_file_sync, p, cfg.max_file_chars
            )
            b = _format_file_block(str(p.relative_to(cfg.cwd)), content, chars, truncated)
        except OSError as exc:
            b = f"[⚠ {p}: {exc}]"
        blocks.append(b)
        total_chars += len(b)
    combined = "\n\n".join(blocks) or f"[⚠ no files matched {mention.path}]"
    return InjectedContent(mention=mention, block=combined, chars_used=total_chars)


async def build_context_prefix(
    text: str,
    cwd: Path | None = None,
    cfg: InjectionConfig | None = None,
) -> tuple[str, list[InjectedContent]]:
    """
    Parse @mentions from *text*, resolve each, apply token budget, return
    (prefix_block, resolved_list).

    The prefix_block is empty string when there are no mentions.
    Caller prepends prefix_block to the user message sent to the LLM.
    """
    from .parser import parse_mentions  # noqa: PLC0415

    cfg = cfg or InjectionConfig(cwd=cwd or Path.cwd())
    mentions = parse_mentions(text, cwd=cfg.cwd)
    if not mentions:
        return "", []

    resolved = await asyncio.gather(
        *(resolve_mention(m, cfg) for m in mentions)
    )

    # Apply overall token budget (best-effort: truncate last block)
    budget = cfg.mention_token_budget
    blocks: list[str] = []
    used = 0
    for r in resolved:
        if r.error == "not_found":
            blocks.append(r.block)   # warnings always included
            continue
        if used + r.chars_used > budget:
            remaining = max(0, budget - used)
            if remaining > 100:
                truncated_block = r.block[:remaining] + "\n[… budget exceeded]"
                blocks.append(truncated_block)
            else:
                blocks.append(f"[⚠ {r.mention.raw} omitted — budget exceeded]")
            used = budget
        else:
            blocks.append(r.block)
            used += r.chars_used

    prefix = "\n\n".join(b for b in blocks if b) + "\n\n" if blocks else ""
    return prefix, list(resolved)
```

---

## Integration in `_run_agent_turn()`

In `src/agenthicc/__main__.py`, in `_run_agent_turn()`, before calling
`_active_runner.run()`:

```python
from agenthicc.mentions.injector import build_context_prefix, InjectionConfig  # noqa: PLC0415

_mention_cfg = InjectionConfig(
    mention_token_budget=getattr(cfg.execution, "mention_token_budget", 32_000),
    max_file_chars=getattr(cfg.execution, "mention_max_file_chars", 16_000),
    max_glob_files=getattr(cfg.execution, "mention_max_glob_files", 20),
    cwd=Path(os.getcwd()),
)
_mention_prefix, _injected = await build_context_prefix(
    text, cwd=_mention_cfg.cwd, cfg=_mention_cfg
)
# Prepend file content blocks to the user message
_agent_text = _mention_prefix + text if _mention_prefix else text

response = await _active_runner.run(
    _agent_instance,
    _agent_text,       # ← was plain `text`
    memory=session_memory,
    config_override=_cfg,
)
```

---

## Tests

```python
# tests/unit/test_mention_injector.py

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from agenthicc.mentions.injector import (
    InjectionConfig, resolve_mention, build_context_prefix, _is_binary,
)
from agenthicc.mentions.parser import Mention, MentionKind

pytestmark = pytest.mark.unit


def _file_mention(tmp_path, filename, content):
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return Mention(
        raw=f"@{filename}", path=filename, kind=MentionKind.FILE,
        resolved=p.resolve(), start=0, end=len(filename) + 1,
    )


def _unresolved(name):
    return Mention(
        raw=f"@{name}", path=name, kind=MentionKind.UNRESOLVED,
        resolved=None, start=0, end=len(name) + 1,
    )


@pytest.mark.asyncio
async def test_resolve_file_mention(tmp_path):
    m = _file_mention(tmp_path, "hello.py", "print('hi')")
    cfg = InjectionConfig(cwd=tmp_path)
    result = await resolve_mention(m, cfg)
    assert result.ok
    assert "hello.py" in result.block
    assert "print('hi')" in result.block


@pytest.mark.asyncio
async def test_resolve_file_truncates_large_file(tmp_path):
    m = _file_mention(tmp_path, "big.py", "x" * 50_000)
    cfg = InjectionConfig(cwd=tmp_path, max_file_chars=100)
    result = await resolve_mention(m, cfg)
    assert "truncated" in result.block
    assert len(result.block) < 1000


@pytest.mark.asyncio
async def test_resolve_unresolved_mention(tmp_path):
    m = _unresolved("ghost.py")
    result = await resolve_mention(m, InjectionConfig(cwd=tmp_path))
    assert result.error == "not_found"
    assert "not found" in result.block


@pytest.mark.asyncio
async def test_resolve_directory_mention(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    m = Mention(
        raw="@src/", path="src/", kind=MentionKind.DIRECTORY,
        resolved=(tmp_path / "src").resolve(), start=0, end=5,
    )
    result = await resolve_mention(m, InjectionConfig(cwd=tmp_path))
    assert "main.py" in result.block
    assert "<dir" in result.block


@pytest.mark.asyncio
async def test_binary_file_shows_placeholder(tmp_path):
    p = tmp_path / "image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    m = Mention(
        raw="@image.png", path="image.png", kind=MentionKind.FILE,
        resolved=p.resolve(), start=0, end=10,
    )
    result = await resolve_mention(m, InjectionConfig(cwd=tmp_path))
    assert "binary" in result.block
    assert result.ok


@pytest.mark.asyncio
async def test_build_context_prefix_no_mentions(tmp_path):
    prefix, resolved = await build_context_prefix("hello world", cwd=tmp_path)
    assert prefix == ""
    assert resolved == []


@pytest.mark.asyncio
async def test_build_context_prefix_injects_file(tmp_path):
    (tmp_path / "auth.py").write_text("def login(): pass")
    prefix, resolved = await build_context_prefix("Review @auth.py", cwd=tmp_path)
    assert "def login" in prefix
    assert len(resolved) == 1


@pytest.mark.asyncio
async def test_build_context_prefix_budget_exceeded(tmp_path):
    (tmp_path / "f.py").write_text("a" * 5_000)
    cfg = InjectionConfig(cwd=tmp_path, mention_token_budget=100, max_file_chars=5_000)
    prefix, _ = await build_context_prefix("@f.py", cwd=tmp_path, cfg=cfg)
    assert "budget exceeded" in prefix or "omitted" in prefix
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mention_injector.py -v

# Manual: create a file and mention it
echo "def hello(): return 42" > /tmp/test.py
uv run agenthicc
# Type: "what does @/tmp/test.py do?"
# Agent should see the file content injected before the question
```
