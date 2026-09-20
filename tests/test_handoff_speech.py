import asyncio
import struct

from test_live import FakeAudio, FakeConnection, audio_event, event
from test_phone_live import call_events

from combadge.config import Settings
from combadge.handoff import HandoffSpeech
from combadge.live import run_session
from combadge.phone.config import SipSettings


def test_speech_and_captions_extend_wait_but_continuous_silence_does_not():
    gate = HandoffSpeech()
    assert not gate.ready(100)
    gate.begin(100)
    assert not gate.ready(101)
    gate.audio(struct.pack("<h", 3000) * 480, 101.8)
    gate.audio(bytes(960), 102.7)
    assert not gate.ready(102.7)
    assert gate.ready(102.9)
    gate.activity(103)
    assert not gate.ready(103.5)
    gate.activity(109.9)
    assert gate.ready(110)  # Bounded even with endless speech/noise.


def test_handoff_keeps_receiving_farewell_then_drains_before_close(monkeypatch):
    from combadge import live

    monkeypatch.setattr(live, "HandoffSpeech", lambda: HandoffSpeech(minimum=0.08, quiet=0.04))

    async def scenario():
        order = []
        tail = b"\x00\x10" * 480

        class Connection(FakeConnection):
            async def send(self, message):
                await super().send(message)
                if message["type"] == "response.create":

                    async def delayed_speech():
                        await asyncio.sleep(0.03)
                        await self.events.put(audio_event(tail))
                        await self.events.put(
                            event("session.output_transcript.delta", delta=" Edmon.")
                        )

                    self.tail = asyncio.create_task(delayed_speech())
                if message["type"] == "session.close":
                    order.append("session-close")

        class Audio(FakeAudio):
            async def read(self):
                await asyncio.sleep(0.005)
                return b"\x01\x00" * 480

            async def write(self, data):
                self.output.append(data)

            async def drain(self):
                order.append("drain")

            async def close(self):
                order.append("audio-close")

        connection = Connection(call_events())
        stop = asyncio.Event()
        audio = Audio(stop)
        config = SipSettings(
            "test.pstn.twilio.com", "badge", "secret", "+14165550100", {"alex": "+14165550101"}
        )
        result = await run_session(
            connection,
            audio,
            Settings(),
            stop,
            phone_settings=config,
            seconds=1,
            report=lambda _: None,
        )
        assert result.phone_contact == "alex"
        assert audio.output == [tail]
        assert order == ["drain", "audio-close", "session-close"]
        assert connection.input[0] != bytes(960)
        assert connection.input[-1] == bytes(960)
        await connection.tail

    asyncio.run(scenario())
