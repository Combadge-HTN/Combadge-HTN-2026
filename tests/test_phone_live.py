import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_live import FakeAudio, FakeConnection, event

from combadge.config import Settings
from combadge.live import connect_voice, run_session
from combadge.phone.config import PhoneSettings, SipSettings
from combadge.sms import SmsClient, SmsSettings


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


def test_text_then_call_shares_session_and_closes_for_phone_handoff():
    async def scenario():
        texts = call_events()
        for envelope in texts:
            envelope.delegation_id = "sms-request"
            if hasattr(envelope.event, "response"):
                envelope.event.response.id = "sms-response"
        texts[1].event.item.call_id = "sms-call"
        texts[1].event.item.name = "send_text"
        texts[1].event.item.arguments = '{"recipient":"alex","body":"I am running late"}'
        connection = FakeConnection([*texts, *call_events()])
        stop = asyncio.Event()
        sms = SmsClient(SmsSettings("AC" + "a" * 32, "test-token", "+15485550100"))
        sms.execute = AsyncMock(return_value={"status": "queued", "delivered": False})
        phone = SipSettings(
            "test.pstn.twilio.com",
            "badge",
            "private-password",
            "+14165550100",
            {"alex": "+14165550101"},
        )
        stats = await run_session(
            connection,
            FakeAudio(stop),
            Settings(),
            stop,
            phone_settings=phone,
            sms=sms,
            seconds=1,
            report=lambda _: None,
        )
        assert stats.finalized and stats.phone_contact == "alex"
        sms.execute.assert_awaited_once_with(
            "send_text", {"recipient": "alex", "body": "I am running late"}, "sms-request"
        )
        outputs = [
            json.loads(m["item"]["output"])
            for m in connection.messages
            if m["type"] == "response.item.create"
        ]
        assert outputs[0]["status"] == "queued"
        assert outputs[1] == {"status": "handoff_requested", "contact": "alex", "dialed": False}

    asyncio.run(scenario())


@pytest.mark.parametrize("acknowledge_close", [True, False])
def test_call_request_completes_tool_exchange_before_finalizing_live(
    monkeypatch, acknowledge_close
):
    async def scenario():
        from combadge.phone import client

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
@pytest.mark.parametrize("outcome", ["completed", "no-answer", "busy"])
def test_voice_disconnects_before_dialing_and_reconnects_only_after_call_ends(
    monkeypatch, acknowledge_close, transport, outcome
):
    async def scenario():
        import websockets.asyncio.client

        from combadge import live
        from combadge.phone import client

        sequence = []
        connection = FakeConnection(call_events(), acknowledge_close=acknowledge_close)
        resumed = FakeConnection()
        opened = []

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
            def __init__(self, connection):
                self.connection = connection

            async def send(self, data):
                await self.connection.send(json.loads(data))

            async def recv(self):
                return json.dumps(await self.connection.recv(), default=vars)

        class Context:
            def __init__(self, *args, **kwargs):
                self.connection = (connection, resumed)[len(opened)]
                opened.append(self.connection)

            async def __aenter__(self):
                sequence.append("openai-open")
                return WebSocket(self.connection)

            async def __aexit__(self, *_):
                assert self.connection.closed
                sequence.append("openai-closed")

        async def names(_):
            return ["alex"]

        async def phone(settings, contact, audio, **kwargs):
            assert sequence[-2:] == ["openai-closed", "audio-start"]
            assert connection.closed and not audio.closed
            assert contact == "alex"
            sequence.append("phone-call")
            return {"status": outcome, "contact": contact}

        monkeypatch.setattr(live, "AlsaAudio", Audio)
        monkeypatch.setattr(live, "MacAudio", Audio)
        monkeypatch.setattr(live, "run_session", fast_session)
        monkeypatch.setattr(websockets.asyncio.client, "connect", Context)
        if transport == "relay":
            monkeypatch.setattr(client, "contacts", names)
            monkeypatch.setattr(client, "call_contact", phone)
            config = PhoneSettings("wss://example.com", "x" * 32)
        else:
            from combadge.phone import direct

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
            seconds=0.02,
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
        assert stats.phone_contact is None  # Previous call cannot trigger a second dial.
        resume_config = resumed.messages[0]["session"]
        context = resume_config["input"][0]["content"][0]["text"]
        assert outcome in context and "alex" in context
        assert "Do not repeat" in resume_config["instructions"]
        assert any(
            message["type"] == "session.instructions.append"
            and "Computer is back" in message["content"]
            for message in resumed.messages
        )
        assert sequence == [
            "openai-open",
            "audio-start",
            "audio-close",
            "openai-closed",
            "audio-start",
            "phone-call",
            "audio-close",
            "openai-open",
            "audio-start",
            "audio-close",
            "openai-closed",
        ]

    asyncio.run(scenario())


def test_stop_during_phone_call_waits_for_hangup_then_closes_audio(monkeypatch):
    async def scenario():
        from combadge.phone import client

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
