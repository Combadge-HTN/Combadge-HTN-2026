import asyncio
import os
import sys
from pathlib import Path

import pytest

from commbadge.capture import SnapshotCapture


def make_helper(tmp_path, body):
    helper = tmp_path / "capture.py"
    helper.write_text("import pathlib, sys, time, os\n" + body)
    return [sys.executable, str(helper), "{directory}"]


def test_capture_uses_fresh_directory_and_removes_image(tmp_path):
    marker = tmp_path / "directory"
    command = make_helper(
        tmp_path,
        f"pathlib.Path({str(marker)!r}).write_text(sys.argv[1])\n"
        "(pathlib.Path(sys.argv[1]) / 'screen.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\nimage')\n",
    )
    capture = SnapshotCapture(command)
    image = asyncio.run(capture.capture("What is shown?"))
    first_directory = marker.read_text()
    assert image.question == "What is shown?"
    assert not Path(first_directory).exists()
    asyncio.run(capture.capture("Look again"))
    assert marker.read_text() != first_directory
    assert not Path(marker.read_text()).exists()


@pytest.mark.parametrize(
    "body",
    [
        "pass\n",  # Cancelled helper can exit successfully without an image.
        "raise SystemExit(1)\n",
        "(pathlib.Path(sys.argv[1]) / 'screen.png').write_text('invalid image')\n",
        "(pathlib.Path(sys.argv[1]) / 'screen.png').symlink_to('/etc/passwd')\n",
    ],
)
def test_bad_capture_never_reuses_an_old_image(tmp_path, body):
    capture = SnapshotCapture(make_helper(tmp_path, body))
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(capture.capture("Look"))


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_or_cancellation_stops_helper_and_cleans_directory(tmp_path, cancel):
    marker = tmp_path / "started"
    command = make_helper(
        tmp_path,
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()) + '\\n' + sys.argv[1])\n"
        "time.sleep(30)\n",
    )

    async def scenario():
        capture = SnapshotCapture(command, timeout=3 if cancel else 0.3)
        task = asyncio.create_task(capture.capture("Look"))
        if cancel:
            async with asyncio.timeout(2):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task

    asyncio.run(scenario())
    pid, directory = marker.read_text().splitlines()
    assert not Path(directory).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid), 0)


def test_qnx_camera_uses_installed_helper_and_physical_unit(monkeypatch):
    from commbadge.capture import qnx_camera_capture

    monkeypatch.setattr("commbadge.capture.platform.system", lambda: "QNX")
    monkeypatch.setattr("commbadge.capture.shutil.which", lambda _: "/opt/bin/combadge-camera")
    capture = qnx_camera_capture()
    assert capture.source == "camera"
    assert capture.command == [
        "/opt/bin/combadge-camera",
        "--unit",
        "4",
        "--output-dir",
        "{directory}",
    ]
    assert qnx_camera_capture(5).command[2] == "5"


def test_qnx_camera_does_not_fall_back_to_desktop_screenshot(monkeypatch):
    from commbadge.capture import qnx_camera_capture

    monkeypatch.setattr("commbadge.capture.platform.system", lambda: "Linux")
    with pytest.raises(ValueError, match="QNX"):
        qnx_camera_capture()


def test_qnx_camera_missing_build_has_actionable_error(monkeypatch):
    from commbadge.capture import qnx_camera_capture

    monkeypatch.setattr("commbadge.capture.platform.system", lambda: "QNX")
    monkeypatch.setattr("commbadge.capture.shutil.which", lambda _: None)
    monkeypatch.setattr("commbadge.capture.Path.is_file", lambda _: False)
    with pytest.raises(ValueError, match="make -C native/qnx-camera"):
        qnx_camera_capture()
