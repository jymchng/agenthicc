"""Lauren-ai web-search tools backed by Brave Search and HTTP page fetching.

The skill returns configured instances of native ``@tool()`` classes. All
network I/O still goes through Agenthicc's shared HTTP factory so timeout
configuration and network-error classification remain centralized.
"""

import re
from html.parser import HTMLParser

from lauren_ai import tool

from agenthicc.tools.capabilities import tool_network_read, tool_network_search

__all__ = ["FetchPageTool", "SearchWebTool"]

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_MAX_SEARCH_RESULTS = 20
_IGNORED_HTML_TAGS = frozenset({"script", "style", "noscript", "template"})


class _VisibleTextParser(HTMLParser):
    """Extract visible text while ignoring executable and template markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _IGNORED_HTML_TAGS:
            self._ignored_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        return None

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _IGNORED_HTML_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def _visible_text(html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(html)
    parser.close()
    return parser.text()


def _error_result(exc: BaseException) -> dict[str, object]:
    return {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "recoverable": True,
    }


def _is_network_error(exc: BaseException) -> bool:
    """Use the shared classifier plus common transport-message fallbacks."""
    from agenthicc.tools import http as http_tools  # noqa: PLC0415

    if http_tools.is_network_error(exc):
        return True
    message = str(exc).lower()
    return any(
        marker in message for marker in ("connection refused", "connection reset", "timed out")
    )


def _search_results(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    web = payload.get("web")
    if not isinstance(web, dict):
        return []
    raw_results = web.get("results")
    if not isinstance(raw_results, list):
        return []

    results: list[dict[str, object]] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            continue
        results.append(
            {
                "title": str(raw_result.get("title", "")),
                "url": str(raw_result.get("url", "")),
                "description": str(raw_result.get("description", raw_result.get("snippet", ""))),
            }
        )
    return results


@tool_network_search
@tool(
    name="search_web",
    description="Search the web and return ranked Brave Search results.",
)
class SearchWebTool:
    """Search the web with the Brave Search API."""

    name = "search_web"

    def __init__(
        self,
        api_key: str,
        engine: str = "brave",
        max_results: int = 5,
    ) -> None:
        self._api_key = api_key.strip()
        self._engine = engine.strip().lower()
        self._max_results = max(1, min(max_results, _MAX_SEARCH_RESULTS))

    async def run(self, query: str, n: int = 5) -> dict[str, object]:
        """Validate arguments and execute a Brave web search.

        Args:
            query: Search terms.
            n: Maximum number of results, from 1 to 20.
        """
        query = query.strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        if not self._api_key:
            return {"ok": False, "error": "Brave Search API key is required"}
        if self._engine != "brave":
            return {"ok": False, "error": f"Unknown search engine: {self._engine}"}

        try:
            count = self._max_results if n == 5 else int(n)
        except (TypeError, ValueError):
            return {"ok": False, "error": "n must be an integer"}
        if count < 1:
            return {"ok": False, "error": "n must be at least 1"}
        return await self._brave_search(query, min(count, _MAX_SEARCH_RESULTS))

    async def _brave_search(self, query: str, count: int) -> dict[str, object]:
        from agenthicc.tools import http as http_tools  # noqa: PLC0415

        try:
            async with http_tools.agenthicc_http_client() as client:
                response = await client.get(
                    _BRAVE_SEARCH_URL,
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self._api_key,
                    },
                    params={"q": query, "count": count},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            if _is_network_error(exc):
                return _error_result(exc)
            raise

        results = _search_results(payload)
        return {
            "ok": True,
            "query": query,
            "count": len(results),
            "results": results,
        }


@tool_network_read
@tool(
    name="fetch_page",
    description="Fetch a web page and extract its visible text.",
)
class FetchPageTool:
    """Fetch a URL and return readable text extracted from its HTML."""

    name = "fetch_page"

    async def run(self, url: str) -> dict[str, object]:
        """Fetch and parse one page, containing only recoverable network errors.

        Args:
            url: Page URL to fetch.
        """
        url = url.strip()
        if not url:
            return {"ok": False, "error": "url is required"}

        from agenthicc.tools import http as http_tools  # noqa: PLC0415

        try:
            async with http_tools.agenthicc_http_client() as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
                status_code = response.status_code
        except Exception as exc:  # noqa: BLE001
            if _is_network_error(exc):
                return _error_result(exc)
            raise

        return {
            "ok": True,
            "url": url,
            "status_code": status_code,
            "content": _visible_text(html),
        }
