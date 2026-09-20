import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from combadge.composio import ComposioClient
from combadge.config import Settings, load_settings
from combadge.live import session_config
from combadge.merchant import (
    GRAPHQL_TOOL,
    MAX_READS,
    MERCHANT_TOOL_NAMES,
    SEARCH_QUERY,
    MerchantInventory,
)

DOMAIN = "418-teapot-shop.myshopify.com"


def setup(data=None, **extra):
    client = ComposioClient("secret-key", "wearer", {"shopify": "ca_store"})
    client.account = AsyncMock(return_value="ca_store")
    if data is None:
        data = {
            "data": {
                "shop": {"name": "Teapot Shop", "myshopifyDomain": DOMAIN},
                "productVariants": {
                    "nodes": [],
                    "pageInfo": {
                        "hasNextPage": True,
                        "endCursor": "next-page",
                    },
                },
            }
        }
    client._api = AsyncMock(
        side_effect=[
            {"slug": GRAPHQL_TOOL, "toolkit": {"slug": "shopify"}, "version": "20260916_00"},
            {"successful": True, "data": data, "log_id": "log_stock", **extra},
        ]
    )
    return client, MerchantInventory(client, DOMAIN)


def test_search_binds_owner_account_and_version_and_preserves_pagination():
    async def scenario():
        client, merchant = setup()
        query = 'teapot" } mutation { productDelete(id: "1") }'
        result = await merchant.execute(
            "search_merchant_products",
            {
                "query": query,
                "after": None,
            },
        )
        payload = client._api.call_args.kwargs["payload"]
        assert payload["user_id"] == "wearer"
        assert payload["connected_account_id"] == "ca_store"
        assert payload["version"] == "20260916_00"
        assert payload["arguments"] == {
            "query": SEARCH_QUERY,
            "variables": {"query": query, "after": None},
        }
        assert result["status"] == "ok" and result["log_id"] == "log_stock"
        assert result["data"]["productVariants"]["pageInfo"]["hasNextPage"] is True
        assert result["checked_at"]
        client.account.assert_awaited_once_with("shopify")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data,extra",
    [
        ({"errors": [{"message": "Access denied"}], "data": {"shop": {}}}, {}),
        ("invalid-json", {}),
        ({}, {"successful": False}),
        ({}, {"successful": None}),
    ],
)
def test_http_success_does_not_hide_provider_or_graphql_failure(data, extra):
    _, merchant = setup(data, **extra)
    result = asyncio.run(merchant.execute("search_merchant_products", {"query": "", "after": None}))
    assert result["status"] == "failed"
    assert result["log_id"] == "log_stock"
    assert "data" not in result


def test_wrong_store_cannot_return_stock():
    _, merchant = setup({"data": {"shop": {"myshopifyDomain": "other.myshopify.com"}}})
    with pytest.raises(RuntimeError, match="does not match"):
        asyncio.run(merchant.execute("search_merchant_products", {"query": "", "after": None}))


def test_untracked_inventory_and_location_pagination_remain_explicit():
    variant = {
        "id": "gid://shopify/ProductVariant/42",
        "inventoryItem": {
            "tracked": False,
            "inventoryLevels": {
                "nodes": [],
                "pageInfo": {
                    "hasNextPage": False,
                    "endCursor": None,
                },
            },
        },
    }
    client, merchant = setup(
        json.dumps(
            {
                "data": {
                    "shop": {"myshopifyDomain": DOMAIN},
                    "productVariant": variant,
                }
            }
        )
    )
    result = asyncio.run(
        merchant.execute(
            "get_merchant_stock",
            {
                "variant_id": variant["id"],
                "after": "cursor",
            },
        )
    )
    assert result["data"]["productVariant"] == variant
    assert client._api.call_args.kwargs["payload"]["arguments"]["variables"] == {
        "id": variant["id"],
        "after": "cursor",
    }


@pytest.mark.parametrize(
    "name,args",
    [
        (GRAPHQL_TOOL, {"query": "mutation { bad }"}),
        ("search_merchant_products", {"query": "tea", "after": None, "account": "other"}),
        ("search_merchant_products", {"query": "x" * 301, "after": None}),
        ("search_merchant_products", {"query": "tea", "after": {}}),
        ("get_merchant_stock", {"variant_id": "gid://shopify/Product/42", "after": None}),
    ],
)
def test_invalid_tool_arguments_never_execute(name, args):
    client, merchant = setup()
    with pytest.raises(ValueError):
        asyncio.run(merchant.execute(name, args))
    client._api.assert_not_called()


def test_merchant_uses_existing_owner_selection_and_read_budget():
    client, merchant = setup()
    client.user_id = ""
    with pytest.raises(ValueError, match="COMPOSIO_USER_ID"):
        asyncio.run(merchant.execute("search_merchant_products", {"query": "", "after": None}))
    client.user_id = "wearer"
    merchant._reads["turn"] = MAX_READS
    with pytest.raises(RuntimeError, match="limit"):
        asyncio.run(
            merchant.execute("search_merchant_products", {"query": "", "after": None}, "turn")
        )
    client._api.assert_not_called()


def test_general_composio_runner_cannot_inspect_arbitrary_shopify_graphql():
    client, _ = setup()
    with pytest.raises(ValueError, match="not enabled"):
        asyncio.run(client.schema(GRAPHQL_TOOL))


def test_merchant_tools_only_registered_with_configured_domain_and_composio():
    def names(settings, enabled):
        config = session_config(settings, composio=enabled)
        return {t["name"] for t in config["delegation"]["responses"].get("tools", [])}

    configured = Settings(shopify_merchant_domain=DOMAIN)
    assert MERCHANT_TOOL_NAMES <= names(configured, True)
    assert not MERCHANT_TOOL_NAMES & names(configured, False)
    assert not MERCHANT_TOOL_NAMES & names(Settings(), True)


def test_merchant_configuration(tmp_path, monkeypatch):
    for key in ("SHOPIFY_MERCHANT_DOMAIN", "COMPOSIO_SHOPIFY_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"SHOPIFY_MERCHANT_DOMAIN={DOMAIN}\nCOMPOSIO_SHOPIFY_ACCOUNT_ID=ca_store\n")
    settings = load_settings(env)
    assert settings.shopify_merchant_domain == DOMAIN
    assert settings.composio_accounts["shopify"] == "ca_store"
    with pytest.raises(ValueError, match="hostname"):
        MerchantInventory(None, "https://" + DOMAIN)
