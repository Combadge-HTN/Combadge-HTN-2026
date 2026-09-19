"""Capture a fresh still image with a configured device helper."""

import asyncio
import shutil
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from commbadge.vision import ImageInput

COSMIC_SCREENSHOT = [
    "cosmic-screenshot",
    "--interactive=false",
    "--notify=false",
    "--save-dir",
    "{directory}",
]


class SnapshotCapture:
    """Run a trusted command that writes one encoded image into a fresh directory."""

    def __init__(self, command: list[str], *, timeout: float = 30):
        if not command or not any("{directory}" in arg for arg in command):
            raise ValueError("Snapshot command must contain {directory} for its output directory.")
        self.command = command
        self.timeout = timeout

    def preflight(self) -> None:
        if not shutil.which(self.command[0]):
            raise ValueError(f"Snapshot helper is missing: {self.command[0]}")

    async def capture(self, question: str) -> ImageInput:
        self.preflight()
        with TemporaryDirectory(prefix="commbadge-snapshot-") as directory:
            command = [arg.replace("{directory}", directory) for arg in self.command]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                try:
                    code = await asyncio.wait_for(process.wait(), self.timeout)
                except TimeoutError as error:
                    raise RuntimeError(
                        "Capture timed out; check screen/camera permissions."
                    ) from error
                if code:
                    raise RuntimeError("Capture failed; check the helper and device permissions.")
                files = [
                    path
                    for path in Path(directory).iterdir()
                    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                    and path.is_file()
                    and not path.is_symlink()
                ]
                if len(files) != 1:
                    raise RuntimeError(
                        "Capture must produce one image; it may have been cancelled."
                    )
                # Decoding/resizing must not block the audio event loop. Keep the directory
                # alive until the worker finishes, including when the session is cancelled.
                conversion = asyncio.create_task(
                    asyncio.to_thread(ImageInput.from_file, files[0], question)
                )
                try:
                    return await asyncio.shield(conversion)
                except asyncio.CancelledError:
                    await asyncio.gather(conversion, return_exceptions=True)
                    raise
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
