import asyncio
import copy
import io
import json
import urllib.error
from unittest.mock import Mock, patch

import pytest

from commbadge.shopify import CatalogClient, ShoppingSession, https_url
from commbadge.vision import ImageInput

PROFILE = "https://example.com/ucp-agent.json"
PID = "gid://shopify/p/bottle"
VID = "gid://shopify/ProductVariant/123"


def product(amount=2300, *, currency="CAD", available=True, color="Blue"):
    return {
        "id": PID,
        "title": "Bottle",
        "options": [{"name": "Color", "values": [{"label": color, "available": available}]}],
        "variants": [
            {
                "id": VID,
                "title": "Bottle",
                "price": {"amount": amount, "currency": currency},
                "availability": {"available": available},
                "options": [{"name": "Color", "label": color}],
                "seller": {"id": "shop1", "name": "Bottle shop", "url": "https://shop.example.com"},
                "checkout_url": "https://shop.example.com/cart/123:1",
            }
        ],
    }


class Catalog:
    country = "CA"
    currency = "CAD"

    def __init__(self, products=None):
        self.products = products if products is not None else [product()]
        self.current = product()
        self.calls = []

    def filters(self):
        return {"available": True, "ships_to": {"country": "CA"}}

    async def call(self, name, request):
        self.calls.append((name, request))
        return copy.deepcopy(
            {"products": self.products} if name == "search_catalog" else {"product": self.current}
        )


def test_visual_search_forwards_image_and_excludes_wrong_currency_unavailable_over_budget():
    async def scenario():
        entries = [
            product(),
            product(1, currency="USD"),
            product(2, available=False),
            product(9999),
        ]
        entries[-1]["variants"][0]["id"] = "gid://shopify/ProductVariant/456"
        catalog = Catalog(entries)
        shopping = ShoppingSession(catalog, report=lambda _: None)
        with pytest.raises(ValueError, match="Capture an image first"):
            await shopping.search("bottle", True, 3000)
        shopping.image = ImageInput.from_bytes(b"\x89PNG\r\n\x1a\nimage")
        result = await shopping.search("blue bottle", True, 3000)
        assert [offer["price_minor"] for offer in result["offers"]] == [2300]
        request = catalog.calls[0][1]
        assert request["like"][0]["image"]["content_type"] == "image/png"
        assert request["filters"]["price"] == {"max": 3000}
        assert request["filters"]["ships_to"] == {"country": "CA"}
        await shopping.search("cheaper blue bottle", True, 2500)
        assert catalog.calls[1][1]["like"] == request["like"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "query,use_image,budget",
    [("", False, None), ("bottle", "yes", None), ("bottle", False, -1), ("bottle", False, True)],
)
def test_bad_search_arguments_do_not_call_catalog(query, use_image, budget):
    catalog = Catalog()
    with pytest.raises(ValueError):
        asyncio.run(ShoppingSession(catalog).search(query, use_image, budget))
    assert not catalog.calls


def test_unknown_ids_and_unavailable_color_never_open_checkout():
    async def scenario():
        catalog, opener = Catalog(), Mock(return_value=True)
        shopping = ShoppingSession(
            catalog, open_checkout=True, opener=opener, report=lambda _: None
        )
        with pytest.raises(ValueError):
            await shopping.checkout(VID)
        with pytest.raises(ValueError):
            await shopping.product("https://example.com/invented", [])
        assert not catalog.calls
        await shopping.search("bottle", False, None)
        result = await shopping.product(PID, [{"name": "Color", "label": "Red"}])
        assert result["status"] == "unavailable"
        assert not result["offers"]
        opener.assert_not_called()

    asyncio.run(scenario())


def test_checkout_rechecks_price_and_requires_another_request_after_change():
    async def scenario():
        catalog, opener = Catalog(), Mock(return_value=True)
        shopping = ShoppingSession(
            catalog, open_checkout=True, opener=opener, report=lambda _: None
        )
        await shopping.search("bottle", False, None)
        catalog.current = product(2900)
        result = await shopping.checkout(VID)
        assert result["status"] == "offer_changed"
        opener.assert_not_called()
        result = await shopping.checkout(VID)
        assert result["status"] == "checkout_opened"
        opener.assert_called_once_with("https://shop.example.com/cart/123:1")
        assert "No purchase" in result["message"]
        assert [call[0] for call in catalog.calls] == [
            "search_catalog",
            "get_product",
            "get_product",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    ["unavailable", "wrong_variant", "wrong_currency", "malicious_url", "other_seller_url"],
)
def test_checkout_does_not_open_stale_or_untrusted_links(change):
    async def scenario():
        catalog, opener = Catalog(), Mock(return_value=True)
        shopping = ShoppingSession(
            catalog, open_checkout=True, opener=opener, report=lambda _: None
        )
        await shopping.search("bottle", False, None)
        variant = catalog.current["variants"][0]
        if change == "unavailable":
            variant["availability"]["available"] = False
        elif change == "wrong_variant":
            variant["id"] = "gid://shopify/ProductVariant/456"
        elif change == "wrong_currency":
            variant["price"]["currency"] = "USD"
        elif change == "malicious_url":
            variant["checkout_url"] = "file:///etc/passwd"
        else:
            variant["checkout_url"] = "https://attacker.example/cart/123:1"
        assert (await shopping.checkout(VID))["status"] == "unavailable"
        opener.assert_not_called()

    asyncio.run(scenario())


def test_link_handoff_and_empty_results():
    async def scenario():
        catalog, opener = Catalog(), Mock(return_value=True)
        shopping = ShoppingSession(catalog, opener=opener, report=lambda _: None)
        await shopping.search("bottle", False, None)
        result = await shopping.checkout(VID)
        assert result["status"] == "checkout_link_ready"
        assert result["checkout_url"].startswith("https://shop.example.com/")
        opener.assert_not_called()
        catalog.products = []
        result = await shopping.search("missing", False, None)
        assert result["status"] == "no_matches" and result["offers"] == []

    asyncio.run(scenario())


def test_relevant_complete_item_is_not_dropped_for_cheap_accessories():
    async def scenario():
        complete = product(6500)
        entries = [complete]
        for i in range(12):
            accessory = product(1000 + i)
            accessory["id"] = f"gid://shopify/p/lid{i}"
            accessory["variants"][0]["id"] = f"gid://shopify/ProductVariant/{1000 + i}"
            entries.append(accessory)
        shopping = ShoppingSession(Catalog(entries), report=lambda _: None)
        result = await shopping.search("complete bottle", False, None)
        assert result["offers"][0]["variant_id"] == VID
        assert len(result["offers"]) == 13

    asyncio.run(scenario())


def test_malformed_product_becomes_tool_error():
    shopping = ShoppingSession(Catalog([{"variants": None}]), report=lambda _: None)
    with pytest.raises(RuntimeError, match="malformed product"):
        asyncio.run(shopping.search("bottle", False, None))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://127.0.0.1/cart",
        "https://host.local/x",
        "file:///x",
        "https://user:pass@example.com/x",
        "https://example.com:99/x",
    ],
)
def test_rejects_nonpublic_urls(url):
    with pytest.raises(ValueError):
        https_url(url)


def test_catalog_wire_payload_and_response():
    response = io.BytesIO(json.dumps({"result": {"structuredContent": {"products": []}}}).encode())
    with patch("urllib.request.urlopen", return_value=response) as send:
        data = asyncio.run(CatalogClient(PROFILE).call("search_catalog", {"query": "bottle"}))
    assert data == {"products": []}
    payload = json.loads(send.call_args.args[0].data)
    assert payload["params"]["arguments"]["meta"]["ucp-agent"]["profile"] == PROFILE
    assert payload["params"]["arguments"]["catalog"]["context"] == {
        "address_country": "CA",
        "currency": "CAD",
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'{"error":{"message":"secret"}}',
        b'{"result":{"isError":true}}',
        b'{"result":{"structuredContent":[]}}',
    ],
)
def test_bad_catalog_response_is_an_actionable_error(raw):
    with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
        with pytest.raises(RuntimeError, match="invalid or unsuccessful"):
            asyncio.run(CatalogClient(PROFILE).call("search_catalog", {}))


def test_catalog_http_failure_does_not_leak_response_body():
    error = urllib.error.HTTPError("https://example.com", 429, "limit", {}, io.BytesIO(b"secret"))
    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(RuntimeError, match="HTTP 429") as caught,
    ):
        asyncio.run(CatalogClient(PROFILE).call("search_catalog", {}))
    assert "secret" not in str(caught.value)
