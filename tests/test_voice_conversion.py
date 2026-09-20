import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from combadge.audio import FRAME_BYTES, RATE
from combadge.config import Settings, load_settings
from combadge.voice_conversion import ConvertedAudio, LocalConverter


class Audio:
    def __init__(self, *args):
        self.output = []
        self.started = self.closed = self.drained = False

    def preflight(self):
        pass

    async def start(self):
        self.started = True

    async def read(self):
        return b"\x01\x00" * (FRAME_BYTES // 2)

    async def write(self, data):
        self.output.append(data)

    async def drain(self):
        self.drained = True

    async def close(self):
        self.closed = True


class Socket:
    def __init__(self, *, rate=RATE, reply=True, malformed=None):
        self.events = asyncio.Queue()
        self.events.put_nowait(json.dumps({"type": "ready", "rate": rate, "protocol": 1}))
        self.reply, self.malformed = reply, malformed
        self.sent = []
        self.closed = False

    async def recv(self):
        return await self.events.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, bytes) and self.reply:
            output = struct.pack("!I", len(data)) + b"\x03\x00" * (len(data) // 2)
            await self.events.put(output if self.malformed is None else self.malformed)
        elif isinstance(data, str):
            # Padding/tail has no associated input bytes but must still be played.
            await self.events.put(struct.pack("!I", 0) + b"\x04\x00")
            await self.events.put(json.dumps({"type": "flushed"}))

    async def close(self):
        self.closed = True


def connect_to(monkeypatch, socket):
    connect = AsyncMock(return_value=socket)
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    return connect


def test_microphone_is_unchanged_and_only_converted_output_and_tail_are_played(monkeypatch):
    async def scenario():
        socket, device = Socket(), Audio()
        connect = connect_to(monkeypatch, socket)
        audio = ConvertedAudio(device, "ws://localhost:8765", "private-test-token")
        await audio.start()
        assert await audio.read() == b"\x01\x00" * (FRAME_BYTES // 2)
        source = b"\x02\x00" * (FRAME_BYTES // 2)
        await audio.write(source)
        await audio.drain()
        assert device.output == [b"\x03\x00" * (FRAME_BYTES // 2), b"\x04\x00"]
        assert socket.sent[0] == source
        assert device.drained and audio.pending == 0
        assert connect.call_args.kwargs["additional_headers"] == {
            "Authorization": "Bearer private-test-token"
        }
        await audio.close()
        assert socket.closed and device.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [b"\0", struct.pack("!I", FRAME_BYTES + 2) + b"\0\0"])
def test_invalid_conversion_fails_without_playing_original_audio(monkeypatch, bad):
    async def scenario():
        device = Audio()
        socket = Socket(malformed=bad)
        connect_to(monkeypatch, socket)
        audio = ConvertedAudio(device, "ws://localhost:8765")
        await audio.start()
        try:
            await audio.write(bytes(FRAME_BYTES))
            with pytest.raises(RuntimeError, match="invalid PCM|Invalid voice converter"):
                await audio.drain()
            assert not device.output
        finally:
            await audio.close()

    asyncio.run(scenario())


def test_incompatible_server_releases_device_and_connection(monkeypatch):
    async def scenario():
        device, socket = Audio(), Socket(rate=48_000)
        connect_to(monkeypatch, socket)
        audio = ConvertedAudio(device, "ws://localhost:8765")
        with pytest.raises(RuntimeError, match="incompatible"):
            await audio.start()
        assert not device.started and device.closed and socket.closed

    asyncio.run(scenario())


def test_slow_converter_has_bounded_backlog(monkeypatch):
    async def scenario():
        device, socket = Audio(), Socket(reply=False)
        connect_to(monkeypatch, socket)
        audio = ConvertedAudio(device, "ws://localhost:8765")
        await audio.start()
        try:
            for _ in range(150):
                await audio.write(bytes(FRAME_BYTES))
            with pytest.raises(RuntimeError, match="fell behind"):
                await audio.write(bytes(FRAME_BYTES))
            assert not device.output
        finally:
            await audio.close()

    asyncio.run(scenario())


def test_configuration_loads_converter_without_exposing_token(tmp_path, monkeypatch):
    monkeypatch.delenv("COMBADGE_VOICE_CONVERSION_URL", raising=False)
    monkeypatch.delenv("COMBADGE_VOICE_CONVERSION_TOKEN", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "COMBADGE_VOICE_CONVERSION_URL=ws://localhost:8765\n"
        "COMBADGE_VOICE_CONVERSION_TOKEN=private-test-token\n"
        "COMBADGE_VOICE_CONVERSION_AUTOSTART=true\n"
    )
    settings = load_settings(path)
    assert settings.voice_conversion_url == "ws://localhost:8765"
    assert settings.voice_conversion_token == "private-test-token"
    assert settings.voice_conversion_autostart is True
    assert "private-test-token" not in repr(settings)


def test_phone_audio_bypasses_conversion_and_resumed_assistant_uses_it(monkeypatch):
    from combadge import live
    from combadge.phone import client

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def scenario():
        sessions, calls = [], []

        async def session(connection, audio, settings, stop, **kwargs):
            sessions.append(audio)
            return live.LiveStats(
                finalized=True,
                close_reason="close_requested",
                phone_contact="friend" if len(sessions) == 1 else None,
            )

        async def call(settings, contact, audio, stop, **kwargs):
            calls.append(audio)
            return {"status": "completed"}

        monkeypatch.setattr("websockets.asyncio.client.connect", lambda *a, **k: Connection())
        monkeypatch.setattr(live, "AlsaAudio", Audio)
        monkeypatch.setattr(live, "run_session", session)
        monkeypatch.setattr(client, "call_until_stopped", call)
        await live.connect_voice(
            Settings(voice_conversion_url="ws://localhost:8765"),
            check=False,
            input_device="default",
            output_device="default",
            seconds=1,
            captions=False,
            backend="alsa",
            phone_settings=SimpleNamespace(),
        )
        assert len(sessions) == 2 and all(isinstance(a, ConvertedAudio) for a in sessions)
        assert calls == [sessions[0].audio]
        assert not isinstance(calls[0], ConvertedAudio)
        assert sessions[0].audio is not sessions[1].audio

    asyncio.run(scenario())


def test_local_autostart_reuses_existing_server_without_terminating_it(monkeypatch):
    async def scenario():
        converter = LocalConverter("ws://127.0.0.1:8765")
        monkeypatch.setattr(converter, "_probe", AsyncMock())
        spawn = AsyncMock()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        await converter.start(asyncio.Event())
        await converter.close()
        spawn.assert_not_called()

    asyncio.run(scenario())


def test_local_autostart_rejects_remote_address():
    async def scenario():
        converter = LocalConverter("ws://192.0.2.1:8765")
        with pytest.raises(ValueError, match="local"):
            await converter.start(asyncio.Event())
        await converter.close()

    asyncio.run(scenario())


def test_local_autostart_passes_auth_and_cleans_up_owned_process(tmp_path, monkeypatch):
    from combadge import voice_conversion

    class Process:
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True

        async def wait(self):
            self.returncode = 0

    async def scenario():
        python = tmp_path / ".majel" / "env" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.touch()
        monkeypatch.setattr(voice_conversion, "ROOT", tmp_path)
        process = Process()
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        converter = LocalConverter("ws://127.0.0.1:8765", "private-test-token")
        monkeypatch.setattr(converter, "_probe", AsyncMock(side_effect=[OSError(), None]))
        try:
            await converter.start(asyncio.Event())
            assert spawn.call_args.args[0] == str(python)
            assert "private-test-token" not in spawn.call_args.args
            assert (
                spawn.call_args.kwargs["env"]["COMBADGE_VOICE_CONVERSION_TOKEN"]
                == "private-test-token"
            )
        finally:
            await converter.close()
        assert process.terminated and process.returncode == 0

    asyncio.run(scenario())
