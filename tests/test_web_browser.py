import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from commbadge.browserbase import MAX_CONTENT_CHARS
from commbadge.web_browser import WebBrowser


def snapshot(text="Page body", url="https://example.com"):
    return {
        "url": url,
        "title": "Example",
        "text": text,
        "links": [
            {"url": "https://example.com/video", "title": "Latest video"},
            {"url": "javascript:alert(1)", "title": "Bad link"},
            {"url": "https://example.com/video", "title": "Duplicate"},
        ],
    }


def test_snapshot_excerpts_links_and_stale_link_rejection():
    async def scenario():
        browser = WebBrowser(NS())
        page = browser._store(snapshot("x" * (MAX_CONTENT_CHARS + 10)))
        assert len(page["content"]) == MAX_CONTENT_CHARS and page["more_available"]
        assert len(page["links"]) == 1
        link_id = page["links"][0]["link_id"]
        remaining = await browser.more()
        assert remaining["content"] == "x" * 10 and not remaining["more_available"]
        with pytest.raises(ValueError, match="No more"):
            await browser.more()
        browser.open = AsyncMock(return_value={"status": "ok"})
        await browser.follow(link_id)
        browser.open.assert_awaited_once_with("https://example.com/video")
        browser._store(snapshot(url="https://example.com/video"))
        with pytest.raises(ValueError, match="older page"):
            await browser.follow(link_id)

    asyncio.run(scenario())


def test_redirect_to_private_url_never_becomes_model_evidence():
    browser = WebBrowser(NS())
    with pytest.raises(ValueError):
        browser._store(snapshot(url="http://127.0.0.1"))


def test_cdp_correlates_responses_and_preserves_navigation_events():
    async def scenario():
        browser = WebBrowser(NS())
        browser.target_session = "page-session"
        browser.socket = NS(
            send=AsyncMock(),
            recv=AsyncMock(
                side_effect=[
                    json.dumps(
                        {
                            "method": "Page.lifecycleEvent",
                            "params": {"name": "DOMContentLoaded", "loaderId": "loader"},
                        }
                    ),
                    json.dumps({"id": 123, "result": {}}),
                    json.dumps({"id": 1, "result": {"value": "matched"}}),
                ]
            ),
        )
        assert await browser._rpc("Page.enable") == {"value": "matched"}
        assert browser.loaded == {"loader"}
        assert json.loads(browser.socket.send.call_args.args[0])["sessionId"] == "page-session"

    asyncio.run(scenario())


def test_failed_connection_redacts_credentials_and_releases_session():
    async def scenario():
        client = NS(_post=Mock())
        browser = WebBrowser(client)
        browser.session_id = "session-123"
        browser.start = AsyncMock(side_effect=RuntimeError("wss://secret-api-key"))
        with pytest.raises(RuntimeError) as error:
            await browser.open("https://example.com")
        assert "secret-api-key" not in str(error.value)
        client._post.assert_called_once_with("sessions/session-123", {"status": "REQUEST_RELEASE"})

    asyncio.run(scenario())


def test_cancellation_during_creation_can_still_release_remote_session():
    async def scenario():
        client = NS(_post=Mock())
        browser = WebBrowser(client)
        ready = asyncio.Event()

        async def create():
            await ready.wait()
            return {"id": "created-session"}

        browser.opening = asyncio.create_task(create())
        cleanup = asyncio.create_task(browser.close())
        await asyncio.sleep(0)
        assert not cleanup.done()
        ready.set()
        await cleanup
        client._post.assert_called_once_with(
            "sessions/created-session", {"status": "REQUEST_RELEASE"}
        )
        await browser.close()
        assert client._post.call_count == 1

    asyncio.run(scenario())
