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

    from combadge.speechmatics import StreamingSpeakerInput

    async def scenario():
        pipeline = StreamingSpeakerInput("private", [NS(name="Edmon")], profiles=[])
        events = asyncio.Queue()
        sent = []
        context_ready = asyncio.Event()

        async def recv():
            return json.dumps(await events.get())

        async def upload(pcm):
            pass

        async def context(message):
            sent.append(message)
            context_ready.set()

        pipeline.websocket = NS(send=upload, recv=recv)
        worker = asyncio.create_task(pipeline.run(NS(send=context), lambda _: None))
        pcm = b"\x01\x00" * RATE
        try:
            pipeline.feed(pcm)
            assert pipeline.frame(RATE * 2) == bytes(RATE * 2)
            message = transcript("Edmon")
            message["metadata"]["end_time"] = 1
            message["results"][0].update(start_time=0, end_time=1)
            message["results"][0]["alternatives"][0]["content"] = "Computer"
            await events.put(message)
            await asyncio.wait_for(context_ready.wait(), 1)
            assert not pipeline.batches[0]["ready"]
            assert "Computer" in sent[0]["content"]
            pipeline.observe(
                NS(type="session.thinking.appended", client_event_id=sent[0]["event_id"])
            )
            for _ in range(5):
                await asyncio.sleep(0)
            assert pipeline.batches[0]["ready"]
            # Four more seconds of clock input precede the scheduled original PCM.
            for _ in range(4):
                assert pipeline.frame(RATE * 2) == bytes(RATE * 2)
            assert pipeline.frame(RATE * 2) == pcm
            assert 0 not in pipeline.batches
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
    pipeline.feed(b"\x02\x00" * (RATE * 3))
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


@pytest.mark.parametrize("provider_delay_ticks", [50, 160])
def test_long_conversation_has_fixed_latency_and_bounded_memory(provider_delay_ticks):
    import asyncio
    import json
    from types import SimpleNamespace as NS

    from combadge.audio import FRAME_BYTES
    from combadge.speechmatics import StreamingSpeakerInput

    async def scenario():
        pipeline = StreamingSpeakerInput(
            "private", [NS(name="Edmon"), NS(name="Samuel")], profiles=[]
        )
        events = asyncio.Queue()
        pending_results, pending_acks, originals = [], [], []
        tick = 0
        sent_samples = 0
        seen_context = []

        async def upload(pcm):
            nonlocal sent_samples
            sent_samples += len(pcm) // 2
            if sent_samples % RATE == 0:
                second = sent_samples // RATE
                label = "Edmon" if second <= 15 else "Samuel" if second <= 30 else "S1"
                message = transcript(label)
                message["metadata"]["end_time"] = second
                message["results"][0].update(start_time=second - 1, end_time=second)
                message["results"][0]["alternatives"][0]["content"] = "hello"
                pending_results.append((tick + provider_delay_ticks, message))

        async def recv():
            return json.dumps(await events.get())

        async def context(message):
            seen_context.append(message)
            pending_acks.append((tick + 40, message["event_id"]))  # 800 ms acknowledgment.

        pipeline.websocket = NS(send=upload, recv=recv)
        worker = asyncio.create_task(pipeline.run(NS(send=context), lambda _: None, captions=False))
        try:
            for tick in range(50 * 45):
                pcm = (1 + tick % 30000).to_bytes(2, "little") * (FRAME_BYTES // 2)
                originals.append(pcm)
                pipeline.feed(pcm)
                for due, message in pending_results[:]:
                    if due <= tick:
                        pending_results.remove((due, message))
                        await events.put(message)
                for due, event_id in pending_acks[:]:
                    if due <= tick:
                        pending_acks.remove((due, event_id))
                        pipeline.observe(
                            NS(type="session.thinking.appended", client_event_id=event_id)
                        )
                for _ in range(5):
                    await asyncio.sleep(0)
                if worker.done():
                    worker.result()
                expected = bytes(FRAME_BYTES) if tick < 250 else originals[tick - 250]
                assert pipeline.frame(FRAME_BYTES) == expected
                assert len(pipeline.batches) <= 5
                assert pipeline.buffer.received - pipeline.buffer.released <= 3 * RATE
            joined = " ".join(m["content"] for m in seen_context)
            if provider_delay_ticks == 50:
                assert "Edmon" in joined and "Samuel" in joined
            else:
                assert "Edmon" not in joined and "Samuel" not in joined
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
