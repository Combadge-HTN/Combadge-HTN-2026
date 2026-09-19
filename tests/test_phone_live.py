import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from test_live import FakeAudio, FakeConnection, event

from commbadge.config import Settings
from commbadge.live import connect_voice, run_session
from commbadge.phone.config import PhoneSettings, SipSettings


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
def test_call_request_completes_tool_exchange_before_finalizing_live(
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
        exchange = [
            m
            for m in connection.messages
            if m["type"] in ("response.item.create", "response.create", "session.close")
        ]
        assert [m["type"] for m in exchange] == [
            "response.item.create",
            "response.create",
            "session.close",
        ]
        assert exchange[0]["item"]["call_id"] == "c1"
        assert json.loads(exchange[0]["item"]["output"]) == {
            "status": "handoff_requested",
            "contact": "alex",
            "dialed": False,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("acknowledge_close", [True, False])
@pytest.mark.parametrize("transport", ["relay", "sip"])
def test_voice_disconnects_before_dialing_and_never_reconnects(
    monkeypatch, acknowledge_close, transport
):
    async def scenario():
        import websockets.asyncio.client

        from commbadge import live
        from commbadge.phone import client

        sequence = []
        connection = FakeConnection(call_events(), acknowledge_close=acknowledge_close)

        async def fast_session(*args, **kwargs):
            return await run_session(*args, **kwargs, close_timeout=0.01)

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
        monkeypatch.setattr(live, "MacAudio", Audio)
        monkeypatch.setattr(live, "run_session", fast_session)
        monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **kw: Context())
        if transport == "relay":
            monkeypatch.setattr(client, "contacts", names)
            monkeypatch.setattr(client, "call_contact", phone)
            config = PhoneSettings("wss://example.com", "x" * 32)
        else:
            from commbadge.phone import direct

            monkeypatch.setattr(direct, "call_contact", phone)
            config = SipSettings(
                "test.pstn.twilio.com",
                "badge",
                "secret-password",
                "+14165550100",
                {"alex": "+14165550101"},
            )
        session = connect_voice(
            Settings(),
            check=False,
            input_device="default",
            output_device="default",
            seconds=1,
            captions=False,
            phone_settings=config,
        )
        if not acknowledge_close:
            with pytest.raises(RuntimeError, match="finalization was not confirmed"):
                await session
            assert sequence == ["openai-open", "audio-start", "audio-close", "openai-closed"]
            return
        stats = await session
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
