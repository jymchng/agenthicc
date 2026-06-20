"""Web search and page fetch tools for the web_search skill (PRD-18)."""
from __future__ import annotations

import re
from typing import Any

from agenthicc.tools.base import Tool

__all__ = ["FetchPageTool", "SearchWebTool"]


class SearchWebTool(Tool):
    name = "search_web"
    description = "Search the web using a configured search engine."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "n": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str = "", engine: str = "brave", max_results: int = 5) -> None:
        self._api_key = api_key
        self._engine = engine
        self._max_results = max_results

    async def execute(self, args: dict, context: dict) -> Any:
        if not self._api_key:
            return {"ok": False, "error": "No API key configured for web search"}
        query = args["query"]
        n = int(args.get("n", self._max_results))
        if self._engine == "brave":
            return await self._brave_search(query, n)
        return {"ok": False, "error": f"Unknown search engine: {self._engine!r}"}

    async def _brave_search(self, query: str, n: int) -> dict:
        from agenthicc.tools.http import agenthicc_http_client, is_network_error  # noqa: PLC0415
        try:
            async with agenthicc_http_client() as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json",
                             "X-Subscription-Token": self._api_key},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            if is_network_error(exc):
                return {"ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "recoverable": True}
            raise
        results = [
            {"title": item.get("title", ""), "url": item.get("url", ""),
             "description": item.get("description", "")}
            for item in data.get("web", {}).get("results", [])[:n]
        ]
        return {"results": results, "count": len(results)}


class FetchPageTool(Tool):
    name = "fetch_page"
    description = "Fetch and return the text content of a web page."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "timeout": {"type": "number", "default": 15.0},
        },
        "required": ["url"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        from agenthicc.tools.http import agenthicc_http_client  # noqa: PLC0415
        url = args["url"]
        timeout = float(args.get("timeout", 15.0))
        try:
            async with agenthicc_http_client(
                timeout=timeout, follow_redirects=True
            ) as client:
                r = await client.get(url, headers={"User-Agent": "agenthicc/1.0"})
                r.raise_for_status()
            text = re.sub(r"<[^>]+>", " ", r.text)
            text = re.sub(r"\s+", " ", text).strip()
            return {"ok": True, "url": url, "content": text[:8000], "status_code": r.status_code}
        except Exception as exc:
            # Always include the exception class name so the agent can identify
            # the failure type (e.g. ReadTimeout vs HTTPStatusError).
            return {"ok": False, "url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                    "recoverable": True}
