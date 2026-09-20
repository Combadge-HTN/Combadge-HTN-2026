import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from combadge.web_lookup import MAX_LOOKUP_ACTIONS, Lookup, WebLookup


def client():
    return NS(
        search=AsyncMock(return_value={"status": "ok", "sources": []}),
        read_page=AsyncMock(return_value={"status": "ok", "content": "a fact"}),
        browse=AsyncMock(return_value={"status": "ok", "url": "https://example.com"}),
    )


def test_two_searches_then_navigation_and_new_question():
    async def scenario():
        web = client()
        lookup = WebLookup(web, lambda _: None)
        for query in ("weather", "forecast"):
            await lookup.execute("search_web", {"query": query}, "question1")
        with pytest.raises(RuntimeError, match="Search limit"):
            await lookup.execute("search_web", {"query": "weather today"}, "question1")
        await lookup.execute("browse_web_page", {"url": "https://example.com"}, "question1")
        await lookup.execute("search_web", {"query": "new question"}, "question2")
        assert web.search.await_count == 3 and web.browse.await_count == 1

    asyncio.run(scenario())


def test_cached_search_still_consumes_action_budget_and_stops_loop():
    async def scenario():
        web = client()
        lookup = WebLookup(web, lambda _: None)
        for _ in range(MAX_LOOKUP_ACTIONS):
            result = await lookup.execute("search_web", {"query": "Weather TODAY"}, "q")
        assert result["cached"]
        assert result["lookup_budget"]["actions_remaining"] == 0
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await lookup.execute("search_web", {"query": "weather today"}, "q")
        assert web.search.await_count == 1

    asyncio.run(scenario())


def test_deadline_cannot_be_reset_by_another_response_in_same_delegation(monkeypatch):
    async def scenario():
        web = client()
        lookup = WebLookup(web, lambda _: None)
        lookup.lookups["q"] = Lookup(started=0)
        monkeypatch.setattr("combadge.web_lookup.time.monotonic", lambda: 61)
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await lookup.execute("search_web", {"query": "query"}, "q")
        web.search.assert_not_called()

    asyncio.run(scenario())


def test_page_cache_preserves_url_case_and_reuses_exact_url_only():
    async def scenario():
        web = client()
        lookup = WebLookup(web, lambda _: None)
        for url in ("https://example.com/A", "https://example.com/A", "https://example.com/a"):
            await lookup.execute("read_web_page", {"url": url}, "q")
        assert web.read_page.await_count == 2

    asyncio.run(scenario())


def test_navigation_snapshots_are_not_cached():
    async def scenario():
        web = client()
        lookup = WebLookup(web, lambda _: None)
        for _ in range(2):
            await lookup.execute("browse_web_page", {"url": "https://example.com"}, "q")
        assert web.browse.await_count == 2

    asyncio.run(scenario())


def test_terminal_reports_candidate_sources_separately_from_successful_page_reads():
    async def scenario():
        web, logs = client(), []
        web.search.return_value["sources"] = [{"url": "https://example.com", "title": "Title\nX"}]
        lookup = WebLookup(web, logs.append)
        await lookup.execute("search_web", {"query": "test"}, "q")
        assert "not yet read" in "".join(logs) and "Title X" in "".join(logs)
        web.browse.side_effect = RuntimeError("blocked")
        with pytest.raises(RuntimeError):
            await lookup.execute("browse_web_page", {"url": "https://example.com"}, "q")
        assert "Read source" not in "".join(logs)

    asyncio.run(scenario())
