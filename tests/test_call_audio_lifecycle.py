"""Call handoff with real subprocess pipes, without cloud services or audio devices."""

import asyncio
import sys

import pytest

from combadge.audio import FRAME_BYTES, CommandAudio
from combadge.phone import client, direct
from combadge.phone.config import SipSettings


@pytest.mark.parametrize("outcome", ["answer", "busy", "cancel", "capture-fails", "playback-fails"])
def test_real_audio_helpers_remain_closed_while_ringing(tmp_path, monkeypatch, outcome):
    async def scenario():
        capture = tmp_path / "capture.py"
        playback = tmp_path / "playback.py"
        played = tmp_path / "played.pcm"
        capture.write_text(
            "import os,sys,time\n"
            "if len(sys.argv)>1: raise SystemExit(1)\n"
            "os.write(2,b'capture-generation\\n')\n"
            f"os.write(1,b'\\x01\\x00'*{FRAME_BYTES // 2})\n"
            "time.sleep(20)\n"
        )
        playback.write_text(
            "import pathlib,sys,time\n"
            "if len(sys.argv)>2: raise SystemExit(1)\n"
            f"data=sys.stdin.buffer.read({FRAME_BYTES})\n"
            "pathlib.Path(sys.argv[1]).write_bytes(data)\n"
            "time.sleep(20)\n"
        )
        audio = CommandAudio(
            [sys.executable, str(capture)],
            [sys.executable, str(playback), str(played)],
        )
        # Exercise the same object through a prior live-session open and close.
        await audio.start()
        assert await asyncio.wait_for(audio.read(), 2) == b"\x01\x00" * (FRAME_BYTES // 2)
        await audio.close()
        previous_capture, previous_player = audio.recorder, audio.player
        assert audio._logs["capture"].count("capture-generation") == 1
        if outcome == "capture-fails":
            audio.capture_command.append("fail")
        if outcome == "playback-fails":
            audio.playback_command.append("fail")
        ringing, answer, stop = (asyncio.Event() for _ in range(3))

        class Call:
            invited = ended = closed = False
            sent_packets = received_packets = rejected_packets = 0

            async def open(self):
                pass

            async def invite(self):
                self.invited = True
                ringing.set()
                await answer.wait()
                return outcome != "busy"

            async def signaling(self):
                await asyncio.Future()

            async def media_loop(self, media):
                frame = await media.read()
                if outcome == "playback-fails":
                    async with asyncio.timeout(2):
                        while audio.player.poll() is None:
                            await asyncio.sleep(0.01)
                await media.write(frame)
                async with asyncio.timeout(2):
                    while not played.exists() or played.stat().st_size != FRAME_BYTES:
                        await asyncio.sleep(0.01)
                assert played.read_bytes() == frame

            async def hangup(self):
                self.ended = True

            async def close(self):
                self.closed = True

        call = Call()
        monkeypatch.setattr(direct, "crypto", lambda: None)
        monkeypatch.setattr(direct, "SipCall", lambda *_: call)
        settings = SipSettings(
            "test.pstn.twilio.com",
            "badge",
            "test-password",
            "+14165550100",
            {"edmon": "+14165550101"},
        )
        task = asyncio.create_task(client.call_until_stopped(settings, "edmon", audio, stop))
        try:
            await asyncio.wait_for(ringing.wait(), 2)
            # Ringing remains gated until the test explicitly answers or cancels.
            assert audio.recorder is previous_capture and previous_capture.poll() is not None
            assert audio.player is previous_player and previous_player.poll() is not None
            if outcome == "cancel":
                stop.set()
            else:
                answer.set()
            if outcome.endswith("fails"):
                with pytest.raises(RuntimeError, match="Microphone stopped|Speaker stopped"):
                    await asyncio.wait_for(task, 3)
            else:
                await asyncio.wait_for(task, 3)
            assert call.ended and call.closed
            assert audio.recorder.poll() is not None and audio.player.poll() is not None
            if outcome == "answer":
                assert audio.recorder is not previous_capture
                assert audio._logs["capture"].count("capture-generation") == 1
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await audio.close()

    asyncio.run(scenario())
