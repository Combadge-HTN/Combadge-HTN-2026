#!/usr/bin/env python3
"""Convert the client's 24 kHz mono PCM to Dan's 44.1 kHz stereo Bluetooth FIFO."""

import argparse
import math
import os
import struct
import sys
import time
from array import array
from pathlib import Path


class Resampler:
    """Stateful linear interpolation; retain phase across arbitrary input chunks."""

    def __init__(self, gain=1):
        self.gain = gain
        self.samples = array("h")
        self.position = 0
        self.pending = b""

    def feed(self, data, *, final=False):
        data = self.pending + data
        self.pending = data[len(data) // 2 * 2 :]
        incoming = array("h")
        incoming.frombytes(data[: len(data) // 2 * 2])
        if sys.byteorder != "little":
            incoming.byteswap()
        self.samples.extend(incoming)
        if final:
            if self.pending:
                raise ValueError("Input ended with an incomplete 16-bit PCM sample")
            if self.samples:
                self.samples.append(self.samples[-1])
        output = array("h")
        # 24000 / 44100 = 80 / 147 input samples per output sample.
        while self.position < (len(self.samples) - 1) * 147:
            index, fraction = divmod(self.position, 147)
            value = round(
                (self.samples[index] * (147 - fraction) + self.samples[index + 1] * fraction) / 147
            )
            value = max(-32768, min(32767, round(value * self.gain)))
            output.extend((value, value))
            self.position += 80
        consumed = min(self.position // 147, max(0, len(self.samples) - 1))
        del self.samples[:consumed]
        self.position -= consumed * 147
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()


def speech_lead_in(chunks, *, clock=time.monotonic):
    """Prime a speaker's silence gate without trimming the following PCM.

    A quiet 180 ms tone precedes the first sound and sounds after >=500 ms
    silence. Account for both PCM silence and gaps in the input stream.
    Keep this in the Bluetooth adapter; other audio devices need no cue.
    """
    count = 4320  # 180 ms at 24 kHz
    cue = struct.pack(
        f"<{count}h",
        *(round(240 * min(i / 240, (count - 1 - i) / 240, 1)
                * math.sin(2 * math.pi * 220 * i / 24000)) for i in range(count)),
    )
    quiet = 0.5
    previous = clock()
    for data in chunks:
        now = clock()
        if any(data):
            if quiet >= 0.5 or now - previous >= 0.5:
                # Small chunks preserve the FIFO writer's bounded backpressure.
                for offset in range(0, len(cue), 960):
                    yield cue[offset : offset + 960]
            quiet = 0.0
        else:
            quiet += len(data) / 48000
        yield data
        previous = clock()


def converted_input(gain=1, *, lead_in=False):
    resampler = Resampler(gain=gain)
    chunks = iter(lambda: os.read(sys.stdin.fileno(), 960), b"")
    if lead_in:
        chunks = speech_lead_in(chunks)
    for data in chunks:
        if output := resampler.feed(data):
            yield output
    if output := resampler.feed(b"", final=True):
        yield output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bluetooth-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "qnx-bluetooth",
        help="Dan's qnx-bluetooth directory containing play_pcm.py and audio.pcm",
    )
    args = parser.parse_args()
    if not (args.bluetooth_dir / "play_pcm.py").is_file():
        parser.error("play_pcm.py is missing; set --bluetooth-dir to the driver directory")
    sys.path.insert(0, str(args.bluetooth_dir.resolve()))
    from play_pcm import send_pcm

    try:
        # Reuse the driver's bounded FIFO writes, backpressure, and disconnect handling.
        # Slightly attenuate both assistant speech and phone audio before Bluetooth playback.
        send_pcm(converted_input(gain=0.60, lead_in=True))
    except (OSError, ValueError, TimeoutError) as error:
        parser.exit(1, f"Bluetooth playback: {error}\n")


if __name__ == "__main__":
    main()
