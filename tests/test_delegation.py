import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from commbadge.delegation import SnapshotDelegation
from commbadge.vision import ImageInput


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
