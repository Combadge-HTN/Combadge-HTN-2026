import asyncio
import io
import json
import os
import urllib.error
from itertools import product
from unittest.mock import AsyncMock, patch

import pytest

from combadge.cli import main
from combadge.composio import (
    API_URL,
    COMPOSIO_TOOL_NAMES,
    MAX_RESULT_CHARS,
    ComposioClient,
)
from combadge.config import Settings, load_settings
from combadge.live import session_config

SLUG = "GMAIL_SEND_EMAIL"


def account(app="gmail", user="wearer", identity="ca_test", **extra):
    return {
        "id": identity,
        "user_id": user,
        "toolkit": {"slug": app},
        "status": "ACTIVE",
        "state": {"val": {"access_token": "must-not-leak"}},
        **extra,
    }


def schema(slug=SLUG, app="gmail"):
    return {
        "slug": slug,
        "toolkit": {"slug": app},
        "version": "20260915_00",
        "description": "Send email",
        "input_parameters": {
            "type": "object",
            "properties": {"recipient_email": {"type": "string"}},
        },
    }


def prepared_client(result=None):
    client = ComposioClient("private-api-key", "wearer")
    client.account = AsyncMock(return_value="ca_test")
    client._api = AsyncMock(
        return_value=result
        or {
            "successful": True,
            "data": {"id": "msg_123", "status": "queued"},
            "error": None,
        }
    )
    client._remember_schema(schema())
    client._inspected.add(SLUG)
    return client


def send_args():
    return {
        "tool_slug": SLUG,
        "arguments_json": '{"recipient_email":"recipient@example.com","body":"Test"}',
    }


def test_configuration_is_optional_private_and_environment_takes_precedence(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "COMPOSIO_API_KEY=file-key\nCOMPOSIO_USER_ID=wearer\n"
        "COMPOSIO_GMAIL_ACCOUNT_ID=ca_gmail\nCOMBADGE_TIMEZONE=Europe/London\n"
    )
    with patch.dict(os.environ, {"COMPOSIO_API_KEY": "exported-key"}, clear=True):
        settings = load_settings(path)
        assert settings.composio_api_key == "exported-key"
        assert settings.composio_user_id == "wearer"
        assert settings.composio_accounts == {"gmail": "ca_gmail"}
        assert settings.timezone == "Europe/London"
        assert "exported-key" not in repr(settings)
        assert "ca_gmail" not in repr(settings)


def test_account_discovery_paginates_filters_user_and_never_returns_credentials():
    async def scenario():
        client = ComposioClient("key", "wearer")
        client._api = AsyncMock(
            side_effect=[
                {
                    "items": [account(user="someone-else"), account(app="browserbase")],
                    "next_cursor": "p2",
                },
                {"items": [account()]},
            ]
        )
        result = await client.connected_accounts()
        assert len(result) == 1 and result[0]["id"] == "ca_test"
        assert "must-not-leak" not in json.dumps(result)
        assert client._api.call_args.kwargs["query"]["user_ids"] == ["wearer"]
        assert client._api.call_args.kwargs["query"]["cursor"] == "p2"

    asyncio.run(scenario())


def test_ambiguous_inactive_disabled_and_wrong_user_accounts_are_not_selected():
    async def scenario():
        client = ComposioClient("key", "wearer")
        client._api = AsyncMock(return_value={"items": [account(), account(identity="ca_second")]})
        with pytest.raises(RuntimeError, match="Multiple"):
            await client.account("gmail")
        client.accounts["gmail"] = "ca_second"
        assert await client.account("gmail") == "ca_second"
        for candidate in (
            account(status="EXPIRED"),
            account(is_disabled=True),
            account(user="someone-else"),
        ):
            client.accounts.clear()
            client._api.return_value = {"items": [candidate]}
            with pytest.raises(RuntimeError, match="No active"):
                await client.account("gmail")

    asyncio.run(scenario())


def test_status_reports_missing_apps_without_hiding_other_connections():
    async def scenario():
        client = ComposioClient("key", "wearer")
        client._api = AsyncMock(return_value={"items": [account()]})
        status = await client.connection_status()
        assert set(status) == {"gmail", "googlecalendar"}
        assert status["gmail"]["status"] == "ACTIVE"
        assert status["googlecalendar"]["status"] == "unavailable"
        assert client._api.await_count == 1

    asyncio.run(scenario())


def test_discovery_restricts_actions_and_requires_schema_inspection():
    async def scenario():
        client = prepared_client()
        client._inspected.clear()
        client._api.return_value = {"items": [schema(), schema("GMAIL_DELETE_MESSAGE")]}
        result = await client.list_tools("gmail")
        assert [t["tool_slug"] for t in result["tools"]] == [SLUG]
        with pytest.raises(ValueError, match="Inspect"):
            await client.execute("run_connected_app_tool", send_args())
        result = await client.schema(SLUG)
        assert result["version"] == "20260915_00" and "input_parameters" in result
        with pytest.raises(ValueError):
            await client.schema("BROWSERBASE_MCP_ACT")
        with pytest.raises(ValueError):
            await client.list_tools("shopify")
        with pytest.raises(ValueError):
            await client.schema("ANDROID_TEXTER_SEND_MESSAGE")
        with pytest.raises(ValueError):
            await client.list_tools("android_texter")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "app,slug,arguments",
    [
        ("gmail", SLUG, {"recipient_email": "recipient@example.com", "body": "Test"}),
        (
            "googlecalendar",
            "GOOGLECALENDAR_CREATE_EVENT",
            {"calendar_id": "primary", "summary": "Demo", "start_datetime": "2026-09-20T14:00:00"},
        ),
    ],
)
def test_writes_bind_account_and_schema_version_and_deduplicate_within_request(
    app, slug, arguments
):
    async def scenario():
        client = prepared_client()
        client._remember_schema(schema(slug, app))
        await client.schema(slug)
        args = {"tool_slug": slug, "arguments_json": json.dumps(arguments)}
        first = await client.execute("run_connected_app_tool", args, "d1")
        second = await client.execute("run_connected_app_tool", args, "d1")
        assert first == second
        assert first["status"] == "ok" and first["data"]["status"] == "queued"
        assert client._api.await_count == 1
        payload = client._api.call_args.kwargs["payload"]
        assert payload["user_id"] == "wearer"
        assert payload["connected_account_id"] == "ca_test"
        assert payload["version"] == "20260915_00"
        assert payload["arguments"] == arguments
        client.account.assert_awaited_once_with(app)
        await client.execute("run_connected_app_tool", args, "new-user-request")
        assert client._api.await_count == 2

    asyncio.run(scenario())


def test_unknown_write_is_not_retried_even_in_another_delegation():
    async def scenario():
        client = prepared_client()
        client._api.side_effect = RuntimeError("Timeout")
        first = await client.execute("run_connected_app_tool", send_args(), "d1")
        second = await client.execute("run_connected_app_tool", send_args(), "d2")
        assert first["status"] == second["status"] == "unknown"
        assert client._api.await_count == 1

    asyncio.run(scenario())


def test_cancelled_write_remains_uncertain():
    async def scenario():
        client = prepared_client()
        client._api.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await client.execute("run_connected_app_tool", send_args(), "d1")
        result = await client.execute("run_connected_app_tool", send_args(), "d2")
        assert result["status"] == "unknown" and client._api.await_count == 1

    asyncio.run(scenario())


def test_application_failure_and_large_outputs_are_not_misreported():
    async def scenario():
        client = prepared_client({"successful": False, "error": "private-api-key"})
        result = await client.execute("run_connected_app_tool", send_args())
        assert result["status"] == "failed" and "private-api-key" not in json.dumps(result)
        client = prepared_client({"successful": True, "data": "x" * (MAX_RESULT_CHARS + 10)})
        result = await client.execute("run_connected_app_tool", send_args())
        assert result["truncated"] and len(result["data_excerpt"]) == MAX_RESULT_CHARS
        client = prepared_client({"data": {"id": "no-success-field"}})
        assert (await client.execute("run_connected_app_tool", send_args()))["status"] == "unknown"

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["[]", '{"n": NaN}', '{"n": Infinity}', "not json"])
def test_invalid_arguments_never_reach_execution(value):
    async def scenario():
        client = prepared_client()
        with pytest.raises(ValueError):
            await client.execute("run_connected_app_tool", {**send_args(), "arguments_json": value})
        client._api.assert_not_awaited()

    asyncio.run(scenario())


def test_rest_contract_and_redirects_do_not_expose_api_key():
    client = ComposioClient("private-api-key", "wearer")
    with patch("urllib.request.build_opener") as build:
        build.return_value.open.return_value.__enter__.return_value = io.BytesIO(b'{"items":[]}')
        assert client._request("GET", "/tools", query={"toolkit_versions": "latest"}) == {
            "items": []
        }
        req = build.return_value.open.call_args.args[0]
        assert req.full_url == API_URL + "/tools?toolkit_versions=latest"
        assert req.get_header("X-api-key") == "private-api-key"
        handler = build.call_args.args[0]()
        assert handler.redirect_request(req, None, 302, "", {}, "https://evil.invalid") is None
        build.return_value.open.side_effect = urllib.error.HTTPError(
            req.full_url, 401, "private-api-key", {}, io.BytesIO(b"private-api-key")
        )
        with pytest.raises(RuntimeError) as error:
            client._request("GET", "/tools")
        assert "private-api-key" not in str(error.value)


@pytest.mark.parametrize("snapshots,shopping,web,calls", list(product([False, True], repeat=4)))
def test_app_tools_are_additive_for_every_existing_feature_combination(
    snapshots, shopping, web, calls
):
    options = dict(
        snapshots=snapshots, shopping=shopping, web=web, call_names=["Edmon"] if calls else None
    )
    before = session_config(Settings(), **options)["delegation"]["responses"]
    after = session_config(Settings(), composio=True, **options)["delegation"]["responses"]
    original = {t["name"]: t for t in before.get("tools", [])}
    combined = {t["name"]: t for t in after["tools"]}
    assert set(combined) == set(original) | COMPOSIO_TOOL_NAMES
    assert all(combined[name] == tool for name, tool in original.items())
    assert "no external action tools" not in after["instructions"]
    assert "no other action tools" not in after["instructions"]
    assert "Finish all requested app tasks before" in after["instructions"]
    assert "America/Toronto" in after["instructions"]


@pytest.mark.parametrize(
    "key,user,options,enabled",
    [
        ("private-key", "wearer", [], True),
        ("private-key", "wearer", ["--composio"], True),
        ("private-key", "wearer", ["--no-composio"], False),
        ("private-key", "wearer", ["--check"], False),
        ("", "", [], False),
        ("private-key", "", [], False),
        ("", "wearer", [], False),
    ],
)
def test_voice_enables_composio_when_configured_and_preserves_browserbase(
    tmp_path, capsys, key, user, options, enabled
):
    path = tmp_path / ".env"
    path.write_text(
        "OPENAI_API_KEY=test\nBROWSERBASE_API_KEY=test\n"
        f"COMPOSIO_API_KEY={key}\nCOMPOSIO_USER_ID={user}\n"
    )
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("combadge.live.connect_voice", new_callable=AsyncMock) as connect,
    ):
        assert main(["voice", "--env-file", str(path), *options]) == 0
    assert (connect.call_args.kwargs["web"] is not None) == ("--check" not in options)
    assert (connect.call_args.kwargs["composio"] is not None) == enabled
    output = capsys.readouterr().out
    assert ("Composio enabled (Gmail, Google Calendar)" in output) == enabled
    if not enabled and "--check" not in options:
        assert "Connected apps:" in output
    assert "private-key" not in output


def test_cli_requires_user_and_keeps_check_mode_without_app_actions(tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=test\nCOMPOSIO_API_KEY=private-key\n")
    with patch.dict(os.environ, {}, clear=True), pytest.raises(SystemExit):
        main(["voice", "--env-file", str(path), "--composio"])
    error = capsys.readouterr().err
    assert "COMPOSIO_USER_ID" in error and "private-key" not in error
    with pytest.raises(SystemExit):
        main(["voice", "--check", "--composio"])


def test_accounts_command_reveals_owner_when_configured_user_is_wrong(tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text("COMPOSIO_API_KEY=test\nCOMPOSIO_USER_ID=wrong-user\n")
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(ComposioClient, "_api", new_callable=AsyncMock) as api,
    ):
        api.return_value = {"items": [account()]}
        assert main(["composio", "accounts", "--env-file", str(path)]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output[0]["user_id"] == "wearer"
        assert "must-not-leak" not in json.dumps(output)
        assert "user_ids" not in api.call_args.kwargs["query"]
