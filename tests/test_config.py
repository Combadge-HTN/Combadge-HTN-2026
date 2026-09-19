import os
from unittest.mock import patch

from commbadge.cli import main
from commbadge.config import load_settings


def test_environment_overrides_file_without_mutating_environment(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=file-value\nBROWSERBASE_API_KEY=local-value\n")
    with patch.dict(os.environ, {"OPENAI_API_KEY": "exported-value"}, clear=True):
        settings = load_settings(path)
        assert settings.openai_api_key == "exported-value"
        assert settings.browserbase_api_key == "local-value"
        assert "BROWSERBASE_API_KEY" not in os.environ


def test_missing_file_and_empty_values_are_allowed(tmp_path):
    with patch.dict(os.environ, {}, clear=True):
        assert load_settings(tmp_path / "missing").openai_api_key == ""
        path = tmp_path / ".env"
        path.write_text("OPENAI_API_KEY\nBROWSERBASE_API_KEY=\n")
        assert load_settings(path).openai_api_key == ""
        assert load_settings(path).browserbase_api_key == ""


def test_secrets_are_not_printed_or_in_repr(tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=private-test-value\n")
    with patch.dict(os.environ, {}, clear=True):
        assert "private-test-value" not in repr(load_settings(path))
        assert main(["doctor", "--env-file", str(path)]) == 0
    output = capsys.readouterr()
    assert "private-test-value" not in output.out + output.err
    assert "OPENAI_API_KEY: set" in output.out


def test_values_are_not_interpolated(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY='literal-${OTHER}'\n")
    with patch.dict(os.environ, {"OTHER": "expanded"}, clear=True):
        assert load_settings(path).openai_api_key == "literal-${OTHER}"
