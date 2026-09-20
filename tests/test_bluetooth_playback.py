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


def test_streaming_adapter_preserves_speech_dynamic_range(monkeypatch):
    from unittest.mock import Mock

    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/bluetooth_playback.py")
    )
    reads = iter([struct.pack("<h", 20000) * 240, b""])
    monkeypatch.setattr(namespace["os"], "read", lambda *_: next(reads))
    stdin = Mock()
    stdin.fileno.return_value = 0
    monkeypatch.setattr(namespace["sys"], "stdin", stdin)
    output = b"".join(namespace["converted_input"]())
    assert output == struct.pack("<hh", 20000, 20000) * 441


def test_lead_in_preserves_speech_and_only_primes_after_silence():
    prime = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/bluetooth_playback.py")
    )["speech_lead_in"]
    speech = struct.pack("<h", 20000) * 480
    silence = bytes(24000)
    output = list(prime([speech, speech, silence, speech], clock=lambda: 0))
    cue = output[:9]
    assert len(b"".join(cue)) == 8640
    assert any(b"".join(cue))
    assert output[9:12] == [speech, speech, silence]
    assert output[12:21] == cue
    assert output[21:] == [speech]
    assert list(prime([silence], clock=lambda: 0)) == [silence]


def test_lead_in_rearms_after_wall_clock_gap():
    prime = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/bluetooth_playback.py")
    )["speech_lead_in"]
    times = iter([0, 0, 0.2, 1, 1.2])
    speech = b"\x01\x00" * 480
    output = list(prime([speech, speech], clock=lambda: next(times)))
    assert len(output) == 20
    assert output[9] == output[19] == speech
