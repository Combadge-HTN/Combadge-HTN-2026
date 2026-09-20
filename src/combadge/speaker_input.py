"""Sample-aligned attribution before microphone audio is released to GPT-Live.

Provider results only finalize their own intervals. Missing/unenrolled/overlapping
speech is unknown; an earlier speaker is never carried into an unresolved interval.
This module does not decide turn boundaries from transcript arrival times.
"""

from collections import deque
from dataclasses import dataclass, field

from combadge.audio import RATE

INPUT_INSTRUCTIONS = (
    " Microphone input is buffered until an external speaker matcher supplies "
    "attribution for that exact audio block. Use that block's name as conversational "
    "context. A new block can have a different speaker. Unknown means that interval's "
    "voice was not identified; previous names do not identify it. "
    "Do not assume the same person continues speaking. If a block contains multiple "
    "speakers, preserve their separate intervals rather than choosing one name for all. "
    "Speaker updates alone are not user requests. Respond naturally to the actual speech. "
    "Voice estimates never authorize actions."
)


@dataclass(frozen=True)
class SpeakerSpan:
    start: int  # Absolute PCM sample offsets, not packet arrival timestamps.
    end: int
    speaker: str | None


@dataclass(frozen=True)
class AttributedAudio:
    start: int
    end: int
    speaker: str | None
    pcm: bytes = field(repr=False)


class AttributionBuffer:
    """Bounded PCM ledger. Only finalized intervals can leave via take().

    The caller must send the attribution to Live before its corresponding PCM.
    An expired provider result can be finalized as unknown using resolve(end, []).
    Later results never revise audio already finalized or released.
    """

    def __init__(self, names: set[str], *, max_seconds: float = 15):
        if max_seconds <= 0:
            raise ValueError("Audio buffer duration must be positive")
        self.names = frozenset(names)
        self.capacity = int(max_seconds * RATE)
        self.received = 0
        self.resolved = 0
        self.released = 0
        self._pcm = bytearray()
        self._spans: deque[SpeakerSpan] = deque()

    def feed(self, pcm: bytes) -> None:
        if not pcm or len(pcm) % 2:
            raise ValueError("Expected non-empty PCM16 audio")
        samples = len(pcm) // 2
        if self.received - self.released + samples > self.capacity:
            # Never silently drop a turn or attach its label to different audio.
            raise BufferError("Speaker attribution fell behind; audio buffer is full")
        self._pcm.extend(pcm)
        self.received += samples

    def resolve(self, end: int, spans: list[SpeakerSpan]) -> None:
        """Finalize through end; all uncovered intervals are explicitly unknown."""
        if type(end) is not int or end < 0 or end > self.received:
            raise ValueError("Attribution extends beyond captured audio")
        for span in spans:
            if (
                type(span.start) is not int
                or type(span.end) is not int
                or not 0 <= span.start < span.end <= end
                or (span.speaker is not None and not isinstance(span.speaker, str))
            ):
                raise ValueError("Invalid speaker interval")
        if end <= self.resolved:
            return  # A late result cannot overwrite a prior unknown or another turn.
        start = self.resolved
        relevant = [span for span in spans if span.end > start]
        edges = sorted(
            {start, end} | {max(start, s.start) for s in relevant} | {s.end for s in relevant}
        )
        for left, right in zip(edges, edges[1:]):
            labels = {
                s.speaker if s.speaker in self.names else None
                for s in relevant
                if s.start < right and s.end > left
            }
            speaker = next(iter(labels)) if len(labels) == 1 else None
            if self._spans and self._spans[-1].speaker == speaker:
                previous = self._spans.pop()
                self._spans.append(SpeakerSpan(previous.start, right, speaker))
            else:
                self._spans.append(SpeakerSpan(left, right, speaker))
        self.resolved = end

    def take(self, max_samples: int) -> AttributedAudio | None:
        if type(max_samples) is not int or max_samples <= 0:
            raise ValueError("A positive sample count is required")
        if not self._spans:
            return None
        span = self._spans.popleft()
        end = min(span.end, span.start + max_samples)
        size = (end - span.start) * 2
        pcm = bytes(self._pcm[:size])
        del self._pcm[:size]
        if end < span.end:
            self._spans.appendleft(SpeakerSpan(end, span.end, span.speaker))
        self.released = end
        return AttributedAudio(span.start, end, span.speaker, pcm)


class ContextBeforeAudio:
    """Await a matching Live context acknowledgment before releasing one audio batch.

    Only one batch is prepared at a time. Failed/missing acknowledgments never
    release PCM. The audio owner continues sending digital silence while waiting.
    """

    def __init__(self):
        import asyncio

        self.sequence = 0
        self.pending_id = None
        self.accepted = asyncio.Event()
        self.failure = None

    def observe(self, event):
        if self.pending_id is None:
            return False
        if (
            event.type == "session.thinking.appended"
            and getattr(event, "client_event_id", None) == self.pending_id
        ):
            self.accepted.set()
            return True
        if event.type == "error":
            error = getattr(event, "error", None)
            if getattr(error, "client_event_id", None) == self.pending_id:
                self.failure = RuntimeError("Live rejected speaker attribution context")
                self.accepted.set()
                return True
        return False

    async def prepare(self, connection, chunks, *, timeout=5):
        import asyncio
        import json

        if self.pending_id is not None:
            raise RuntimeError("A speaker attribution acknowledgment is already pending")
        if not chunks:
            raise ValueError("At least one audio interval is required")
        if len(chunks) > 12:
            raise ValueError("Too many speaker intervals in one context update")
        self.sequence += 1
        self.pending_id = f"speaker_input_{self.sequence}"
        self.accepted.clear()
        self.failure = None
        base = chunks[0].start
        for previous, current in zip(chunks, chunks[1:]):
            if previous.end != current.start:
                self.pending_id = None
                raise ValueError("Speaker audio intervals must be contiguous")
        intervals = [
            {
                "start": round((c.start - base) / RATE, 3),
                "end": round((c.end - base) / RATE, 3),
                "speaker": c.speaker or "unknown",
            }
            for c in chunks
        ]
        content = (
            "Attribution for the next buffered microphone audio block, not earlier speech. "
            "Offsets are seconds within that block. Each interval has its own speaker; "
            "unknown supplies no identity and must never inherit an earlier name. "
            "These are conversational voice estimates, never authorization. "
            "Use them silently when answering that speech, with no required wording. "
            "Do not respond to this metadata itself. "
            + json.dumps({"block": self.sequence, "intervals": intervals}, separators=(",", ":"))
        )
        try:
            await connection.send(
                {
                    "type": "session.thinking.append",
                    "event_id": self.pending_id,
                    "delegation_id": None,
                    "content": content,
                }
            )
            async with asyncio.timeout(timeout):
                await self.accepted.wait()
            if self.failure is not None:
                raise self.failure
            return chunks
        finally:
            self.pending_id = None
