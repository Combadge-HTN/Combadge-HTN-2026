import asyncio
import io
import json
import threading
import urllib.error
from unittest.mock import Mock, patch

import pytest

from commbadge.browserbase import BrowserbaseClient, BrowserbaseError, BrowserSession, lookup_result
from commbadge.cli import main
from commbadge.config import Settings
from commbadge.delegation import FunctionCall, SnapshotDelegation
from commbadge.live import session_config


def run(status="COMPLETED", **kwargs):
    return {
        "runId": "run-123",
        "status": status,
        "result": {"output": {"answer": "Example Domain", "sources": ["https://example.com"]}},
        **kwargs,
    }


def test_hosted_wire_and_auth_only_go_to_browserbase():
    client = BrowserbaseClient("private-key")
    with patch(
        "urllib.request.OpenerDirector.open", return_value=io.BytesIO(b'{"runId":"x"}')
    ) as send:
        client.start("What is this site for?", "https://example.com")
    req = send.call_args.args[0]
    assert req.full_url == "https://api.browserbase.com/v1/agents/runs"
    assert req.get_header("X-bb-api-key") == "private-key"
    payload = json.loads(req.data)
    assert "private-key" not in req.data.decode()
    assert "resultSchema" in payload and "Do not sign in" in payload["task"]
    assert "browserSettings" not in payload  # No persisted or signed-in browser context.


def test_http_failure_does_not_leak_body_or_key():
    error = urllib.error.HTTPError(
        "https://api.browserbase.com", 401, "bad", {}, io.BytesIO(b"private-key")
    )
    with patch("urllib.request.OpenerDirector.open", side_effect=error):
        with pytest.raises(BrowserbaseError, match="HTTP 401") as caught:
            BrowserbaseClient("private-key").get("run-123")
    assert "private-key" not in str(caught.value)


def test_real_wrapped_result_shape_is_bounded_and_sourced():
    data = run()
    data["result"]["output"]["answer"] = "a" * 5000
    data["result"]["summary"] = "ignored arbitrary summary"
    result = lookup_result(data)
    assert result["status"] == "completed" and len(result["answer"]) == 4000
    assert result["sources"] == ["https://example.com"]
    assert "summary" not in result


@pytest.mark.parametrize(
    "data",
    [
        run("FAILED"),
        run("STOPPED"),
        run("TIMED_OUT"),
        run(result=None),
        run(result={"answer": "hello", "sources": []}),
    ],
)
def test_no_sourced_completion_never_claims_success(data):
    assert lookup_result(data)["status"] == "failed"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///etc/passwd",
        "https://localhost",
        "https://127.0.0.1/",
        "https://host.internal",
        12,
    ],
)
def test_invalid_starting_url_never_starts_browser(url):
    client = Mock(_api_key="")
    with pytest.raises(ValueError):
        asyncio.run(BrowserSession(client).lookup("Read this", url))
    client.start.assert_not_called()


def test_pending_to_completed_keeps_event_loop_free_and_starts_once():
    async def scenario():
        started = asyncio.Event()
        client = Mock(_api_key="")
        client.start.return_value = run("PENDING")
        client.get.side_effect = [run("RUNNING"), run()]
        session = BrowserSession(client, poll_seconds=0.001, report=lambda _: None)
        task = asyncio.create_task(session.lookup("What is this for?", "https://example.com"))
        asyncio.get_running_loop().call_soon(started.set)
        await asyncio.wait_for(started.wait(), 1)
        assert (await task)["status"] == "completed"
        client.start.assert_called_once()
        client.stop.assert_not_called()

    asyncio.run(scenario())


def test_timeout_requests_stop_without_starting_again():
    client = Mock(_api_key="")
    client.start.return_value = run("RUNNING")
    result = asyncio.run(
        BrowserSession(client, timeout=0.001, report=lambda _: None).lookup("Read", None)
    )
    assert result["status"] == "timed_out"
    client.start.assert_called_once()
    client.stop.assert_called_once_with("run-123")


def test_cancel_during_creation_recovers_id_and_stops_remote_run():
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def start(*_):
            entered.set()
            assert release.wait(timeout=1)
            return run("RUNNING")

        client = Mock(_api_key="")
        client.start.side_effect = start
        task = asyncio.create_task(
            BrowserSession(client, report=lambda _: None).lookup("Read", None)
        )
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.stop.assert_called_once_with("run-123")

    asyncio.run(scenario())


def test_mismatched_poll_response_stops_original_run():
    client = Mock(_api_key="")
    client.start.return_value = run("RUNNING")
    client.get.return_value = run(runId="wrong-run")
    with pytest.raises(BrowserbaseError, match="different run ID"):
        asyncio.run(
            BrowserSession(client, poll_seconds=0, report=lambda _: None).lookup("Read", None)
        )
    client.stop.assert_called_once_with("run-123")


def test_browser_tool_composes_with_shopping_snapshot_and_phone():
    baseline = session_config(Settings())
    assert "tools" not in baseline["delegation"]["responses"]
    config = session_config(
        Settings(),
        browser=True,
        shopping=True,
        snapshots=True,
        shop_account=True,
        call_names=["Edmon"],
    )
    names = {t["name"] for t in config["delegation"]["responses"]["tools"]}
    assert {
        "browse_web",
        "capture_snapshot",
        "search_shopify",
        "save_shopify_item",
        "call_contact",
    } <= names


def test_browser_result_is_returned_through_voice_delegation():
    from unittest.mock import AsyncMock

    async def scenario():
        done = asyncio.Event()
        messages = []

        class Connection:
            async def send(self, message):
                messages.append(message)
                if message["type"] == "response.create":
                    done.set()

        browser = Mock()
        browser.lookup = AsyncMock(return_value=lookup_result(run()))
        worker = SnapshotDelegation(Connection(), None, lambda _: None, browser=browser)
        worker.queue.put_nowait([FunctionCall("c", "browse_web", '{"question":"Read","url":null}')])
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(done.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        browser.lookup.assert_awaited_once_with(question="Read", url=None)
        assert json.loads(messages[0]["item"]["output"])["answer"] == "Example Domain"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "args",
    [
        ["--browserbase", "--check"],
        ["--browserbase", "--list-devices"],
        ["--browserbase", "--browser-timeout", "301"],
    ],
)
def test_invalid_voice_options_do_not_connect(args):
    with pytest.raises(SystemExit) as error:
        main(["voice", *args])
    assert error.value.code == 2


def test_live_audio_continues_while_browser_is_pending():
    from types import SimpleNamespace as NS

    from test_live import FakeAudio, FakeConnection, audio_event, event

    from commbadge.audio import FRAME_BYTES
    from commbadge.live import run_session

    async def scenario():
        stop, audio_written = asyncio.Event(), asyncio.Event()

        class Browser:
            async def lookup(self, question, url):
                await audio_written.wait()
                return lookup_result(run())

        class Audio(FakeAudio):
            async def write(self, data):
                self.output.append(data)
                audio_written.set()

        class Connection(FakeConnection):
            async def send(self, message):
                await super().send(message)
                if message["type"] == "response.create":
                    stop.set()

        script = [
            event(
                "response.event",
                delegation_id="d",
                event=event("response.created", response=NS(id="r")),
            ),
            event(
                "response.event",
                delegation_id="d",
                event=event(
                    "response.output_item.done",
                    item=NS(
                        type="function_call",
                        call_id="c",
                        name="browse_web",
                        arguments='{"question":"Read the page","url":null}',
                    ),
                ),
            ),
            event(
                "response.event",
                delegation_id="d",
                event=event("response.completed", response=NS(id="r")),
            ),
            audio_event(b"\x01\x00" * (FRAME_BYTES // 2)),
        ]
        connection, audio = Connection(script), Audio(stop)
        stats = await run_session(
            connection, audio, Settings(), stop, seconds=1, browser=Browser(), report=lambda _: None
        )
        assert audio.output and stats.finalized
        outputs = [m["item"]["output"] for m in connection.messages if "item" in m]
        assert json.loads(outputs[0])["status"] == "completed"
        config = next(m["session"] for m in connection.messages if m["type"] == "session.start")
        assert "browse_web" in {t["name"] for t in config["delegation"]["responses"]["tools"]}

    asyncio.run(scenario())
