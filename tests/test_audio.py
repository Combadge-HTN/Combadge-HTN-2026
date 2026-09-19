import asyncio
import sys

from commbadge.audio import FRAME_BYTES, CommandAudio


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
    from commbadge.audio import AlsaAudio

    monkeypatch.setattr(sys, "platform", "qnx8")
    monkeypatch.setattr("commbadge.audio.shutil.which", lambda name: f"/system/bin/{name}")
    AlsaAudio().preflight()


def test_alsa_preflight_requires_both_tools(monkeypatch):
    import pytest

    from commbadge.audio import AlsaAudio

    monkeypatch.setattr(
        "commbadge.audio.shutil.which", lambda name: None if name == "aplay" else name
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
