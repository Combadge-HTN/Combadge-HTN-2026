import os
from unittest.mock import patch

from combadge.cli import main
from combadge.config import SHOPIFY_PROFILE_URL, load_settings


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


def test_shopify_defaults_and_overrides(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SHOPIFY_AGENT_PROFILE_URL=\nSHOPIFY_COUNTRY=US\nSHOPIFY_CURRENCY=USD\n")
    with patch.dict(os.environ, {}, clear=True):
        settings = load_settings(path)
        assert settings.shopify_agent_profile_url == SHOPIFY_PROFILE_URL
        assert (settings.shopify_country, settings.shopify_currency) == ("US", "USD")
    with patch.dict(
        os.environ, {"SHOPIFY_AGENT_PROFILE_URL": "https://example.com/profile.json"}, clear=True
    ):
        assert load_settings(path).shopify_agent_profile_url == "https://example.com/profile.json"


def test_speechmatics_credential_is_loaded_privately(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SPEECHMATICS_API_KEY=speaker-private-value\n")
    with patch.dict(os.environ, {}, clear=True):
        settings = load_settings(path)
        assert settings.speechmatics_api_key == "speaker-private-value"
        assert "speaker-private-value" not in repr(settings)
