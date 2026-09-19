"""PCM audio transports for voice sessions."""

import asyncio
import shutil
import subprocess
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
