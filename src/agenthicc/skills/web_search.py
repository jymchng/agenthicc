"""Lauren-ai web-search tools backed by Brave Search and HTTP page fetching.

The skill returns configured instances of native ``@tool()`` classes. All
network I/O still goes through Agenthicc's shared HTTP factory so timeout
configuration and network-error classification remain centralized.
"""

import asyncio
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from lauren_ai import tool

from agenthicc.tools.capabilities import tool_network_read, tool_network_search

__all__ = ["FetchPageTool", "SearchWebTool"]

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_MAX_SEARCH_RESULTS = 20
_IGNORED_HTML_TAGS = frozenset({"script", "style", "noscript", "template"})
#: A response body is read up to this many bytes, then cut (TOOLR-05).
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
#: Page text returned per call; ``offset`` pages through the rest.
MAX_PAGE_CHARS = 40_000
MAX_PAGE_LINKS = 50
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript")
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class _VisibleTextParser(HTMLParser):
    """Extract visible text while ignoring executable and template markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _IGNORED_HTML_TAGS:
            self._ignored_depth += 1
        elif tag.lower() == "a" and len(self.links) < MAX_PAGE_LINKS:
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.links.append(href)

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


def _page_text(html: str, base_url: str) -> tuple[str, list[str]]:
    """Visible text and absolute links of an HTML page (CPU-bound: run in a thread)."""
    parser = _VisibleTextParser()
    parser.feed(html)
    parser.close()
    links = list(dict.fromkeys(urljoin(base_url, href) for href in parser.links))
    return parser.text(), links


def _error_result(exc: BaseException) -> dict[str, object]:
    """The envelope for a failed request: an HTTP status or a network error."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return {
            "ok": False,
            "error_code": "http_status",
            "status_code": status,
            "error": f"HTTP {status}",
            "retryable": status in {408, 425, 429} or status >= 500,
        }
    return {
        "ok": False,
        "error_code": "network",
        "error": f"{type(exc).__name__}: {exc}",
        "retryable": True,
    }


def _is_network_error(exc: BaseException) -> bool:
    """Use the shared classifier plus common transport-message fallbacks."""
    from agenthicc.tools import http as http_tools

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
    """Search the web with the Brave Search API.

    Args:
        query: Search terms.
        n: Maximum number of results, from 1 to 20.
    """

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
        from agenthicc.tools import http as http_tools

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
    """Fetch a URL and return readable text extracted from its HTML.

    Args:
        url: Page URL to fetch (http or https).
        offset: Character offset into the page text, to read past the first page.
    """

    name = "fetch_page"

    async def run(self, url: str, offset: int = 0) -> dict[str, object]:
        """Fetch one page and return its readable text, bounded and paged.

        Only destinations the session's network policy allows are fetched
        (``[security] network_allow_list``); redirects are re-checked.

        Args:
            url: Page URL to fetch (http or https).
            offset: Character offset into the page text, to read past the first page.
        """
        url = url.strip()
        if not url:
            return {"ok": False, "error_code": "invalid_argument", "error": "url is required"}
        if offset < 0:
            return {"ok": False, "error_code": "invalid_argument", "error": "offset must be >= 0"}

        from agenthicc.tools import http as http_tools

        policy = http_tools.network_policy()
        body = bytearray()
        cut = False
        try:
            await policy.check(url)
            async with http_tools.agenthicc_http_client(policy=policy) as client:
                async with client.stream("GET", url) as response:
                    status_code = response.status_code
                    final_url = str(response.url)
                    content_type = (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                    )
                    charset = response.charset_encoding or "utf-8"
                    if status_code >= 400:
                        return {
                            "ok": False,
                            "error_code": "http_status",
                            "status_code": status_code,
                            "url": final_url,
                            "error": f"HTTP {status_code}",
                            "retryable": status_code in {408, 425, 429} or status_code >= 500,
                        }
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_RESPONSE_BYTES:
                            del body[MAX_RESPONSE_BYTES:]
                            cut = True
                            break
        except http_tools.HttpPolicyError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "retryable": exc.code == "network",
            }
        except Exception as exc:  # noqa: BLE001
            if _is_network_error(exc):
                return _error_result(exc)
            raise

        if content_type and not (
            content_type in _HTML_TYPES
            or content_type.startswith(_TEXT_TYPES)
            or content_type.endswith(("+json", "+xml"))
        ):
            return {
                "ok": False,
                "error_code": "undecodable",
                "reason": "unsupported_content",
                "error": f"{content_type} is not text; fetch_page returns text pages only",
                "content_type": content_type,
                "bytes": len(body),
                "url": final_url,
            }
        try:
            decoded = bytes(body).decode(charset, errors="replace")
        except LookupError:
            decoded = bytes(body).decode("utf-8", errors="replace")
        links: list[str] = []
        if not content_type or content_type in _HTML_TYPES:
            text, links = await asyncio.to_thread(_page_text, decoded, final_url)
        else:
            text = decoded
        page = text[offset : offset + MAX_PAGE_CHARS]
        result: dict[str, object] = {
            "ok": True,
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "content_type": content_type,
            "content": page,
            "total_chars": len(text),
            "offset": offset,
        }
        if offset + MAX_PAGE_CHARS < len(text):
            result["next_offset"] = offset + MAX_PAGE_CHARS
        if cut or "next_offset" in result:
            result["truncated"] = True
        if cut:
            result["body_truncated_at_bytes"] = MAX_RESPONSE_BYTES
        if links:
            result["links"] = links
        return result
