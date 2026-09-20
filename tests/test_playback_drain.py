import asyncio
import base64
import sys

from combadge.audio import CommandAudio, FRAME_BYTES
from combadge.config import Settings
from combadge.live import run_session
from test_live import FakeConnection, event


def test_command_player_receives_eof_before_close(tmp_path):
    async def scenario():
        target = tmp_path / "tail.pcm"
        audio = CommandAudio(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            [sys.executable, "-c",
             "import sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
             str(target)],
        )
        try:
            await audio.start()
            await audio.write(b"\x01\x02" * 100)
            await audio.drain()
            assert target.read_bytes() == b"\x01\x02" * 100
            assert audio.player.returncode == 0
        finally:
            await audio.close()
    asyncio.run(scenario())


def test_session_timeout_drains_queued_speech():
    async def scenario():
        class Audio:
            output = bytearray()
            drained = False

            async def start(self):
                pass

            async def read(self):
                await asyncio.sleep(0.001)
                return bytes(FRAME_BYTES)

            async def write(self, data):
                await asyncio.sleep(0.03)
                self.output.extend(data)

            async def drain(self):
                self.drained = True

            async def close(self):
                assert self.drained

        payload = b"\x01\x00" * (FRAME_BYTES * 5 // 2)
        connection = FakeConnection([
            event("session.output_audio.delta", delta=base64.b64encode(payload).decode())
        ])
        audio = Audio()
        await run_session(connection, audio, Settings(), asyncio.Event(), seconds=0.04)
        assert bytes(audio.output) == payload
    asyncio.run(scenario())
