import os
from unittest.mock import patch

import pytest

from commbadge.cli import main


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
        patch("commbadge.live.connect_voice", side_effect=RuntimeError("bad secret-test-value")),
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
    with patch("commbadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
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
    ],
)
def test_invalid_snapshot_options_fail_before_connecting(args):
    with patch("commbadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
        main(["voice", *args])
    assert error.value.code == 2
    connect.assert_not_called()
