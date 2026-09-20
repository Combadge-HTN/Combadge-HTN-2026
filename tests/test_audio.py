import asyncio
import sys

from combadge.audio import FRAME_BYTES, CommandAudio


def test_local_cue_plays_without_starting_or_reading_microphone(tmp_path, monkeypatch):
    from combadge.audio import RATE, play_local_cue

    async def forbidden(*args):
        raise AssertionError("Local cue must not start or read capture")

    monkeypatch.setattr(CommandAudio, "start", forbidden)
    monkeypatch.setattr(CommandAudio, "read", forbidden)
    output = tmp_path / "cue.pcm"
    cue = b"\x12\x34" * 10944
    command = [
        sys.executable, "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
        str(output),
    ]
    asyncio.run(play_local_cue(command, cue))
    assert output.read_bytes() == cue + bytes(RATE // 5 * 2)


def test_fifo_cue_preserves_samples_with_partial_writes(monkeypatch):
    import stat
    from importlib.resources import files
    from types import SimpleNamespace
    from combadge.audio import play_fifo_cue

    output = bytearray()
    closed = []

    def write(fd, data):
        assert fd == 12345
        count = min(len(data), 100)
        output.extend(data[:count])
        return count

    async def scenario():
        monkeypatch.setattr("combadge.audio.os.open", lambda *args: 12345)
        monkeypatch.setattr("combadge.audio.os.fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFIFO))
        monkeypatch.setattr("combadge.audio.os.write", write)
        monkeypatch.setattr("combadge.audio.os.close", closed.append)
        await play_fifo_cue("unused")

    asyncio.run(scenario())
    cue = files("combadge").joinpath("assets/tng_chirp_stereo.pcm").read_bytes()
    assert output == cue + bytes(4410 * 4)
    assert closed == [12345]


def test_console_capture_needs_no_playback_process():
    async def scenario():
        audio = CommandAudio(
            [sys.executable, "-c", "import os,time; os.write(1, bytes(960)); time.sleep(20)"],
            None,
        )
        try:
            await audio.start()
            assert audio.player is None
            assert await asyncio.wait_for(audio.read(), 3) == bytes(FRAME_BYTES)
            await audio.write(b"\x01\x00" * 480)
        finally:
            await audio.close()
        assert audio.recorder.returncode is not None

    asyncio.run(scenario())


def test_console_preflight_does_not_require_aplay(monkeypatch):
    from combadge.audio import AlsaAudio

    monkeypatch.setattr(
        "combadge.audio.shutil.which", lambda name: "/bin/arecord" if name == "arecord" else None
    )
    audio = AlsaAudio(playback=False)
    audio.preflight()
    assert audio.playback_command is None


def test_native_helper_contract_handles_partial_reads_and_stops_processes(tmp_path):
    async def scenario():
        capture = tmp_path / "capture.py"
        capture.write_text(
            "import os, time\n"
            "os.write(1, b'\\x01')\n"
            "time.sleep(0.02)\n"
            f"os.write(1, b'\\x00' * {FRAME_BYTES - 1})\n"
            "time.sleep(20)\n"
        )
        output = tmp_path / "played.pcm"
        playback = tmp_path / "playback.py"
        playback.write_text(
            "import sys, pathlib, time\n"
            f"data = sys.stdin.buffer.read({FRAME_BYTES})\n"
            "pathlib.Path(sys.argv[1]).write_bytes(data)\n"
            "time.sleep(20)\n"
        )
        audio = CommandAudio(
            [sys.executable, str(capture)], [sys.executable, str(playback), str(output)]
        )
        try:
            await audio.start()
            frame = await asyncio.wait_for(audio.read(), 3)
            assert frame == b"\x01" + bytes(FRAME_BYTES - 1)
            await audio.write(frame)
            async with asyncio.timeout(3):
                while not output.exists() or output.stat().st_size < FRAME_BYTES:
                    await asyncio.sleep(0.01)
            assert output.read_bytes() == frame
        finally:
            await audio.close()
        assert audio.recorder.returncode is not None
        assert audio.player.returncode is not None

    asyncio.run(scenario())


def test_alsa_preflight_accepts_qnx_tools(monkeypatch):
    from combadge.audio import AlsaAudio

    monkeypatch.setattr(sys, "platform", "qnx8")
    monkeypatch.setattr("combadge.audio.shutil.which", lambda name: f"/system/bin/{name}")
    AlsaAudio().preflight()


def test_alsa_preflight_requires_both_tools(monkeypatch):
    import pytest

    from combadge.audio import AlsaAudio

    monkeypatch.setattr(
        "combadge.audio.shutil.which", lambda name: None if name == "aplay" else name
    )
    with pytest.raises(RuntimeError, match="aplay is missing"):
        AlsaAudio().preflight()


def test_playback_larger_than_pipe_buffer_arrives_intact(tmp_path):
    async def scenario():
        payload = bytes(range(256)) * 4096
        output = tmp_path / "played.pcm"
        audio = CommandAudio(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            [
                sys.executable,
                "-c",
                "import pathlib, sys, time; "
                f"data=sys.stdin.buffer.read({len(payload)}); "
                "pathlib.Path(sys.argv[1]).write_bytes(data); time.sleep(20)",
                str(output),
            ],
        )
        try:
            await audio.start()
            await asyncio.wait_for(audio.write(payload), 5)
            async with asyncio.timeout(3):
                while not output.exists() or output.stat().st_size != len(payload):
                    await asyncio.sleep(0.01)
            assert output.read_bytes() == payload
        finally:
            await audio.close()

    asyncio.run(scenario())


def test_shutdown_releases_blocked_capture_and_playback():
    async def scenario():
        command = [sys.executable, "-c", "import time; time.sleep(20)"]
        audio = CommandAudio(command, command)
        await audio.start()
        read = asyncio.create_task(audio.read())
        write = asyncio.create_task(audio.write(bytes(2 * 1024 * 1024)))
        await asyncio.sleep(0.1)
        await asyncio.wait_for(audio.close(), 5)
        results = await asyncio.wait_for(asyncio.gather(read, write, return_exceptions=True), 2)
        assert all(isinstance(result, RuntimeError) for result in results)
        assert audio.recorder.returncode is not None
        assert audio.player.returncode is not None
        await audio.close()

    asyncio.run(scenario())
