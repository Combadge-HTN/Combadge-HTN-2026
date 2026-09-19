import asyncio
import base64
from types import SimpleNamespace as NS

import pytest

from commbadge.audio import FRAME_BYTES
from commbadge.config import Settings
from commbadge.live import run_session
from commbadge.vision import ImageInput


def event(kind, **values):
    return NS(type=kind, **values)


class FakeConnection:
    def __init__(self, events=(), acknowledge_start=True, acknowledge_close=True):
        self.events = asyncio.Queue()
        self.script = list(events)
        self.acknowledge_start = acknowledge_start
        self.acknowledge_close = acknowledge_close
        self.ready = False
        self.closed = False
        self.input = []
        self.messages = []
        self.session = NS(
            start=self.start,
            close=self.close,
            input_audio=NS(append=self.append),
            instructions=NS(append=self.greet),
        )

    async def send(self, message):
        self.messages.append(message)
        kind = message["type"]
        if kind == "session.start":
            await self.start(session=message["session"])
        elif kind == "session.close":
            await self.close()
        elif kind == "session.input_audio.append":
            await self.append(audio=message["audio"])
        elif kind == "session.instructions.append":
            await self.greet()
        elif kind == "response.item.create":
            assert self.ready
        elif kind == "response.create":
            assert self.ready
        else:
            raise AssertionError(f"Unknown client event: {kind}")

    async def start(self, **kwargs):
        if self.acknowledge_start:
            await self.events.put(event("session.started"))

    async def recv(self):
        message = await self.events.get()
        if message.type == "session.started":
            self.ready = True
        return message

    async def append(self, *, audio):
        assert self.ready, "Must not transmit before startup acknowledgment"
        self.input.append(base64.b64decode(audio))
        for message in self.script:
            await self.events.put(message)
        self.script = []

    async def greet(self, **kwargs):
        pass

    async def close(self):
        self.closed = True
        if self.acknowledge_close:
            await self.events.put(event("session.closed", reason="close_requested"))


class FakeAudio:
    def __init__(self, stop, *, fail_start=False, fail_read=False):
        self.stop = stop
        self.closed = False
        self.output = []
        self.read_count = 0
        self.fail_start = fail_start
        self.fail_read = fail_read

    async def start(self):
        if self.fail_start:
            raise RuntimeError("device unavailable")

    async def read(self):
        if self.fail_read:
            raise RuntimeError("capture disconnected")
        self.read_count += 1
        if self.read_count > 1:
            await asyncio.Event().wait()
        return b"\x01\x00" * (FRAME_BYTES // 2)

    async def write(self, data):
        self.output.append(data)
        self.stop.set()

    async def close(self):
        self.closed = True


def audio_event(data):
    return event("session.output_audio.delta", delta=base64.b64encode(data).decode())


def test_bidirectional_audio_and_captions_finalize_cleanly():
    async def scenario():
        stop = asyncio.Event()
        pcm = b"\x02\x00" * (FRAME_BYTES // 2)
        connection = FakeConnection(
            [
                event("session.input_transcript.delta", delta="Hello"),
                event("session.input_transcript.delta", delta=" there"),
                audio_event(pcm),
            ]
        )
        audio = FakeAudio(stop)
        captions = []
        stats = await run_session(connection, audio, Settings(), stop, report=captions.append)
        assert connection.input == [b"\x01\x00" * (FRAME_BYTES // 2)]
        assert audio.output == [pcm]
        assert "Hello there" in "".join(captions)
        assert stats.finalized and connection.closed and audio.closed

    asyncio.run(scenario())


def test_cloud_check_requires_non_silent_audio():
    async def scenario():
        stop = asyncio.Event()
        connection = FakeConnection([audio_event(bytes(FRAME_BYTES * 5))])
        with pytest.raises(RuntimeError, match="No usable generated audio"):
            await run_session(
                connection,
                FakeAudio(stop),
                Settings(),
                stop,
                check=True,
                seconds=0.02,
                report=lambda _: None,
            )
        assert connection.closed

    asyncio.run(scenario())


def test_cloud_check_succeeds_on_real_pcm_and_closes():
    async def scenario():
        stop = asyncio.Event()
        connection = FakeConnection([audio_event(b"\x01\x00" * FRAME_BYTES * 3)])
        stats = await run_session(
            connection, FakeAudio(stop), Settings(), stop, check=True, report=lambda _: None
        )
        assert stats.speech_bytes >= FRAME_BYTES * 5 and stats.finalized

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "message,match",
    [
        (event("error", error=NS(code="denied", message="access denied")), "access denied"),
        (event("session.output_audio.delta", delta="not-base64"), "invalid audio encoding"),
        (audio_event(b"\x01"), "incomplete PCM16"),
        (audio_event(bytes(FRAME_BYTES * 101)), "Playback fell behind"),
        (event("session.closed", reason="connection_lost"), "connection_lost"),
    ],
)
def test_stream_failures_close_audio(message, match):
    async def scenario():
        stop = asyncio.Event()
        audio = FakeAudio(stop)
        connection = FakeConnection([message])
        with pytest.raises(RuntimeError, match=match):
            await run_session(connection, audio, Settings(), stop, report=lambda _: None)
        assert audio.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["start", "read"])
def test_audio_failure_still_finalizes_server(failure):
    async def scenario():
        stop = asyncio.Event()
        audio = FakeAudio(stop, fail_start=failure == "start", fail_read=failure == "read")
        connection = FakeConnection()
        with pytest.raises(RuntimeError):
            await run_session(connection, audio, Settings(), stop, report=lambda _: None)
        assert audio.closed and connection.closed

    asyncio.run(scenario())


def test_startup_timeout_never_reads_microphone():
    async def scenario():
        stop = asyncio.Event()
        audio = FakeAudio(stop)
        connection = FakeConnection(acknowledge_start=False)
        with pytest.raises(TimeoutError):
            await run_session(
                connection, audio, Settings(), stop, startup_timeout=0.01, report=lambda _: None
            )
        assert not connection.input and audio.read_count == 0
        assert audio.closed and connection.closed

    asyncio.run(scenario())


def test_unacknowledged_close_is_a_failure():
    async def scenario():
        stop = asyncio.Event()
        connection = FakeConnection([audio_event(bytes(FRAME_BYTES))], acknowledge_close=False)
        with pytest.raises(RuntimeError, match="finalization was not confirmed"):
            await run_session(
                connection,
                FakeAudio(stop),
                Settings(),
                stop,
                close_timeout=0.01,
                report=lambda _: None,
            )

    asyncio.run(scenario())


def backend_event(kind, **values):
    return event("response.event", delegation_id="vision_1", event=event(kind, **values))


@pytest.mark.parametrize("check", [True, False])
def test_image_is_delegated_after_start_and_spoken_audio_is_observed(check):
    async def scenario():
        stop = asyncio.Event()
        image = ImageInput.from_bytes(b"\x89PNG\r\n\x1a\nencoded", "What color?")
        connection = FakeConnection(
            [
                # In check mode, early speech must not prematurely stop delegation.
                *([audio_event(b"\x01\x00" * FRAME_BYTES * 3)] if check else []),
                backend_event("response.created", response=NS(id="response_1")),
                backend_event("response.output_text.delta", delta="A red square."),
                backend_event("response.completed", response=NS(id="response_1")),
                audio_event(b"\x02\x00" * FRAME_BYTES * 3),
            ]
        )
        reports = []
        stats = await run_session(
            connection,
            FakeAudio(stop),
            Settings(),
            stop,
            image=image,
            check=check,
            report=reports.append,
        )
        types = [message["type"] for message in connection.messages]
        assert types.index("session.start") < types.index("response.item.create")
        assert types.index("response.item.create") < types.index("response.create")
        assert "session.instructions.append" not in types
        startup = connection.messages[0]["session"]
        assert startup["input"][0]["content"] == [{"type": "input_text", "text": "What color?"}]
        assert image.data_url not in str(startup)
        assert stats.image_answer == "A red square."
        assert stats.image_completed and stats.image_backend_seconds is not None
        assert stats.image_audio_seconds is not None
        assert stats.finalized
        assert "Vision: A red square." in "".join(reports)

    asyncio.run(scenario())


def test_incomplete_image_backend_fails_and_finalizes():
    async def scenario():
        stop = asyncio.Event()
        connection = FakeConnection(
            [
                backend_event("response.created", response=NS(id="response_1")),
                backend_event("response.incomplete", response=NS(id="response_1")),
            ]
        )
        with pytest.raises(RuntimeError, match="Image analysis was incomplete"):
            await run_session(
                connection,
                FakeAudio(stop),
                Settings(),
                stop,
                image=ImageInput.from_bytes(b"\x89PNG\r\n\x1a\nencoded"),
                check=True,
                report=lambda _: None,
            )
        assert connection.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("complete", [True, False])
def test_image_check_cannot_pass_on_early_speech_or_backend_text_alone(complete):
    async def scenario():
        stop = asyncio.Event()
        script = [
            audio_event(b"\x01\x00" * FRAME_BYTES * 3),
            backend_event("response.created", response=NS(id="response_1")),
            backend_event("response.output_text.delta", delta="A red square."),
        ]
        if complete:
            script.append(backend_event("response.completed", response=NS(id="response_1")))
        connection = FakeConnection(script)
        match = "No usable speech" if complete else "Image analysis did not complete"
        with pytest.raises(RuntimeError, match=match):
            await run_session(
                connection,
                FakeAudio(stop),
                Settings(),
                stop,
                image=ImageInput.from_bytes(b"\x89PNG\r\n\x1a\nencoded"),
                check=True,
                seconds=0.02,
                report=lambda _: None,
            )
        assert connection.closed

    asyncio.run(scenario())
