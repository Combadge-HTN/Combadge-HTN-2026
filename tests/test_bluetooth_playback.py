import runpy
import struct
from pathlib import Path

import pytest

Resampler = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/bluetooth_playback.py")
)["Resampler"]


def convert(chunks):
    converter = Resampler()
    return b"".join(converter.feed(chunk) for chunk in chunks) + converter.feed(b"", final=True)


def test_rate_channels_and_signed_samples():
    data = struct.pack("<h", -12345) * 24000
    output = convert([data])
    assert len(output) == 44100 * 4
    assert output == struct.pack("<hh", -12345, -12345) * 44100


def test_chunk_boundaries_do_not_change_output():
    data = struct.pack("<1000h", *(i * 60 - 30000 for i in range(1000)))
    whole = convert([data])
    assert convert([data[i : i + 7] for i in range(0, len(data), 7)]) == whole
    frames = list(struct.iter_unpack("<hh", whole))
    assert all(left == right for left, right in frames)
    assert frames[0] == (-30000, -30000)


def test_empty_and_truncated_input():
    assert convert([]) == b""
    with pytest.raises(ValueError, match="incomplete"):
        convert([b"\x00"])
