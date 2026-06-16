"""Unit tests for the skills system (PRD-18)."""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agenthicc.skills import SkillRegistry, _BUILTIN
from agenthicc.skills.web_search import SearchWebTool, FetchPageTool

pytestmark = pytest.mark.unit


class TestSkillRegistry:
    def test_unknown_skill_false(self):
        assert not SkillRegistry().load("totally_nonexistent")

    def test_web_search_missing_api_key_fails(self):
        assert not SkillRegistry().load("web_search", config={})

    def test_web_search_with_key_loads(self):
        reg = SkillRegistry()
        assert reg.load("web_search", config={"api_key": "test_key"})
        names = {t.name for t in reg.tools}
        assert "search_web" in names and "fetch_page" in names

    def test_docker_loads_without_config(self):
        reg = SkillRegistry()
        result = reg.load("docker")
        assert result is True   # docker skill has empty tools list but loads

    def test_system_prompt_empty_no_skills(self):
        assert SkillRegistry().system_prompt_suffix == ""

    def test_system_prompt_set_after_skill(self):
        reg = SkillRegistry()
        reg.load("web_search", config={"api_key": "k"})
        assert len(reg.system_prompt_suffix) > 0

    def test_load_all(self):
        reg = SkillRegistry()
        reg.load_all(["web_search", "docker"], {"web_search": {"api_key": "k"}})
        names = {t.name for t in reg.tools}
        assert "search_web" in names

    def test_broken_skill_no_crash(self):
        from agenthicc.skills import SkillBundle
        class _Broken(SkillBundle):
            @property
            def name(self): return "_broken_99"
            def tools(self, config): raise RuntimeError("broken")
        _BUILTIN["_broken_99"] = _Broken
        try:
            reg = SkillRegistry()
            assert not reg.load("_broken_99")
        finally:
            del _BUILTIN["_broken_99"]


class TestSearchWebTool:
    async def test_no_api_key_returns_error(self):
        t = SearchWebTool(api_key="")
        r = await t.execute({"query": "test"}, {})
        assert r["ok"] is False

    async def test_brave_search_mocked(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"web": {"results": [
            {"title": "T1", "url": "https://ex.com", "description": "D1"}
        ]}}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            t = SearchWebTool(api_key="key", engine="brave")
            r = await t.execute({"query": "hello", "n": 1}, {})
        assert r["count"] == 1
        assert r["results"][0]["title"] == "T1"

    async def test_unknown_engine_error(self):
        t = SearchWebTool(api_key="k", engine="unknown_engine")
        r = await t.execute({"query": "x"}, {})
        assert r["ok"] is False


class TestFetchPageTool:
    async def test_strips_html_and_returns_text(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<html><body><h1>Hello</h1><p>World</p></body></html>"
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            r = await FetchPageTool().execute({"url": "https://example.com"}, {})
        assert r["ok"] is True
        assert "<html>" not in r["content"]
        assert "Hello" in r["content"] or "World" in r["content"]

    async def test_network_error_returns_ok_false(self):
        with patch("httpx.AsyncClient", side_effect=Exception("connection refused")):
            r = await FetchPageTool().execute({"url": "https://bad.example"}, {})
        assert r["ok"] is False
