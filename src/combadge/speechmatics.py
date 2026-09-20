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


class SpeechmaticsError(RuntimeError):
    def __init__(self, category):
        self.category = category
        super().__init__(f"Speechmatics rejected the recognition session ({category})")


async def receive(websocket):
    message = json.loads(await websocket.recv())
    if not isinstance(message, dict):
        raise RuntimeError("Invalid Speechmatics response")
    if message.get("message") == "Error":
        # Server reason text can contain request material. Keep errors credential-safe.
        kind = message.get("type")
        known_errors = {
            "invalid_message",
            "invalid_model",
            "invalid_language",
            "invalid_config",
            "invalid_audio_type",
            "invalid_output_format",
            "not_authorised",
            "not_allowed",
            "job_error",
            "protocol_error",
            "quota_exceeded",
            "timelimit_exceeded",
            "idle_timeout",
            "session_timeout",
            "unknown_error",
        }
        kind = kind if kind in known_errors else "unknown_error"
        raise SpeechmaticsError(kind)
    return message


@asynccontextmanager
async def session(api_key, config):
    from websockets.asyncio.client import connect

    # The service documents a 5–10 second retry interval for these startup errors.
    # Enrollment can briefly retain a slot after EndOfTranscript/socket close.
    # Never retry after yielding: replaying a running conversation is not safe.
    for attempt in range(3):
        websocket = await connect(
            URL,
            additional_headers={"Authorization": "Bearer " + api_key},
            open_timeout=15,
            close_timeout=1,
            max_size=2_000_000,
            max_queue=16,
        )
        try:
            await websocket.send(json.dumps(config))
            async with asyncio.timeout(20):
                while (await receive(websocket)).get("message") != "RecognitionStarted":
                    pass
        except BaseException as error:
            await websocket.close()
            if (
                isinstance(error, SpeechmaticsError)
                and error.category in {"quota_exceeded", "job_error"}
                and attempt < 2
            ):
                await asyncio.sleep(5)
                continue
            raise
        break
    try:
        yield websocket
    finally:
        await websocket.close()


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
    """A fixed-delay input stream; context preparation runs ahead of playback.

    Every capture sample has one scheduled Live sample. Provider results expire
    to unknown before that deadline. Context updates are pipelined, so a long
    utterance does not accumulate a new acknowledgment delay for every packet.
    """

    DELAY = 5 * RATE
    PROVIDER_DEADLINE = 2 * RATE

    def __init__(self, api_key, references, *, profiles=None):
        from combadge.speaker_input import AttributionBuffer

        self.api_key = api_key
        self.references = references
        self.profiles = profiles
        self.names = {r.name for r in references}
        self.buffer = AttributionBuffer(self.names)
        self.outgoing = asyncio.Queue(maxsize=100)
        self.available = asyncio.Event()
        self.batches = {}
        self.gates = {}
        self.words = []
        self.live_samples = 0
        self.prepared = 0
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
        expired = self.buffer.received - self.PROVIDER_DEADLINE
        if expired > self.buffer.resolved:
            self.buffer.resolve(expired, [])
        self.available.set()

    def frame(self, size):
        if size <= 0 or size % 2:
            raise ValueError("Expected a positive PCM16 frame size")
        samples = size // 2
        if self.live_samples < self.DELAY:
            self.live_samples += samples
            return bytes(size)
        source = self.live_samples - self.DELAY
        index = source // RATE
        batch = self.batches.get(index)
        if batch is None or not batch["ready"]:
            # Do not shift the stream and invalidate every later attribution time.
            raise RuntimeError("Speaker context missed its audio deadline; stopping voice")
        offset = (source - index * RATE) * 2
        pcm = batch["pcm"][offset : offset + size]
        if len(pcm) != size:
            raise RuntimeError("Audio frame crosses the attribution packet boundary")
        self.live_samples += samples
        if source + samples == (index + 1) * RATE:
            self.batches.pop(index)
        return pcm

    def observe(self, event):
        for gate in tuple(self.gates.values()):
            if gate.observe(event):
                return True
        return False

    async def run(self, connection, report, *, captions=True):
        from combadge.speaker_input import AttributedAudio, ContextBeforeAudio

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
                    for word, span in zip(
                        (r for r in message["results"] if r.get("type") == "word"), spans
                    ):
                        if span.end > self.prepared:
                            alternatives = word.get("alternatives") or [{}]
                            text = str(alternatives[0].get("content", ""))
                            self.words.append((span.start, span.end, text[:80]))
                    self.available.set()
                if message.get("message") == "EndOfTranscript":
                    raise RuntimeError("Speaker recognition stopped during voice capture")

        async def prepare(index, chunks, anchors):
            pcm = b"".join(c.pcm for c in chunks)
            batch = {"pcm": pcm, "ready": False}
            self.batches[index] = batch
            # Silence is already unknown in the initial instructions and cannot
            # contain a voice. It needs no new context or extra processing delay.
            if any(pcm):
                gate = ContextBeforeAudio(prefix=f"speaker_packet_{index}")
                self.gates[index] = gate
                try:
                    await gate.prepare(
                        connection,
                        chunks,
                        live_start=index + self.DELAY / RATE,
                        words=anchors,
                    )
                finally:
                    self.gates.pop(index, None)
            batch["ready"] = True
            if captions and anchors:
                report(
                    f"\n[Speaker input {index:.0f}–{index + 1:.0f}s: "
                    + "; ".join(f"{name}: {text}" for name, text in anchors)
                    + "]\n"
                )

        async def schedule(group):
            while True:
                await self.available.wait()
                self.available.clear()
                while self.buffer.resolved >= self.prepared + RATE:
                    index = self.prepared // RATE
                    end = self.prepared + RATE
                    chunks = []
                    while self.buffer.released < end:
                        chunks.append(self.buffer.take(end - self.buffer.released))
                    if len(chunks) > 12:
                        # Excessively fragmented/overlapping intervals cannot be
                        # summarized into a single reliable identity.
                        chunks = [
                            AttributedAudio(
                                self.prepared, end, None, b"".join(c.pcm for c in chunks)
                            )
                        ]
                    anchors = []
                    for start, stop, text in self.words:
                        if start < end and stop > self.prepared:
                            labels = {c.speaker for c in chunks if c.start < stop and c.end > start}
                            name = next(iter(labels)) if len(labels) == 1 else None
                            anchors.append([name or "unknown", text])
                    self.words = [w for w in self.words if w[1] > end]
                    self.prepared = end
                    group.create_task(prepare(index, chunks, anchors[:12]))

        async with asyncio.TaskGroup() as group:
            group.create_task(upload())
            group.create_task(receive_labels())
            group.create_task(schedule(group))

    async def close(self):
        if self.connection_context is not None:
            await self.connection_context.__aexit__(None, None, None)
            self.connection_context = None
        self.websocket = None
        self.batches.clear()
        self.words.clear()
        self.gates.clear()
