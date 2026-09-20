"""Bounded speech-tail detection for GPT-Live's continuous audio stream."""

import struct


class HandoffSpeech:
    """Live has no audio-done event; allow a short farewell and a quiet tail.

    A network stall cannot prove speech completion, so the maximum wait is a
    deliberate fallback. Playback must still be drained after this gate opens.
    """

    def __init__(self, *, minimum=2.0, quiet=1.0, maximum=10.0):
        self.minimum, self.quiet, self.maximum = minimum, quiet, maximum
        self.started = None
        self.last_activity = 0.0

    def begin(self, now):
        self.started = now
        self.last_activity = now

    def activity(self, now):
        self.last_activity = now

    def audio(self, pcm, now):
        # Ignore digital silence and the low-level noise floor in continuous output.
        if any(abs(sample[0]) > 200 for sample in struct.iter_unpack("<h", pcm)):
            self.activity(now)

    def ready(self, now):
        if self.started is None:
            return False
        elapsed = now - self.started
        return elapsed >= self.maximum or (
            elapsed >= self.minimum and now - self.last_activity >= self.quiet
        )
