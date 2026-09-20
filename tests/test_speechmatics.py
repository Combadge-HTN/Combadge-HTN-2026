import math

import pytest

from combadge.audio import RATE
from combadge.speaker_input import SpeakerSpan
from combadge.speechmatics import final_spans, recognition_config, sample_offset


def transcript(label):
    return {
        "message": "AddTranscript",
        "metadata": {"end_time": 0.6},
        "results": [
            {
                "type": "word",
                "start_time": 0.1,
                "end_time": 0.6,
                "alternatives": [{"speaker": label, "confidence": 0.99}],
            }
        ],
    }


@pytest.mark.parametrize(
    "label,expected",
    [("Edmon", "Edmon"), ("Samuel", "Samuel"), ("S1", None), ("UU", None), (None, None)],
)
def test_final_word_identity_is_bound_to_sample_interval(label, expected):
    end, spans = final_spans(transcript(label), {"Edmon", "Samuel"})
    assert end == round(0.6 * RATE)
    assert spans == [SpeakerSpan(round(0.1 * RATE), end, expected)]


def test_partial_results_cannot_release_audio():
    message = transcript("Edmon")
    message["message"] = "AddPartialTranscript"
    assert final_spans(message, {"Edmon"}) is None


@pytest.mark.parametrize("value", [True, "1", -1, math.nan, math.inf])
def test_bad_provider_timestamps_are_rejected(value):
    with pytest.raises(ValueError):
        sample_offset(value)


def test_configuration_allows_new_speakers_and_uses_existing_qnx_audio_format():
    config = recognition_config(speakers=[{"label": "Edmon", "speaker_identifiers": ["opaque"]}])
    assert config["audio_format"] == {"type": "raw", "encoding": "pcm_s16le", "sample_rate": RATE}
    diarization = config["transcription_config"]["speaker_diarization_config"]
    assert diarization["prefer_current_speaker"] is False
    assert "max_speakers" not in diarization
    assert config["transcription_config"]["enable_partials"] is False


def test_streaming_pipeline_never_releases_pcm_before_provider_and_live_ack():
    import asyncio
    import json
    from types import SimpleNamespace as NS

    from combadge.audio import FRAME_BYTES
    from combadge.speechmatics import StreamingSpeakerInput

    async def scenario():
        pipeline = StreamingSpeakerInput("private", [NS(name="Edmon")], profiles=[])
        events = asyncio.Queue()
        sent = []
        context_ready = asyncio.Event()
        audio_uploaded = asyncio.Event()

        async def recv():
            return json.dumps(await events.get())

        async def upload(pcm):
            assert pcm == b"\x01\x00" * (FRAME_BYTES // 2)
            audio_uploaded.set()

        async def context(message):
            sent.append(message)
            context_ready.set()

        pipeline.websocket = NS(send=upload, recv=recv)
        worker = asyncio.create_task(pipeline.run(NS(send=context), lambda _: None))
        pcm = b"\x01\x00" * (FRAME_BYTES // 2)
        try:
            pipeline.feed(pcm)
            await asyncio.wait_for(audio_uploaded.wait(), 1)
            assert pipeline.frame(FRAME_BYTES) == bytes(FRAME_BYTES)
            message = transcript("Edmon")
            message["metadata"]["end_time"] = 0.02
            message["results"][0].update(start_time=0, end_time=0.02)
            await events.put(message)
            await asyncio.wait_for(context_ready.wait(), 1)
            assert pipeline.frame(FRAME_BYTES) == bytes(FRAME_BYTES)
            pipeline.observe(
                NS(type="session.thinking.appended", client_event_id=sent[0]["event_id"])
            )
            for _ in range(5):
                await asyncio.sleep(0)
                if pipeline.playable:
                    break
            assert pipeline.frame(FRAME_BYTES) == pcm
            assert pipeline.frame(FRAME_BYTES) == bytes(FRAME_BYTES)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_late_provider_results_expire_to_unknown_without_previous_name():
    from types import SimpleNamespace as NS

    from combadge.speechmatics import StreamingSpeakerInput

    pipeline = StreamingSpeakerInput("private", [NS(name="Edmon")], profiles=[])
    pipeline.feed(b"\x01\x00" * RATE)
    pipeline.buffer.resolve(RATE, [SpeakerSpan(0, RATE, "Edmon")])
    assert pipeline.buffer.take(RATE).speaker == "Edmon"
    pipeline.feed(b"\x02\x00" * (RATE * 5))
    assert pipeline.buffer.take(RATE).speaker is None
    assert pipeline.buffer.take(RATE) is None


def test_transient_startup_quota_retries_but_active_session_never_replays(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from combadge.speechmatics import SpeechmaticsError, session

    async def scenario():
        rejected = NS(
            send=AsyncMock(),
            close=AsyncMock(),
            recv=AsyncMock(
                return_value=json.dumps(
                    {"message": "Error", "type": "quota_exceeded", "reason": "private details"}
                )
            ),
        )
        accepted = NS(
            send=AsyncMock(),
            close=AsyncMock(),
            recv=AsyncMock(return_value=json.dumps({"message": "RecognitionStarted"})),
        )
        connect = AsyncMock(side_effect=[rejected, accepted])
        sleep = AsyncMock()
        monkeypatch.setattr("websockets.asyncio.client.connect", connect)
        monkeypatch.setattr("combadge.speechmatics.asyncio.sleep", sleep)
        with pytest.raises(SpeechmaticsError):
            async with session("secret", recognition_config()) as ws:
                assert ws is accepted
                raise SpeechmaticsError("quota_exceeded")
        assert connect.await_count == 2
        sleep.assert_awaited_once_with(5)
        rejected.close.assert_awaited_once()
        accepted.close.assert_awaited_once()

    asyncio.run(scenario())
