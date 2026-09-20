import asyncio
import base64
import io
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from test_delegation import Connection, call, event
from test_live import FakeAudio, FakeConnection
from test_phone_live import call_events

from commbadge.cli import main
from commbadge.config import Settings
from commbadge.delegation import SnapshotDelegation
from commbadge.live import run_session, session_config
from commbadge.sms import SmsClient, SmsSettings

SID = "AC" + "a" * 32
MESSAGE = "SM" + "b" * 32
CALL_NUMBER = "+14165550100"
SMS_NUMBER = "+15485550100"
RECIPIENT = "+14165550101"


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"TWILIO_ACCOUNT_SID={SID}\nTWILIO_AUTH_TOKEN=private-test-token\n"
        f"TWILIO_FROM_NUMBER={CALL_NUMBER}\nTWILIO_FROM_NUMBER_TXT='{SMS_NUMBER}'\n"
        f'CALL_CONTACTS={{"Alex":"{RECIPIENT}"}}\n'
    )
    with patch.dict(os.environ, {}, clear=True):
        yield path


def client():
    return SmsClient(SmsSettings(SID, "private-test-token", SMS_NUMBER, {"alex": RECIPIENT}))


def queued():
    return {"sid": MESSAGE, "status": "queued"}


def test_config_uses_text_sender_contacts_and_environment_precedence(env_file, monkeypatch):
    settings = SmsSettings.load(env_file)
    assert settings.from_number == SMS_NUMBER
    assert settings.recipient(" ALEX ") == RECIPIENT
    assert settings.recipient(RECIPIENT) == RECIPIENT
    assert "private-test-token" not in repr(settings)
    assert SMS_NUMBER not in repr(settings)
    monkeypatch.setenv("SMS_CONTACTS", '{"Edmon":"+15195550102"}')
    monkeypatch.setenv("TWILIO_FROM_NUMBER_TXT", "+15195550103")
    overridden = SmsSettings.load(env_file)
    assert overridden.contacts == {"edmon": "+15195550102"}
    assert overridden.from_number == "+15195550103"
    monkeypatch.setenv("SMS_CONTACTS", "{}")
    assert SmsSettings.load(env_file).contacts == {}


@pytest.mark.parametrize(
    "name,value",
    [
        ("TWILIO_ACCOUNT_SID", ""),
        ("TWILIO_AUTH_TOKEN", ""),
        ("TWILIO_FROM_NUMBER_TXT", ""),
        ("TWILIO_FROM_NUMBER_TXT", "5485550100"),
        ("SMS_CONTACTS", "[]"),
        ("SMS_CONTACTS", "not-json"),
        ("SMS_CONTACTS", '{"Alex":"+14165550101","alex":"+14165550102"}'),
        ("SMS_CONTACTS", '{"alex":1234}'),
    ],
)
def test_invalid_config_fails_without_falling_back_to_call_number(
    env_file, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        SmsSettings.load(env_file)


def test_send_posts_correct_text_sender_and_auth_without_claiming_delivery(monkeypatch):
    captured = []

    def open_request(request, timeout):
        captured.append(request)
        assert timeout == 15
        return io.BytesIO(json.dumps(queued()).encode())

    monkeypatch.setattr("commbadge.sms.urlopen", open_request)
    result = asyncio.run(client().send("Alex", "Meet at 5 & bring café ☕"))
    request = captured[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    assert parse_qs(request.data.decode()) == {
        "To": [RECIPIENT],
        "From": [SMS_NUMBER],
        "Body": ["Meet at 5 & bring café ☕"],
    }
    auth = base64.b64decode(request.get_header("Authorization").split()[1]).decode()
    assert auth == f"{SID}:private-test-token"
    assert result == {"status": "queued", "message_sid": MESSAGE, "delivered": False}


@pytest.mark.parametrize(
    "recipient,body", [("unknown", "Hello"), ("alex", " "), ("alex", "x" * 1601)]
)
def test_invalid_send_never_contacts_twilio(recipient, body):
    sms = client()
    sms._request = Mock()
    with pytest.raises(ValueError):
        asyncio.run(sms.send(recipient, body))
    sms._request.assert_not_called()


def test_send_deduplicates_aliases_within_delegation_and_allows_new_request():
    async def scenario():
        sms = client()
        sms._request = Mock(return_value=queued())
        first = await sms.send("alex", "Hello", "d1")
        assert await sms.send(RECIPIENT, "Hello", "d1") == first
        assert sms._request.call_count == 1
        await sms.send("alex", "Hello", "d2")
        assert sms._request.call_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [URLError("private-test-token"), TimeoutError(), ValueError()])
def test_network_or_invalid_response_is_unknown_and_never_resent(monkeypatch, failure):
    async def scenario():
        opener = Mock(side_effect=failure)
        monkeypatch.setattr("commbadge.sms.urlopen", opener)
        sms = client()
        first = await sms.send("alex", "Hello", "d1")
        assert first["status"] == "unknown"
        assert await sms.send("alex", "Hello", "d2") == first
        opener.assert_called_once()
        assert "private-test-token" not in json.dumps(first)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,expected", [(400, "failed"), (401, "failed"), (429, "failed"), (500, "unknown")]
)
def test_http_errors_are_sanitized_and_not_retried(monkeypatch, status, expected):
    payload = {"code": 21211, "message": f"secret body private-test-token {RECIPIENT}"}
    opener = Mock(
        side_effect=HTTPError(
            "https://api.twilio.com", status, "bad", {}, io.BytesIO(json.dumps(payload).encode())
        )
    )
    monkeypatch.setattr("commbadge.sms.urlopen", opener)
    result = asyncio.run(client().send("alex", "secret body"))
    assert result["status"] == expected
    assert "secret body" not in json.dumps(result)
    assert "private-test-token" not in json.dumps(result)
    assert RECIPIENT not in json.dumps(result)
    if expected == "failed":
        assert "21211" in result["error"]
    opener.assert_called_once()


@pytest.mark.parametrize("response", [{}, {"sid": MESSAGE, "status": "new-unrecognized-status"}])
def test_malformed_success_blocks_retries(response):
    async def scenario():
        sms = client()
        sms._request = Mock(return_value=response)
        assert (await sms.send("alex", "Hello", "d1"))["status"] == "unknown"
        assert (await sms.send("alex", "Hello", "d2"))["status"] == "unknown"
        sms._request.assert_called_once()

    asyncio.run(scenario())


def test_cancelled_send_is_uncertain_even_in_new_delegation():
    async def scenario():
        sms = client()
        with patch("commbadge.sms.asyncio.to_thread", side_effect=asyncio.CancelledError()) as send:
            with pytest.raises(asyncio.CancelledError):
                await sms.send("alex", "Hello", "d1")
            assert (await sms.send("alex", "Hello", "d2"))["status"] == "unknown"
            send.assert_called_once()

    asyncio.run(scenario())


def test_check_is_read_only_and_scoped_to_text_number(monkeypatch):
    def open_request(request, timeout):
        assert request.get_method() == "GET"
        assert "/IncomingPhoneNumbers.json?" in request.full_url
        assert parse_qs(urlsplit(request.full_url).query) == {
            "PhoneNumber": [SMS_NUMBER],
            "PageSize": ["1"],
        }
        return io.BytesIO(
            json.dumps(
                {
                    "incoming_phone_numbers": [
                        {"phone_number": SMS_NUMBER, "capabilities": {"sms": True}}
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr("commbadge.sms.urlopen", open_request)
    assert asyncio.run(client().check()) == {
        "status": "ok",
        "sms_capable": True,
        "message_sent": False,
    }


@pytest.mark.parametrize(
    "numbers", [[], [{"phone_number": SMS_NUMBER, "capabilities": {"sms": False}}]]
)
def test_check_rejects_missing_or_incapable_sender(numbers):
    sms = client()
    sms._request = Mock(return_value={"incoming_phone_numbers": numbers})
    with pytest.raises(RuntimeError):
        asyncio.run(sms.check())


def test_read_scopes_to_sms_sender_and_filters_other_number_messages():
    sms = client()
    incoming = {
        "sid": MESSAGE,
        "from": RECIPIENT,
        "to": SMS_NUMBER,
        "direction": "inbound",
        "body": "Hi",
        "status": "received",
        "date_sent": "today",
    }
    sms._request = Mock(
        return_value={
            "messages": [
                incoming,
                {**incoming, "to": CALL_NUMBER},
                {**incoming, "direction": "outbound-api"},
                {**incoming, "from": "+15195550105"},
            ]
        }
    )
    result = asyncio.run(sms.read("alex", 5))
    sms._request.assert_called_once_with(
        "Messages", query={"To": SMS_NUMBER, "From": RECIPIENT, "PageSize": 5}
    )
    assert len(result["messages"]) == 1
    assert result["messages"][0]["body"] == "Hi" and result["untrusted_content"]


def test_sms_tools_coexist_with_other_features_and_require_a_client():
    default = session_config(Settings())
    assert "tools" not in default["delegation"]["responses"]
    enabled = session_config(
        Settings(),
        sms_names=["alex"],
        call_names=["alex"],
        web=True,
        composio=True,
        snapshots=True,
        shopping=True,
    )
    backend = enabled["delegation"]["responses"]
    names = {tool["name"] for tool in backend["tools"]}
    assert {
        "send_text",
        "read_texts",
        "call_contact",
        "capture_snapshot",
        "run_connected_app_tool",
        "search_shopify",
    } <= names
    assert "You have no external action tools" not in backend["instructions"]
    assert "never resend automatically" in backend["instructions"]
    assert backend["parallel_tool_calls"] is False
    assert len(session_config(Settings(), sms_names=[])["delegation"]["responses"]["tools"]) == 2


def test_delegation_routes_sms_and_continues_response():
    async def scenario():
        connection = Connection()
        sms = NS(execute=AsyncMock(return_value=queued()))
        delegation = SnapshotDelegation(connection, None, lambda _: None, sms=sms)
        args = {"recipient": "alex", "body": "Hello"}
        delegation.observe(event("response.created", response=NS(id="r1")))
        delegation.observe(call("c1", "send_text", json.dumps(args)))
        delegation.observe(event("response.completed", response=NS(id="r1")))
        worker = asyncio.create_task(delegation.run())
        try:
            await asyncio.wait_for(connection.continued.wait(), 1)
            sms.execute.assert_awaited_once_with("send_text", args, "d1")
            assert json.loads(connection.messages[0]["item"]["output"]) == queued()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_voice_session_registers_sms_and_finishes_tool_exchange():
    async def scenario():
        stop = asyncio.Event()
        events = call_events()
        events[1].event.item.name = "send_text"
        events[1].event.item.arguments = '{"recipient":"alex","body":"Hello"}'
        connection = FakeConnection(events)
        sms = client()

        async def send(*_):
            stop.set()
            return queued()

        sms.execute = AsyncMock(side_effect=send)
        stats = await run_session(
            connection, FakeAudio(stop), Settings(), stop, sms=sms, seconds=1, report=lambda _: None
        )
        assert stats.finalized and stats.phone_contact is None
        tools = connection.messages[0]["session"]["delegation"]["responses"]["tools"]
        assert {t["name"] for t in tools} == {"send_text", "read_texts"}
        sms.execute.assert_awaited_once_with(
            "send_text", {"recipient": "alex", "body": "Hello"}, "d1"
        )
        assert any(m["type"] == "response.create" for m in connection.messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["check", "contacts", "send", "read"])
def test_cli_sms_needs_no_openai_or_sip_configuration(env_file, capsys, action):
    args = ["sms", "--env-file", str(env_file), action]
    if action == "send":
        args += ["alex", "Hello"]
    with patch("commbadge.cli.SmsClient") as cls:
        cls.return_value.settings = SmsSettings.load(env_file)
        cls.return_value.check = AsyncMock(return_value={"status": "ok"})
        cls.return_value.send = AsyncMock(return_value=queued())
        cls.return_value.read = AsyncMock(return_value={"status": "ok", "messages": []})
        assert main(args) == 0
        if action == "contacts":
            cls.return_value.check.assert_not_called()
            cls.return_value.send.assert_not_called()
        if action == "send":
            cls.return_value.send.assert_awaited_once_with("alex", "Hello")
    assert "private-test-token" not in capsys.readouterr().out


@pytest.mark.parametrize("status", ["failed", "unknown", "undelivered"])
def test_cli_unsuccessful_send_has_nonzero_exit(env_file, status):
    with patch(
        "commbadge.cli.SmsClient.send", new_callable=AsyncMock, return_value={"status": status}
    ):
        assert main(["sms", "--env-file", str(env_file), "send", "alex", "Hi"]) == 1


@pytest.mark.parametrize("options,enabled", [([], True), (["--sms"], True), (["--no-sms"], False)])
def test_voice_cli_enables_sms_automatically_with_explicit_override(
    env_file, monkeypatch, capsys, options, enabled
):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with (
        patch("commbadge.live.connect_voice", new_callable=AsyncMock) as connect,
        patch("commbadge.sms.urlopen") as request,
    ):
        assert main(["voice", "--env-file", str(env_file), *options]) == 0
        sms = connect.call_args.kwargs["sms"]
        assert (sms is not None) == enabled
        if enabled:
            assert sms.settings.from_number == SMS_NUMBER
            assert sms.settings.recipient("Alex") == RECIPIENT
        assert connect.call_args.kwargs["phone_settings"] is None
        request.assert_not_called()
    output = capsys.readouterr().out
    assert ("SMS contacts: alex" in output) == enabled
    assert "private-test-token" not in output
    assert RECIPIENT not in output


@pytest.mark.parametrize(
    "missing", ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER_TXT"]
)
def test_voice_without_complete_sms_config_still_works(env_file, monkeypatch, capsys, missing):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv(missing, "")
    with patch("commbadge.live.connect_voice", new_callable=AsyncMock) as connect:
        assert main(["voice", "--env-file", str(env_file)]) == 0
        assert connect.call_args.kwargs["sms"] is None
    assert "Text messaging: unavailable" in capsys.readouterr().out
    with patch("commbadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
        main(["voice", "--sms", "--env-file", str(env_file)])
    assert error.value.code == 1
    connect.assert_not_called()


def test_voice_audio_check_does_not_load_configured_sms(env_file, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with (
        patch("commbadge.live.connect_voice", new_callable=AsyncMock) as connect,
        patch("commbadge.cli.SmsSettings.load") as load_sms,
    ):
        assert main(["voice", "--check", "--env-file", str(env_file)]) == 0
        assert connect.call_args.kwargs["sms"] is None
        load_sms.assert_not_called()


def test_plain_voice_exposes_edmon_to_live_and_backend_without_exposing_number(
    env_file, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("CALL_CONTACTS", json.dumps({"Edmon": RECIPIENT}))
    with patch("commbadge.live.connect_voice", new_callable=AsyncMock) as connect:
        assert main(["voice", "--env-file", str(env_file)]) == 0
    sms = connect.call_args.kwargs["sms"]
    config = session_config(Settings(), sms_names=sorted(sms.settings.contacts))
    backend = config["delegation"]["responses"]
    assert 'Configured SMS contacts: ["edmon"]' in config["instructions"]
    assert 'Configured SMS contacts: ["edmon"]' in backend["instructions"]
    assert "letter-by-letter spelling" in config["instructions"]
    assert RECIPIENT not in json.dumps(config)
    sms._request = Mock(return_value=queued())
    result = asyncio.run(
        sms.execute("send_text", {"recipient": "edmon", "body": "I'm running late"})
    )
    assert result["status"] == "queued"
    assert sms._request.call_args.kwargs["data"] == {
        "To": RECIPIENT,
        "From": SMS_NUMBER,
        "Body": "I'm running late",
    }


def test_disabled_sms_instructions_explain_missing_capability():
    config = session_config(Settings(), composio=True)
    assert "SMS texting is disabled" in config["instructions"]
    assert not {"send_text", "read_texts"} & {
        tool["name"] for tool in config["delegation"]["responses"]["tools"]
    }


@pytest.mark.parametrize("option", ["--check", "--list-devices"])
def test_voice_check_cannot_enable_sms(env_file, option):
    with patch("commbadge.live.connect_voice") as connect, pytest.raises(SystemExit) as error:
        main(["voice", "--sms", option, "--env-file", str(env_file)])
    assert error.value.code == 2
    connect.assert_not_called()
