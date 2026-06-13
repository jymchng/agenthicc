---
title: "PRD-35: @Mention Advanced Features — Globs, Conversation Memory, and @-URL"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-33-at-mention-content-injection.md, prd-34-at-mention-ui-and-transcript.md
---

# PRD-35: @Mention Advanced Features

## Context

PRDs 32–34 cover the core @mention pipeline (parse → inject → display).
This PRD specifies three advanced capabilities that build on top:

1. **Glob mentions** — `@src/**/*.py` expands to all matching files within the
   token budget, with a summary header listing which files were included and
   which were omitted.
2. **Conversation memory** — when the same file is mentioned again in a later
   turn, agenthicc detects the re-mention and either re-reads the file
   (if modified) or inlines a reference to the previous injection.
3. **`@https://…` URL fetching** — fetches web pages, strips HTML to plain
   text, respects `robots.txt` if configured, handles redirects and errors.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `@src/**/*.py` expands to at most `max_glob_files` files, budget-aware |
| G2 | Glob summary header: `@src/**/*.py → 12 files (3 omitted: budget exceeded)` |
| G3 | Files modified since last injection are flagged `[modified since last mention]` |
| G4 | Unchanged re-mentioned files reference the previous turn instead of re-injecting |
| G5 | URL fetching strips HTML, follows redirects, times out in `url_timeout_seconds` |
| G6 | A `[robots.txt disallows]` warning is shown when `respect_robots = true` and scraping is blocked |
| G7 | `@https://…` completion in the input bar suggests recent/bookmarked URLs |
| G8 | All fetched URLs are cached in-session (not cross-session) to avoid redundant fetches |

---

## 1. Glob Expansion (enhanced PRD-33 `_resolve_glob`)

### Summary header

```python
async def _resolve_glob(mention: Mention, cfg: InjectionConfig) -> InjectedContent:
    import glob as _gl
    pattern = str(cfg.cwd / mention.path)
    all_matches = sorted(p for p in _gl.glob(pattern, recursive=True)
                         if Path(p).is_file())

    included: list[str] = []
    omitted_budget: list[str] = []
    omitted_binary: list[str] = []
    blocks: list[str] = []
    total_chars = 0

    for match in all_matches:
        p = Path(match)
        rel = str(p.relative_to(cfg.cwd))
        if len(included) >= cfg.max_glob_files:
            omitted_budget.append(rel)
            continue
        if _is_binary(p):
            omitted_binary.append(rel)
            continue
        try:
            content, chars, truncated = await asyncio.to_thread(
                _read_file_sync, p, cfg.max_file_chars
            )
        except OSError:
            omitted_budget.append(rel)
            continue
        if total_chars + len(content) > cfg.mention_token_budget:
            omitted_budget.append(rel)
            continue
        blocks.append(_format_file_block(rel, content, chars, truncated))
        included.append(rel)
        total_chars += len(content)

    # Build summary header
    summary_parts = [f"@{mention.path} → {len(included)} file(s)"]
    if omitted_budget:
        summary_parts.append(f"{len(omitted_budget)} omitted (budget)")
    if omitted_binary:
        summary_parts.append(f"{len(omitted_binary)} binary skipped")
    header = f"<!-- {', '.join(summary_parts)} -->"

    combined = header + "\n\n" + "\n\n".join(blocks) if blocks else \
               f"[⚠ no text files matched {mention.path}]"
    return InjectedContent(mention=mention, block=combined, chars_used=total_chars)
```

---

## 2. Conversation Memory — Re-mention Detection

### `MentionCache` — in-session file content cache

```python
# src/agenthicc/mentions/cache.py

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MentionCache"]


@dataclass
class CacheEntry:
    path: str
    content_hash: str        # SHA-256 of file bytes at injection time
    injected_at_turn: int    # transcript turn index
    chars_used: int


class MentionCache:
    """Per-session cache that tracks which files have been injected and their state."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}  # path → entry

    def record(
        self,
        path: str,
        resolved: Path,
        chars_used: int,
        turn_index: int,
    ) -> None:
        content_hash = _sha256_file(resolved)
        self._entries[path] = CacheEntry(
            path=path,
            content_hash=content_hash,
            injected_at_turn=turn_index,
            chars_used=chars_used,
        )

    def is_unchanged(self, path: str, resolved: Path) -> bool:
        """True if the file hasn't changed since it was last injected."""
        entry = self._entries.get(path)
        if entry is None:
            return False
        return _sha256_file(resolved) == entry.content_hash

    def last_turn(self, path: str) -> int | None:
        entry = self._entries.get(path)
        return entry.injected_at_turn if entry else None

    def clear(self) -> None:
        self._entries.clear()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        h.update(path.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()
```

### Integration in `resolve_mention()`

```python
# src/agenthicc/mentions/injector.py — resolve_mention() extension

async def resolve_mention(
    mention: Mention,
    cfg: InjectionConfig,
    cache: MentionCache | None = None,
    current_turn: int = 0,
) -> InjectedContent:
    ...
    # FILE: check cache before reading
    if mention.kind == MentionKind.FILE and cache is not None:
        if cache.is_unchanged(mention.path, mention.resolved):
            last = cache.last_turn(mention.path)
            block = (
                f'<file path="{mention.path}" cached="true">'
                f'\n[Same content as turn {last} — file unchanged]\n</file>'
            )
            return InjectedContent(mention=mention, block=block, chars_used=50)
        elif cache.last_turn(mention.path) is not None:
            # File changed since last mention
            ...  # proceed to re-read but prefix with [modified since last mention]
    ...
    # After successful read, record in cache:
    if cache is not None and mention.resolved:
        cache.record(mention.path, mention.resolved, len(block), current_turn)
    ...
```

### Wiring the cache in `_run_tui_session()`

```python
# In _run_tui_session():
from agenthicc.mentions.cache import MentionCache  # noqa: PLC0415
_mention_cache = MentionCache()
renderer._mention_cache = _mention_cache

# In _run_agent_turn():
_prefix, _injected = await build_context_prefix(
    text,
    cwd=_mention_cfg.cwd,
    cfg=_mention_cfg,
    cache=getattr(renderer, "_mention_cache", None),
    current_turn=renderer._status.completed_agents,
)
```

---

## 3. URL Fetching — Enhanced `_format_url_block()`

### robots.txt enforcement (opt-in)

```python
# src/agenthicc/mentions/injector.py

import urllib.robotparser
import urllib.parse

async def _check_robots(url: str, user_agent: str = "agenthicc") -> bool:
    """Return True if scraping is allowed. Non-blocking best-effort."""
    try:
        parsed = urllib.parse.urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(robots_url)
                rp.parse(resp.text.splitlines())
            except Exception:
                return True   # can't fetch robots.txt → allow
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


async def _format_url_block(
    url: str,
    timeout: float,
    respect_robots: bool = False,
    session_url_cache: dict | None = None,
) -> str:
    # In-session cache
    if session_url_cache is not None and url in session_url_cache:
        return session_url_cache[url]

    # robots.txt check
    if respect_robots and not await _check_robots(url):
        block = f'<url href="{url}">\n[robots.txt disallows scraping this URL]\n</url>'
        return block

    # Fetch
    try:
        import httpx, re  # noqa: PLC0415
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "agenthicc/1.0"})
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                text = re.sub(r"<script[^>]*>.*?</script>", "", resp.text,
                              flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text,
                              flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()[:16_000]
            elif "json" in ct:
                text = resp.text[:16_000]
            else:
                text = f"[{ct} — {len(resp.content):,} bytes]"
    except Exception as exc:
        text = f"[fetch failed: {exc}]"

    block = f'<url href="{url}">\n{text}\n</url>'
    if session_url_cache is not None:
        session_url_cache[url] = block
    return block
```

### Config additions

```toml
[execution]
mention_respect_robots = false   # set true to honour robots.txt for @https:// mentions
```

---

## 4. URL Autocomplete in Input Bar

When the user types `@https://` or `@http://`, the completer can suggest
recently visited / bookmarked URLs from a per-session history list:

```python
# src/agenthicc/tui/input_bar.py — AtMentionCompleter extension

class AtMentionCompleter(Completer):
    def __init__(
        self,
        base_path: str | Path = ".",
        recent_urls: list[str] | None = None,  # ← new
    ) -> None:
        self._base = Path(base_path).resolve()
        self._recent_urls: list[str] = recent_urls or []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        at_idx = text.rfind("@")
        if at_idx == -1:
            return
        fragment = text[at_idx + 1:]

        # URL completions
        if fragment.startswith("http"):
            for url in self._recent_urls:
                remaining = url[len(fragment):]
                if url.startswith(fragment) and remaining:
                    yield Completion(
                        text=remaining,
                        start_position=0,
                        display=f"@{url}",
                        display_meta="url",
                    )
            return

        # Existing file completions ...
        ...
```

---

## Tests

```python
# tests/unit/test_mention_cache.py

import pytest
from pathlib import Path
from agenthicc.mentions.cache import MentionCache

pytestmark = pytest.mark.unit


def test_record_and_is_unchanged(tmp_path):
    f = tmp_path / "auth.py"
    f.write_text("x = 1")
    cache = MentionCache()
    cache.record("auth.py", f, chars_used=5, turn_index=0)
    assert cache.is_unchanged("auth.py", f)


def test_is_changed_after_file_modified(tmp_path):
    f = tmp_path / "auth.py"
    f.write_text("x = 1")
    cache = MentionCache()
    cache.record("auth.py", f, chars_used=5, turn_index=0)
    f.write_text("x = 2")  # modify
    assert not cache.is_unchanged("auth.py", f)


def test_last_turn_returns_none_for_unknown(tmp_path):
    cache = MentionCache()
    assert cache.last_turn("unknown.py") is None


def test_clear_empties_cache(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("y")
    cache = MentionCache()
    cache.record("f.py", f, 1, 0)
    cache.clear()
    assert not cache.is_unchanged("f.py", f)


# tests/unit/test_glob_expansion.py

@pytest.mark.asyncio
async def test_glob_includes_summary_header(tmp_path):
    (tmp_path / "a.py").write_text("a = 1")
    (tmp_path / "b.py").write_text("b = 2")
    from agenthicc.mentions.parser import Mention, MentionKind
    from agenthicc.mentions.injector import InjectionConfig, resolve_mention

    m = Mention(raw="@*.py", path="*.py", kind=MentionKind.GLOB,
                resolved=None, start=0, end=5)
    cfg = InjectionConfig(cwd=tmp_path, max_glob_files=10)
    result = await resolve_mention(m, cfg)
    assert "→" in result.block or "file" in result.block
    assert "a.py" in result.block
    assert "b.py" in result.block


@pytest.mark.asyncio
async def test_glob_respects_budget(tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.py").write_text("x" * 500)
    from agenthicc.mentions.parser import Mention, MentionKind
    from agenthicc.mentions.injector import InjectionConfig, resolve_mention

    m = Mention(raw="@*.py", path="*.py", kind=MentionKind.GLOB,
                resolved=None, start=0, end=5)
    cfg = InjectionConfig(cwd=tmp_path, mention_token_budget=1000, max_glob_files=20)
    result = await resolve_mention(m, cfg)
    # Budget should limit how many files are fully included
    included = result.block.count("<file ")
    assert included < 10  # not all 10 fit in budget
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mention_cache.py \
                                 tests/unit/test_glob_expansion.py -v

# Glob test:
uv run agenthicc
# "@src/**/*.py summarise all the Python files in src/"
# → chips show "→ N files", agent has all content injected

# Re-mention test:
# Turn 1: "read @pyproject.toml"
# Turn 2: "what are the dependencies in @pyproject.toml"
# → second turn shows "[Same content as turn 1 — file unchanged]"
# → modify pyproject.toml between turns
# → third mention shows "[modified since last mention]" flag

# URL test:
# "@https://docs.python.org/3/library/asyncio.html summarise asyncio"
# → page fetched, HTML stripped, content injected
```
