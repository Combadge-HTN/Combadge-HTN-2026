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


@pytest.mark.parametrize("status", [100, 180, 183])
def test_sip_provisional_response_starts_ringback_only_for_call_progress(status):
    from unittest.mock import AsyncMock, Mock

    from combadge.phone.config import SipSettings
    from combadge.phone.direct import SipCall, SipRejected
    from combadge.phone.sip import Message

    async def scenario():
        config = SipSettings(
            "test.pstn.twilio.com", "badge", "password", "+14165550100", {"edmon": "+14165550101"}
        )
        reports = []
        call = SipCall(config, config.contacts["edmon"], reports.append)
        call.sdp = b""
        call.request = Mock(return_value=None)
        call.send = AsyncMock()
        played = asyncio.Event()
        frames = []

        class Audio:
            async def start_playback(self):
                pass

            async def start(self):
                raise AssertionError("Capture must remain closed before answer")

            async def write(self, data):
                frames.append(data)
                played.set()

        def response(code):
            return Message(
                f"SIP/2.0 {code} Test", [("Call-ID", call.call_id), ("CSeq", f"{call.cseq} INVITE")]
            )

        task = asyncio.create_task(invite_with_ringback(call, Audio(), enabled=True))
        try:
            await call.messages.put(response(status))
            if status == 100:
                await asyncio.sleep(0.04)
                assert not frames and not reports
            else:
                await asyncio.wait_for(played.wait(), 1)
                assert any(frames[0])
                assert reports == [f"\nCall: {'ringing' if status == 180 else 'connecting'}\n"]
            await call.messages.put(response(486))
            with pytest.raises(SipRejected):
                await asyncio.wait_for(task, 1)
            count = len(frames)
            await asyncio.sleep(0.04)
            assert len(frames) == count
            assert call.ended and call.on_ringing is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
