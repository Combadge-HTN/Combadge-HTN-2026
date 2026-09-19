"""Streaming 24 kHz PCM16LE / 8 kHz G.711 mu-law conversion, without audioop.

Conversion runs on the relay. The badge retains its existing PCM helper contract.
"""

import math
import struct
from collections import deque


def encode_sample(sample: int) -> int:
    # G.711 operates on signed 14-bit samples; preserve negative rounding.
    sample >>= 2
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample) + 33, 8191)
    exponent = max(0, magnitude.bit_length() - 6)
    mantissa = (magnitude >> (exponent + 1)) & 15
    return ~(sign | (exponent << 4) | mantissa) & 255


def decode_sample(code: int) -> int:
    code = ~code & 255
    magnitude = (((code & 15) << 3) + 132) << ((code >> 4) & 7)
    return 132 - magnitude if code & 128 else magnitude - 132


def lowpass():
    # 63-tap Hamming-windowed sinc, 3.4 kHz cutoff before decimation to 8 kHz.
    values = []
    for index in range(63):
        x = index - 31
        sinc = (
            2 * 3400 / 24000 if x == 0 else math.sin(2 * math.pi * 3400 / 24000 * x) / (math.pi * x)
        )
        values.append(sinc * (0.54 - 0.46 * math.cos(2 * math.pi * index / 62)))
    total = sum(values)
    return tuple(v / total for v in values)


FILTER = lowpass()


class PhoneCodec:
    def __init__(self):
        self.history = deque([0] * len(FILTER), maxlen=len(FILTER))
        self.phase = 0
        self.previous = 0

    def encode(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("Expected complete PCM16 samples")
        result = bytearray()
        for (sample,) in struct.iter_unpack("<h", pcm):
            self.history.appendleft(sample)
            self.phase = (self.phase + 1) % 3
            if self.phase == 0:
                filtered = round(sum(c * s for c, s in zip(FILTER, self.history, strict=True)))
                result.append(encode_sample(max(-32768, min(32767, filtered))))
        return bytes(result)

    def decode(self, data: bytes) -> bytes:
        result = bytearray()
        for code in data:
            current = decode_sample(code)
            for step in (1, 2, 3):
                sample = round(self.previous + (current - self.previous) * step / 3)
                result.extend(struct.pack("<h", sample))
            self.previous = current
        return bytes(result)
