"""TOOLR-05: fetch_page under the session's destination policy, bounded."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from agenthicc.skills.web_search import MAX_PAGE_CHARS, MAX_RESPONSE_BYTES, FetchPageTool
from agenthicc.tools import http as http_tools
from agenthicc.tools.http import HttpDestinationPolicy

pytestmark = pytest.mark.unit

Handler = Callable[[httpx.Request], httpx.Response]


def _resolver(addresses: dict[str, str]) -> Callable[[str], object]:
    async def resolve(host: str) -> list[str]:
        return [addresses.get(host, "93.184.216.34")]

    return resolve


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Route every client through a MockTransport; collect the requests made."""
    requests: list[httpx.Request] = []
    handler: dict[str, Handler] = {}
    original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler["fn"](request)

        return original(transport=httpx.MockTransport(record), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    served.handler = handler  # type: ignore[attr-defined]
    yield requests
    monkeypatch.setattr(http_tools, "_network_policy", HttpDestinationPolicy())


def _serve(handler: Handler) -> None:
    served.handler["fn"] = handler  # type: ignore[attr-defined]


def _policy(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> None:
    resolver = kwargs.pop("resolver", _resolver({}))
    monkeypatch.setattr(
        http_tools,
        "_network_policy",
        HttpDestinationPolicy(resolver=resolver, **kwargs),  # type: ignore[arg-type]
    )


async def test_an_empty_allow_list_refuses_without_connecting(
    served: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch)
    _serve(lambda request: httpx.Response(200, text="never"))

    result = await FetchPageTool().run("https://example.com")

    assert result["ok"] is False and result["error_code"] == "outside_allow_list"
    assert "network_allow_list" in result["error"]
    assert served == []


async def test_an_allow_listed_page_is_fetched_as_text_with_links(
    served: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, allowed_domains=("example.com",))
    _serve(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><style>hidden</style></head><body><h1>Hello&nbsp;world</h1>"
                "<script>hidden</script><a href='/docs'>Docs</a><p>Text &amp; more</p></body>"
            ),
        )
    )

    result = await FetchPageTool().run("https://www.example.com/page#section")

    assert result["ok"] is True, result
    assert "Hello world" in result["content"] and "Text & more" in result["content"]
    assert "hidden" not in result["content"]
    assert result["links"] == ["https://www.example.com/docs"]


async def test_a_redirect_to_cloud_metadata_is_refused_at_the_hop(
    served: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, allowed_domains=("example.com",))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        return httpx.Response(200, text="secret credentials")

    _serve(handler)

    result = await FetchPageTool().run("https://example.com/")

    assert result["ok"] is False and result["error_code"] == "outside_allow_list"
    assert [request.url.host for request in served] == ["example.com"]


async def test_a_host_resolving_to_loopback_is_refused(
    served: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, allow_all=True, resolver=_resolver({"rebind.test": "127.0.0.1"}))
    _serve(lambda request: httpx.Response(200, text="internal"))

    result = await FetchPageTool().run("http://rebind.test/")

    assert result["error_code"] == "outside_allow_list" and served == []


async def test_a_huge_body_and_long_text_are_bounded_and_paged(
    served: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, allow_all=True)
    _serve(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"x" * (MAX_RESPONSE_BYTES * 4)
        )
    )

    first = await FetchPageTool().run("https://big.example/")

    assert first["ok"] is True and first["truncated"] is True
    assert first["body_truncated_at_bytes"] == MAX_RESPONSE_BYTES
    assert len(first["content"]) == MAX_PAGE_CHARS
    assert first["total_chars"] == MAX_RESPONSE_BYTES
    second = await FetchPageTool().run("https://big.example/", offset=first["next_offset"])
    assert second["offset"] == MAX_PAGE_CHARS and len(second["content"]) == MAX_PAGE_CHARS


async def test_an_http_error_is_an_envelope_not_an_exception(
    served: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, allow_all=True)
    _serve(lambda request: httpx.Response(404, text="missing"))

    missing = await FetchPageTool().run("https://example.com/nope")
    _serve(lambda request: httpx.Response(503, text="later"))
    busy = await FetchPageTool().run("https://example.com/busy")

    assert missing == {
        "ok": False,
        "error_code": "http_status",
        "status_code": 404,
        "url": "https://example.com/nope",
        "error": "HTTP 404",
        "retryable": False,
    }
    assert busy["retryable"] is True


async def test_binary_content_is_reported_not_decoded(
    served: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(monkeypatch, allow_all=True)
    _serve(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7 binary"
        )
    )

    result = await FetchPageTool().run("https://example.com/doc.pdf")

    assert result["error_code"] == "undecodable" and result["reason"] == "unsupported_content"
    assert result["content_type"] == "application/pdf" and result["bytes"] == 15


async def test_a_non_http_scheme_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _policy(monkeypatch, allow_all=True)
    result = await FetchPageTool().run("file:///etc/passwd")
    assert result["ok"] is False and result["error_code"] == "outside_allow_list"


def test_tool_code_never_builds_its_own_http_client() -> None:
    """Only tools/http.py may construct httpx.AsyncClient for tool traffic."""
    from pathlib import Path

    import agenthicc

    root = Path(agenthicc.__file__).parent
    offenders = [
        str(path.relative_to(root))
        for folder in ("tools", "skills", "workflows")
        for path in (root / folder).rglob("*.py")
        if "httpx.AsyncClient(" in path.read_text(encoding="utf-8")
        and path.relative_to(root).as_posix() != "tools/http.py"
    ]
    assert offenders == []
