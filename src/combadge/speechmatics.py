"""Streaming named-speaker attribution using the Speechmatics wire protocol."""

import asyncio
import io
import json
import math
import wave
from contextlib import asynccontextmanager

from combadge.audio import RATE
from combadge.speaker_input import SpeakerSpan

URL = "wss://global.rt.speechmatics.com/v2/"


def recognition_config(*, speakers=(), enrollment=False, rate=RATE):
    diarization = {"prefer_current_speaker": False}
    if speakers:
        diarization["speakers"] = list(speakers)
    if enrollment:
        diarization["get_speakers"] = True
    return {
        "message": "StartRecognition",
        "audio_format": {"type": "raw", "encoding": "pcm_s16le", "sample_rate": rate},
        "transcription_config": {
            "language": "en",
            "model": "enhanced",
            "diarization": "speaker",
            "speaker_diarization_config": diarization,
            "max_delay": 1,
            "max_delay_mode": "fixed",
            "enable_partials": False,
            "enable_entities": False,
            "conversation_config": {"end_of_utterance_silence_trigger": 0.5},
        },
    }


def sample_offset(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid Speechmatics timestamp")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid Speechmatics timestamp")
    return round(value * RATE)


def final_spans(message, names):
    """Final word intervals only; ASR confidence is not speaker confidence."""
    if message.get("message") != "AddTranscript":
        return None
    end = sample_offset(message["metadata"]["end_time"])
    spans = []
    for result in message["results"]:
        if result.get("type") != "word":
            continue
        start, stop = sample_offset(result["start_time"]), sample_offset(result["end_time"])
        if not 0 <= start < stop <= end:
            raise ValueError("Invalid Speechmatics word interval")
        alternatives = result.get("alternatives", [])
        label = alternatives[0].get("speaker") if alternatives else None
        spans.append(SpeakerSpan(start, stop, label if label in names else None))
    return end, spans


async def receive(websocket):
    message = json.loads(await websocket.recv())
    if not isinstance(message, dict):
        raise RuntimeError("Invalid Speechmatics response")
    if message.get("message") == "Error":
        # Server reason text can contain request material. Keep errors credential-safe.
        raise RuntimeError("Speechmatics rejected the recognition session")
    return message


@asynccontextmanager
async def session(api_key, config):
    from websockets.asyncio.client import connect

    async with connect(
        URL,
        additional_headers={"Authorization": "Bearer " + api_key},
        open_timeout=15,
        close_timeout=1,
        max_size=2_000_000,
        max_queue=16,
    ) as websocket:
        await websocket.send(json.dumps(config))
        async with asyncio.timeout(20):
            while (await receive(websocket)).get("message") != "RecognitionStarted":
                pass
        yield websocket


async def enroll(api_key, reference):
    """Generate a voiceprint for one explicitly supplied, single-person WAV."""
    with wave.open(io.BytesIO(reference.data), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("Enrollment requires mono PCM16")
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    config = recognition_config(enrollment=True, rate=rate)
    async with asyncio.timeout(45):
        async with session(api_key, config) as websocket:

            async def send():
                sequence = 0
                step = rate // 10 * 2
                for offset in range(0, len(pcm), step):
                    await websocket.send(pcm[offset : offset + step])
                    sequence += 1
                    await asyncio.sleep(0.1)
                await websocket.send(
                    json.dumps({"message": "EndOfStream", "last_seq_no": sequence})
                )

            sender = asyncio.create_task(send())
            try:
                identifiers = None
                finished = False
                while identifiers is None or not finished:
                    message = await receive(websocket)
                    if message.get("message") == "SpeakersResult":
                        speakers = message.get("speakers", [])
                        if len(speakers) != 1 or not speakers[0].get("speaker_identifiers"):
                            raise ValueError("Enrollment did not contain exactly one clear speaker")
                        identifiers = speakers[0]["speaker_identifiers"]
                        if not all(isinstance(item, str) and item for item in identifiers):
                            raise ValueError("Invalid speaker identifiers")
                    if message.get("message") == "EndOfTranscript":
                        finished = True
                await sender
                return {"label": reference.name, "speaker_identifiers": identifiers}
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)


class StreamingSpeakerInput:
    """Keep raw capture behind finalized attribution and Live context acceptance."""

    def __init__(self, api_key, references, *, profiles=None):
        from collections import deque

        from combadge.speaker_input import AttributionBuffer, ContextBeforeAudio

        self.api_key = api_key
        self.references = references
        self.profiles = profiles
        self.names = {r.name for r in references}
        self.buffer = AttributionBuffer(self.names)
        self.gate = ContextBeforeAudio()
        self.outgoing = asyncio.Queue(maxsize=100)
        self.available = asyncio.Event()
        self.drained = asyncio.Event()
        self.drained.set()
        self.playable = deque()
        self.connection_context = None
        self.websocket = None
        self.sequence = 0

    async def start(self):
        if self.profiles is None:
            self.profiles = [await enroll(self.api_key, ref) for ref in self.references]
        self.connection_context = session(self.api_key, recognition_config(speakers=self.profiles))
        self.websocket = await self.connection_context.__aenter__()

    def feed(self, pcm):
        self.buffer.feed(pcm)
        try:
            self.outgoing.put_nowait(pcm)
        except asyncio.QueueFull:
            raise RuntimeError("Streaming speaker connection fell behind") from None
        # Missing/late provider results expire per interval, never to a previous name.
        expired = self.buffer.received - 4 * RATE
        if expired > self.buffer.resolved:
            self.buffer.resolve(expired, [])
        self.available.set()

    def frame(self, size):
        if not self.playable:
            return bytes(size)
        data = self.playable.popleft()
        if len(data) > size:
            self.playable.appendleft(data[size:])
        if not self.playable:
            self.drained.set()
        return data[:size].ljust(size, b"\0")

    def observe(self, event):
        return self.gate.observe(event)

    async def run(self, connection, report, *, captions=True):
        async def upload():
            while True:
                pcm = await self.outgoing.get()
                await self.websocket.send(pcm)
                self.sequence += 1

        async def receive_labels():
            while True:
                message = await receive(self.websocket)
                parsed = final_spans(message, self.names)
                if parsed is not None:
                    end, spans = parsed
                    self.buffer.resolve(end, spans)
                    self.available.set()
                if message.get("message") == "EndOfTranscript":
                    raise RuntimeError("Speaker recognition stopped during voice capture")

        async def release():
            while True:
                await self.drained.wait()
                await self.available.wait()
                self.available.clear()
                chunks = []
                # Batch ready intervals into one context update, respecting the Live
                # context size limit and retaining per-speaker sample boundaries.
                for _ in range(12):
                    chunk = self.buffer.take(RATE)
                    if chunk is None:
                        break
                    chunks.append(chunk)
                if not chunks:
                    continue
                # The Live clock already received digital silence while waiting.
                # Replaying empty audio adds delay and irrelevant unknown contexts.
                if not any(any(c.pcm) for c in chunks):
                    self.available.set()
                    continue
                await self.gate.prepare(connection, chunks)
                self.drained.clear()
                self.playable.append(b"".join(c.pcm for c in chunks))
                self.available.set()
                if captions:
                    identities = list(dict.fromkeys(c.speaker or "unknown" for c in chunks))
                    report(
                        f"\n[Attributed input {chunks[0].start / RATE:.2f}–"
                        f"{chunks[-1].end / RATE:.2f}s: " + ", ".join(identities) + "]\n"
                    )

        async with asyncio.TaskGroup() as group:
            group.create_task(upload())
            group.create_task(receive_labels())
            group.create_task(release())

    async def close(self):
        if self.connection_context is not None:
            await self.connection_context.__aexit__(None, None, None)
            self.connection_context = None
        self.websocket = None
        self.playable.clear()
