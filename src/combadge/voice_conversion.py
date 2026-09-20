"""Stream assistant playback through an optional external RVC voice server."""

import asyncio
import json
import os
import struct
from pathlib import Path
from urllib.parse import urlsplit

from combadge.audio import RATE, AudioIO

ROOT = Path(__file__).resolve().parents[2]


def _validate_ready(message):
    ready = json.loads(message)
    if ready.get("type") != "ready" or ready.get("rate") != RATE or ready.get("protocol") != 1:
        raise RuntimeError("The voice converter uses an incompatible audio protocol.")


class LocalConverter:
    """Start the installed laptop converter, or reuse an already running server."""

    def __init__(self, url: str, token: str = ""):
        self.url, self.token = url, token
        self.process = None

    async def _probe(self):
        from websockets.asyncio.client import connect

        async with connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
            open_timeout=2,
            close_timeout=1,
        ) as socket:
            async with asyncio.timeout(3):
                _validate_ready(await socket.recv())

    async def start(self, stop: asyncio.Event):
        address = urlsplit(self.url)
        if address.scheme != "ws" or address.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Converter autostart requires a local ws:// address.")
        try:
            await self._probe()
            return
        except OSError:
            pass
        root = ROOT
        runtime = root / ".majel"
        python = runtime / "env" / "bin" / "python"
        if not python.is_file():
            raise RuntimeError("Majel is not installed. Run scripts/setup-majel.sh first.")
        log_path = runtime / "server.log"
        print(f"Warming up the Majel voice converter (log: {log_path})…", flush=True)
        with log_path.open("ab") as log:
            self.process = await asyncio.create_subprocess_exec(
                str(python),
                str(root / "scripts" / "majel_server.py"),
                "--host",
                address.hostname,
                "--port",
                str(address.port or 80),
                cwd=root,
                env={**os.environ, "COMBADGE_VOICE_CONVERSION_TOKEN": self.token},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
        try:
            async with asyncio.timeout(90):
                while not stop.is_set():
                    if self.process.returncode is not None:
                        raise RuntimeError(f"Majel startup failed. See {log_path}.")
                    try:
                        await self._probe()
                        return
                    except OSError:
                        pass
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
        except TimeoutError:
            raise RuntimeError(f"Majel startup timed out. See {log_path}.") from None

    async def close(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None


class ConvertedAudio:
    """Convert output only; the microphone still goes directly to GPT-Live.

    Sending and playback run independently so conversion overlaps native playback.
    The server acknowledges input bytes separately from padded output/tail audio.
    """

    def __init__(self, audio: AudioIO, url: str, token: str = ""):
        self.audio = audio
        self.url = url
        self.token = token
        self.socket = None
        self.receiver = None
        self.pending = 0
        self.flushed = asyncio.Event()

    async def start(self) -> None:
        from websockets.asyncio.client import connect

        self.pending = 0
        self.flushed.clear()
        try:
            self.socket = await connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                open_timeout=10,
                close_timeout=1,
                max_size=RATE * 2 + 4,
                max_queue=4,
            )
            async with asyncio.timeout(20):
                _validate_ready(await self.socket.recv())
            await self.audio.start()
            self.receiver = asyncio.create_task(self._receive())
        except BaseException:
            await self.close()
            raise

    def _check(self) -> None:
        if self.receiver is None or self.socket is None:
            raise RuntimeError("The voice converter is not connected.")
        if self.receiver.done():
            self.receiver.result()
            raise RuntimeError("The voice converter disconnected.")

    async def _receive(self) -> None:
        async for message in self.socket:
            if isinstance(message, str):
                if json.loads(message).get("type") != "flushed" or self.pending:
                    raise RuntimeError("Invalid voice converter flush acknowledgment.")
                self.flushed.set()
                continue
            if len(message) < 6 or (len(message) - 4) % 2:
                raise RuntimeError("The voice converter returned invalid PCM audio.")
            consumed = struct.unpack("!I", message[:4])[0]
            if consumed % 2 or consumed > self.pending:
                raise RuntimeError("Invalid voice converter input acknowledgment.")
            self.pending -= consumed
            await self.audio.write(message[4:])

    async def read(self) -> bytes:
        self._check()
        return await self.audio.read()

    async def write(self, data: bytes) -> None:
        self._check()
        if not data or len(data) % 2:
            raise ValueError("Voice conversion requires complete PCM16 samples.")
        if self.pending + len(data) > RATE * 2 * 3:
            raise RuntimeError("Voice conversion fell behind; stopping to avoid stale speech.")
        self.pending += len(data)
        await self.socket.send(data)

    async def drain(self) -> None:
        self._check()
        self.flushed.clear()
        await self.socket.send(json.dumps({"type": "flush"}))
        waiter = asyncio.create_task(self.flushed.wait())
        try:
            async with asyncio.timeout(10):
                await asyncio.wait((waiter, self.receiver), return_when=asyncio.FIRST_COMPLETED)
                self._check()
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        drain = getattr(self.audio, "drain", None)
        if drain is not None:
            await drain()

    async def close(self) -> None:
        if self.receiver is not None:
            self.receiver.cancel()
        # Closing the device releases any native writer before joining its task.
        try:
            await self.audio.close()
        finally:
            if self.socket is not None:
                await self.socket.close()
                self.socket = None
            if self.receiver is not None:
                await asyncio.gather(self.receiver, return_exceptions=True)
                self.receiver = None
