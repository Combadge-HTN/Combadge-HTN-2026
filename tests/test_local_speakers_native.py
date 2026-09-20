"""Opt-in integration checks against the real native binary and pinned model.

Set COMBADGE_TEST_SPEAKER_WORKER and COMBADGE_TEST_SPEAKER_MODEL to run.
No recordings, microphone, cloud services, or API keys are used.
"""

import asyncio
import json
import math
import os
import random
import struct
from pathlib import Path

import pytest

from combadge.local_speakers import Worker

WORKER = os.environ.get("COMBADGE_TEST_SPEAKER_WORKER")
MODEL = os.environ.get("COMBADGE_TEST_SPEAKER_MODEL")
pytestmark = pytest.mark.skipif(not (WORKER and MODEL), reason="native worker/model not configured")


def signal(rate):
    # Modulated multitone exercises the anti-aliasing and feature pipeline.
    return b"".join(
        struct.pack(
            "<h",
            round(
                7000
                * (1 + 0.3 * math.sin(2 * math.pi * 3 * i / rate))
                * math.sin(2 * math.pi * 437 * i / rate)
            ),
        )
        for i in range(rate * 2)
    )


def test_native_process_reuses_model_and_resampling_preserves_embedding():
    async def scenario():
        worker = Worker(WORKER, MODEL)
        await worker.start()
        process = worker.process
        try:
            reference = await worker.embed(signal(16000), 16000)
            # QNX previously treated an idle, still-open stdin as EOF after one
            # request. Keep the same worker alive across realistic input gaps.
            await asyncio.sleep(0.1)
            assert process.returncode is None
            again = await worker.embed(signal(16000), 16000)
            resampled = await worker.embed(signal(24000), 24000)
            assert sum(a * b for a, b in zip(reference, again)) > 0.99999
            # Rate conversion adds PCM16 rounding and boundary transients. Require
            # a close embedding, not bit equality across independently sampled signals.
            assert sum(a * b for a, b in zip(reference, resampled)) > 0.95
            assert abs(sum(v * v for v in reference) - 1) < 1e-5
            assert worker.process is process and process.returncode is None
        finally:
            await worker.close()
        assert process.returncode is not None

    asyncio.run(scenario())


def test_native_rejects_oversized_request_without_allocating_payload():
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            WORKER,
            MODEL,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(30):
                assert await process.stdout.readexactly(4) == b"CSP1"
                process.stdin.write(struct.pack("<II", 24000, 2**32 - 1))
                await process.stdin.drain()
                assert await process.wait() != 0
                assert b"expected 0.5-10 seconds" in await process.stderr.read()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(scenario())


def test_native_matches_reference_embedding_for_generated_signal():
    # No recorded voice: deterministic chirp + tone + noise. The reference was
    # cross-checked against the official Python ONNX Runtime CPU implementation.
    # Self-consistency alone did not catch an incorrect QNX inference result.
    rng = random.Random(739)
    pcm = bytearray()
    for i in range(36000):
        t = i / 24000
        value = (
            4000 * math.sin(2 * math.pi * (130 * t + 110 * t * t))
            + 2000 * math.sin(2 * math.pi * 1301 * t)
            + 1200 * rng.uniform(-1, 1)
        )
        pcm.extend(struct.pack("<h", round(value)))
    reference = json.loads(
        (Path(__file__).parent / "fixtures/campplus-synthetic-embedding.json").read_text()
    )

    async def scenario():
        worker = Worker(WORKER, MODEL)
        await worker.start()
        try:
            actual = await worker.embed(bytes(pcm), 24000)
            assert sum(a * b for a, b in zip(reference, actual)) > 0.999
        finally:
            await worker.close()

    asyncio.run(scenario())
