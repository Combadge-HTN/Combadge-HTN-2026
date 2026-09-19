import asyncio
import io
import json
import urllib.error
from unittest.mock import AsyncMock, Mock, patch

import pytest

from commbadge.browserbase import (
    MAX_CONTENT_CHARS,
    MAX_REQUESTS,
    MAX_RESPONSE_BYTES,
    BrowserbaseClient,
    NoRedirects,
    public_url,
)


def response(data):
    return io.BytesIO(json.dumps(data).encode())


def test_search_posts_documented_contract_and_returns_bounded_sources():
    data = {
        "results": [
            {
                "url": f"https://example.com/{i}",
                "title": "x" * 400,
                "publishedDate": "2026-09-19",
                "secret_extra": "not forwarded",
            }
            for i in range(10)
        ]
    }
    opener = Mock()
    opener.open.return_value = response(data)
    with patch("urllib.request.build_opener", return_value=opener):
        result = asyncio.run(BrowserbaseClient("private-key").search("  current weather  "))
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.browserbase.com/v1/search"
    assert request.get_header("X-bb-api-key") == "private-key"
    assert json.loads(request.data) == {"query": "current weather", "numResults": 5}
    assert result["status"] == "ok" and len(result["sources"]) == 5
    assert len(result["sources"][0]["title"]) == 300
    assert result["sources"][0]["published_date"] == "2026-09-19"
    assert "retrieved_at" in result
    assert "private-key" not in json.dumps(result)
    assert "secret_extra" not in json.dumps(result)


def test_fetch_requests_markdown_and_bounds_content():
    client = BrowserbaseClient("key")
    client._request = AsyncMock(
        return_value={"statusCode": 200, "content": "a" * (MAX_CONTENT_CHARS + 1)}
    )
    result = asyncio.run(client.read_page("https://example.com/article"))
    client._request.assert_awaited_once_with(
        "fetch",
        {"url": "https://example.com/article", "format": "markdown", "allowRedirects": True},
    )
    assert len(result["content"]) == MAX_CONTENT_CHARS and result["truncated"]
    assert result["url"] == "https://example.com/article"


@pytest.mark.parametrize("data", [{}, {"results": "bad"}, {"results": [None]}])
def test_bad_search_response_is_not_reported_as_no_matches(data):
    client = BrowserbaseClient("key")
    client._request = AsyncMock(return_value=data)
    with pytest.raises(RuntimeError):
        asyncio.run(client.search("query"))


def test_empty_search_is_explicit():
    client = BrowserbaseClient("key")
    client._request = AsyncMock(return_value={"results": []})
    assert asyncio.run(client.search("query"))["status"] == "no_matches"


@pytest.mark.parametrize(
    "data",
    [
        {"statusCode": 403, "content": "Access denied"},
        {"statusCode": 302, "content": "Redirect"},
        {"statusCode": 200, "content": ""},
        {"statusCode": 200, "content": {}},
        {"content": "missing status"},
    ],
)
def test_fetch_failures_do_not_become_evidence(data):
    client = BrowserbaseClient("key")
    client._request = AsyncMock(return_value=data)
    with pytest.raises(RuntimeError):
        asyncio.run(client.read_page("https://example.com"))


@pytest.mark.parametrize("query", ["", "  ", "a" * 201, None, 7])
def test_bad_query_never_sends_request(query):
    client = BrowserbaseClient("key")
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.search(query))
    client._request.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://localhost/",
        "http://localhost./",
        "http://127.0.0.1",
        "https://10.0.0.1",
        "https://[::1]/",
        "http://169.254.169.254/",
        "https://user:password@example.com",
        "https://example.com:8000",
        "http://service.local",
        "https://example.com\n",
        "https://example.com\\@localhost",
        "http://2130706433",
        "http://127.1",
        "http://internal",
        None,
        1,
    ],
)
def test_invalid_url_never_sends_request(url):
    client = BrowserbaseClient("key")
    client._request = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.read_page(url))
    client._request.assert_not_called()


@pytest.mark.parametrize("url", ["https://example.com/x?q=hello", "http://example.com"])
def test_public_urls_are_allowed(url):
    assert public_url(url) == url


@pytest.mark.parametrize("code", [401, 402, 403, 429, 500, 302])
def test_http_errors_are_actionable_and_hide_secrets(code):
    error = urllib.error.HTTPError("https://private-key", code, "private-key", {}, None)
    opener = Mock()
    opener.open.side_effect = error
    with (
        patch("urllib.request.build_opener", return_value=opener),
        pytest.raises(RuntimeError) as e,
    ):
        asyncio.run(BrowserbaseClient("private-key").search("query"))
    assert str(code) in str(e.value) and "private-key" not in str(e.value)


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("private-key"), TimeoutError("private-key")]
)
def test_network_errors_hide_secrets(error):
    opener = Mock()
    opener.open.side_effect = error
    with (
        patch("urllib.request.build_opener", return_value=opener),
        pytest.raises(RuntimeError) as e,
    ):
        asyncio.run(BrowserbaseClient("private-key").search("query"))
    assert "private-key" not in str(e.value)


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"\xff", b"a" * (MAX_RESPONSE_BYTES + 1)])
def test_invalid_or_oversized_api_response_is_rejected(raw):
    opener = Mock()
    opener.open.return_value = io.BytesIO(raw)
    with patch("urllib.request.build_opener", return_value=opener), pytest.raises(RuntimeError):
        asyncio.run(BrowserbaseClient("key").search("query"))


def test_api_redirects_are_not_followed_with_credentials():
    assert NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.com") is None


def test_session_budget_stops_more_network_requests():
    client = BrowserbaseClient("key")
    client.requests = MAX_REQUESTS
    with patch.object(client, "_post") as post, pytest.raises(RuntimeError, match="limit"):
        asyncio.run(client.search("query"))
    post.assert_not_called()


def test_slow_request_does_not_block_event_loop():
    import threading

    released = threading.Event()
    started = threading.Event()
    client = BrowserbaseClient("key")

    def slow_post(*_):
        started.set()
        assert released.wait(2)
        return {"results": []}

    async def scenario():
        task = asyncio.create_task(client.search("query"))
        try:
            async with asyncio.timeout(1):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                released.set()
                return await task
        finally:
            released.set()
            await task

    with patch.object(client, "_post", side_effect=slow_post):
        assert asyncio.run(scenario())["status"] == "no_matches"
