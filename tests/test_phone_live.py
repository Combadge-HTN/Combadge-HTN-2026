import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from test_live import FakeAudio, FakeConnection, event

from commbadge.config import Settings
from commbadge.live import connect_voice, run_session
from commbadge.phone.config import PhoneSettings


def call_events():
    def nested(kind, **kwargs):
        return event("response.event", delegation_id="d1", event=event(kind, **kwargs))

    return [
        nested("response.created", response=NS(id="r1")),
        nested(
            "response.output_item.done",
            item=NS(
                type="function_call",
                call_id="c1",
                name="call_contact",
                arguments='{"contact":"alex"}',
            ),
        ),
        nested("response.completed", response=NS(id="r1")),
    ]


@pytest.mark.parametrize("acknowledge_close", [True, False])
def test_call_request_finalizes_live_without_returning_fake_tool_result(
    monkeypatch, acknowledge_close
):
    async def scenario():
        from commbadge.phone import client

        async def names(_):
            return ["alex"]

        monkeypatch.setattr(client, "contacts", names)
        stop = asyncio.Event()
        connection = FakeConnection(call_events(), acknowledge_close=acknowledge_close)
        audio = FakeAudio(stop)
        task = run_session(
            connection,
            audio,
            Settings(),
            stop,
            phone_settings=PhoneSettings("wss://example.com", "x" * 32),
            seconds=1,
            close_timeout=0.01,
            report=lambda _: None,
        )
        if acknowledge_close:
            stats = await task
            assert stats.phone_contact == "alex" and stats.finalized
        else:
            with pytest.raises(RuntimeError, match="finalization was not confirmed"):
                await task
        assert connection.closed and audio.closed
        assert not any(m["type"] == "response.create" for m in connection.messages)
        assert not any(m["type"] == "response.item.create" for m in connection.messages)

    asyncio.run(scenario())


def test_voice_disconnects_before_dialing_and_never_reconnects(monkeypatch):
    async def scenario():
        import websockets.asyncio.client

        from commbadge import live
        from commbadge.phone import client

        sequence = []
        connection = FakeConnection(call_events())

        class Audio(FakeAudio):
            def __init__(self, *_):
                super().__init__(asyncio.Event())

            def preflight(self):
                pass

            async def start(self):
                sequence.append("audio-start")
                self.closed = False
                await super().start()

            async def close(self):
                sequence.append("audio-close")
                await super().close()

        class WebSocket:
            async def send(self, data):
                await connection.send(json.loads(data))

            async def recv(self):
                return json.dumps(await connection.recv(), default=vars)

        class Context:
            async def __aenter__(self):
                sequence.append("openai-open")
                return WebSocket()

            async def __aexit__(self, *_):
                assert connection.closed
                sequence.append("openai-closed")

        async def names(_):
            return ["alex"]

        async def phone(settings, contact, audio, **kwargs):
            assert sequence[-2:] == ["openai-closed", "audio-start"]
            assert connection.closed and not audio.closed
            assert contact == "alex"
            sequence.append("phone-call")
            return {"status": "completed", "contact": contact}

        monkeypatch.setattr(live, "AlsaAudio", Audio)
        monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **kw: Context())
        monkeypatch.setattr(client, "contacts", names)
        monkeypatch.setattr(client, "call_contact", phone)
        stats = await connect_voice(
            Settings(),
            check=False,
            input_device="default",
            output_device="default",
            seconds=1,
            captions=False,
            phone_settings=PhoneSettings("wss://example.com", "x" * 32),
        )
        assert stats.finalized
        assert sequence == [
            "openai-open",
            "audio-start",
            "audio-close",
            "openai-closed",
            "audio-start",
            "phone-call",
            "audio-close",
        ]

    asyncio.run(scenario())


def test_stop_during_phone_call_waits_for_hangup_then_closes_audio(monkeypatch):
    async def scenario():
        from commbadge.phone import client

        stop, started, hung_up = (asyncio.Event() for _ in range(3))
        audio = FakeAudio(stop)

        async def phone(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0.01)
                assert not audio.closed
                hung_up.set()

        monkeypatch.setattr(client, "call_contact", phone)
        task = asyncio.create_task(client.call_until_stopped(None, "alex", audio, stop))
        await asyncio.wait_for(started.wait(), 1)
        stop.set()
        assert await asyncio.wait_for(task, 1) is None
        assert hung_up.is_set() and audio.closed

    asyncio.run(scenario())
