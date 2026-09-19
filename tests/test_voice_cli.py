import os
from unittest.mock import patch

import pytest

from combadge.cli import main


def test_missing_key_fails_without_connecting(tmp_path, capsys):
    with patch.dict(os.environ, {}, clear=True), pytest.raises(SystemExit) as error:
        main(["voice", "--check", "--env-file", str(tmp_path / "missing")])
    assert error.value.code == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_api_failure_redacts_key(tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=secret-test-value\n")
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("combadge.live.connect_voice", side_effect=RuntimeError("bad secret-test-value")),
        pytest.raises(SystemExit),
    ):
        main(["voice", "--check", "--env-file", str(path)])
    output = capsys.readouterr()
    assert "secret-test-value" not in output.out + output.err
    assert "[REDACTED]" in output.err


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_duration_must_be_positive_and_finite(value):
    with pytest.raises(SystemExit) as error:
        main(["voice", f"--max-seconds={value}"])
    assert error.value.code == 2


def test_invalid_image_fails_before_connecting(tmp_path):
    path = tmp_path / "invalid.png"
    path.write_text("not an image")
    with patch("combadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
        main(["voice", "--image", str(path), "--check"])
    assert error.value.code == 2
    connect.assert_not_called()


def test_question_without_image_is_rejected():
    with pytest.raises(SystemExit) as error:
        main(["voice", "--question", "What is this?"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["--screenshots", "--check"],
        ["--snapshot-command", "capture-without-output-placeholder"],
        ["--screenshots", "--snapshot-command", "capture {directory}"],
        ["--open-checkout"],
        ["--shopify", "--check"],
        ["--shopify", "--list-devices"],
    ],
)
def test_invalid_snapshot_options_fail_before_connecting(args):
    with patch("combadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
        main(["voice", *args])
    assert error.value.code == 2
    connect.assert_not_called()


@pytest.mark.parametrize("mode", ["--check", "--list-devices"])
def test_speaker_options_require_voice(mode):
    with patch("combadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
        main(["voice", "--speaker", "Edmon=missing.wav", mode])
    assert error.value.code == 2
    connect.assert_not_called()


def test_speaker_references_are_passed_to_voice(tmp_path):
    from combadge.speakers import pcm_wav

    path = tmp_path / "edmon.wav"
    path.write_bytes(pcm_wav(b"\x01\x00" * 24000 * 4))
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=test-key\n")
    with patch("combadge.live.connect_voice") as connect:
        assert main(["voice", "--env-file", str(env), "--speaker", f"Edmon={path}"]) == 0
    tracker = connect.call_args.kwargs["speaker_tracker"]
    assert tracker.transcriber.references[0].name == "Edmon"


def test_speaker_analysis_command_prints_segments_without_transcript(tmp_path, capsys):
    from combadge.speakers import Segment, pcm_wav

    path = tmp_path / "edmon.wav"
    path.write_bytes(pcm_wav(b"\x01\x00" * 24000 * 4))
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=test-key\n")
    with patch("combadge.speakers.Transcriber.analyze", return_value=[Segment(0, 4, "Edmon")]):
        assert (
            main(["speakers", str(path), "--env-file", str(env), "--speaker", f"Edmon={path}"]) == 0
        )
    output = capsys.readouterr().out
    assert "Edmon" in output
    assert "analysis_seconds" in output
    assert "test-key" not in output


@pytest.mark.parametrize(
    "args",
    [
        ["--camera", "--screenshots"],
        ["--camera", "--snapshot-command", "helper {directory}"],
        ["--camera", "--check"],
        ["--camera", "--list-devices"],
        ["--camera-unit", "4"],
        ["--camera", "--camera-unit", "0"],
    ],
)
def test_invalid_camera_options(args):
    with pytest.raises(SystemExit) as error:
        main(["voice", *args])
    assert error.value.code == 2


def test_camera_option_configures_photo_capture(tmp_path):
    from combadge.capture import SnapshotCapture

    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=test-key\n")
    capture = SnapshotCapture(["camera", "{directory}"], source="camera")
    with (
        patch("combadge.cli.qnx_camera_capture", return_value=capture) as factory,
        patch.object(capture, "preflight"),
        patch("combadge.live.connect_voice") as connect,
    ):
        assert main(["voice", "--camera", "--camera-unit", "4", "--env-file", str(env)]) == 0
    factory.assert_called_once_with(4)
    assert connect.call_args.kwargs["snapshot_capture"].source == "camera"
