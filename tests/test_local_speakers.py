import asyncio
import math
import struct
from unittest.mock import AsyncMock, patch

import pytest

from combadge.config import Settings
from combadge.live import session_config
from combadge.local_speakers import INSTRUCTIONS, LocalSpeakerInput, Matcher, Worker, unit_vector


def vector(index):
    return tuple(float(i == index) for i in range(512))


def pcm(seconds, value=1000):
    return struct.pack("<h", value) * int(24000 * seconds)


class FakeWorker:
    def __init__(self):
        self.value = vector(0)
        self.embed = AsyncMock(side_effect=lambda _: self.value)
        self.close = AsyncMock()


def pipeline():
    worker = FakeWorker()
    source = LocalSpeakerInput(worker, ())
    source.matcher = Matcher({"Edmon": vector(0), "Samuel": vector(1)})
    return source, worker


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_matcher_rejects_unknown_ambiguous_and_invalid_embeddings():
    matcher = Matcher({"Edmon": vector(0), "Samuel": vector(1)})
    assert matcher.match(vector(0)).name == "Edmon"
    assert matcher.match(vector(1)).name == "Samuel"
    assert matcher.match(vector(2)).name is None
    assert matcher.match([a + b for a, b in zip(vector(0), vector(1))]).name is None
    for invalid in [[], [0] * 512, [math.nan] * 512, [math.inf] * 512]:
        with pytest.raises(ValueError):
            unit_vector(invalid)


def test_microphone_is_bit_exact_and_never_waits_for_identification():
    source, _ = pipeline()
    for i in range(100):
        data = pcm(0.02, i * 123 - 5000)
        source.feed(data)
        assert source.frame(len(data)) == data
    assert len(source.buffer) <= source.window_bytes
    assert source.latest is not None
    assert len(source.latest[2]) == source.window_bytes
    with pytest.raises(ValueError):
        source.frame(2)


def test_sparse_updates_clear_old_identity_on_new_unknown_speech():
    async def scenario():
        source, worker = pipeline()
        connection = type("Connection", (), {"send": AsyncMock()})()
        task = asyncio.create_task(source.run(connection, lambda _: None))
        try:
            source.feed(pcm(1.5))
            await settle()
            assert connection.send.await_count == 1
            assert "Edmon" in connection.send.call_args.args[0]["content"]
            source.feed(pcm(0.5))
            await settle()
            assert connection.send.await_count == 1  # No per-word/per-window spam.
            source.feed(pcm(0.5, 0))
            worker.value = vector(2)
            source.feed(pcm(0.02))
            await settle()
            assert connection.send.await_count == 2
            assert "cannot identify" in connection.send.call_args.args[0]["content"]
            source.feed(pcm(1.5))
            await settle()
            assert connection.send.await_count == 2  # Repeated unknown is deduplicated.
            source.feed(pcm(0.5, 0))
            worker.value = vector(1)
            source.feed(pcm(1.5))
            await settle()
            assert connection.send.await_count == 3
            assert "Samuel" in connection.send.call_args.args[0]["content"]
            for call in connection.send.call_args_list:
                message = call.args[0]
                assert message["type"] == "session.thinking.append"
                assert set(message) == {"type", "content", "event_id", "delegation_id"}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_inflight_old_speaker_cannot_overwrite_new_turn():
    async def scenario():
        source, worker = pipeline()
        started, release = asyncio.Event(), asyncio.Event()

        async def embed(_):
            started.set()
            await release.wait()
            return vector(0)

        worker.embed = embed
        connection = type("Connection", (), {"send": AsyncMock()})()
        task = asyncio.create_task(source.run(connection, lambda _: None))
        try:
            source.feed(pcm(1.5))
            await started.wait()
            source.feed(pcm(0.5, 0))
            source.feed(pcm(0.02))
            release.set()
            await settle()
            await settle()
            connection.send.assert_not_awaited()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_slow_worker_has_one_latest_job_and_stale_result_is_discarded():
    async def scenario():
        source, worker = pipeline()
        started, release = asyncio.Event(), asyncio.Event()

        async def embed(_):
            started.set()
            await release.wait()
            return vector(0)

        worker.embed = embed
        connection = type("Connection", (), {"send": AsyncMock()})()
        task = asyncio.create_task(source.run(connection, lambda _: None))
        try:
            source.feed(pcm(1.5))
            await started.wait()
            for _ in range(100):
                source.feed(pcm(0.5))
            assert len(source.buffer) == source.window_bytes
            assert source.latest[1] == source.total
            # A new utterance invalidates both queued and in-flight jobs.
            source.feed(pcm(0.5, 0))
            source.feed(pcm(0.02))
            release.set()
            await settle()
            connection.send.assert_not_awaited()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_local_prompt_preserves_native_audio_contract_and_no_identity_tool():
    config = session_config(
        Settings(), attributed_speakers=True, attribution_instructions=INSTRUCTIONS
    )
    assert INSTRUCTIONS in config["instructions"]
    assert "audio is buffered" not in config["instructions"]
    assert "exact audio block" not in config["instructions"]
    assert "tools" not in config["delegation"]["responses"]


def test_wrong_model_never_starts_process(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"incorrect model")
    worker = Worker(tmp_path / "worker", model)
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        with pytest.raises(ValueError, match="checksum"):
            asyncio.run(worker.start())
    spawn.assert_not_awaited()


def test_native_protocol_failure_closes_worker():
    async def scenario():
        worker = Worker("worker", "model")
        process = type("Process", (), {})()
        process.returncode = None
        process.stdin = type("Input", (), {"write": lambda *a: None, "drain": AsyncMock()})()
        process.stdout = type(
            "Output",
            (),
            {"readexactly": AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 2048))},
        )()
        worker.process = process
        worker.close = AsyncMock()
        with pytest.raises(asyncio.IncompleteReadError):
            await worker.embed(pcm(1))
        worker.close.assert_awaited_once()

    asyncio.run(scenario())


def test_new_turn_clears_identity_even_while_worker_is_blocked():
    async def scenario():
        source, worker = pipeline()
        source.last_name = "Edmon"
        source.speaking = True
        source.generation = 1
        started, release = asyncio.Event(), asyncio.Event()

        async def embed(_):
            started.set()
            await release.wait()
            return vector(0)

        worker.embed = embed
        connection = type("Connection", (), {"send": AsyncMock()})()
        task = asyncio.create_task(source.run(connection, lambda _: None))
        try:
            source.feed(pcm(1.5))
            await started.wait()
            source.feed(pcm(0.5, 0))
            source.feed(pcm(0.02))
            await settle()
            assert not release.is_set()
            assert "cannot identify" in connection.send.call_args.args[0]["content"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_short_question_gets_final_window_and_ending_silence_does_not_discard_it():
    async def scenario():
        source, _ = pipeline()
        connection = type("Connection", (), {"send": AsyncMock()})()
        task = asyncio.create_task(source.run(connection, lambda _: None))
        try:
            source.feed(pcm(0.8))
            assert source.latest is None
            source.feed(pcm(0.4, 0))
            assert not source.speaking
            await settle()
            assert "Edmon" in connection.send.call_args.args[0]["content"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("next_index,next_name", [(0, "Edmon"), (1, "Samuel")])
def test_touch_sessions_discard_old_audio_and_publish_fresh_identity(next_index, next_name):
    async def scenario():
        source, worker = pipeline()
        worker.start = AsyncMock()
        matcher = source.matcher
        first = type("Connection", (), {"send": AsyncMock()})()
        task = asyncio.create_task(source.run(first, lambda _: None))
        try:
            source.feed(pcm(1.5))
            await settle()
            assert "Edmon" in first.send.call_args.args[0]["content"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A session can end with a job, PCM frame, and speaker estimate queued.
        source.feed(pcm(0.5))
        await source.close()
        worker.embed.reset_mock()
        worker.value = vector(next_index)
        second = type("Connection", (), {"send": AsyncMock()})()
        with patch("combadge.local_speakers.enroll", new=AsyncMock(return_value=matcher)):
            await source.start()
        task = asyncio.create_task(source.run(second, lambda _: None))
        try:
            await settle()
            worker.embed.assert_not_awaited()
            second.send.assert_not_awaited()
            source.feed(pcm(1.5))
            await settle()
            second.send.assert_awaited_once()
            assert next_name in second.send.call_args.args[0]["content"]
            assert second.send.call_args.args[0]["event_id"] == "local_speaker_1"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await source.close()

    asyncio.run(scenario())
