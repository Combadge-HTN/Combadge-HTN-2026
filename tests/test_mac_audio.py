import asyncio
import queue
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from combadge.audio import FRAME_BYTES, RATE, MacAudio, audio_backend


def driver(monkeypatch):
    streams = []

    def stream(**kwargs):
        result = Mock(active=True)
        result.options = kwargs
        streams.append(result)
        return result

    sd = NS(
        RawInputStream=Mock(side_effect=stream),
        RawOutputStream=Mock(side_effect=stream),
        check_input_settings=Mock(),
        check_output_settings=Mock(),
        PortAudioError=RuntimeError,
    )
    monkeypatch.setattr(MacAudio, "driver", staticmethod(lambda: sd))
    return sd, streams


@pytest.mark.parametrize(
    "platform,expected", [("darwin", "mac"), ("linux", "alsa"), ("qnx8", "alsa")]
)
def test_platform_default_preserves_explicit_backend(monkeypatch, platform, expected):
    monkeypatch.setattr("sys.platform", platform)
    assert audio_backend() == expected
    assert audio_backend("commands") == "commands"
    assert audio_backend("alsa") == "alsa"


def test_mac_callback_audio_contract_and_cleanup(monkeypatch):
    sd, streams = driver(monkeypatch)

    async def scenario():
        audio = MacAudio("2", "Built-in Output")
        await audio.start()
        try:
            sd.check_input_settings.assert_called_with(
                device=2, channels=1, dtype="int16", samplerate=RATE
            )
            assert sd.RawOutputStream.call_args.kwargs["device"] == "Built-in Output"
            pcm = b"\x01\x00" * (FRAME_BYTES // 2)
            audio._capture(pcm, FRAME_BYTES // 2, None, None)
            assert await audio.read() == pcm
            await audio.write(pcm)
            # Different callback sizes preserve every sample and pad underruns with silence.
            first, second = bytearray(100), bytearray(FRAME_BYTES)
            audio._play(first, 50, None, None)
            audio._play(second, FRAME_BYTES // 2, None, None)
            assert bytes(first) + bytes(second) == pcm + bytes(100)
        finally:
            await audio.close()
        await audio.close()
        for stream in streams:
            stream.abort.assert_called_once()
            stream.close.assert_called_once()
        # Native callbacks racing shutdown cannot repopulate the microphone queue.
        audio._capture(b"\x00\x00", 1, None, None)
        assert audio._input.empty()

    asyncio.run(scenario())


def test_partial_startup_failure_releases_both_devices(monkeypatch):
    sd, streams = driver(monkeypatch)
    sd.RawInputStream.side_effect = RuntimeError("Microphone denied")

    async def scenario():
        audio = MacAudio()
        with pytest.raises(RuntimeError, match="Microphone access"):
            await audio.start()
        assert audio._closed
        streams[0].abort.assert_called_once()
        streams[0].close.assert_called_once()

    asyncio.run(scenario())


def test_pending_reads_and_writes_are_cancellable(monkeypatch):
    driver(monkeypatch)

    async def scenario():
        audio = MacAudio()
        await audio.start()
        read = asyncio.create_task(audio.read())
        for _ in range(5):
            await audio.write(bytes(FRAME_BYTES))
        write = asyncio.create_task(audio.write(bytes(FRAME_BYTES)))
        await asyncio.sleep(0)
        assert not read.done() and not write.done()
        read.cancel()
        write.cancel()
        results = await asyncio.gather(read, write, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        await audio.close()

    asyncio.run(scenario())


def test_capture_queue_overflow_is_reported(monkeypatch):
    driver(monkeypatch)

    async def scenario():
        audio = MacAudio()
        await audio.start()
        audio._input = queue.Queue(maxsize=1)
        audio._capture(bytes(FRAME_BYTES), 480, None, None)
        audio._capture(bytes(FRAME_BYTES), 480, None, None)
        try:
            with pytest.raises(RuntimeError, match="fell behind"):
                await audio.read()
        finally:
            await audio.close()

    asyncio.run(scenario())


def test_mac_device_listing_does_not_connect_to_openai(monkeypatch, capsys):
    from combadge.cli import main

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        MacAudio,
        "driver",
        staticmethod(lambda: NS(query_devices=lambda: "0 MacBook Microphone\n1 MacBook Speakers")),
    )
    assert main(["voice", "--list-devices"]) == 0
    assert "MacBook Microphone" in capsys.readouterr().out


@pytest.mark.parametrize("backend,selected", [("auto", "MacAudio"), ("alsa", "AlsaAudio")])
def test_voice_selects_correct_adapter_before_network(monkeypatch, backend, selected):
    from combadge import live
    from combadge.config import Settings

    monkeypatch.setattr("sys.platform", "darwin")
    adapter = Mock()
    adapter.return_value.preflight.side_effect = RuntimeError("selected adapter")
    monkeypatch.setattr(live, selected, adapter)
    with pytest.raises(RuntimeError, match="selected adapter"):
        asyncio.run(
            live.connect_voice(
                Settings(),
                check=False,
                input_device="default",
                output_device="default",
                seconds=1,
                captions=False,
                backend=backend,
            )
        )
    adapter.assert_called_once_with("default", "default")
