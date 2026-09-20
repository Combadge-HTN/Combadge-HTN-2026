"""Opt-in, asynchronous speaker attribution for the incoming PCM stream."""

import asyncio
import base64
import io
import json
import math
import re
import struct
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from combadge.audio import RATE

REQUEST_TIMEOUT = 8
BYTES_PER_SECOND = RATE * 2
MAX_WAV_BYTES = BYTES_PER_SECOND * 30 + 4096
LOOKUP_SECONDS = 12
LOOKUP_TIMEOUT = 10
SPEAKER_TOOL = {
    "type": "function",
    "name": "identify_speaker",
    "description": (
        "Identify the speaker from recently captured microphone audio. Call for a user asking "
        "whether you recognize them, their name, or who is speaking. Wait for the result; "
        "background labels may not yet cover the question. No recording or name arguments needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "strict": True,
}
LOOKUP_INSTRUCTIONS = (
    " When the user asks whether you recognize them, their name, or who is speaking, "
    "delegate to the backend for identify_speaker and wait for its result before answering "
    "the identity question. You may acknowledge naturally while waiting. "
    "A pending lookup is not an unknown result: do not say you cannot recognize the user "
    "before the lookup returns. Do not substitute a delayed background label for this lookup. "
    "Use a matched name as conversational context; ambiguous, unknown, or unavailable results "
    "do not establish a name. No particular reply wording is required."
)
INSTRUCTIONS = (
    " This badge has an external enrolled-speaker matcher. It compares microphone speech "
    "with user-provided voice references and supplies named matches as silent context. "
    "A clear MATCH supplies the speaker's name for ordinary conversation. "
    "Use it as context, adapting your response to what the user is asking. "
    "Remember names the user gives or confirms; an UNKNOWN observation alone does not "
    "contradict their self-introduction. "
    "You have access to these external observations; do not deny that capability. "
    "A context update is not a user request: do not read it aloud, announce a name or status, "
    "repeat an earlier answer, or bring the conversation back to identification. "
    "MATCH indicates one enrolled name in recent speech; UNKNOWN supplies no name. "
    "MULTIPLE and OVERLAP cannot establish which single person is asking. "
    "Never choose the last segment's name from a MULTIPLE or OVERLAP report, "
    "even if one speaker appears more recent or speaks longer. "
    "Follow speaker changes; do not assign everyone's words to the same person. "
    "Do not wait for labels before ordinary replies. "
    "These estimates never authorize calls, purchases, or access to personal information."
)


def speaker_context(labels: list[dict], audio_seconds: float) -> str:
    """Make external matches usable without inventing a persistent current speaker."""
    names = sorted({row["speaker"] for row in labels} - {"unknown", "ambiguous"})
    if not names:
        summary = "No enrolled name matched this report. Earlier names do not identify this speech."
    elif len(names) > 1 or any(row["speaker"] == "ambiguous" for row in labels):
        summary = (
            "Multiple speakers or overlap: " + ", ".join(names) + ". No single speaker identified."
        )
    else:
        summary = f"Speaker: {names[0]}."
        if any(row["speaker"] == "unknown" for row in labels):
            summary += " Other speech in this report was unidentified."
    if any(row["speaker"] == "ambiguous" for row in labels):
        result = "OVERLAP: speech overlapped; no single speaker identified."
    elif len(names) > 1:
        result = "MULTIPLE: " + " and ".join(names) + "; which person is asking is uncertain."
    elif names:
        result = "MATCH: " + names[0] + "."
    else:
        result = "UNKNOWN: recent speech has not been identified."
    newest = max(row["end"] for row in labels)
    return (
        "Background speaker context (silent update). "
        + summary
        + f" Report ends {max(0, audio_seconds - newest):.1f}s behind microphone input. "
        "Input-audio intervals (seconds): "
        + json.dumps(labels, separators=(",", ":"))
        + " Attribution: "
        + result
    )


def pcm_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 2:
        raise ValueError("Expected nonempty PCM16 audio")
    target = io.BytesIO()
    with wave.open(target, "wb") as wav:
        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return target.getvalue()


def read_wav(path: Path, *, minimum: float, maximum: float) -> bytes:
    with path.open("rb") as source:
        data = source.read(MAX_WAV_BYTES + 1)
    if len(data) > MAX_WAV_BYTES:
        raise ValueError("WAV file is too large")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE):
                raise ValueError("Use uncompressed mono PCM16 WAV at 24000 Hz")
            count = wav.getnframes()
            if not minimum <= count / RATE <= maximum:
                raise ValueError(f"WAV duration must be {minimum:g}–{maximum:g} seconds")
            pcm = wav.readframes(count)
            if len(pcm) != count * 2:
                raise ValueError("Truncated WAV audio")
    except (wave.Error, EOFError) as error:
        raise ValueError("Invalid PCM WAV file") from error
    return pcm


@dataclass(frozen=True)
class Reference:
    name: str
    data: bytes = field(repr=False)


def load_references(specs: list[str]) -> tuple[Reference, ...]:
    if not 1 <= len(specs) <= 4:
        raise ValueError("Provide one to four --speaker NAME=FILE.wav options")
    references = []
    names = set()
    for spec in specs:
        name, separator, path = spec.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,31}", name)
            or name.casefold() in names | {"unknown", "ambiguous"}
        ):
            raise ValueError("Speaker names must be unique, plain names: --speaker Edmon=FILE.wav")
        with Path(path).open("rb") as source:
            data = source.read(3_840_045)
        if len(data) > 3_840_044:
            raise ValueError("Speaker reference is too large")
        try:
            with wave.open(io.BytesIO(data), "rb") as wav:
                channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
                if channels not in (1, 2) or width != 2 or not 8000 <= rate <= 96000:
                    raise ValueError("References require mono/stereo PCM16 WAV at 8–96 kHz")
                count = wav.getnframes()
                if not 2 <= count / rate <= 10:
                    raise ValueError("Speaker reference duration must be 2–10 seconds")
                pcm = wav.readframes(count)
                if len(pcm) != count * channels * width:
                    raise ValueError("Truncated speaker reference")
        except (wave.Error, EOFError) as error:
            raise ValueError("Invalid speaker reference WAV") from error
        # Keep each multipart reference below the API's 1 MiB form-part limit,
        # including base64 expansion, even at 96 kHz. No resampler is needed.
        pcm = pcm[: 4 * rate * channels * width]
        if channels == 2:
            pcm = b"".join(
                struct.pack("<h", int((left + right) / 2))
                for left, right in struct.iter_unpack("<hh", pcm)
            )
        if not any(pcm):
            raise ValueError("The first four seconds of a speaker reference cannot be silent")
        # Rebuild the container to exclude optional metadata chunks from uploads.
        target = io.BytesIO()
        with wave.open(target, "wb") as wav:
            wav.setparams((1, width, rate, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        names.add(name.casefold())
        references.append(Reference(name, target.getvalue()))
    return tuple(references)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    speaker: str


def parse_segments(payload: dict, names: set[str], duration: float) -> list[Segment]:
    """Only enrolled names survive; API-local A/B labels aren't persistent identities."""
    rows = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError("Invalid diarization segments")
    segments = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid diarization segment")
        start, end, speaker = row.get("start"), row.get("end"), row.get("speaker")
        if (
            type(start) not in (int, float)
            or type(end) not in (int, float)
            or not math.isfinite(start)
            or not math.isfinite(end)
            or not (0 <= start < duration and start < end <= duration + 0.1)
            or not isinstance(speaker, str)
        ):
            raise ValueError("Invalid diarization timing or speaker")
        segments.append(
            Segment(start, min(end, duration), speaker if speaker in names else "unknown")
        )
    # Split overlaps instead of assigning an entire turn to its last speaker.
    bounds = sorted({point for s in segments for point in (s.start, s.end)})
    result = []
    for start, end in zip(bounds, bounds[1:]):
        active = {s.speaker for s in segments if s.start < end and s.end > start}
        if not active:
            continue
        label = next(iter(active)) if len(active) == 1 else "ambiguous"
        if result and result[-1].speaker == label and result[-1].end == start:
            result[-1] = Segment(result[-1].start, end, label)
        else:
            result.append(Segment(start, end, label))
    changes = [b.start for a, b in zip(result, result[1:]) if a.speaker != b.speaker]
    if any(changes[i + 2] - changes[i] < 2 for i in range(len(changes) - 2)):
        # Rapid alternating names can be cross-talk or unstable attribution. This
        # conservative heuristic is not a general overlap detector.
        return [Segment(result[0].start, result[-1].end, "ambiguous")]
    return result


class SpeakerAPIError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Speaker API returned HTTP {status}")


class Transcriber:
    def __init__(self, api_key: str, references: tuple[Reference, ...]):
        self.api_key = api_key
        self.references = references
        self.pending: asyncio.Task | None = None

    async def analyze(self, pcm: bytes) -> list[Segment]:
        from combadge.transcription import request

        if not pcm or len(pcm) % 2 or len(pcm) > BYTES_PER_SECOND * 30:
            raise ValueError("Speaker analysis requires 0–30 seconds of PCM16 audio")
        if self.pending is not None and not self.pending.done():
            raise RuntimeError("Previous speaker request is still finishing")
        payload = {
            "api_key": self.api_key,
            "audio": base64.b64encode(pcm_wav(pcm)).decode("ascii"),
            "references": [
                {"name": r.name, "audio": base64.b64encode(r.data).decode("ascii")}
                for r in self.references
            ],
        }
        # Match the standard-library HTTPS/thread adapter used by the catalog.
        # Shield the request so cancellation doesn't allow another upload while
        # the original request is still finishing. Late results are never reused.
        self.pending = asyncio.create_task(asyncio.to_thread(request, payload))
        async with asyncio.timeout(REQUEST_TIMEOUT):
            response = await asyncio.shield(self.pending)
        if type(response.get("status")) is int:
            raise SpeakerAPIError(response["status"])
        if "result" not in response:
            raise RuntimeError("Speaker request failed")
        return parse_segments(
            response["result"], {r.name for r in self.references}, len(pcm) / BYTES_PER_SECOND
        )


@dataclass(frozen=True)
class Window:
    start: float
    pcm: bytes = field(repr=False)
    captured_at: float

    @property
    def end(self) -> float:
        return self.start + len(self.pcm) / BYTES_PER_SECOND


class SpeakerTracker:
    """One request in flight, one pending window; feed never waits for the network."""

    def __init__(self, transcriber, *, clock=time.monotonic):
        self.transcriber = transcriber
        self.clock = clock
        self.pending: asyncio.Queue[Window] = asyncio.Queue(maxsize=1)
        self.buffer = bytearray()
        self.recent = bytearray()
        self.analysis_lock = asyncio.Lock()
        self.offset = 0
        self.total_bytes = 0
        self.published_until = 0.0
        self.disabled = False
        self.failures = 0
        self.dropped = 0
        self.published = 0
        self.acknowledged = 0
        self.sequence = 0
        self.awaiting: dict[str, float] = {}
        self.report = lambda _: None

    def reset_session(self) -> None:
        """Discard timing and queued audio from the previous Live session."""
        self.pending = asyncio.Queue(maxsize=1)
        self.buffer.clear()
        self.recent.clear()
        self.offset = 0
        self.total_bytes = 0
        self.published_until = 0.0
        self.disabled = False
        self.failures = 0
        self.dropped = 0
        self.published = 0
        self.acknowledged = 0
        self.sequence = 0
        self.awaiting.clear()

    def feed(self, pcm: bytes) -> None:
        # Called only after the same PCM was successfully sent to GPT-Live.
        if self.disabled:
            return
        self.total_bytes += len(pcm)
        self.recent.extend(pcm)
        del self.recent[: max(0, len(self.recent) - LOOKUP_SECONDS * BYTES_PER_SECOND)]
        self.buffer.extend(pcm)
        while len(self.buffer) >= 6 * BYTES_PER_SECOND:
            data = bytes(self.buffer[: 6 * BYTES_PER_SECOND])
            window = Window(self.offset / BYTES_PER_SECOND, data, self.clock())
            del self.buffer[: 3 * BYTES_PER_SECOND]
            self.offset += 3 * BYTES_PER_SECOND
            if any(data):
                if self.pending.full():
                    self.pending.get_nowait()
                    self.dropped += 1
                self.pending.put_nowait(window)

    async def identify(self) -> dict:
        """Resolve a question's captured audio without waiting for a full background window."""
        if self.disabled:
            return {"status": "unavailable", "reason": "speaker_analysis_disabled"}
        pcm = bytes(self.recent)
        end = self.total_bytes / BYTES_PER_SECOND
        start = end - len(pcm) / BYTES_PER_SECOND
        if len(pcm) < BYTES_PER_SECOND // 10 or not any(pcm):
            return {"status": "unavailable", "reason": "insufficient_audio"}
        self.report("\nSpeaker lookup requested; waiting for recognition…\n")
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                async with self.analysis_lock:
                    segments = await self.transcriber.analyze(pcm)
        except TimeoutError:
            return {"status": "unavailable", "reason": "recognition_timeout"}
        except Exception:
            return {"status": "unavailable", "reason": "recognition_failed"}
        names = sorted({segment.speaker for segment in segments} - {"unknown", "ambiguous"})
        ambiguous = (
            len(names) > 1
            or any(s.speaker == "ambiguous" for s in segments)
            or bool(names and any(s.speaker == "unknown" for s in segments))
        )
        status = "ambiguous" if ambiguous else "matched" if names else "unknown"
        self.report(f"\nSpeaker lookup completed: {status}.\n")
        return {
            "status": status,
            "name": names[0] if status == "matched" else None,
            "speakers": names,
            "audio_start": round(start, 3),
            "audio_end": round(end, 3),
        }

    def stale(self, window: Window) -> bool:
        return (
            self.clock() - window.captured_at > 8
            or self.total_bytes / BYTES_PER_SECOND - window.end > 8
        )

    def observe(self, event) -> bool:
        event_id = getattr(event, "client_event_id", None)
        if event.type == "session.thinking.appended" and event_id in self.awaiting:
            self.awaiting.pop(event_id)
            self.acknowledged += 1
            return True
        if event.type == "error":
            event_id = getattr(getattr(event, "error", None), "client_event_id", None)
            if event_id in self.awaiting:
                self.awaiting.pop(event_id)
                self.disabled = True
                self.report("\nSpeaker context was rejected; voice continues without labels.\n")
                return True
        return False

    async def run(self, connection, report, *, captions=True) -> None:
        self.report = report
        while True:
            window = await self.pending.get()
            if self.disabled or self.stale(window):
                self.dropped += 1
                continue
            try:
                async with self.analysis_lock:
                    segments = await self.transcriber.analyze(window.pcm)
                self.failures = 0
                if self.disabled or self.stale(window):
                    self.dropped += 1
                    continue
                labels = [
                    {
                        "start": round(max(window.start + s.start, self.published_until), 3),
                        "end": round(window.start + s.end, 3),
                        "speaker": s.speaker,
                    }
                    for s in segments
                    if window.start + s.end > self.published_until
                ]
                observations = [
                    {
                        "start": round(window.start + s.start, 3),
                        "end": round(window.start + s.end, 3),
                        "speaker": s.speaker,
                    }
                    for s in segments
                ]
                self.published_until = window.end
                if not labels:
                    continue
                # Bound context size and unacknowledged events; don't flood the Live session.
                if len(observations) > 6:
                    self.dropped += 1
                    continue
                if len(self.awaiting) >= 3:
                    self.disabled = True
                    report("\nSpeaker context could not keep up; voice continues without labels.\n")
                    continue
                self.sequence += 1
                event_id = f"speaker_context_{self.sequence}"
                self.awaiting[event_id] = self.clock()
                await connection.send(
                    {
                        "type": "session.thinking.append",
                        "event_id": event_id,
                        "delegation_id": None,
                        # Summarize the complete analyzed window. Deduplicating
                        # console intervals must not erase another speaker from
                        # the evidence the model uses to answer a name question.
                        "content": speaker_context(
                            observations, self.total_bytes / BYTES_PER_SECOND
                        ),
                    }
                )
                self.published += 1
                for label in labels if captions else []:
                    report(
                        f"\n[Speaker {label['start']:.1f}–{label['end']:.1f}s: "
                        f"{label['speaker']} (estimate)]\n"
                    )
            except Exception as error:
                # Never log remote bodies, speech, references, or credentials on this path.
                self.failures += 1
                self.disabled = (
                    isinstance(error, SpeakerAPIError) and error.status in (401, 403)
                ) or self.failures >= 3
                report("\nSpeaker analysis unavailable; voice continues without new labels.\n")
                if not self.disabled:
                    await asyncio.sleep(min(2**self.failures, 8))
