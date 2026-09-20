import asyncio
import struct

import pytest

from combadge.phone.ringback import invite_with_ringback, ring_frame


def test_ring_cadence_and_volume():
    assert len(ring_frame(1)) == 960
    assert max(abs(x[0]) for x in struct.iter_unpack("<h", ring_frame(1))) <= 3600
    assert any(ring_frame(99))
    assert not any(ring_frame(100))
    assert not any(ring_frame(299))
    assert ring_frame(300) == ring_frame(0)


@pytest.mark.parametrize("outcome", ["answer", "busy", "cancel", "speaker-fails"])
def test_ringback_stops_on_every_exit_without_opening_microphone(outcome):
    async def scenario():
        played, release = asyncio.Event(), asyncio.Event()
        frames = []

        class Audio:
            async def start_playback(self):
                pass

            async def start(self):
                raise AssertionError("Must not start capture while ringing")

            async def write(self, data):
                played.set()
                if outcome == "speaker-fails":
                    raise RuntimeError("speaker disconnected")
                frames.append(data)

        class Call:
            on_ringing = None
            stopped = False

            async def invite(self):
                try:
                    self.on_ringing()
                    await release.wait()
                    return outcome == "answer"
                finally:
                    self.stopped = True

        call = Call()
        task = asyncio.create_task(invite_with_ringback(call, Audio(), enabled=True))
        await asyncio.wait_for(played.wait(), 1)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "speaker-fails":
            with pytest.raises(RuntimeError, match="speaker disconnected"):
                await task
        else:
            release.set()
            assert await task is (outcome == "answer")
        count = len(frames)
        await asyncio.sleep(0.03)
        assert len(frames) == count
        assert call.stopped and call.on_ringing is None

    asyncio.run(scenario())


def test_playback_only_start_reuses_player_when_capture_starts(tmp_path):
    import sys

    from combadge.audio import CommandAudio

    async def scenario():
        output = tmp_path / "output.pcm"
        audio = CommandAudio(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            [
                sys.executable,
                "-c",
                "import pathlib,sys; "
                "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
                str(output),
            ],
        )
        try:
            await audio.start_playback()
            player = audio.player
            assert audio.recorder is None
            await audio.write(b"\x01\x00" * 480)
            await audio.start()
            assert audio.player is player
            assert audio.recorder.poll() is None
            await audio.write(b"\x02\x00" * 480)
            await audio.drain()
            assert output.read_bytes() == b"\x01\x00" * 480 + b"\x02\x00" * 480
        finally:
            await audio.close()
        assert audio.player.poll() is not None and audio.recorder.poll() is not None

    asyncio.run(scenario())
