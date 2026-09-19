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
