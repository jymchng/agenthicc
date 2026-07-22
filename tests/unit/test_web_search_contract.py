"""Detailed contract tests for the web-search skill."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from lauren_ai._tools import TOOL_META
import pytest

from agenthicc.skills.web_search import FetchPageTool, SearchWebTool
from agenthicc.tools import AgenthiccToolExecutor
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities

pytestmark = pytest.mark.unit


def _http_client(response: MagicMock) -> tuple[MagicMock, AsyncMock]:
    factory = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, client


def test_tools_use_lauren_metadata_and_capabilities() -> None:
    search_meta = getattr(SearchWebTool, TOOL_META)
    fetch_meta = getattr(FetchPageTool, TOOL_META)

    assert search_meta.name == "search_web"
    assert fetch_meta.name == "fetch_page"
    assert search_meta.parameters["input_schema"]["required"] == ["query"]
    assert fetch_meta.parameters["input_schema"]["required"] == ["url"]
    assert get_tool_capabilities(SearchWebTool) == frozenset(
        {ToolCapability.NETWORK, ToolCapability.SEARCH}
    )
    assert get_tool_capabilities(FetchPageTool) == frozenset(
        {ToolCapability.NETWORK, ToolCapability.READ}
    )


@pytest.mark.asyncio
async def test_native_search_instance_runs_through_lauren_adapter() -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"web": {"results": []}}
    factory, _ = _http_client(response)

    with patch("agenthicc.tools.http.agenthicc_http_client", factory):
        result = await AgenthiccToolExecutor([SearchWebTool("secret")]).execute(
            "search_web",
            {"query": "native"},
            "native-1",
        )

    assert result.ok is True
    assert result.value == {"ok": True, "query": "native", "count": 0, "results": []}


@pytest.mark.asyncio
async def test_native_fetch_instance_runs_through_lauren_adapter() -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.status_code = 200
    response.text = "<main>native page</main>"
    factory, _ = _http_client(response)

    with patch("agenthicc.tools.http.agenthicc_http_client", factory):
        result = await AgenthiccToolExecutor([FetchPageTool()]).execute(
            "fetch_page",
            {"url": "https://example.com"},
            "native-2",
        )

    assert result.ok is True
    assert result.value == {
        "ok": True,
        "url": "https://example.com",
        "status_code": 200,
        "content": "native page",
    }


@pytest.mark.asyncio
async def test_search_sends_trimmed_query_headers_and_bounded_count() -> None:
    response = MagicMock()
    response.json.return_value = {
        "web": {
            "results": [
                {"title": "One", "url": "https://one.example", "description": "First"},
                {"title": "Two", "url": "https://two.example", "snippet": "Second"},
            ]
        }
    }
    response.raise_for_status.return_value = None
    factory, client = _http_client(response)

    with patch("agenthicc.tools.http.agenthicc_http_client", factory):
        result = await SearchWebTool("secret", engine="BRAVE").run("  cats and dogs  ", 99)

    assert result == {
        "ok": True,
        "query": "cats and dogs",
        "count": 2,
        "results": [
            {"title": "One", "url": "https://one.example", "description": "First"},
            {"title": "Two", "url": "https://two.example", "description": "Second"},
        ],
    }
    client.get.assert_awaited_once()
    request = client.get.await_args
    assert request.args[0] == "https://api.search.brave.com/res/v1/web/search"
    assert request.kwargs["params"] == {"q": "cats and dogs", "count": 20}
    assert request.kwargs["headers"]["X-Subscription-Token"] == "secret"


@pytest.mark.asyncio
async def test_search_handles_missing_or_malformed_result_payloads() -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"web": {"results": [{"title": "valid"}, "invalid"]}}
    factory, _ = _http_client(response)

    with patch("agenthicc.tools.http.agenthicc_http_client", factory):
        result = await SearchWebTool("secret").run("test")

    assert result["count"] == 1
    assert result["results"] == [{"title": "valid", "url": "", "description": ""}]


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"web": []}, {"web": {"results": None}}],
)
@pytest.mark.asyncio
async def test_search_handles_missing_result_sections(payload: object) -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    factory, _ = _http_client(response)

    with patch("agenthicc.tools.http.agenthicc_http_client", factory):
        result = await SearchWebTool("secret").run("test")

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["results"] == []


@pytest.mark.asyncio
async def test_search_validation_does_not_make_network_requests() -> None:
    with patch("agenthicc.tools.http.agenthicc_http_client") as factory:
        tool = SearchWebTool("")
        missing_query = await tool.run("")
        missing_key = await tool.run("test")
        invalid_count = await SearchWebTool("key").run("test", "many")
        nonpositive_count = await SearchWebTool("key").run("test", 0)
        invalid_engine = await SearchWebTool("key", engine="bing").run("test")

    assert missing_query == {"ok": False, "error": "query is required"}
    assert missing_key["ok"] is False
    assert invalid_count == {"ok": False, "error": "n must be an integer"}
    assert nonpositive_count == {"ok": False, "error": "n must be at least 1"}
    assert invalid_engine == {"ok": False, "error": "Unknown search engine: bing"}
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_page_extracts_visible_text_and_preserves_response_metadata() -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.status_code = 200
    response.text = (
        "<html><head><style>hidden style</style></head><body>"
        "<h1>Hello&nbsp;world</h1><br/><script>hidden script</script>"
        "<p>Text &amp; more</p></body></html>"
    )
    factory, client = _http_client(response)

    with patch("agenthicc.tools.http.agenthicc_http_client", factory):
        result = await FetchPageTool().run(" https://example.com/page ")

    assert result["ok"] is True
    assert result["url"] == "https://example.com/page"
    assert result["status_code"] == 200
    content = result["content"]
    assert isinstance(content, str)
    assert "Hello world" in content
    assert "Text & more" in content
    assert "hidden style" not in content
    assert "hidden script" not in content
    client.get.assert_awaited_once_with("https://example.com/page")


@pytest.mark.asyncio
async def test_fetch_page_validation_is_local() -> None:
    with patch("agenthicc.tools.http.agenthicc_http_client") as factory:
        missing_url = await FetchPageTool().run("")

    assert missing_url == {"ok": False, "error": "url is required"}
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_page_propagates_logic_errors() -> None:
    with patch("agenthicc.tools.http.agenthicc_http_client") as factory:
        client = AsyncMock()
        client.get.side_effect = ValueError("invalid response state")
        factory.return_value.__aenter__ = AsyncMock(return_value=client)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(ValueError, match="invalid response state"):
            await FetchPageTool().run("https://example.com")
