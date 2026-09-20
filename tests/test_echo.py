import ctypes.util
import math
import random
import struct

import pytest

from combadge.echo import EchoReference, SpeexEcho, configured_echo


def test_reference_tracks_delay_silence_and_bounds():
    now = [0.0]
    reference = EchoReference(200, clock=lambda: now[0])
    data = struct.pack("<h", 1234) * 480
    reference.playback(data)
    now[0] = 0.22
    assert reference.capture_reference() == data
    now[0] = 0.3
    assert reference.capture_reference() == bytes(960)
    for _ in range(1000):
        reference.playback(data)
    assert len(reference.history) <= 200
    for value in (-1, 1001, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            EchoReference(value)


def test_reference_preserves_samples_despite_bursty_capture_and_playback():
    now = [0.0]
    reference = EchoReference(200, clock=lambda: now[0])
    frames = [struct.pack("<h", i + 1) * 480 for i in range(20)]
    # The producer is occasionally late, but the speaker has 200 ms buffered.
    for i, frame in enumerate(frames):
        now[0] = i * 0.02 + (0.009 if i % 3 else 0)
        reference.playback(frame)
    # Capture pipe reads alternate between late wakeups and rapid backlog reads.
    for i, expected in enumerate(frames):
        now[0] = 0.22 + i * 0.02 + (0.012 if i % 2 else 0)
        assert reference.capture_reference() == expected


def test_playback_reanchors_after_a_real_gap():
    now = [0.0]
    reference = EchoReference(200, clock=lambda: now[0])
    reference.playback(struct.pack("<h", 1) * 480)
    now[0] = 1.0
    reference.playback(struct.pack("<h", 2) * 480)
    assert [start for start, _ in reference.history] == [0, 1]


def test_causal_headroom_is_applied_without_accepting_invalid_delay(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("combadge.echo.SpeexEcho", lambda _: "processor")
    settings = SimpleNamespace(echo_mode="speex", echo_delay_ms="500", echo_library="test")
    processor, reference = configured_echo(settings)
    assert processor == "processor"
    assert reference.delay == 0.45
    for value in ("-1", "1001", "nan", "inf"):
        settings.echo_delay_ms = value
        with pytest.raises(ValueError, match="between 0 and 1000"):
            configured_echo(settings)


def test_native_canceller_reduces_echo_and_preserves_near_end_speech():
    if not ctypes.util.find_library("speexdsp"):
        pytest.skip("Native SpeexDSP is optional")
    echo = SpeexEcho()
    rng = random.Random(731)
    references = []
    raw_energy = clean_energy = near_energy = error_energy = 0
    try:
        for block in range(700):
            reference = [rng.randint(-6000, 6000) for _ in range(480)]
            references.append(reference)
            acoustic = references[max(0, block - 2)]
            near = [
                round(1500 * math.sin(2 * math.pi * 731 * (block * 480 + i) / 24000))
                if block >= 500
                else 0
                for i in range(480)
            ]
            mic = [int(0.5 * a) + n for a, n in zip(acoustic, near)]
            output = struct.unpack(
                "<480h", echo.process(struct.pack("<480h", *mic), struct.pack("<480h", *reference))
            )
            if 400 <= block < 500:
                raw_energy += sum(x * x for x in mic)
                clean_energy += sum(x * x for x in output)
            if block >= 600:
                near_energy += sum(x * x for x in near)
                error_energy += sum((a - b) ** 2 for a, b in zip(output, near))
        assert 10 * math.log10(raw_energy / max(1, clean_energy)) > 15
        assert error_energy < 0.5 * near_energy
        with pytest.raises(ValueError):
            echo.process(b"", b"")
    finally:
        echo.close()
    echo.close()
    with pytest.raises(RuntimeError, match="closed"):
        echo.process(bytes(960), bytes(960))


def test_native_cancellation_with_jittery_io_preserves_interruption():
    if not ctypes.util.find_library("speexdsp"):
        pytest.skip("Native SpeexDSP is optional")
    rng = random.Random(894)
    far = [[rng.randint(-6000, 6000) for _ in range(480)] for _ in range(700)]
    now = [0.0]
    reference = EchoReference(40, clock=lambda: now[0])
    events = []
    for i in range(len(far)):
        events.append((i * 0.02 + (0.009 if i % 3 else 0), "render", i))
        events.append(((i + 1) * 0.02 + (0.012 if i % 2 else 0), "capture", i))
    raw_energy = clean_energy = near_energy = error_energy = 0
    echo = SpeexEcho()
    try:
        for at, kind, i in sorted(events):
            now[0] = at
            if kind == "render":
                reference.playback(struct.pack("<480h", *far[i]))
                continue
            acoustic = far[i - 4] if i >= 4 else [0] * 480
            near = [
                round(1500 * math.sin(2 * math.pi * 731 * (i * 480 + j) / 24000)) if i >= 500 else 0
                for j in range(480)
            ]
            mic = [int(0.5 * a) + n for a, n in zip(acoustic, near)]
            out = struct.unpack(
                "<480h", echo.process(struct.pack("<480h", *mic), reference.capture_reference())
            )
            if 400 <= i < 500:
                raw_energy += sum(x * x for x in mic)
                clean_energy += sum(x * x for x in out)
            if i >= 600:
                near_energy += sum(x * x for x in near)
                error_energy += sum((x - y) ** 2 for x, y in zip(out, near))
        assert 10 * math.log10(raw_energy / max(1, clean_energy)) > 15
        assert error_energy < 0.5 * near_energy
    finally:
        echo.close()


def test_echo_requires_explicit_delay_and_valid_mode(monkeypatch):
    monkeypatch.setenv("COMBADGE_AEC", "off")
    assert configured_echo() is None
    monkeypatch.setenv("COMBADGE_AEC", "unknown")
    with pytest.raises(ValueError, match="off or speex"):
        configured_echo()
    monkeypatch.setenv("COMBADGE_AEC", "speex")
    monkeypatch.delenv("COMBADGE_AEC_DELAY_MS", raising=False)
    with pytest.raises(ValueError, match="measured"):
        configured_echo()


def test_env_file_configures_echo_without_mutating_environment(tmp_path, monkeypatch):
    from combadge.config import load_settings

    for name in ("COMBADGE_AEC", "COMBADGE_AEC_DELAY_MS", "COMBADGE_AEC_LIBRARY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text("COMBADGE_AEC=speex\nCOMBADGE_AEC_DELAY_MS=500\n")
    settings = load_settings(path)
    assert settings.echo_mode == "speex"
    assert settings.echo_delay_ms == "500"
    monkeypatch.setenv("COMBADGE_AEC", "off")
    assert load_settings(path).echo_mode == "off"
