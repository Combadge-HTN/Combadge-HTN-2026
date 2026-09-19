import asyncio
import json
from types import SimpleNamespace as NS

from test_live import FakeConnection, event

from commbadge.audio import FRAME_BYTES, SilenceAudio
from commbadge.config import Settings
from commbadge.live import run_session
from commbadge.phone.config import PhoneSettings


def test_live_call_delegation_mutes_ai_and_resumes_after_phone(monkeypatch):
    async def scenario():
        stop = asyncio.Event()
        spoken = []
        phone_samples = []
        session_input = []
        call_active = False
        from commbadge.phone import client

        async def names(_):
            return ["alex"]

        async def phone(settings, contact, audio, **kwargs):
            nonlocal call_active
            assert contact == "alex"
            call_active = True
            for _ in range(3):
                phone_samples.append(await audio.read())
            await audio.write(b"\x02\0" * 480)
            call_active = False
            return {"status": "completed", "contact": contact}

        monkeypatch.setattr(client, "contacts", names)
        monkeypatch.setattr(client, "call_contact", phone)

        class Audio(SilenceAudio):
            async def read(self):
                await asyncio.sleep(0.005)
                return b"\x01\0" * 480

            async def write(self, data):
                spoken.append(data)

        def nested(kind, **kwargs):
            return event("response.event", delegation_id="d1", event=event(kind, **kwargs))

        class Connection(FakeConnection):
            async def append(self, *, audio):
                import base64

                if call_active:
                    session_input.append(base64.b64decode(audio))
                await super().append(audio=audio)

            async def send(self, message):
                await super().send(message)
                if message["type"] == "response.create":
                    stop.set()

        connection = Connection(
            [
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
        )
        stats = await run_session(
            connection,
            Audio(),
            Settings(),
            stop,
            phone_settings=PhoneSettings("wss://example.com", "x" * 32),
            seconds=2,
            report=lambda _: None,
        )
        outputs = [m["item"] for m in connection.messages if m["type"] == "response.item.create"]
        assert json.loads(outputs[0]["output"])["status"] == "completed"
        assert phone_samples and all(any(data) for data in phone_samples)
        assert session_input and all(data == bytes(FRAME_BYTES) for data in session_input)
        assert spoken == [b"\x02\0" * 480]
        assert stats.finalized

    asyncio.run(scenario())
