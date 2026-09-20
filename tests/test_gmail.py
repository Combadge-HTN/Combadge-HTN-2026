import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest

from combadge.composio import MAX_RESULT_CHARS, ComposioClient
from combadge.gmail import BODY_PAGE_CHARS, message_page


def part(text, mime="text/plain", **extra):
    return {
        "mimeType": mime,
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")},
        **extra,
    }


def message(identity="m1", labels=None, **extra):
    return {
        "messageId": identity,
        "threadId": "thread1",
        "labelIds": ["INBOX", "UNREAD"] if labels is None else labels,
        "subject": "A generic subject",
        "preview": {"body": "A misleading preview"},
        "payload": part("The actual appointment is at 3 PM, not 2 PM."),
        **extra,
    }


def client_with(*responses):
    client = ComposioClient("secret-api-key", "owner")
    client.account = AsyncMock(return_value="ca_gmail")
    client._api = AsyncMock(side_effect=[{"successful": True, "data": r} for r in responses])
    for slug in ("GMAIL_FETCH_EMAILS", "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"):
        client._remember_schema(
            {
                "slug": slug,
                "toolkit": {"slug": "gmail"},
                "version": "20260915_00",
                "input_parameters": {"type": "object", "properties": {}},
            }
        )
    return client


def search_args(read_status="unread", **extra):
    return {"query": "", "read_status": read_status, "limit": 3, "page_token": None, **extra}


@pytest.mark.parametrize(
    "labels,expected", [(["UNREAD"], "unread"), ([], "read"), (["INBOX"], "read")]
)
def test_read_status_comes_from_message_labels(labels, expected):
    result = message_page(message(labels=labels), "m1")
    assert result["read_status"] == expected
    assert result["is_unread"] == (expected == "unread")
    assert "3 PM" in result["body"]
    assert "misleading preview" not in result["body"]


def test_missing_labels_and_missing_body_are_explicit_not_inferred_from_preview():
    data = message(payload=None, messageText=None)
    del data["labelIds"]
    result = message_page(data, "m1")
    assert result["read_status"] == "unknown" and result["is_unread"] is None
    assert result["body_available"] is False and result["body"] == ""


def test_nested_mime_prefers_plain_alternative_and_skips_text_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [part("<p>Duplicate HTML</p>", "text/html"), part("Hello Montréal!")],
            },
            part("A separate inline paragraph."),
            part("Secret attachment", filename="notes.txt"),
        ],
    }
    result = message_page(message(payload=payload), "m1")
    assert result["body"] == "Hello Montréal!\n\nA separate inline paragraph."
    assert result["body_available"] and not result["body_truncated"]


def test_html_only_message_is_readable_and_does_not_include_scripts_or_styles():
    payload = part(
        "<html><head><style>.hidden{color:red}</style></head><body><p>Meet at "
        "<b>3 PM</b> &amp; bring ID.</p><script>tracking()</script><p>Room 4</p></body></html>",
        "text/html",
    )
    result = message_page(message(payload=payload), "m1")
    assert result["body"] == "Meet at 3 PM & bring ID.\n\nRoom 4"


def test_charset_and_provider_message_text_fallback():
    payload = part("")
    payload["body"]["data"] = base64.urlsafe_b64encode("café".encode("latin-1")).decode()
    payload["headers"] = [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}]
    assert message_page(message(payload=payload), "m1")["body"] == "café"
    fallback = message_page(message(payload=None, messageText="Actual full body"), "m1")
    assert fallback["body"] == "Actual full body" and fallback["body_source"] == "messageText"


def test_bad_encoding_or_missing_body_part_is_not_presented_as_complete():
    payload = {"mimeType": "text/plain", "body": {"data": "!!!!"}}
    result = message_page(message(payload=payload), "m1")
    assert not result["body_available"] and result["body_incomplete"]
    payload["body"] = {"attachmentId": "external-body", "size": 100}
    assert message_page(message(payload=payload), "m1")["body_incomplete"]


def test_unread_search_filters_and_opens_actual_messages_not_list_previews():
    async def scenario():
        client = client_with(
            {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "page2"},
            message(),
            message("m2", labels=["INBOX"]),  # Became read after list was fetched.
        )
        result = await client.execute(
            "search_gmail_messages",
            search_args(query="from:a@example.com OR from:b@example.com"),
            "d1",
        )
        assert result["status"] == "partial" and result["returned_count"] == 1
        assert result["messages"][0]["read_status"] == "unread"
        assert "3 PM" in result["messages"][0]["body"]
        assert result["next_page_token"] == "page2"
        calls = client._api.call_args_list
        args = calls[0].kwargs["payload"]["arguments"]
        assert args["query"] == "(from:a@example.com OR from:b@example.com) is:unread"
        assert args["label_ids"] == ["UNREAD"] and args["ids_only"] is True
        assert args["max_results"] == 3
        for call in calls[1:]:
            assert call.args == ("POST", "/tools/execute/GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID")
            payload = call.kwargs["payload"]
            assert payload["arguments"]["format"] == "full"
            assert payload["connected_account_id"] == "ca_gmail"
            assert payload["user_id"] == "owner"
        assert client._executions["d1"] == 3

    asyncio.run(scenario())


def test_read_search_keeps_pagination_and_does_not_mix_in_unread_mail():
    async def scenario():
        client = client_with({"messages": [{"messageId": "m1"}]}, message(labels=["INBOX"]))
        result = await client.execute(
            "search_gmail_messages", search_args("read", page_token="page2"), "d1"
        )
        assert result["messages"][0]["is_unread"] is False
        args = client._api.call_args_list[0].kwargs["payload"]["arguments"]
        assert args["query"] == "is:read" and args["page_token"] == "page2"
        assert "label_ids" not in args

    asyncio.run(scenario())


def test_long_message_is_decoded_before_bounding_and_can_be_read_to_the_end():
    async def scenario():
        body = "Body content. " * 3000 + "Last paragraph matters."
        client = client_with(message(payload=part(body)), message(payload=part(body)))
        result = await client.execute("read_gmail_message", {"message_id": "m1", "offset": 0})
        first = result["data"]
        assert result["status"] == "ok" and not result["truncated"]
        assert first["body"] == body[:BODY_PAGE_CHARS]
        assert first["next_body_offset"] == BODY_PAGE_CHARS and first["body_truncated"]
        assert len(json.dumps(result)) < MAX_RESULT_CHARS
        result = await client.execute(
            "read_gmail_message", {"message_id": "m1", "offset": len(body) - 30}
        )
        assert result["data"]["body"].endswith("Last paragraph matters.")
        assert result["data"]["next_body_offset"] is None
        assert not result["data"]["body_truncated"]

    asyncio.run(scenario())


def test_partial_fetch_failure_keeps_other_messages_and_never_claims_empty_mailbox():
    async def scenario():
        client = client_with({"messages": [{"id": "m1"}, {"id": "m2"}]}, message())
        client._api.side_effect = [
            {"successful": True, "data": {"messages": [{"id": "m1"}, {"id": "m2"}]}},
            {"successful": False, "error": "permission denied"},
            {"successful": True, "data": message("m2")},
        ]
        result = await client.execute("search_gmail_messages", search_args())
        assert result["status"] == "partial" and result["returned_count"] == 1
        assert result["issues"] and result["messages"][0]["message_id"] == "m2"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "extra", [{"limit": 0}, {"limit": 6}, {"limit": True}, {"read_status": "new"}]
)
def test_invalid_search_does_not_reach_composio(extra):
    client = client_with()
    with pytest.raises(ValueError):
        asyncio.run(client.execute("search_gmail_messages", search_args(**extra)))
    client._api.assert_not_awaited()


def test_read_helpers_require_owner_and_do_not_leak_api_key_in_body():
    client = client_with(message(payload=part("The text mentions secret-api-key.")))
    result = asyncio.run(client.execute("read_gmail_message", {"message_id": "m1", "offset": 0}))
    assert "secret-api-key" not in json.dumps(result)
    client.user_id = ""
    with pytest.raises(ValueError, match="COMPOSIO_USER_ID"):
        asyncio.run(client.execute("search_gmail_messages", search_args()))
