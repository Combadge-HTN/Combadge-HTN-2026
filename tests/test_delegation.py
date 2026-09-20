import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from commbadge.delegation import SnapshotDelegation
from commbadge.shopify import ShoppingSession
from commbadge.vision import MAX_SESSION_IMAGE_BYTES, ImageInput


def event(kind, **fields):
    return NS(delegation_id="d1", event=NS(type=kind, **fields))


def call(call_id="c1", name="capture_snapshot", arguments='{"question":"Look at this"}'):
    return event(
        "response.output_item.done",
        item=NS(
            type="function_call",
            call_id=call_id,
            name=name,
            arguments=arguments,
        ),
    )


class Capture:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def capture(self, question):
        self.calls.append(question)
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("Permission denied")
        return ImageInput.from_bytes(b"\x89PNG\r\n\x1a\nencoded", question)


class Connection:
    def __init__(self):
        self.messages = []
        self.continued = asyncio.Event()

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "response.create":
            self.continued.set()


async def execute_events(events, fail=False):
    connection, capture = Connection(), Capture(fail)
    delegation = SnapshotDelegation(connection, capture, lambda _: None)
    for item in events:
        delegation.observe(item)
    worker = asyncio.create_task(delegation.run())
    try:
        await asyncio.wait_for(connection.continued.wait(), 1)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    return connection, capture


def test_waits_for_completed_response_and_deduplicates_calls():
    async def scenario():
        connection, capture = Connection(), Capture()
        delegation = SnapshotDelegation(connection, capture, lambda _: None)
        worker = asyncio.create_task(delegation.run())
        try:
            delegation.observe(event("response.created", response=NS(id="r1")))
            delegation.observe(call())
            delegation.observe(call())
            await asyncio.sleep(0)
            assert not capture.calls
            # Live intentionally omits function calls from response.output.
            done = event("response.completed", response=NS(id="r1", output=[]))
            delegation.observe(done)
            delegation.observe(done)
            await asyncio.wait_for(connection.continued.wait(), 1)
            assert capture.calls == ["Look at this"]
            messages = connection.messages
            assert messages[0]["item"]["call_id"] == "c1"
            assert json.loads(messages[0]["item"]["output"])["status"] == "captured"
            assert messages[1]["item"]["content"][1]["type"] == "input_image"
            assert messages[2] == {"type": "response.create"}
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "function,fail",
    [
        (call(name="execute_shell"), False),
        (call(arguments="not json"), False),
        (call(arguments='{"question":"Look","path":"/etc/passwd"}'), False),
        (call(), True),
    ],
)
def test_function_failures_return_error_without_image(function, fail):
    connection, capture = asyncio.run(
        execute_events(
            [
                event("response.created", response=NS(id="r1")),
                function,
                event("response.completed", response=NS(id="r1")),
            ],
            fail,
        )
    )
    assert len(capture.calls) == (1 if fail else 0)
    assert len(connection.messages) == 2
    assert json.loads(connection.messages[0]["item"]["output"])["status"] == "failed"


def test_returns_all_function_results_before_continuation():
    connection, capture = asyncio.run(
        execute_events(
            [
                event("response.created", response=NS(id="r1")),
                call("c1"),
                call("c2"),
                event("response.completed", response=NS(id="r1")),
            ]
        )
    )
    assert len(capture.calls) == 2
    outputs = [
        m["item"]["call_id"]
        for m in connection.messages
        if m.get("item", {}).get("type") == "function_call_output"
    ]
    assert outputs == ["c1", "c2"]
    assert connection.messages[-1] == {"type": "response.create"}


def test_connected_app_result_returns_to_live_backend_and_continues():
    async def scenario():
        connection = Connection()
        composio = NS(execute=AsyncMock(return_value={"status": "ok", "data": {"items": []}}))
        delegation = SnapshotDelegation(connection, None, lambda _: None, composio=composio)
        args = {"tool_slug": "GOOGLECALENDAR_EVENTS_LIST", "arguments_json": "{}"}
        delegation.observe(event("response.created", response=NS(id="r1")))
        delegation.observe(call("c1", "run_connected_app_tool", json.dumps(args)))
        delegation.observe(event("response.completed", response=NS(id="r1")))
        worker = asyncio.create_task(delegation.run())
        try:
            await asyncio.wait_for(connection.continued.wait(), 1)
            composio.execute.assert_awaited_once_with("run_connected_app_tool", args, "d1")
            result = connection.messages[0]["item"]
            assert result["call_id"] == "c1"
            assert json.loads(result["output"]) == {"status": "ok", "data": {"items": []}}
            assert connection.messages[-1] == {"type": "response.create"}
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_full_image_budget_reports_error_and_continues_without_sending_image():
    async def scenario():
        connection, capture = Connection(), Capture()
        delegation = SnapshotDelegation(connection, capture, lambda _: None)
        delegation.image_budget.used_bytes = MAX_SESSION_IMAGE_BYTES
        delegation.observe(event("response.created", response=NS(id="r1")))
        delegation.observe(call())
        delegation.observe(event("response.completed", response=NS(id="r1")))
        worker = asyncio.create_task(delegation.run())
        try:
            await asyncio.wait_for(connection.continued.wait(), 1)
            assert len(connection.messages) == 2
            result = json.loads(connection.messages[0]["item"]["output"])
            assert result["status"] == "failed" and "Restart" in result["error"]
            assert connection.messages[-1] == {"type": "response.create"}
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_capture_image_is_reused_by_shopping_tool_and_failure_clears_it():
    class Catalog:
        country = "CA"
        currency = "CAD"
        calls = []

        def filters(self):
            return {"available": True}

        async def call(self, name, args):
            self.calls.append((name, args))
            return {"products": []}

    async def scenario():
        connection, capture, catalog = Connection(), Capture(), Catalog()
        shopping = ShoppingSession(catalog, report=lambda _: None)
        delegation = SnapshotDelegation(connection, capture, lambda _: None, shopping=shopping)
        worker = asyncio.create_task(delegation.run())

        async def invoke(response_id, function):
            connection.continued.clear()
            delegation.observe(event("response.created", response=NS(id=response_id)))
            delegation.observe(function)
            delegation.observe(event("response.completed", response=NS(id=response_id)))
            await asyncio.wait_for(connection.continued.wait(), 1)

        try:
            await invoke("r1", call())
            await invoke(
                "r2",
                call(
                    "c2",
                    "search_shopify",
                    '{"query":"blue bottle","use_image":true,"max_price_minor":null}',
                ),
            )
            assert len(capture.calls) == 1
            assert catalog.calls[0][1]["like"][0]["image"]["data"] in shopping.image.data_url
            capture.fail = True
            await invoke("r3", call("c3"))
            assert shopping.image is None
            await invoke(
                "r4",
                call(
                    "c4",
                    "search_shopify",
                    '{"query":"bottle","use_image":true,"max_price_minor":null}',
                ),
            )
            assert len(catalog.calls) == 1
            output = connection.messages[-2]["item"]["output"]
            assert json.loads(output)["status"] == "failed"
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,arguments,fails",
    [
        ("search_web", '{"query":"weather Toronto"}', False),
        ("read_web_page", '{"url":"https://example.com"}', False),
        ("search_web", '{"query":"weather Toronto"}', True),
        ("search_web", '{"query":"weather","extra":"bad"}', False),
        ("read_web_page", '{"url":42}', False),
    ],
)
def test_web_tools_return_results_or_errors_and_continue(name, arguments, fails):
    from unittest.mock import AsyncMock

    async def scenario():
        connection = Connection()
        web = NS(search=AsyncMock(), read_page=AsyncMock())
        handler = web.search if name == "search_web" else web.read_page
        handler.return_value = {"status": "ok", "sources": []}
        if fails:
            handler.side_effect = RuntimeError("Browserbase unavailable")
        # A shopping session must not swallow web tool dispatch.
        delegation = SnapshotDelegation(connection, None, lambda _: None, web=web, shopping=NS())
        delegation.observe(event("response.created", response=NS(id="r1")))
        delegation.observe(call(name=name, arguments=arguments))
        delegation.observe(event("response.completed", response=NS(id="r1")))
        worker = asyncio.create_task(delegation.run())
        try:
            await asyncio.wait_for(connection.continued.wait(), 1)
            messages = connection.messages
            assert len(messages) == 2
            assert messages[0]["item"]["call_id"] == "c1"
            result = json.loads(messages[0]["item"]["output"])
            invalid = "extra" in arguments or "42" in arguments
            assert result["status"] == ("failed" if fails or invalid else "ok")
            assert handler.await_count == (0 if invalid else 1)
            assert messages[-1] == {"type": "response.create"}
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_stopping_worker_cancels_web_lookup_without_late_output():
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Web:
            async def search(self, query):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        connection = Connection()
        delegation = SnapshotDelegation(connection, None, lambda _: None, web=Web())
        delegation.observe(event("response.created", response=NS(id="r1")))
        delegation.observe(call(name="search_web", arguments='{"query":"news"}'))
        delegation.observe(event("response.completed", response=NS(id="r1")))
        worker = asyncio.create_task(delegation.run())
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert cancelled.is_set()
        assert connection.messages == []

    asyncio.run(scenario())
