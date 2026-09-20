import asyncio
import base64
from types import SimpleNamespace as NS

import pytest

from combadge.audio import FRAME_BYTES
from combadge.config import Settings
from combadge.live import run_session, session_config
from combadge.vision import ImageInput


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


@pytest.mark.parametrize(
    "voice,expected",
    [("marin", "marin"), ("cedar", "cedar"), ("voice_test_custom", {"id": "voice_test_custom"})],
)
def test_voice_is_selected_in_initial_session_from_environment(
    tmp_path, monkeypatch, voice, expected
):
    from combadge.config import load_settings

    monkeypatch.delenv("OPENAI_LIVE_VOICE", raising=False)
    path = tmp_path / ".env"
    path.write_text(f"OPENAI_LIVE_VOICE={voice}\n")

    async def scenario():
        stop = asyncio.Event()
        conn = FakeConnection([audio_event(b"\x01\x00" * (FRAME_BYTES // 2))])
        stats = await run_session(
            conn, FakeAudio(stop), load_settings(path), stop, seconds=1, report=lambda _: None
        )
        assert conn.messages[0]["type"] == "session.start"
        assert conn.messages[0]["session"]["audio"]["output"]["voice"] == expected
        assert stats.finalized

    asyncio.run(scenario())


def test_shopping_tools_are_opt_in_and_snapshot_is_independently_optional():
    baseline = session_config(Settings())
    assert "tools" not in baseline["delegation"]["responses"]
    shopping = session_config(Settings(), shopping=True)
    names = {t["name"] for t in shopping["delegation"]["responses"]["tools"]}
    assert names == {"search_shopify", "get_shopify_product", "open_shopify_checkout"}
    both = session_config(Settings(), shopping=True, snapshots=True)
    assert "capture_snapshot" in {t["name"] for t in both["delegation"]["responses"]["tools"]}


def test_shopping_without_capture_runs_tools_and_keeps_audio_flowing():
    class Shopping:
        image = None

        async def search(self, **kwargs):
            await audio_written.wait()
            return {"status": "no_matches", "offers": []}

        async def product(self, **kwargs):
            raise AssertionError("Unexpected product lookup")

        async def checkout(self, **kwargs):
            raise AssertionError("Unexpected checkout")

    class Audio(FakeAudio):
        async def write(self, data):
            self.output.append(data)
            audio_written.set()

    class Connection(FakeConnection):
        async def send(self, message):
            await super().send(message)
            if message["type"] == "response.create":
                stop.set()

    async def scenario():
        nonlocal audio_written, stop
        audio_written, stop = asyncio.Event(), asyncio.Event()
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
                        name="search_shopify",
                        arguments='{"query":"bottle","use_image":false,"max_price_minor":null}',
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
        conn, audio = Connection(script), Audio(stop)
        stats = await run_session(
            conn, audio, Settings(), stop, seconds=1, shopping=Shopping(), report=lambda _: None
        )
        assert audio.output and stats.finalized
        assert any(m.get("item", {}).get("type") == "function_call_output" for m in conn.messages)

    audio_written = stop = None
    asyncio.run(scenario())


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


def test_snapshot_capture_does_not_block_audio_receiver():
    async def scenario():
        stop, capturing, release, played = (asyncio.Event() for _ in range(4))

        class SlowCapture:
            async def capture(self, question):
                capturing.set()
                await release.wait()
                return ImageInput.from_bytes(b"\x89PNG\r\n\x1a\nencoded", question)

        class PlayingAudio(FakeAudio):
            async def write(self, data):
                self.output.append(data)
                played.set()

        class ContinuingConnection(FakeConnection):
            async def send(self, message):
                await super().send(message)
                if message["type"] == "response.create":
                    stop.set()

        connection = ContinuingConnection(
            [
                backend_event("response.created", response=NS(id="r1")),
                backend_event(
                    "response.output_item.done",
                    item=NS(
                        type="function_call",
                        call_id="c1",
                        name="capture_snapshot",
                        arguments='{"question":"Look at this"}',
                    ),
                ),
                backend_event("response.completed", response=NS(id="r1", output=[])),
                audio_event(b"\x01\x00" * (FRAME_BYTES // 2)),
            ]
        )
        task = asyncio.create_task(
            run_session(
                connection,
                PlayingAudio(stop),
                Settings(),
                stop,
                snapshot_capture=SlowCapture(),
                report=lambda _: None,
            )
        )
        try:
            await asyncio.wait_for(capturing.wait(), 1)
            await asyncio.wait_for(played.wait(), 1)
            release.set()
            stats = await asyncio.wait_for(task, 1)
            assert stats.finalized
            types = [m["type"] for m in connection.messages]
            assert types.count("response.item.create") == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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


@pytest.mark.parametrize("integrations", [False, True])
def test_camera_context_and_tool_describe_physical_camera(integrations):
    for shopping in (False, True):
        config = session_config(
            Settings(),
            snapshots=True,
            capture_source="camera",
            shopping=shopping,
            speakers=integrations,
            web=integrations,
            composio=integrations,
            sms_names=["alex"] if integrations else None,
            call_names=["alex"] if integrations else None,
            resume_context='{"previous_session_history":[]}' if integrations else None,
        )
        assert "badge's physical camera" in config["instructions"]
        backend = config["delegation"]["responses"]
        assert "physical camera" in backend["instructions"]
        tool = next(t for t in backend["tools"] if t["name"] == "capture_snapshot")
        assert "badge's physical camera" in tool["description"]
        assert "screen or camera" not in tool["description"]


@pytest.mark.parametrize("snapshots,shopping,calls", [(False, False, False), (True, True, True)])
def test_web_tools_coexist_with_other_tools(snapshots, shopping, calls):
    config = session_config(
        Settings(),
        web=True,
        snapshots=snapshots,
        shopping=shopping,
        call_names=["Alex"] if calls else None,
    )
    backend = config["delegation"]["responses"]
    names = {tool["name"] for tool in backend["tools"]}
    assert {"search_web", "read_web_page"} <= names
    if snapshots:
        assert "capture_snapshot" in names
    if shopping:
        assert "search_shopify" in names
    if calls:
        assert "call_contact" in names
    assert "no external action tools" not in backend["instructions"]
    assert "no other action tools" not in backend["instructions"]
    assert "untrusted data" in backend["instructions"]
    assert "Do not answer those questions from memory first" in config["instructions"]
    assert "unavailable in this session" not in config["instructions"]


def test_web_unavailable_is_explicit_without_tools():
    config = session_config(Settings())
    assert "Live web access is unavailable" in config["instructions"]
    assert "tools" not in config["delegation"]["responses"]


def test_web_only_session_keeps_audio_flowing_and_continues_with_sources():
    import json

    async def scenario():
        stop, played = asyncio.Event(), asyncio.Event()

        class Web:
            async def search(self, query):
                assert query == "current news"
                await played.wait()
                return {"status": "ok", "sources": [{"url": "https://example.com/news"}]}

        class Audio(FakeAudio):
            async def write(self, data):
                self.output.append(data)
                played.set()

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
                        name="search_web",
                        arguments='{"query":"current news"}',
                    ),
                ),
            ),
            event(
                "response.event",
                delegation_id="d",
                event=event("response.completed", response=NS(id="r", output=[])),
            ),
            audio_event(b"\x01\x00" * (FRAME_BYTES // 2)),
        ]
        conn, audio = Connection(script), Audio(stop)
        stats = await run_session(
            conn, audio, Settings(), stop, seconds=1, web=Web(), report=lambda _: None
        )
        assert audio.output and stats.finalized
        backend = conn.messages[0]["session"]["delegation"]["responses"]
        assert {"search_web", "read_web_page", "browse_web_page", "follow_web_link"} <= {
            t["name"] for t in backend["tools"]
        }
        outputs = [m["item"] for m in conn.messages if m.get("item", {}).get("call_id") == "c"]
        assert len(outputs) == 1
        assert json.loads(outputs[0]["output"])["sources"][0]["url"] == "https://example.com/news"
        assert any(m["type"] == "response.create" for m in conn.messages)

    asyncio.run(scenario())


def test_composio_only_session_executes_tools_without_blocking_voice_audio():
    import json

    async def scenario():
        stop, played = asyncio.Event(), asyncio.Event()

        class Apps:
            async def execute(self, name, args, delegation_id):
                assert name == "list_connected_app_tools"
                assert args == {"app": "gmail"} and delegation_id == "d"
                await played.wait()
                return {"status": "ok", "tools": []}

        class Audio(FakeAudio):
            async def write(self, data):
                self.output.append(data)
                played.set()

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
                        name="list_connected_app_tools",
                        arguments='{"app":"gmail"}',
                    ),
                ),
            ),
            event(
                "response.event",
                delegation_id="d",
                event=event("response.completed", response=NS(id="r", output=[])),
            ),
            audio_event(b"\x01\x00" * (FRAME_BYTES // 2)),
        ]
        connection, audio = Connection(script), Audio(stop)
        stats = await run_session(
            connection, audio, Settings(), stop, seconds=1, composio=Apps(), report=lambda _: None
        )
        assert audio.output and stats.finalized
        outputs = [
            m["item"] for m in connection.messages if m.get("item", {}).get("call_id") == "c"
        ]
        assert len(outputs) == 1 and json.loads(outputs[0]["output"])["status"] == "ok"
        assert any(m["type"] == "response.create" for m in connection.messages)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "apps", [{}, {"snapshots": True}, {"shopping": True}, {"call_names": ["Edmon"]}]
)
def test_speaker_lookup_tool_composes_with_other_features(apps):
    config = session_config(Settings(), speakers=True, **apps)
    backend = config["delegation"]["responses"]
    names = [tool["name"] for tool in backend["tools"]]
    assert names.count("identify_speaker") == 1
    assert backend["parallel_tool_calls"] is False
    assert "wait for its result" in config["instructions"]


def test_streaming_speaker_context_does_not_enable_legacy_identity_tool():
    config = session_config(Settings(), attributed_speakers=True)
    assert "exact audio block" in config["instructions"]
    assert "tools" not in config["delegation"]["responses"]
