"""@mention content injection (PRD-33 + PRD-35)."""
from __future__ import annotations

import asyncio
import glob as _glob
import mimetypes
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cache import MentionCache

from .parser import Mention, MentionKind

__all__ = ["InjectionConfig", "InjectedContent", "build_context_prefix", "resolve_mention"]


# ---------------------------------------------------------------------------
# Configuration & result types
# ---------------------------------------------------------------------------


@dataclass
class InjectionConfig:
    mention_token_budget: int = 32_000  # total chars across all mentions
    max_file_chars: int = 16_000  # per-file truncation threshold
    max_glob_files: int = 20  # max files from one glob
    url_timeout_seconds: float = 10.0
    cwd: Path = field(default_factory=Path.cwd)


@dataclass
class InjectedContent:
    mention: Mention
    block: str  # formatted content block (empty string on error)
    chars_used: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Binary detection helpers
# ---------------------------------------------------------------------------

_BINARY_MIMES = frozenset(
    {
        "image/",
        "audio/",
        "video/",
        "application/pdf",
        "application/zip",
        "application/octet-stream",
    }
)


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


# ---------------------------------------------------------------------------
# Block formatters
# ---------------------------------------------------------------------------


def _format_file_block(path_str: str, content: str, total_chars: int, truncated: bool) -> str:
    trunc_note = (
        f"\n[… truncated {total_chars - len(content):,} chars]" if truncated else ""
    )
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
                mtime = time.strftime("%Y-%m-%d", time.localtime(entry.stat().st_mtime))
                lines.append(f"{entry.name}  {size_kb:.1f} KB  {mtime}")
    except OSError as exc:
        lines.append(f"[error reading directory: {exc}]")
    body = "\n".join(lines) or "(empty)"
    return f'<dir path="{path_str}">\n{body}\n</dir>'


async def _check_robots(url: str, user_agent: str = "agenthicc") -> bool:
    """Return True if scraping is allowed.  Non-blocking best-effort."""
    try:
        parsed = urllib.parse.urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            import httpx  # noqa: PLC0415
        except ImportError:
            return True
        from agenthicc.tools.http import agenthicc_http_client  # noqa: PLC0415
        async with agenthicc_http_client(timeout=5.0) as client:
            try:
                resp = await client.get(robots_url)
                rp.parse(resp.text.splitlines())
            except Exception:  # noqa: BLE001
                return True  # can't fetch robots.txt → allow
        return rp.can_fetch(user_agent, url)
    except Exception:  # noqa: BLE001
        return True


async def _format_url_block(
    url: str,
    timeout: float,
    respect_robots: bool = False,
    session_url_cache: dict[str, str] | None = None,
) -> str:
    # In-session cache hit
    if session_url_cache is not None and url in session_url_cache:
        return session_url_cache[url]

    # robots.txt check (opt-in)
    if respect_robots and not await _check_robots(url):
        block = f'<url href="{url}">\n[robots.txt disallows scraping this URL]\n</url>'
        if session_url_cache is not None:
            session_url_cache[url] = block
        return block

    # Fetch
    try:
        from agenthicc.tools.http import agenthicc_http_client  # noqa: PLC0415

        async with agenthicc_http_client(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "agenthicc/1.0"})
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                text = re.sub(
                    r"<script[^>]*>.*?</script>",
                    "",
                    resp.text,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                text = re.sub(
                    r"<style[^>]*>.*?</style>",
                    "",
                    text,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()[:16_000]
            elif "text" in ct or "json" in ct:
                text = resp.text[:16_000]
            else:
                text = f"[{ct} — {len(resp.content):,} bytes]"
    except ImportError:
        text = "[httpx not installed]"
    except Exception as exc:  # noqa: BLE001
        text = f"[fetch failed: {exc}]"

    block = f'<url href="{url}">\n{text}\n</url>'
    if session_url_cache is not None:
        session_url_cache[url] = block
    return block


# ---------------------------------------------------------------------------
# Glob resolver (PRD-35 enhanced with summary header)
# ---------------------------------------------------------------------------


async def _resolve_glob(mention: Mention, cfg: InjectionConfig) -> InjectedContent:
    pattern = str(cfg.cwd / mention.path)
    all_matches = sorted(
        p for p in _glob.glob(pattern, recursive=True) if Path(p).is_file()
    )

    included: list[str] = []
    omitted_budget: list[str] = []
    omitted_binary: list[str] = []
    blocks: list[str] = []
    total_chars = 0

    for match in all_matches:
        p = Path(match)
        try:
            rel = str(p.relative_to(cfg.cwd))
        except ValueError:
            rel = str(p)

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

    if blocks:
        combined = header + "\n\n" + "\n\n".join(blocks)
    else:
        combined = f"[⚠ no text files matched {mention.path}]"

    return InjectedContent(mention=mention, block=combined, chars_used=total_chars)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


async def resolve_mention(
    mention: Mention,
    cfg: InjectionConfig,
    cache: MentionCache | None = None,
    current_turn: int = 0,
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
        # Pass in-session URL cache from MentionCache if available
        session_url_cache: dict[str, str] | None = None
        if cache is not None:
            session_url_cache = cache._url_cache  # type: ignore[attr-defined]
        block = await _format_url_block(
            mention.path,
            cfg.url_timeout_seconds,
            session_url_cache=session_url_cache,
        )
        return InjectedContent(mention=mention, block=block, chars_used=len(block))

    if mention.kind == MentionKind.DIRECTORY:
        block = _format_dir_block(mention.resolved, mention.path)  # type: ignore[arg-type]
        return InjectedContent(mention=mention, block=block, chars_used=len(block))

    if mention.kind == MentionKind.GLOB:
        return await _resolve_glob(mention, cfg)

    # --- FILE ---

    # Cache check: unchanged file → return cached reference
    if cache is not None and mention.resolved is not None:
        if cache.is_unchanged(mention.path, mention.resolved):
            last = cache.last_turn(mention.path)
            block = (
                f'<file path="{mention.path}" cached="true">'
                f"\n[Same content as turn {last} — file unchanged]\n</file>"
            )
            return InjectedContent(mention=mention, block=block, chars_used=50)

    # Build modified-since prefix if we've seen this file before but it changed
    modified_prefix = ""
    if (
        cache is not None
        and mention.resolved is not None
        and cache.last_turn(mention.path) is not None
    ):
        modified_prefix = "[modified since last mention]\n"

    # Binary check
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

    raw_block = _format_file_block(mention.path, content, total, truncated)
    block = modified_prefix + raw_block

    # Record in cache after successful read
    if cache is not None and mention.resolved is not None:
        cache.record(mention.path, mention.resolved, len(block), current_turn)

    return InjectedContent(mention=mention, block=block, chars_used=len(block))


async def build_context_prefix(
    text: str,
    cwd: Path | None = None,
    cfg: InjectionConfig | None = None,
    cache: MentionCache | None = None,
    current_turn: int = 0,
) -> tuple[str, list[InjectedContent]]:
    """Parse @mentions from *text*, resolve each, apply token budget, return
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
        *(resolve_mention(m, cfg, cache=cache, current_turn=current_turn) for m in mentions)
    )

    # Apply overall token budget (best-effort: truncate last block)
    budget = cfg.mention_token_budget
    blocks: list[str] = []
    used = 0
    for r in resolved:
        if r.error == "not_found":
            blocks.append(r.block)  # warnings always included
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
