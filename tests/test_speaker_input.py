import pytest

from combadge.audio import RATE
from combadge.speaker_input import AttributionBuffer, SpeakerSpan


def drain(buffer):
    result = []
    while (chunk := buffer.take(RATE)) is not None:
        result.append(chunk)
    return result


def test_first_short_input_cannot_leave_before_attribution():
    buffer = AttributionBuffer({"Edmon", "Samuel"})
    pcm = b"\x01\x00" * 100  # Far shorter than the old six-second warm-up.
    buffer.feed(pcm)
    assert buffer.take(RATE) is None
    buffer.resolve(100, [SpeakerSpan(0, 100, "Edmon")])
    chunk = buffer.take(RATE)
    assert (chunk.start, chunk.end, chunk.speaker, chunk.pcm) == (0, 100, "Edmon", pcm)
    assert buffer.take(RATE) is None


def test_new_speaker_cannot_inherit_previous_label_while_pending():
    buffer = AttributionBuffer({"Edmon", "Samuel"})
    buffer.feed(b"\x01\x00" * 10)
    buffer.resolve(10, [SpeakerSpan(0, 10, "Edmon")])
    assert buffer.take(10).speaker == "Edmon"
    buffer.feed(b"\x02\x00" * 10)
    assert buffer.take(10) is None
    buffer.resolve(20, [SpeakerSpan(10, 20, "Samuel")])
    chunk = buffer.take(10)
    assert chunk.speaker == "Samuel"
    assert chunk.pcm == b"\x02\x00" * 10


def test_missing_generic_and_overlapping_speech_is_unknown():
    buffer = AttributionBuffer({"Edmon", "Samuel"})
    buffer.feed(b"\x01\x00" * 100)
    buffer.resolve(
        100,
        [
            SpeakerSpan(10, 30, "Edmon"),
            SpeakerSpan(20, 40, "Samuel"),
            SpeakerSpan(50, 60, "S1"),
            SpeakerSpan(70, 80, "Samuel"),
            SpeakerSpan(75, 85, None),
        ],
    )
    assert [(c.start, c.end, c.speaker) for c in drain(buffer)] == [
        (0, 10, None),
        (10, 20, "Edmon"),
        (20, 30, None),
        (30, 40, "Samuel"),
        (40, 70, None),
        (70, 75, "Samuel"),
        (75, 100, None),
    ]


def test_timeout_unknown_is_not_revised_by_late_result():
    buffer = AttributionBuffer({"Edmon"})
    buffer.feed(b"\x01\x00" * 100)
    buffer.resolve(50, [])
    buffer.resolve(100, [SpeakerSpan(0, 100, "Edmon")])
    chunks = drain(buffer)
    assert [(c.start, c.end, c.speaker) for c in chunks] == [(0, 50, None), (50, 100, "Edmon")]
    buffer.resolve(100, [SpeakerSpan(0, 100, "Edmon")])
    assert buffer.take(100) is None


def test_long_input_streams_in_bounded_chunks_without_losing_sample_alignment():
    buffer = AttributionBuffer({"Edmon", "Samuel"}, max_seconds=1)
    for index in range(300):
        pcm = index.to_bytes(2, "little") * RATE
        buffer.feed(pcm)
        label = "Edmon" if index % 2 else "Samuel"
        buffer.resolve((index + 1) * RATE, [SpeakerSpan(index * RATE, (index + 1) * RATE, label)])
        chunks = [buffer.take(701) for _ in range(RATE // 701 + 1)]
        assert b"".join(c.pcm for c in chunks) == pcm
        assert {c.speaker for c in chunks} == {label}
        assert buffer.released == buffer.received


def test_overflow_and_invalid_results_do_not_release_unattributed_audio():
    buffer = AttributionBuffer({"Edmon"}, max_seconds=1)
    buffer.feed(b"\x01\x00" * RATE)
    with pytest.raises(BufferError):
        buffer.feed(b"\x01\x00")
    for end, spans in [
        (RATE + 1, []),
        (RATE, [SpeakerSpan(0, RATE + 1, "Edmon")]),
        (RATE, [SpeakerSpan(-1, 1, "Edmon")]),
    ]:
        with pytest.raises(ValueError):
            buffer.resolve(end, spans)
    assert buffer.take(RATE) is None
    buffer.resolve(RATE, [])
    assert buffer.take(RATE).speaker is None


def test_audio_batch_waits_for_its_own_context_acknowledgment():
    import asyncio
    from types import SimpleNamespace as NS

    from combadge.speaker_input import AttributedAudio, ContextBeforeAudio

    async def scenario():
        gate = ContextBeforeAudio()
        messages = []

        async def send(message):
            messages.append(message)

        chunks = [
            AttributedAudio(0, 10, "Edmon", b"\x01\x00" * 10),
            AttributedAudio(10, 20, None, b"\x02\x00" * 10),
        ]
        task = asyncio.create_task(gate.prepare(NS(send=send), chunks))
        await asyncio.sleep(0)
        assert not task.done()
        assert not gate.observe(NS(type="session.thinking.appended", client_event_id="old"))
        assert not task.done()
        assert gate.observe(
            NS(type="session.thinking.appended", client_event_id=messages[0]["event_id"])
        )
        assert await task == chunks
        assert "unknown" in messages[0]["content"]
        assert gate.pending_id is None

    asyncio.run(scenario())


def test_context_failure_and_timeout_never_release_audio():
    import asyncio
    from types import SimpleNamespace as NS

    from combadge.speaker_input import AttributedAudio, ContextBeforeAudio

    async def scenario():
        gate = ContextBeforeAudio()
        chunks = [AttributedAudio(0, 1, None, b"\x01\x00")]

        async def reject(message):
            gate.observe(NS(type="error", error=NS(client_event_id=message["event_id"])))

        with pytest.raises(RuntimeError, match="rejected"):
            await gate.prepare(NS(send=reject), chunks)

        async def ignore(message):
            pass

        with pytest.raises(TimeoutError):
            await gate.prepare(NS(send=ignore), chunks, timeout=0.001)
        assert gate.pending_id is None

    asyncio.run(scenario())
