"""Capture a fresh still image with a configured device helper."""

import asyncio
import platform
import shutil
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from combadge.vision import ImageInput

COSMIC_SCREENSHOT = [
    "cosmic-screenshot",
    "--interactive=false",
    "--notify=false",
    "--save-dir",
    "{directory}",
]


def qnx_camera_capture(unit: int = 4) -> SnapshotCapture:
    """Select the built QNX helper, independent of the current directory."""
    if platform.system() != "QNX":
        raise ValueError("--camera requires the QNX camera helper on the Pi")
    if unit < 1:
        raise ValueError("Camera unit must be positive")
    executable = shutil.which("combadge-camera")
    if executable is None:
        helper = Path(__file__).resolve().parents[2] / "native/qnx-camera/combadge-camera"
        if not helper.is_file():
            raise ValueError("Build the camera helper first: make -C native/qnx-camera")
        executable = str(helper)
    return SnapshotCapture(
        [executable, "--unit", str(unit), "--output-dir", "{directory}"], source="camera"
    )


class SnapshotCapture:
    """Run a trusted command that writes one encoded image into a fresh directory."""

    def __init__(self, command: list[str], *, timeout: float = 30, source: str = "device"):
        if not command or not any("{directory}" in arg for arg in command):
            raise ValueError("Snapshot command must contain {directory} for its output directory.")
        if source not in ("device", "screen", "camera"):
            raise ValueError("Invalid capture source")
        self.source = source
        self.command = command
        self.timeout = timeout

    def preflight(self) -> None:
        if not shutil.which(self.command[0]):
            raise ValueError(f"Snapshot helper is missing: {self.command[0]}")

    async def capture(self, question: str) -> ImageInput:
        self.preflight()
        with TemporaryDirectory(prefix="combadge-snapshot-") as directory:
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
