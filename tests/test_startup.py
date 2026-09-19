from unittest.mock import patch

from combadge.cli import main
from combadge.startup import ROOT, launch


def test_start_uses_console_camera_and_repo_environment_from_any_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--max-seconds", "30"]) == 0
    args = routed.call_args.args[0]
    with patch("combadge.cli.main", return_value=0) as voice:
        assert launch(args) == 0
    command = voice.call_args.args[0]
    assert command[:3] == ["voice", "--audio-backend", "console"]
    assert command[command.index("--env-file") + 1] == str(ROOT / ".env")
    assert command[-3:] == ["--camera", "--camera-unit", "4"]


def test_start_can_disable_camera():
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--no-camera"]) == 0
    with patch("combadge.cli.main", return_value=0) as voice:
        launch(routed.call_args.args[0])
    assert "--camera" not in voice.call_args.args[0]


def test_bluetooth_is_explicit_and_routes_to_the_example():
    with patch("combadge.bluetooth_startup.launch", return_value=0) as bluetooth:
        assert main(["start", "--bluetooth", "--max-seconds", "30"]) == 0
    assert bluetooth.call_args.args[0].bluetooth


def test_bluetooth_reconfigures_48khz_before_opening_fifo():
    from combadge.bluetooth_startup import Startup

    state = Startup()
    assert state.observe("Controller ready.") == b"a"
    state.observe("Received SBC codec configuration, sampling frequency 48000")
    assert state.observe("Stream established") == b"w"
    state.observe("Received SBC codec configuration, sampling frequency 44100")
    assert state.observe("Stream started") == b"f"
    assert not state.ready
    state.observe("PCM FIFO ready")
    assert state.ready
