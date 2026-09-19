import asyncio
import copy
import json
from unittest.mock import Mock, patch

import pytest
from test_shopify import VID, Catalog, product

from combadge.cli import main
from combadge.config import Settings
from combadge.live import session_config
from combadge.shop_account import ShopAccount, ShopError, TokenStore, checkout_summary
from combadge.shopify import ShoppingSession


def envelope():
    return {
        "result": {
            "structuredContent": {
                "id": "checkout-1",
                "status": "requires_escalation",
                "currency": "CAD",
                "buyer": {"email": "private@example.com"},
                "payment": {"instruments": [{"secret": "private-payment"}]},
                "line_items": [{"quantity": 1, "item": {"id": VID}}],
                "messages": [{"code": "extension_interaction_required"}],
                "totals": [
                    {"type": "subtotal", "amount": 900},
                    {"type": "fulfillment", "amount": 28300},
                    {"type": "tax", "amount": 1460},
                    {"type": "total", "amount": 30660},
                ],
            }
        }
    }


def test_escalation_is_prepared_with_totals_but_without_personal_information():
    data = envelope()
    data["result"]["isError"] = True
    checkout = data["result"]["structuredContent"]
    checkout["ucp"] = {"status": "success"}
    checkout["messages"] = [{"type": "error", "code": "extension_interaction_required"}]
    result = checkout_summary(data, VID, 1)
    assert result["status"] == "checkout_prepared"
    assert result["totals_minor"]["total"] == 30660
    assert result["shipping_exceeds_items"] and result["review_required"]
    assert "private" not in json.dumps(result)
    assert "No payment" in result["message"]


@pytest.mark.parametrize("change", ["variant", "quantity", "status", "malformed", "rpc_error"])
def test_invalid_checkout_cannot_be_reported_as_saved(change):
    data = envelope()
    checkout = data["result"]["structuredContent"]
    if change == "variant":
        checkout["line_items"][0]["item"]["id"] = "gid://shopify/ProductVariant/99"
    elif change == "quantity":
        checkout["line_items"][0]["quantity"] = 2
    elif change == "status":
        checkout["status"] = "completed"
    elif change == "rpc_error":
        data["result"]["isError"] = True
    else:
        checkout["totals"] = None
    with pytest.raises(ShopError):
        checkout_summary(data, VID, 1)


def test_private_store_and_refresh_preserve_rotated_credentials(tmp_path):
    store = TokenStore(tmp_path / "private/auth.json")
    store.write({"access_token": "old", "refresh_token": "refresh"})
    assert store.path.stat().st_mode & 0o777 == 0o600
    account = ShopAccount(store)
    with patch(
        "combadge.shop_account.request",
        side_effect=[
            ShopError("expired", status=401),
            {"access_token": "new", "refresh_token": "rotated"},
        ],
    ) as send:
        assert account.access_token() == "new"
    assert send.call_args.kwargs["form"]["grant_type"] == "refresh_token"
    assert store.read()["refresh_token"] == "rotated"
    store.path.chmod(0o644)
    with pytest.raises(ShopError, match="owner-only"):
        store.read()


def test_transient_auth_failure_does_not_refresh_or_delete_tokens(tmp_path):
    store = TokenStore(tmp_path / "auth.json")
    saved = {"access_token": "old", "refresh_token": "refresh"}
    store.write(saved)
    with patch("combadge.shop_account.request", side_effect=ShopError("offline")) as send:
        with pytest.raises(ShopError, match="offline"):
            ShopAccount(store).access_token()
    assert send.call_count == 1 and store.read() == saved


def test_prepare_wire_only_creates_unpaid_checkout_with_merchant_scoped_token(tmp_path):
    account = ShopAccount(TokenStore(tmp_path / "auth.json"))
    with (
        patch.object(account, "access_token", return_value="account-token"),
        patch(
            "combadge.shop_account.request",
            side_effect=[
                {"access_token": "merchant-token"},
                {"ip": "203.0.113.10"},
                envelope(),
            ],
        ) as send,
    ):
        result = account.prepare("bottles.myshopify.com", VID, 1, "CA")
    assert result["status"] == "checkout_prepared"
    exchange, ip, create = send.call_args_list
    assert exchange.kwargs["form"]["resource"] == "https://bottles.myshopify.com/"
    assert exchange.kwargs["form"]["subject_token"] == "account-token"
    assert create.args[0] == "https://bottles.myshopify.com/api/ucp/mcp"
    assert create.kwargs["token"] == "merchant-token"
    assert create.kwargs["headers"]["Shopify-Buyer-Ip"] == "203.0.113.10"
    params = create.kwargs["payload"]["params"]
    assert params["name"] == "create_checkout"
    assert params["arguments"]["checkout"]["line_items"] == [{"quantity": 1, "item": {"id": VID}}]


@pytest.mark.parametrize(
    "domain",
    [
        "localhost",
        "evil.com",
        "shop.myshopify.com/evil",
        "x.myshopify.com@evil.com",
        "x.myshopify.com:443",
    ],
)
def test_invalid_merchant_never_receives_credentials(domain):
    with patch("combadge.shop_account.request") as send, pytest.raises(ValueError):
        ShopAccount().prepare(domain, VID, 1, "CA")
    send.assert_not_called()


def test_ambiguous_create_failure_is_not_retried(tmp_path):
    account = ShopAccount(TokenStore(tmp_path / "auth.json"))
    with (
        patch.object(account, "access_token", return_value="account-token"),
        patch(
            "combadge.shop_account.request",
            side_effect=[
                {"access_token": "merchant-token"},
                {"ip": "203.0.113.10"},
                ShopError("timeout"),
            ],
        ) as send,
        pytest.raises(ShopError, match="Check before trying again"),
    ):
        account.prepare("bottles.myshopify.com", VID, 1, "CA")
    assert send.call_count == 3


def test_sign_in_obeys_polling_and_stores_only_tokens(tmp_path):
    account = ShopAccount(TokenStore(tmp_path / "auth.json"))
    with (
        patch("combadge.shop_account.time.sleep") as sleep,
        patch(
            "combadge.shop_account.request",
            side_effect=[
                {
                    "verification_uri_complete": "https://accounts.shop.app/oauth/device_code?code=test",
                    "device_code": "device",
                    "expires_in": 600,
                    "interval": 5,
                },
                ShopError("pending", code="authorization_pending"),
                ShopError("slow", code="slow_down"),
                {"access_token": "access", "refresh_token": "refresh", "id_token": "private"},
            ],
        ),
    ):
        account.login(report=lambda _: None)
    assert [call.args[0] for call in sleep.call_args_list] == [5, 5, 10]
    assert account.store.read() == {"access_token": "access", "refresh_token": "refresh"}


def test_changed_price_and_unknown_selection_do_not_create_checkout():
    async def scenario():
        initial = product()
        initial["variants"][0]["seller"]["domain"] = "bottles.myshopify.com"
        catalog = Catalog([initial])
        catalog.current = copy.deepcopy(initial)
        account = Mock()
        account.prepare.return_value = {"status": "checkout_prepared"}
        shopping = ShoppingSession(catalog, account=account, report=lambda _: None)
        with pytest.raises(ValueError):
            await shopping.save(VID, 1)
        await shopping.search("bottle", False, None)
        catalog.current["variants"][0]["price"]["amount"] = 2500
        assert (await shopping.save(VID, 1))["status"] == "offer_changed"
        account.prepare.assert_not_called()
        assert (await shopping.save(VID, 1))["status"] == "checkout_prepared"
        account.prepare.assert_called_once_with("bottles.myshopify.com", VID, 1, "CA")

    asyncio.run(scenario())


def test_account_session_registers_save_and_no_browser_checkout():
    config = session_config(Settings(), shopping=True, shop_account=True)
    backend = config["delegation"]["responses"]
    names = [tool["name"] for tool in backend["tools"]]
    assert "save_shopify_item" in names
    assert "open_shopify_checkout" not in names
    assert "complete_checkout" not in names
    assert "Shop app" in backend["instructions"]
    assert "open_shopify_checkout" not in backend["instructions"]


@pytest.mark.parametrize(
    "args", [["--shop-account"], ["--shopify", "--shop-account", "--open-checkout"]]
)
def test_account_cli_rejects_incompatible_options(args):
    with pytest.raises(SystemExit) as error:
        main(["voice", *args])
    assert error.value.code == 2


def test_delegation_returns_saved_checkout_to_voice_and_rejects_browser_tool():
    from combadge.delegation import FunctionCall, SnapshotDelegation

    async def scenario():
        completed = asyncio.Event()
        output = []

        class Connection:
            async def send(self, message):
                output.append(message)
                if message["type"] == "response.create":
                    completed.set()

        from unittest.mock import AsyncMock

        shopping = Mock(account=object())
        shopping.save = AsyncMock(return_value={"status": "checkout_prepared"})
        worker = SnapshotDelegation(Connection(), None, lambda _: None, shopping=shopping)
        worker.queue.put_nowait(
            [
                FunctionCall(
                    "one", "save_shopify_item", json.dumps({"variant_id": VID, "quantity": 1})
                ),
                FunctionCall("two", "open_shopify_checkout", json.dumps({"variant_id": VID})),
            ]
        )
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(completed.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        shopping.save.assert_awaited_once_with(variant_id=VID, quantity=1)
        shopping.checkout.assert_not_called()
        results = [json.loads(m["item"]["output"]) for m in output if "item" in m]
        assert [r["status"] for r in results] == ["checkout_prepared", "failed"]

    asyncio.run(scenario())


def test_delivery_errors_are_not_mistaken_for_interactive_review():
    data = envelope()
    data["result"]["structuredContent"]["messages"].append(
        {"type": "error", "code": "delivery_no_delivery_available"}
    )
    with pytest.raises(ShopError):
        checkout_summary(data, VID, 1)


def test_checkout_trace_retains_retrieval_evidence_without_buyer_or_payment_data(tmp_path):
    account = ShopAccount(TokenStore(tmp_path / "auth.json"))
    response = envelope()
    response["result"]["structuredContent"]["continue_url"] = (
        "https://bottles.myshopify.com/checkout/private-handoff"
    )
    with (
        patch.object(account, "_merchant_access", return_value=("private-token", "203.0.113.10")),
        patch("combadge.shop_account.request", return_value=response),
    ):
        result = account.prepare("bottles.myshopify.com", VID, 1, "CA")
    assert result["app_visibility"] == "unverified"
    assert "NOT been verified" in result["message"]
    path = account.trace_dir / f"{result['trace_id']}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    saved = json.loads(path.read_text())
    assert saved["response"]["checkout_id"] == "checkout-1"
    assert saved["response"]["continue_url"].endswith("private-handoff")
    assert saved["response"]["checkout_status"] == "requires_escalation"
    assert saved["response"]["totals_minor"]["total"] == 30660
    assert saved["response"]["line_items"] == [{"variant_id": VID, "quantity": 1}]
    for secret in ("private@example.com", "private-payment", "private-token"):
        assert secret not in path.read_text()
    public = json.dumps(account.traces())
    assert "private-handoff" not in public and '"checkout_id"' not in public


def test_trace_records_uncertain_outcome_and_does_not_repeat_create(tmp_path):
    account = ShopAccount(TokenStore(tmp_path / "auth.json"))
    with (
        patch.object(account, "_merchant_access", return_value=("token", "203.0.113.10")),
        patch("combadge.shop_account.request", side_effect=ShopError("timeout")) as send,
    ):
        with pytest.raises(ShopError, match="Trace:"):
            account.prepare("bottles.myshopify.com", VID, 1, "CA")
    assert send.call_count == 1
    trace = account.traces()[0]
    assert trace["stage"] == "failed"
    assert trace["failed_stage"] == "requesting_checkout"
    assert trace["app_visibility"] == "unverified"


def test_trace_records_merchant_errors_even_when_summary_rejects_response(tmp_path):
    account = ShopAccount(TokenStore(tmp_path / "auth.json"))
    response = envelope()
    response["result"]["structuredContent"]["messages"] = [
        {"type": "error", "code": "delivery_no_delivery_available"}
    ]
    with (
        patch.object(account, "_merchant_access", return_value=("token", "203.0.113.10")),
        patch("combadge.shop_account.request", return_value=response),
    ):
        with pytest.raises(ShopError):
            account.prepare("bottles.myshopify.com", VID, 1, "CA")
    trace = account.traces()[0]
    assert trace["response"]["message_codes"] == ["delivery_no_delivery_available"]
    assert trace["failed_stage"] == "response_received"


def test_refresh_reads_checkout_without_creating_one_and_preserves_original_response(tmp_path):
    account = ShopAccount(TokenStore(tmp_path / "auth.json"))
    trace_id = "a" * 32
    account._write_trace(
        {
            "trace_id": trace_id,
            "merchant": "bottles.myshopify.com",
            "response": {"checkout_id": "checkout-1", "marker": "original"},
        }
    )
    with (
        patch.object(account, "_merchant_access", return_value=("token", "203.0.113.10")),
        patch("combadge.shop_account.request", return_value=envelope()) as send,
    ):
        refreshed = account.traces(trace_id, refresh=True)[0]
    params = send.call_args.kwargs["payload"]["params"]
    assert params["name"] == "get_checkout" and params["arguments"]["id"] == "checkout-1"
    assert refreshed["response"]["marker"] == "original"
    assert refreshed["last_read"]["checkout_status"] == "requires_escalation"
    assert "checkout_id" not in refreshed["last_read"]


def test_trace_cli_is_offline_unless_refresh_requested(tmp_path, capsys):
    with patch("combadge.shop_account.request") as send:
        assert main(["shop-account", "trace", "--auth-file", str(tmp_path / "auth.json")]) == 0
    send.assert_not_called()
    assert json.loads(capsys.readouterr().out) == []


def test_voice_never_treats_checkout_creation_as_confirmed_phone_visibility():
    config = session_config(Settings(), shopping=True, shop_account=True)
    assert "does NOT" in config["instructions"]
    assert "app_visibility=unverified" in config["delegation"]["responses"]["instructions"]
