"""Additional branch coverage for safe @mention resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agenthicc.mentions import injector
from agenthicc.mentions.cache import MentionCache
from agenthicc.mentions.injector import InjectionConfig, build_context_prefix, resolve_mention
from agenthicc.mentions.parser import Mention, MentionKind

pytestmark = pytest.mark.unit


def _mention(kind: MentionKind, path: str, resolved: Path | None = None) -> Mention:
    return Mention(
        raw=f"@{path}", path=path, kind=kind, resolved=resolved, start=0, end=len(path) + 1
    )


def test_binary_and_directory_helpers_cover_safe_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"text")
    assert injector._is_binary(image)
    unknown = tmp_path / "data.unknown"
    unknown.write_bytes(b"a\x00b")
    assert injector._is_binary(unknown)
    monkeypatch.setattr(Path, "read_bytes", MagicMock(side_effect=OSError("denied")))
    assert injector._is_binary(tmp_path / "missing.unknown") is False

    (tmp_path / ".hidden").write_text("secret")
    (tmp_path / "folder").mkdir()
    (tmp_path / "plain.txt").write_text("hello")
    rendered = injector._format_dir_block(tmp_path, ".")
    assert "folder/  dir" in rendered and "plain.txt" in rendered and ".hidden" not in rendered
    broken = MagicMock()
    broken.iterdir.side_effect = OSError("denied")
    assert "error reading directory" in injector._format_dir_block(broken, "broken")


class _HttpContext:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error

    async def __aenter__(self) -> object:
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, *_args: object, **_kwargs: object) -> object:
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_url_formatter_cache_html_text_binary_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        headers={"content-type": "text/html"},
        text="<script>x</script><p>Hello</p><style>bad</style>",
        content=b"html",
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(
        "agenthicc.tools.http.agenthicc_http_client", lambda **_: _HttpContext(response)
    )
    cache: dict[str, str] = {}
    html = await injector._format_url_block("https://example.test", 1.0, session_url_cache=cache)
    assert "Hello" in html and "script" not in html
    assert (
        await injector._format_url_block("https://example.test", 1.0, session_url_cache=cache)
        == html
    )

    response.headers = {"content-type": "application/json"}
    response.text = '{"ok": true}'
    text = await injector._format_url_block("https://json.test", 1.0)
    assert "ok" in text
    response.headers = {"content-type": "image/png"}
    response.content = b"1234"
    binary = await injector._format_url_block("https://binary.test", 1.0)
    assert "4 bytes" in binary

    monkeypatch.setattr(
        "agenthicc.tools.http.agenthicc_http_client",
        lambda **_: _HttpContext(error=RuntimeError("offline")),
    )
    assert "fetch failed" in await injector._format_url_block("https://down.test", 1.0)


@pytest.mark.asyncio
async def test_robots_and_glob_budget_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "c.bin").write_bytes(b"\x00")
    cfg = InjectionConfig(cwd=tmp_path, max_glob_files=1, mention_token_budget=100)
    result = await injector._resolve_glob(_mention(MentionKind.GLOB, "*"), cfg)
    assert "1 file(s)" in result.block and "omitted" in result.block

    monkeypatch.setattr(injector, "_check_robots", AsyncMock(return_value=False))
    blocked = await injector._format_url_block("https://blocked.test", 1.0, respect_robots=True)
    assert "disallows" in blocked
    monkeypatch.undo()

    response = SimpleNamespace(text="User-agent: *\nDisallow: /private", content=b"robots")

    class Robot:
        def parse(self, _lines: list[str]) -> None:
            return None

        def can_fetch(self, _agent: str, url: str) -> bool:
            return "/private" not in url

    monkeypatch.setattr(injector.urllib.robotparser, "RobotFileParser", Robot)
    monkeypatch.setattr(
        "agenthicc.tools.http.agenthicc_http_client", lambda **_: _HttpContext(response)
    )
    assert await injector._check_robots("https://example.test/private") is False
    monkeypatch.setattr(
        "agenthicc.tools.http.agenthicc_http_client",
        lambda **_: _HttpContext(error=RuntimeError("robots unavailable")),
    )
    assert await injector._check_robots("https://example.test/public") is True

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    broken = tmp_path / "broken.txt"
    broken.write_text("broken", encoding="utf-8")
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"\x00binary")
    monkeypatch.setattr(
        injector._glob,
        "glob",
        lambda *_args, **_kwargs: [str(outside), str(binary), str(broken)],
    )
    original_reader = injector._read_file_sync

    def fail_broken(path: Path, max_chars: int) -> tuple[str, int, bool]:
        if path == broken:
            raise OSError("read failed")
        return original_reader(path, max_chars)

    monkeypatch.setattr(injector, "_read_file_sync", fail_broken)
    globbed = await injector._resolve_glob(
        _mention(MentionKind.GLOB, "*.txt"),
        InjectionConfig(cwd=tmp_path, max_glob_files=20, mention_token_budget=100),
    )
    assert "binary skipped" in globbed.block and "omitted" in globbed.block


@pytest.mark.asyncio
async def test_file_cache_modified_missing_and_budget_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("hello")
    cache = MentionCache()
    mention = _mention(MentionKind.FILE, "note.txt", path.resolve())
    first = await resolve_mention(mention, InjectionConfig(cwd=tmp_path), cache, current_turn=1)
    assert first.ok
    cached = await resolve_mention(mention, InjectionConfig(cwd=tmp_path), cache, current_turn=2)
    assert 'cached="true"' in cached.block
    path.write_text("changed")
    changed = await resolve_mention(mention, InjectionConfig(cwd=tmp_path), cache, current_turn=3)
    assert "modified since" in changed.block

    unresolved = await resolve_mention(
        _mention(MentionKind.FILE, "none.txt"), InjectionConfig(cwd=tmp_path)
    )
    assert unresolved.error == "not_found"
    monkeypatch.setattr(injector, "_read_file_sync", MagicMock(side_effect=OSError("denied")))
    failed = await resolve_mention(mention, InjectionConfig(cwd=tmp_path), cache=None)
    assert "could not read" in failed.block
    monkeypatch.undo()

    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a" * 300)
    b.write_text("b" * 300)
    prefix, resolved = await build_context_prefix(
        "@a.txt @b.txt @missing.txt",
        cwd=tmp_path,
        cfg=InjectionConfig(cwd=tmp_path, mention_token_budget=200),
    )
    assert resolved and "budget exceeded" in prefix and "not found" in prefix
