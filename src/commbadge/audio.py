"""PCM audio transports for voice sessions."""

import asyncio
import shutil
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
    """Connect raw PCM capture/playback helpers without invoking a shell.

    Helpers must produce/consume mono PCM16LE at 24 kHz.
    """

    def __init__(self, capture_command: list[str], playback_command: list[str]):
        self.capture_command = capture_command
        self.playback_command = playback_command
        self.recorder: asyncio.subprocess.Process | None = None
        self.player: asyncio.subprocess.Process | None = None
        self._logs: dict[str, str] = {}
        self._log_tasks: list[asyncio.Task] = []

    def preflight(self) -> None:
        for command in (self.capture_command, self.playback_command):
            if not command or shutil.which(command[0]) is None:
                raise RuntimeError("Audio helper is missing; check capture/playback commands.")

    async def _collect_errors(self, name: str, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(1024):
            self._logs[name] = (self._logs.get(name, "") + chunk.decode(errors="replace"))[-2048:]

    async def start(self) -> None:
        self.preflight()
        try:
            self.player = await asyncio.create_subprocess_exec(
                *self.playback_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self.recorder = await asyncio.create_subprocess_exec(
                *self.capture_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            for name, process in (("capture", self.recorder), ("playback", self.player)):
                self._log_tasks.append(asyncio.create_task(self._collect_errors(name, process)))
        except BaseException:
            await self.close()
            raise

    async def read(self) -> bytes:
        assert self.recorder and self.recorder.stdout
        try:
            return await self.recorder.stdout.readexactly(FRAME_BYTES)
        except asyncio.IncompleteReadError as error:
            raise RuntimeError(
                "Microphone stopped. Check the capture device/helper. "
                + self._logs.get("capture", "")
            ) from error

    async def write(self, data: bytes) -> None:
        assert self.player and self.player.stdin
        try:
            self.player.stdin.write(data)
            await self.player.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise RuntimeError(
                "Speaker stopped. Check the playback device/helper. "
                + self._logs.get("playback", "")
            ) from error

    async def close(self) -> None:
        for process in (self.recorder, self.player):
            if process is not None:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        for task in self._log_tasks:
            task.cancel()
        await asyncio.gather(*self._log_tasks, return_exceptions=True)


class AlsaAudio(CommandAudio):
    """Raw PCM transport using arecord and aplay."""

    def __init__(self, input_device: str = "default", output_device: str = "default"):
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
            ["aplay", "-D", output_device, *common],
        )

    def preflight(self) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError(
                "ALSA is the Linux adapter. For QNX, see docs/QNX.md and the commands backend."
            )
        for name in ("arecord", "aplay"):
            if shutil.which(name) is None:
                raise RuntimeError(f"{name} is missing. On Linux: sudo apt install alsa-utils")
