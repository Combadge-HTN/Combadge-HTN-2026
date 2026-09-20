"""Opt-in native SpeexDSP acoustic echo cancellation for 24 kHz PCM.

The render reference is the speaker-bound PCM, not microphone audio. Delay is
measured from a playback write to the matching captured echo. Separate USB and
Bluetooth clocks still require hardware validation; this is not enabled by default.
"""

import ctypes
import ctypes.util
import math
import os
import struct
import time
from collections import deque

from combadge.audio import FRAME_BYTES, RATE


class SpeexEcho:
    """Linear cancellation followed by Speex's residual echo suppressor.

    The suppressor adds one 20 ms frame of latency. It uses the echo estimate
    from the canceller, rather than muting capture whenever the speaker plays.
    """

    def __init__(self, library=None, *, tail_ms=200, residual_suppression=True):
        self.state = None
        self.preprocessor = None
        path = library or ctypes.util.find_library("speexdsp")
        if not path:
            raise RuntimeError("SpeexDSP is missing; install the QNX speexdsp package")
        self.lib = ctypes.CDLL(path)
        pointer = ctypes.c_void_p
        pcm = ctypes.POINTER(ctypes.c_int16)
        self.lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        self.lib.speex_echo_state_init.restype = pointer
        self.lib.speex_echo_state_destroy.argtypes = [pointer]
        self.lib.speex_echo_state_destroy.restype = None
        self.lib.speex_echo_ctl.argtypes = [pointer, ctypes.c_int, pointer]
        self.lib.speex_echo_ctl.restype = ctypes.c_int
        self.lib.speex_echo_cancellation.argtypes = [pointer, pcm, pcm, pcm]
        self.lib.speex_echo_cancellation.restype = None
        self.state = self.lib.speex_echo_state_init(FRAME_BYTES // 2, RATE * tail_ms // 1000)
        if not self.state:
            raise RuntimeError("Could not allocate echo canceller")
        rate = ctypes.c_int(RATE)
        if self.lib.speex_echo_ctl(self.state, 24, ctypes.byref(rate)) != 0:
            self.close()
            raise RuntimeError("Could not configure echo cancellation sample rate")
        if residual_suppression:
            try:
                self._init_preprocessor()
            except BaseException:
                self.close()
                raise

    def _init_preprocessor(self):
        pointer = ctypes.c_void_p
        self.lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        self.lib.speex_preprocess_state_init.restype = pointer
        self.lib.speex_preprocess_state_destroy.argtypes = [pointer]
        self.lib.speex_preprocess_state_destroy.restype = None
        self.lib.speex_preprocess_ctl.argtypes = [pointer, ctypes.c_int, pointer]
        self.lib.speex_preprocess_ctl.restype = ctypes.c_int
        self.lib.speex_preprocess_run.argtypes = [pointer, ctypes.POINTER(ctypes.c_int16)]
        self.lib.speex_preprocess_run.restype = ctypes.c_int
        self.preprocessor = self.lib.speex_preprocess_state_init(FRAME_BYTES // 2, RATE)
        if not self.preprocessor:
            raise RuntimeError("Could not allocate residual echo suppressor")
        # SPEEX_PREPROCESS_SET_ECHO_STATE takes the echo-state pointer itself.
        if self.lib.speex_preprocess_ctl(self.preprocessor, 24, self.state) != 0:
            raise RuntimeError("Could not link residual echo suppression")
        # Keep Speex's default noise floor. Forcing it to 0 dB also limits
        # residual echo suppression in bins shared with estimated background noise.

    def process(self, captured, reference):
        if not self.state:
            raise RuntimeError("Echo canceller is closed")
        if len(captured) != FRAME_BYTES or len(reference) != FRAME_BYTES:
            raise ValueError("Echo cancellation requires 20 ms PCM16 frames")
        frame = ctypes.c_int16 * (FRAME_BYTES // 2)
        mic = frame(*struct.unpack("<480h", captured))
        speaker = frame(*struct.unpack("<480h", reference))
        output = frame()
        self.lib.speex_echo_cancellation(self.state, mic, speaker, output)
        if self.preprocessor:
            self.lib.speex_preprocess_run(self.preprocessor, output)
        return struct.pack("<480h", *output)

    def close(self):
        if self.preprocessor:
            self.lib.speex_preprocess_state_destroy(self.preprocessor)
            self.preprocessor = None
        if self.state:
            self.lib.speex_echo_state_destroy(self.state)
            self.state = None


class EchoReference:
    """Bound speaker history and align it using a calibrated end-to-end delay.

    Anchor each continuous stream once, then advance by samples. Thread wakeups
    are not hardware sample timestamps: using each read's wall time would skip
    or repeat the reference whenever capture arrives in bursts.

    Pipe submission times approximate render times. They do not expose A2DP's
    hardware clock; changing speaker buffering can invalidate the calibration.
    """

    def __init__(self, delay_ms, *, clock=time.monotonic):
        if not math.isfinite(delay_ms) or not 0 <= delay_ms <= 1000:
            raise ValueError("COMBADGE_AEC_DELAY_MS must be between 0 and 1000")
        self.delay = delay_ms / 1000
        self.clock = clock
        self.history = deque()
        self.end = 0.0
        self.capture_end = None

    def playback(self, data):
        now = self.clock()
        # A late write can still reach the buffered speaker before its playback
        # deadline. Preserve sample continuity until the estimated buffer drains.
        start = self.end if self.history and now <= self.end + self.delay else now
        self.end = start + len(data) / (RATE * 2)
        self.history.append((start, data))
        while self.history and self.history[0][0] < now - 2:
            self.history.popleft()
        # A stalled capture consumer must never grow this without bound.
        while len(self.history) > 200:
            self.history.popleft()

    def capture_reference(self):
        if self.capture_end is None:
            self.capture_end = self.clock()
        start = self.capture_end - 0.02 - self.delay
        self.capture_end += FRAME_BYTES / (RATE * 2)
        output = bytearray(FRAME_BYTES)
        for render_start, data in self.history:
            offset = round((render_start - start) * RATE)
            source = max(0, -offset)
            target = max(0, offset)
            count = min(len(data) // 2 - source, FRAME_BYTES // 2 - target)
            if count > 0:
                output[target * 2 : (target + count) * 2] = data[source * 2 : (source + count) * 2]
        return bytes(output)


def configured_echo(settings=None):
    mode = (settings.echo_mode if settings else os.environ.get("COMBADGE_AEC", "off")).lower()
    if mode == "off":
        return None
    if mode != "speex":
        raise ValueError("COMBADGE_AEC must be off or speex")
    delay = settings.echo_delay_ms if settings else os.environ.get("COMBADGE_AEC_DELAY_MS")
    if not delay:
        raise ValueError("Set COMBADGE_AEC_DELAY_MS to the measured speaker-to-capture delay")
    measured_delay = float(delay)
    if not math.isfinite(measured_delay) or not 0 <= measured_delay <= 1000:
        raise ValueError("COMBADGE_AEC_DELAY_MS must be between 0 and 1000")
    # Speex uses a causal 200 ms filter: the relevant playback samples must be
    # present before their echo, not delivered just after it. Leave 50 ms for
    # residual path delay and uncertainty in pipe/capture timestamp calibration.
    reference = EchoReference(max(0, measured_delay - 50))
    library = settings.echo_library if settings else os.environ.get("COMBADGE_AEC_LIBRARY")
    processor = SpeexEcho(library or None)
    return processor, reference
