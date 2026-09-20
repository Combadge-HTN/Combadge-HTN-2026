import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from combadge.cli import main
from combadge.config import Settings
from combadge.live import session_config
from combadge.phone.config import PhoneSettings, SipSettings


@pytest.fixture
def calling_env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "OPENAI_API_KEY=test\nCALL_TRANSPORT=sip\n"
        "CALL_SIP_DOMAIN=test.pstn.twilio.com\nCALL_SIP_USERNAME=badge\n"
        "CALL_SIP_PASSWORD=private-sip-password\nTWILIO_FROM_NUMBER=+14165550100\n"
        'CALL_CONTACTS={"alex":"+14165550101","edmon":"+14165550102"}\n'
        "TWILIO_FROM_NUMBER_TXT=+15485550100\n"
        "TWILIO_ACCOUNT_SID=ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "TWILIO_AUTH_TOKEN=private-sms-token\n"
    )
    with patch.dict(os.environ, {}, clear=True):
        yield path


@pytest.mark.parametrize(
    "options,calls,sms",
    [
        ([], True, True),
        (["--calls"], True, True),
        (["--no-calls"], False, True),
        (["--no-sms"], True, False),
        (["--check"], False, False),
    ],
)
def test_plain_voice_loads_calls_and_sms_independently(calling_env, capsys, options, calls, sms):
    with patch("combadge.live.connect_voice", new_callable=AsyncMock) as connect:
        assert main(["voice", "--env-file", str(calling_env), *options]) == 0
    args = connect.call_args.kwargs
    assert (args["phone_settings"] is not None) == calls
    assert (args["sms"] is not None) == sms
    if calls:
        assert isinstance(args["phone_settings"], SipSettings)
        assert args["phone_settings"].contacts["alex"] == "+14165550101"
        assert args["phone_settings"].from_number == "+14165550100"
    if sms:
        assert args["sms"].settings.from_number == "+15485550100"
    output = capsys.readouterr().out
    assert ("Calling: Twilio SIP enabled." in output) == calls
    assert ("Call contacts: alex, edmon" in output) == calls
    assert "private-sip-password" not in output and "private-sms-token" not in output


@pytest.mark.parametrize("missing", ["CALL_SIP_DOMAIN", "CALL_SIP_PASSWORD", "TWILIO_FROM_NUMBER"])
def test_incomplete_call_setup_does_not_disable_sms(calling_env, monkeypatch, capsys, missing):
    monkeypatch.setenv(missing, "")
    with patch("combadge.live.connect_voice", new_callable=AsyncMock) as connect:
        assert main(["voice", "--env-file", str(calling_env)]) == 0
        assert connect.call_args.kwargs["phone_settings"] is None
        assert connect.call_args.kwargs["sms"] is not None
    assert "Calling: unavailable" in capsys.readouterr().out
    with patch("combadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
        main(["voice", "--calls", "--env-file", str(calling_env)])
    assert error.value.code == 2
    connect.assert_not_called()


def test_relay_configuration_also_loads_automatically(calling_env, monkeypatch):
    monkeypatch.setenv("CALL_TRANSPORT", "relay")
    monkeypatch.setenv("CALL_RELAY_URL", "wss://relay.example.com")
    monkeypatch.setenv("CALL_RELAY_TOKEN", "x" * 32)
    with patch("combadge.live.connect_voice", new_callable=AsyncMock) as connect:
        assert main(["voice", "--env-file", str(calling_env)]) == 0
    assert isinstance(connect.call_args.kwargs["phone_settings"], PhoneSettings)
    monkeypatch.setenv("CALL_RELAY_TOKEN", "")
    assert PhoneSettings.load(calling_env, optional=True) is None


@pytest.mark.parametrize("option", ["--check", "--list-devices"])
def test_call_tools_not_loaded_for_diagnostics(calling_env, option):
    with patch("combadge.phone.config.PhoneSettings.load") as load:
        if option == "--check":
            with patch("combadge.live.connect_voice", new_callable=AsyncMock):
                assert main(["voice", option, "--env-file", str(calling_env)]) == 0
        else:
            with patch("combadge.cli.MacAudio.driver") as driver:
                driver.return_value.query_devices.return_value = ["test-device"]
                assert (
                    main(
                        ["voice", option, "--audio-backend", "mac", "--env-file", str(calling_env)]
                    )
                    == 0
                )
        load.assert_not_called()
        with pytest.raises(SystemExit) as error:
            main(["voice", option, "--calls", "--env-file", str(calling_env)])
        assert error.value.code == 2
        load.assert_not_called()


def test_call_prompt_preserves_recent_recipient_context_without_exposing_numbers():
    config = session_config(Settings(), call_names=["alex", "edmon"], sms_names=["alex", "edmon"])
    backend = config["delegation"]["responses"]
    assert {"call_contact", "send_text", "read_texts"} <= {t["name"] for t in backend["tools"]}
    for instructions in (config["instructions"], backend["instructions"]):
        assert 'Configured call contacts: ["alex", "edmon"]' in instructions
        assert "person just texted" in instructions
    assert "+1416" not in json.dumps(config)
    disabled = session_config(Settings(), sms_names=["alex"])
    assert "Phone calling is disabled" in disabled["instructions"]


@pytest.mark.parametrize("outcome,exit_code", [("completed", 0), ("busy", 1), ("failed", 1)])
def test_standalone_call_still_exits_and_reports_failures(calling_env, outcome, exit_code):
    with (
        patch("combadge.phone.cli.AlsaAudio") as audio,
        patch(
            "combadge.phone.client.call_contact",
            new_callable=AsyncMock,
            return_value={"status": outcome},
        ),
        patch("combadge.live.connect_voice") as voice,
    ):
        audio.return_value.start = AsyncMock()
        audio.return_value.close = AsyncMock()
        assert main(["call", "alex", "--env-file", str(calling_env)]) == exit_code
        voice.assert_not_called()
