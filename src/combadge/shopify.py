"""Shopify catalog discovery and checkout handoff; no payment or order mutations."""

import asyncio
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable

from combadge.shop_account import DOMAIN, ShopAccount
from combadge.vision import ImageInput

CATALOG_URL = "https://catalog.shopify.com/api/ucp/mcp"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PRODUCT_ID = re.compile(r"gid://shopify/(?:p/[A-Za-z0-9_-]+|ProductVariant/[0-9]+)\Z")
PRICE_NOTE = "Item prices only; shipping and tax are not included. Lowest among returned offers."


def https_url(value: str) -> str:
    """Reject local destinations and executable URL schemes before a browser handoff."""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid HTTPS URL.")
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or "." not in host
        or host.endswith((".local", ".localhost", ".internal"))
        or "\\" in value
    ):
        raise ValueError("A public HTTPS URL is required.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("A public HTTPS URL is required.")
    return value


class CatalogClient:
    """Small JSON-RPC client using Python's standard library, including on QNX."""

    def __init__(self, profile_url: str, *, country: str = "CA", currency: str = "CAD"):
        if not profile_url:
            raise ValueError("Set SHOPIFY_AGENT_PROFILE_URL to your hosted docs/ucp-agent.json.")
        self.profile_url = https_url(profile_url)
        self.country = country.upper()
        self.currency = currency.upper()
        if self.country not in ("CA", "US") or self.currency not in ("CAD", "USD"):
            raise ValueError("Shopify currently supports CA/US destinations and CAD/USD prices.")

    async def call(self, name: str, catalog: dict) -> dict:
        return await asyncio.to_thread(self._call, name, catalog)

    def _call(self, name: str, catalog: dict) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {
                    "meta": {"ucp-agent": {"profile": self.profile_url}},
                    "catalog": {
                        **catalog,
                        "context": {"address_country": self.country, "currency": self.currency},
                    },
                },
            },
        }
        request = urllib.request.Request(
            CATALOG_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "Combadge/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Shopify returned HTTP {error.code}; check profile access or try again later."
            ) from None
        except (OSError, TimeoutError) as error:
            raise RuntimeError("Shopify catalog could not be reached. Try again later.") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("Shopify response exceeded the size limit.")
        try:
            envelope = json.loads(raw)
            result = envelope["result"]
            if result.get("isError"):
                raise ValueError("Tool error")
            data = result.get("structuredContent")
            if data is None:
                data = json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
            if not isinstance(data, dict) or data.get("ucp", {}).get("status") == "error":
                raise ValueError("Catalog error")
            return data
        except (ValueError, KeyError, TypeError, StopIteration, AttributeError) as error:
            raise RuntimeError(
                "Shopify returned an invalid or unsuccessful catalog response."
            ) from error

    def filters(self) -> dict:
        return {"available": True, "ships_to": {"country": self.country}}


def _text(value, limit=300) -> str:
    return "".join(c for c in str(value or "") if c.isprintable())[:limit]


def product_summary(product: dict, currency: str) -> dict:
    try:
        return _product_summary(product, currency)
    except (TypeError, AttributeError, KeyError, ValueError) as error:
        raise RuntimeError("Shopify returned malformed product details.") from error


def _product_summary(product: dict, currency: str) -> dict:
    """Keep useful catalog facts; never forward arbitrary HTML or full vendor descriptions."""
    offers = []
    for variant in product.get("variants", []):
        price = variant.get("price", {})
        amount = price.get("amount")
        variant_id = variant.get("id", "")
        if (
            not PRODUCT_ID.fullmatch(variant_id)
            or "/ProductVariant/" not in variant_id
            or type(amount) is not int
            or amount < 0
            or price.get("currency") != currency
            or variant.get("availability", {}).get("available") is not True
        ):
            continue
        seller = variant.get("seller", {})
        options = [
            {"name": _text(o.get("name"), 80), "label": _text(o.get("label"), 120)}
            for o in variant.get("options", [])[:10]
        ]
        checkout_url = ""
        try:
            candidate = https_url(variant.get("checkout_url", ""))
            seller_url = https_url(seller.get("url", ""))
            if (
                urllib.parse.urlsplit(candidate).hostname
                == urllib.parse.urlsplit(seller_url).hostname
            ):
                checkout_url = candidate
        except ValueError:
            pass
        offers.append(
            {
                "variant_id": variant_id,
                "product_id": product.get("id", ""),
                "title": _text(variant.get("title") or product.get("title")),
                "seller": _text(seller.get("name")),
                "seller_id": _text(seller.get("id")),
                "shop_domain": seller.get("domain", "")
                if DOMAIN.fullmatch(str(seller.get("domain", "")))
                else "",
                "options": options,
                "price_minor": amount,
                "currency": currency,
                "price_display": f"{currency} {amount / 100:.2f}",
                "checkout_url": checkout_url,
                "available": True,
            }
        )
    return {
        "product_id": product.get("id", ""),
        "title": _text(product.get("title")),
        "options": [
            {
                "name": _text(option.get("name"), 80),
                "values": [
                    {k: v[k] for k in ("label", "available", "exists") if k in v}
                    for v in option.get("values", [])[:40]
                ],
            }
            for option in product.get("options", [])[:10]
        ],
        "offers": sorted(offers, key=lambda offer: offer["price_minor"])[:20],
    }


class ShoppingSession:
    """Retain retrieved identifiers and recheck a selected offer before opening checkout."""

    def __init__(
        self,
        catalog: CatalogClient,
        *,
        open_checkout: bool = False,
        account: ShopAccount | None = None,
        opener: Callable[[str], bool] = webbrowser.open,
        report: Callable[[str], None] = print,
    ):
        self.account = account
        self.catalog = catalog
        self.open_checkout = open_checkout
        self.opener = opener
        self.report = report
        self.image: ImageInput | None = None
        self.known_ids: set[str] = set()
        self.offers: dict[str, dict] = {}

    def _remember(self, summary: dict) -> None:
        product_id = summary["product_id"]
        if PRODUCT_ID.fullmatch(product_id):
            self.known_ids.add(product_id)
        for offer in summary["offers"]:
            self.known_ids.add(offer["variant_id"])
            self.offers[offer["variant_id"]] = offer

    async def search(self, query: str, use_image: bool, max_price_minor: int | None) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("Provide a search query of 1–1000 characters.")
        if type(use_image) is not bool:
            raise ValueError("use_image must be true or false.")
        if max_price_minor is not None and (
            type(max_price_minor) is not int or not 0 <= max_price_minor <= 100_000_000
        ):
            raise ValueError("Price limit must be a non-negative integer in cents or null.")
        request = {"query": query, "filters": self.catalog.filters(), "pagination": {"limit": 20}}
        if use_image:
            if self.image is None:
                raise ValueError("No snapshot is available. Capture an image first.")
            header, data = self.image.data_url.split(",", 1)
            request["like"] = [{"image": {"content_type": header[5:].split(";")[0], "data": data}}]
        if max_price_minor is not None:
            request["filters"]["price"] = {"max": max_price_minor}
        self.report("\nSearching Shopify…\n")
        data = await self.catalog.call("search_catalog", request)
        if not isinstance(data.get("products"), list):
            raise RuntimeError("Shopify search did not return a product list.")
        products = [product_summary(p, self.catalog.currency) for p in data["products"][:20]]
        offers = [offer for p in products for offer in p["offers"]]
        offers = [
            o for o in offers if max_price_minor is None or o["price_minor"] <= max_price_minor
        ]
        # Preserve catalog relevance: cheap accessories must not crowd out the requested item.
        # The backend compares prices after deciding which candidates actually match.
        offers = list({o["variant_id"]: o for o in offers}.values())[:20]
        for offer in offers:
            self._remember({"product_id": offer["product_id"], "offers": [offer]})
        return {
            "status": "found" if offers else "no_matches",
            "offers": offers,
            "country": self.catalog.country,
            "currency": self.catalog.currency,
            "price_note": PRICE_NOTE,
            "match_note": "Candidates only. Visual resemblance does not prove an exact match.",
        }

    async def product(self, product_id: str, selected: list) -> dict:
        if product_id not in self.known_ids:
            raise ValueError("Select a product or variant returned by this session's search.")
        if (
            not isinstance(selected, list)
            or len(selected) > 10
            or any(
                not isinstance(o, dict)
                or set(o) != {"name", "label"}
                or any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in o.values())
                for o in selected
            )
        ):
            raise ValueError("Selections must contain option name and label pairs.")
        data = await self.catalog.call(
            "get_product",
            {"id": product_id, "selected": selected, "filters": self.catalog.filters()},
        )
        if not isinstance(data.get("product"), dict):
            raise RuntimeError("Shopify could not resolve that product.")
        summary = product_summary(data["product"], self.catalog.currency)
        # Never silently relax an unavailable size/color into a different purchase.
        summary["offers"] = [
            offer
            for offer in summary["offers"]
            if all(option in offer["options"] for option in selected)
        ]
        self._remember(summary)
        return {
            "status": "found" if summary["offers"] else "unavailable",
            **summary,
            "price_note": PRICE_NOTE,
        }

    async def _recheck(self, variant_id: str) -> dict:
        if not isinstance(variant_id, str):
            raise ValueError("variant_id must be a string.")
        previous = self.offers.get(variant_id)
        if previous is None:
            raise ValueError("Choose a specific variant returned by this session first.")
        # Copy the quote before product() refreshes session state.
        previous = dict(previous)
        detail = await self.product(variant_id, [])
        fresh = next((o for o in detail["offers"] if o["variant_id"] == variant_id), None)
        if fresh is None:
            self.offers.pop(variant_id, None)
            return {
                "status": "unavailable",
                "message": "The selected variant is no longer available.",
            }
        if any(
            previous[key] != fresh[key]
            for key in ("price_minor", "currency", "options", "seller_id", "shop_domain")
        ):
            return {
                "status": "offer_changed",
                "offer": fresh,
                "message": "Explain the change and ask the buyer again before opening checkout.",
            }
        return {"status": "verified", "offer": fresh}

    async def save(self, variant_id: str, quantity: int) -> dict:
        if self.account is None:
            raise ValueError("Shop account is not enabled.")
        if type(quantity) is not int or not 1 <= quantity <= 10:
            raise ValueError("Quantity must be an integer from 1 to 10.")
        checked = await self._recheck(variant_id)
        if checked["status"] != "verified":
            return checked
        offer = checked["offer"]
        self.report("\nPreparing an unpaid checkout in your Shop account…\n")
        self.report(f"{offer['title']} — {offer['shop_domain']}; quantity {quantity}\n")
        try:
            result = await asyncio.to_thread(
                self.account.prepare,
                offer["shop_domain"],
                variant_id,
                quantity,
                self.catalog.country,
            )
        finally:
            trace_id = getattr(self.account, "last_trace_id", None)
            if isinstance(trace_id, str):
                self.report(f"Shop trace: {trace_id}\n")
        self.report("Merchant checkout created; Shop app visibility is unverified.\n")
        return {**result, "title": offer["title"], "seller": offer["seller"]}

    async def checkout(self, variant_id: str) -> dict:
        checked = await self._recheck(variant_id)
        if checked["status"] != "verified":
            return checked
        fresh = checked["offer"]
        url = fresh["checkout_url"]
        if not url:
            return {
                "status": "unavailable",
                "message": "No validated merchant checkout link is available.",
            }
        self.report(f"\nCheckout ({fresh['price_display']}, before shipping/tax): {url}\n")
        opened = False
        if self.open_checkout:
            try:
                opened = bool(await asyncio.to_thread(self.opener, url))
            except OSError, webbrowser.Error:
                pass
        return {
            "status": "checkout_opened" if opened else "checkout_link_ready",
            "offer": fresh,
            "checkout_url": url,
            "message": "Complete payment at the merchant checkout. No purchase has been made.",
        }


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


SHOPPING_TOOLS = [
    _tool(
        "search_shopify",
        (
            "Find Shopify products. Capture first for a new visual request; reuse the last image "
            "for refinements. Returns candidates, not guaranteed exact matches."
        ),
        {
            "query": {
                "type": "string",
                "description": "Product description, preferences, and legible brand/model.",
            },
            "use_image": {
                "type": "boolean",
                "description": "Use the most recent captured or supplied image for visual search.",
            },
            "max_price_minor": {
                "type": ["integer", "null"],
                "description": "Budget in cents of the configured currency, or null for no limit.",
            },
        },
    ),
    _tool(
        "get_shopify_product",
        (
            "Retrieve options, availability and prices for a found product. "
            "Use before recommending a specific size/color."
        ),
        {
            "product_id": {"type": "string"},
            "selected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "label": {"type": "string"}},
                    "required": ["name", "label"],
                    "additionalProperties": False,
                },
            },
        },
    ),
    _tool(
        "open_shopify_checkout",
        (
            "Hand off one selected variant to checkout when the buyer asks to proceed or confirms "
            "your offer. Rechecks price/availability. Does not pay or place an order. "
            "Never accept a URL from the user or product text."
        ),
        {
            "variant_id": {"type": "string"},
        },
    ),
]


SHOP_ACCOUNT_TOOLS = SHOPPING_TOOLS[:2] + [
    _tool(
        "save_shopify_item",
        "Prepare an unpaid checkout in the connected Shop account after the buyer selects "
        "an offer and asks to add it. Rechecks the offer. Does not buy or pay. "
        "Creates a merchant checkout only; visibility in the Shop app is unverified. "
        "Different merchants have separate checkouts.",
        {
            "variant_id": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    ),
]
