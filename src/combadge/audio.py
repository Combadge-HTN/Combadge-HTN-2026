"""PCM audio transports for voice sessions."""

import asyncio
import queue
import shutil
import subprocess
import sys
from contextlib import suppress
from typing import Protocol

RATE = 24_000
FRAME_BYTES = 960  # 20 milliseconds of mono PCM16LE


class AudioIO(Protocol):
    async def start(self) -> None: ...
    async def read(self) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class SilenceAudio:
    """Paced synthetic input for a cloud smoke check, without opening audio devices."""

    async def start(self) -> None:
        pass

    async def read(self) -> bytes:
        await asyncio.sleep(0.02)
        return bytes(FRAME_BYTES)

    async def write(self, data: bytes) -> None:
        pass

    async def close(self) -> None:
        pass


class CommandAudio:
    """Stream mono PCM16LE at 24 kHz through native capture/playback programs.

    Ordinary pipes keep helper I/O portable across QNX and other platforms.
    Blocking operations run in threads; terminating helpers releases pending I/O.
    """

    def __init__(self, capture_command: list[str], playback_command: list[str] | None):
        self.capture_command = capture_command
        self.playback_command = playback_command
        self.recorder: subprocess.Popen | None = None
        self.player: subprocess.Popen | None = None
        self._logs: dict[str, str] = {}
        self._log_tasks: list[asyncio.Task] = []

    def preflight(self) -> None:
        commands = [self.capture_command]
        if self.playback_command is not None:
            commands.append(self.playback_command)
        for command in commands:
            if not command or shutil.which(command[0]) is None:
                raise RuntimeError("Audio helper is missing; check capture/playback commands.")

    def _collect_errors(self, name: str, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        while chunk := process.stderr.read(1024):
            self._logs[name] = (self._logs.get(name, "") + chunk.decode(errors="replace"))[-2048:]

    async def start(self) -> None:
        self.preflight()
        try:
            if self.playback_command is not None:
                self.player = subprocess.Popen(
                    self.playback_command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            self.recorder = subprocess.Popen(
                self.capture_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            for name, process in (("capture", self.recorder), ("playback", self.player)):
                if process is None:
                    continue
                self._log_tasks.append(
                    asyncio.create_task(asyncio.to_thread(self._collect_errors, name, process))
                )
        except BaseException:
            await self.close()
            raise

    def _read_frame(self) -> bytes:
        assert self.recorder and self.recorder.stdout
        frame = bytearray()
        while len(frame) < FRAME_BYTES:
            chunk = self.recorder.stdout.read(FRAME_BYTES - len(frame))
            if not chunk:
                raise RuntimeError(
                    "Microphone stopped. Check the capture device/helper. "
                    + self._logs.get("capture", "")
                )
            frame.extend(chunk)
        return bytes(frame)

    async def read(self) -> bytes:
        return await asyncio.to_thread(self._read_frame)

    def _write_all(self, data: bytes) -> None:
        assert self.player and self.player.stdin
        remaining = memoryview(data)
        try:
            while remaining:
                count = self.player.stdin.write(remaining)
                if not count:
                    raise BrokenPipeError("Playback pipe closed.")
                remaining = remaining[count:]
        except (BrokenPipeError, ConnectionResetError) as error:
            raise RuntimeError(
                "Speaker stopped. Check the playback device/helper. "
                + self._logs.get("playback", "")
            ) from error

    async def write(self, data: bytes) -> None:
        if self.playback_command is None:
            return
        await asyncio.to_thread(self._write_all, data)

    async def drain(self) -> None:
        """Send EOF so the playback helper can flush its final samples."""
        if self.player is None:
            return
        if self.player.stdin is not None and not self.player.stdin.closed:
            self.player.stdin.close()
        try:
            code = await asyncio.to_thread(self.player.wait, timeout=3)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Playback helper did not finish within three seconds") from error
        if code:
            raise RuntimeError(f"Playback helper exited with status {code}")

    async def close(self) -> None:
        processes = [p for p in (self.recorder, self.player) if p is not None]
        # Stop both ends before waiting, releasing blocked capture and playback threads.
        for process in processes:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.terminate()
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, timeout=2)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    process.kill()
                await asyncio.to_thread(process.wait)
        await asyncio.gather(*self._log_tasks, return_exceptions=True)
        self._log_tasks.clear()
        for process in processes:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


class AlsaAudio(CommandAudio):
    """Raw PCM transport using arecord and aplay."""

    def __init__(
        self,
        input_device: str = "default",
        output_device: str = "default",
        *,
        playback: bool = True,
    ):
        common = [
            "-q",
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-c",
            "1",
            "-r",
            str(RATE),
            "--buffer-time=100000",
        ]
        super().__init__(
            ["arecord", "-D", input_device, *common],
            ["aplay", "-D", output_device, *common] if playback else None,
        )

    def preflight(self) -> None:
        for name in ("arecord", "aplay") if self.playback_command is not None else ("arecord",):
            if shutil.which(name) is None:
                raise RuntimeError(
                    f"{name} is missing. Install the platform audio utilities "
                    "or configure --audio-backend commands; see docs/QNX.md."
                )


def audio_backend(value: str = "auto") -> str:
    return ("mac" if sys.platform == "darwin" else "alsa") if value == "auto" else value


class MacAudio:
    """CoreAudio via sounddevice, with bounded callback queues and no blocking reads."""

    def __init__(self, input_device: str = "default", output_device: str = "default"):
        def device(value):
            return None if value == "default" else int(value) if value.isdecimal() else value

        self.input_device = device(input_device)
        self.output_device = device(output_device)
        self.recorder = self.player = None
        self._input = queue.Queue(maxsize=100)  # At most two seconds of microphone audio.
        self._output = queue.Queue(maxsize=5)  # At most 100 ms in the native output adapter.
        self._remainder = b""
        self._closed = True
        self._failed = False

    @staticmethod
    def driver():
        try:
            import sounddevice
        except (ImportError, OSError) as error:
            raise RuntimeError(
                "Mac audio dependency missing. Run: uv sync --extra voice --extra mac"
            ) from error
        return sounddevice

    def preflight(self) -> None:
        sd = self.driver()
        try:
            sd.check_input_settings(
                device=self.input_device, channels=1, dtype="int16", samplerate=RATE
            )
            sd.check_output_settings(
                device=self.output_device, channels=1, dtype="int16", samplerate=RATE
            )
        except sd.PortAudioError as error:
            raise RuntimeError(
                "Cannot use the selected audio devices at 24 kHz. "
                "Run combadge voice --list-devices and select --input-device/--output-device. "
                + str(error)
            ) from error

    def _capture(self, data, frames, timing, status) -> None:
        if self._closed:
            return
        try:
            self._input.put_nowait(bytes(data))
        except queue.Full:
            self._failed = True

    def _play(self, data, frames, timing, status) -> None:
        size = frames * 2  # Mono PCM16.
        while len(self._remainder) < size:
            try:
                self._remainder += self._output.get_nowait()
            except queue.Empty:
                break
        data[:] = self._remainder[:size].ljust(size, b"\x00")
        self._remainder = self._remainder[size:]

    async def start(self) -> None:
        self.preflight()
        sd = self.driver()
        self._input = queue.Queue(maxsize=100)
        self._output = queue.Queue(maxsize=5)
        self._remainder = b""
        self._failed = False
        self._closed = False
        options = dict(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME_BYTES // 2)
        try:
            self.player = sd.RawOutputStream(
                device=self.output_device, callback=self._play, **options
            )
            self.recorder = sd.RawInputStream(
                device=self.input_device, callback=self._capture, **options
            )
            self.player.start()
            self.recorder.start()
        except Exception as error:
            await self.close()
            raise RuntimeError(
                "Could not start Mac audio. Allow Microphone access for your terminal in "
                "System Settings > Privacy & Security > Microphone, then retry. " + str(error)
            ) from error

    async def read(self) -> bytes:
        while not self._closed:
            if self._failed:
                raise RuntimeError("Microphone processing fell behind; restart the voice session.")
            if not self.recorder.active:
                raise RuntimeError("Mac microphone stopped or was disconnected.")
            try:
                return self._input.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
        raise RuntimeError("Mac audio is closed.")

    async def write(self, data: bytes) -> None:
        if len(data) % 2:
            raise ValueError("Playback needs complete PCM16 samples.")
        for offset in range(0, len(data), FRAME_BYTES):
            while True:
                if self._closed or not self.player.active:
                    raise RuntimeError("Mac speaker stopped or was disconnected.")
                try:
                    self._output.put_nowait(data[offset : offset + FRAME_BYTES])
                    break
                except queue.Full:
                    await asyncio.sleep(0.005)

    async def close(self) -> None:
        self._closed = True
        for stream in (self.recorder, self.player):
            if stream is not None:
                try:
                    stream.abort(ignore_errors=True)
                finally:
                    stream.close(ignore_errors=True)
        self.recorder = self.player = None
