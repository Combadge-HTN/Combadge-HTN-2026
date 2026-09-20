"""Offline CAM++ matching; PCM continues to Live while a bounded worker identifies speech.

This is windowed speaker identification, not word-aligned diarization or overlap
separation. Short turns may finish before a match is available. No transcript is
injected and identity estimates must never authorize actions.
"""

import asyncio
import hashlib
import io
import math
import struct
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from combadge.audio import RATE

MODEL_SHA256 = "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
)
INSTRUCTIONS = (
    " An external voice matcher supplies estimates of who is speaking. "
    "You can use enrolled voice matches to recognize the speaker conversationally; "
    "you have access to that information. Speaker changes and unknown results replace "
    "earlier voice estimates. These estimates describe recent speech and may arrive late; "
    "they do not establish the identity of a new speaker. Treat them as conversational "
    "context, not authentication or a request to speak."
)


def unit_vector(values):
    if len(values) != 512 or any(not math.isfinite(v) for v in values):
        raise ValueError("Invalid CAM++ embedding")
    norm = math.sqrt(sum(v * v for v in values))
    if norm < 1e-8:
        raise ValueError("Empty CAM++ embedding")
    return tuple(v / norm for v in values)


@dataclass(frozen=True)
class Match:
    name: str | None
    score: float
    margin: float


class Matcher:
    def __init__(self, enrolled, *, threshold=0.4, margin=0.12):
        if not enrolled or not 0 < threshold < 1 or not 0 < margin < 1:
            raise ValueError("Enrollment and valid match thresholds are required")
        self.enrolled = {name: unit_vector(v) for name, v in enrolled.items()}
        self.threshold, self.margin = threshold, margin

    def match(self, vector):
        vector = unit_vector(vector)
        scores = sorted(
            (
                (sum(a * b for a, b in zip(vector, ref)), name)
                for name, ref in self.enrolled.items()
            ),
            reverse=True,
        )
        score, name = scores[0]
        margin = score - scores[1][0] if len(scores) > 1 else score + 1
        accepted = score >= self.threshold and margin >= self.margin
        return Match(name if accepted else None, score, margin)


class Worker:
    """Private, persistent native process. No cloud access or Python ML dependencies."""

    def __init__(self, executable: Path, model: Path):
        self.executable, self.model = Path(executable), Path(model)
        self.process = None
        self.lock = asyncio.Lock()

    async def start(self):
        with self.model.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != MODEL_SHA256:
            raise ValueError("CAM++ model checksum differs from the pinned model")
        self.process = await asyncio.create_subprocess_exec(
            str(self.executable.resolve()),
            str(self.model.resolve()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(30):
                magic = await self.process.stdout.readexactly(4)
            if magic != b"CSP1":
                raise RuntimeError("Incompatible speaker worker protocol")
        except BaseException:
            await self.close()
            raise

    async def embed(self, pcm: bytes, rate=RATE):
        if rate < 8000 or rate > 96000 or len(pcm) % 2 or not rate <= len(pcm) <= rate * 20:
            raise ValueError("Expected 0.5–10 seconds of mono PCM16 at 8–96 kHz")
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                raise RuntimeError("Speaker worker is not running")
            try:
                async with asyncio.timeout(10):
                    self.process.stdin.write(struct.pack("<II", rate, len(pcm) // 2) + pcm)
                    await self.process.stdin.drain()
                    raw = await self.process.stdout.readexactly(512 * 4)
                return unit_vector(struct.unpack("<512f", raw))
            except BaseException:
                # A partial response cannot be paired with a later request.
                await self.close()
                raise

    async def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            async with asyncio.timeout(2):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()


async def enroll(worker, references):
    enrolled = {}
    for reference in references:
        with wave.open(io.BytesIO(reference.data), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                raise ValueError("Enrollment must contain mono PCM16")
            enrolled[reference.name] = await worker.embed(
                wav.readframes(wav.getnframes()), wav.getframerate()
            )
    return Matcher(enrolled)


async def analyze_file(worker, references, path):
    """Offline diagnostic using the same window size and matcher as streaming."""
    from combadge.speakers import read_wav

    pcm = read_wav(path, minimum=1.5, maximum=30)
    window, hop = int(1.5 * RATE) * 2, int(0.5 * RATE) * 2
    await worker.start()
    try:
        matcher = await enroll(worker, references)
        rows = []
        for start in range(0, len(pcm) - window + 1, hop):
            begin = time.monotonic()
            embedding = await worker.embed(pcm[start : start + window])
            rows.append(
                {
                    "start": start / (RATE * 2),
                    "end": (start + window) / (RATE * 2),
                    **asdict(matcher.match(embedding)),
                    "analysis_seconds": round(time.monotonic() - begin, 4),
                }
            )
        return rows
    finally:
        await worker.close()


class LocalSpeakerInput:
    instructions = INSTRUCTIONS

    def __init__(self, worker, references, *, window_seconds=1.5, hop_seconds=0.5):
        if not 0.5 <= hop_seconds <= window_seconds <= 4:
            raise ValueError("Invalid speaker window/hop duration")
        self.worker, self.references = worker, references
        self.window_bytes = int(window_seconds * RATE) * 2
        self.hop_bytes = int(hop_seconds * RATE) * 2
        self.pending = asyncio.Event()
        self.latest = None
        self.buffer = bytearray()
        self.total = self.silence = self.since_analysis = self.voiced_bytes = 0
        self.generation = 0
        self.speaking = False
        self.changed = asyncio.Event()
        self.update = None
        self.last_name = None
        self.sequence = 0
        self.passthrough = b""
        self.matcher = None

    async def start(self):
        await self.worker.start()
        try:
            self.matcher = await enroll(self.worker, self.references)
        except BaseException:
            await self.worker.close()
            raise

    def feed(self, pcm):
        if len(pcm) % 2 or not pcm:
            raise ValueError("Expected nonempty PCM16")
        self.passthrough = pcm
        self.total += len(pcm)
        # Simple energy gate; intentionally not presented as a learned VAD.
        power = sum(v * v for (v,) in struct.iter_unpack("<h", pcm)) / (len(pcm) // 2)
        voiced = power > (32768 * 0.005) ** 2
        if voiced:
            if not self.speaking:
                self.generation += 1
                self.buffer.clear()
                self.since_analysis = 0
                self.voiced_bytes = 0
                self.update = (self.generation, None)
                self.changed.set()
            self.speaking = True
            self.silence = 0
            self.voiced_bytes += len(pcm)
        else:
            self.silence += len(pcm)
        if self.speaking:
            self.buffer.extend(pcm)
            del self.buffer[: -self.window_bytes]
            self.since_analysis += len(pcm)
            if (
                voiced
                and len(self.buffer) >= self.window_bytes
                and self.since_analysis >= self.hop_bytes
            ):
                self.latest = (self.generation, self.total, bytes(self.buffer))
                self.since_analysis = 0
                self.pending.set()
            if self.silence >= int(0.4 * RATE) * 2:
                self.speaking = False
                if self.voiced_bytes >= int(0.5 * RATE) * 2:
                    # A short question still gets a final estimate. Silence does not
                    # invalidate its identity; the next speech onset does.
                    self.latest = (self.generation, self.total, bytes(self.buffer))
                    self.pending.set()
                self.buffer.clear()

    def frame(self, size):
        if size != len(self.passthrough):
            raise ValueError("Speaker PCM frame size mismatch")
        pcm, self.passthrough = self.passthrough, b""
        return pcm

    def observe(self, event):
        return False

    async def _publish(self, connection, name, report, captions):
        if name == self.last_name:
            return
        self.last_name = name
        fact = (
            f"The external voice matcher identifies the person speaking as {name}."
            if name
            else "The external voice matcher cannot identify the person speaking."
        )
        self.sequence += 1
        await connection.send(
            {
                "type": "session.thinking.append",
                "event_id": f"local_speaker_{self.sequence}",
                "delegation_id": None,
                "content": fact,
            }
        )
        if captions:
            report(f"\n[Speaker: {name or 'unknown'}]\n")

    async def _infer(self):
        while True:
            await self.pending.wait()
            self.pending.clear()
            job, self.latest = self.latest, None
            if job is None:
                continue
            generation, end, pcm = job
            if generation != self.generation or self.total - end > self.hop_bytes * 2:
                continue
            vector = await self.worker.embed(pcm)
            if generation != self.generation or self.total - end > self.hop_bytes * 2:
                continue  # Do not publish a stale name after a speaker/turn change.
            self.update = (generation, self.matcher.match(vector).name)
            self.changed.set()

    async def _notify(self, connection, report, captions):
        while True:
            await self.changed.wait()
            self.changed.clear()
            generation, name = self.update
            if generation == self.generation:
                await self._publish(connection, name, report, captions)

    async def run(self, connection, report, *, captions=True):
        # Clearing an old identity must not wait for an in-flight inference call.
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._infer())
            tasks.create_task(self._notify(connection, report, captions))

    async def close(self):
        await self.worker.close()
