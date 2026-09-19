"""Personal Shop account authentication and unpaid merchant checkouts (stdlib only)."""

import ipaddress
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

CLIENT_ID = "5c733ab2-1903-400a-891e-7ba20c09e2a3"
PROFILE = "https://shopify.dev/ucp/agent-profiles/2026-04-08/personal_agent.json"
AUTH = "https://accounts.shop.app/oauth"
# Preserve existing credentials and traces across the CLI/package rename.
DEFAULT_AUTH_FILE = Path.home() / ".local/state/commbadge/shop-auth.json"
DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]*\.myshopify\.com\Z")
VARIANT = re.compile(r"gid://shopify/ProductVariant/[0-9]+\Z")


class ShopError(RuntimeError):
    def __init__(self, message, *, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, *, form=None, payload=None, token=None, headers=None):
    """Bound responses and never expose OAuth tokens, addresses or server error bodies."""
    data = None
    outgoing = {"Accept": "application/json", "User-Agent": "Combadge/0.1"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        outgoing["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        outgoing["Content-Type"] = "application/json"
    if token:
        outgoing["Authorization"] = f"Bearer {token}"
    outgoing.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=outgoing)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        code = None
        try:
            candidate = json.loads(error.read(8192)).get("error")
            if candidate in (
                "authorization_pending",
                "slow_down",
                "expired_token",
                "access_denied",
                "invalid_grant",
            ):
                code = candidate
        except ValueError, TypeError, AttributeError:
            pass
        raise ShopError(
            f"Shop request returned HTTP {error.code}.", status=error.code, code=code
        ) from None
    except OSError, TimeoutError:
        raise ShopError("Shop could not be reached. Check your connection.") from None
    try:
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except ValueError, TypeError:
        raise ShopError("Shop returned an invalid response.") from None


class TokenStore:
    def __init__(self, path=DEFAULT_AUTH_FILE):
        self.path = Path(path).expanduser()

    def read(self):
        try:
            if self.path.stat().st_mode & 0o077:
                raise ShopError("Shop credentials must be owner-only. Set file permissions to 600.")
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except FileNotFoundError:
            return {}
        except OSError, ValueError:
            raise ShopError("Cannot read Shop credentials. Run shop-account login again.") from None

    def write(self, value):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".shop-auth-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(value, output)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def clear(self):
        self.path.unlink(missing_ok=True)


class ShopAccount:
    def __init__(self, store=None):
        self.store = store if store is not None else TokenStore()
        self.trace_dir = self.store.path.parent / "shop-traces"
        self.last_trace_id = None

    def _write_trace(self, trace, **values):
        trace.update(values)
        trace["updated_at"] = datetime.now(UTC).isoformat()
        TokenStore(self.trace_dir / f"{trace['trace_id']}.json").write(trace)

    def traces(self, trace_id=None, *, refresh=False):
        if trace_id is not None and not re.fullmatch(r"[a-f0-9]{32}", trace_id):
            raise ValueError("Use a trace ID printed by this app.")
        if refresh and trace_id is None:
            raise ValueError("--refresh requires --trace-id.")
        paths = (
            [self.trace_dir / f"{trace_id}.json"]
            if trace_id
            else sorted(
                self.trace_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:10]
        )
        traces = []
        for path in paths:
            trace = TokenStore(path).read()
            if not trace:
                continue
            if refresh:
                domain = trace.get("merchant", "")
                checkout_id = trace.get("response", {}).get("checkout_id")
                if not DOMAIN.fullmatch(domain) or not isinstance(checkout_id, str):
                    raise ShopError("This trace has no retrievable merchant checkout ID.")
                token, buyer_ip = self._merchant_access(domain)
                envelope = request(
                    f"https://{domain}/api/ucp/mcp",
                    token=token,
                    headers={"Shopify-Buyer-Ip": buyer_ip},
                    payload={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "get_checkout",
                            "arguments": {
                                "meta": {"ucp-agent": {"profile": PROFILE}},
                                "id": checkout_id,
                            },
                        },
                    },
                )
                self._write_trace(
                    trace,
                    last_read=checkout_evidence(envelope),
                    last_read_at=datetime.now(UTC).isoformat(),
                )
            # Checkout IDs and continuation URLs can grant checkout access. Keep them on disk.
            public = dict(trace)
            for field in ("response", "last_read"):
                if field in public:
                    public[field] = {
                        k: v
                        for k, v in public[field].items()
                        if k not in ("checkout_id", "continue_url")
                    }
            traces.append(public)
        return traces

    def login(self, report=print):
        device = request(
            f"{AUTH}/device",
            form={
                "client_id": CLIENT_ID,
                "scope": "openid email personal_agent",
                "device_name": "Computer - Combadge",
            },
        )
        try:
            url = device["verification_uri_complete"]
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "accounts.shop.app":
                raise ValueError()
            interval = max(1, int(device.get("interval", 5)))
            deadline = time.monotonic() + min(int(device["expires_in"]), 1800)
            device_code = device["device_code"]
        except KeyError, TypeError, ValueError:
            raise ShopError("Shop did not return a valid sign-in request.") from None
        report(f"Open this link on your phone and connect your Shop account:\n{url}")
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                tokens = request(
                    f"{AUTH}/token",
                    form={
                        "client_id": CLIENT_ID,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                    },
                )
            except ShopError as error:
                if error.code in ("authorization_pending", "slow_down"):
                    if error.code == "slow_down":
                        interval += 5
                    continue
                raise ShopError(
                    "Shop sign-in ended. Run shop-account login to try again."
                ) from None
            self._save_tokens(tokens)
            report("Shop account connected.")
            return
        raise ShopError("Shop sign-in expired. Run shop-account login again.")

    def _save_tokens(self, tokens, previous=None):
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token") or (previous or {}).get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise ShopError("Shop did not return valid account credentials.")
        self.store.write({"access_token": access, "refresh_token": refresh})
        return access

    def access_token(self):
        saved = self.store.read()
        access = saved.get("access_token")
        if not isinstance(access, str) or not access:
            raise ShopError("Connect your Shop account first: combadge shop-account login")
        try:
            request(f"{AUTH}/userinfo", token=access)
            return access
        except ShopError as error:
            if error.status != 401:
                raise
        refresh = saved.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ShopError("Shop sign-in expired. Run combadge shop-account login again.")
        try:
            tokens = request(
                f"{AUTH}/token",
                form={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
            )
            return self._save_tokens(tokens, saved)
        except ShopError:
            raise ShopError(
                "Shop sign-in could not be refreshed. Run shop-account login."
            ) from None

    def prepare(self, domain, variant_id, quantity, country):
        self.last_trace_id = None
        if not isinstance(domain, str) or not DOMAIN.fullmatch(domain):
            raise ValueError("This offer has no supported Shopify merchant domain.")
        if not isinstance(variant_id, str) or not VARIANT.fullmatch(variant_id):
            raise ValueError("Choose a specific Shopify variant.")
        if type(quantity) is not int or not 1 <= quantity <= 10 or country not in ("CA", "US"):
            raise ValueError("Quantity must be 1–10 and destination CA or US.")
        trace = {
            "trace_id": uuid.uuid4().hex,
            "started_at": datetime.now(UTC).isoformat(),
            "operation": "create_checkout",
            "merchant": domain,
            "variant_id": variant_id,
            "quantity": quantity,
            "country": country,
            "app_visibility": "unverified",
        }
        self.last_trace_id = trace["trace_id"]
        self._write_trace(trace, stage="authorizing")
        try:
            result = self._prepare(domain, variant_id, quantity, country, trace)
            self._write_trace(trace, stage="checkout_prepared")
            return {**result, "trace_id": trace["trace_id"]}
        except (OSError, RuntimeError, ValueError) as error:
            stage = trace["stage"]
            self._write_trace(
                trace,
                stage="failed",
                failed_stage=stage,
                http_status=getattr(error, "status", None),
            )
            if stage == "authorizing":
                raise ShopError(
                    "Shop authorization failed before checkout creation. "
                    f"Trace: {self.last_trace_id}"
                ) from None
            raise ShopError(
                "Could not confirm checkout creation. Check before trying again; "
                f"a checkout may exist. No payment was requested. Trace: {self.last_trace_id}"
            ) from None

    def _merchant_access(self, domain):
        access = self.access_token()
        exchange = request(
            "https://shop.app/oauth/token",
            form={
                "client_id": CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": access,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "resource": f"https://{domain}/",
            },
        )
        merchant_token = exchange.get("access_token")
        if not isinstance(merchant_token, str) or not merchant_token:
            raise ShopError("Shop could not authorize this merchant.")
        try:
            buyer_ip = str(ipaddress.ip_address(request("https://api.ipify.org?format=json")["ip"]))
        except KeyError, ValueError, TypeError:
            raise ShopError("Could not determine the buyer network address.") from None
        return merchant_token, buyer_ip

    def _prepare(self, domain, variant_id, quantity, country, trace):
        merchant_token, buyer_ip = self._merchant_access(domain)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "create_checkout",
                "arguments": {
                    "meta": {"ucp-agent": {"profile": PROFILE}},
                    "checkout": {
                        "context": {"address_country": country},
                        "line_items": [{"quantity": quantity, "item": {"id": variant_id}}],
                    },
                },
            },
        }
        self._write_trace(trace, stage="requesting_checkout")
        envelope = request(
            f"https://{domain}/api/ucp/mcp",
            payload=payload,
            token=merchant_token,
            headers={"Shopify-Buyer-Ip": buyer_ip},
        )
        self._write_trace(trace, stage="response_received", response=checkout_evidence(envelope))
        return checkout_summary(envelope, variant_id, quantity)


def checkout_data(envelope):
    result = envelope["result"]
    data = result.get("structuredContent")
    if data is None:
        data = json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
    return result, data.get("checkout", data)


def checkout_evidence(envelope):
    """Allowlisted diagnostics only: no raw bodies, buyer data, tokens or payment objects."""
    try:
        result, checkout = checkout_data(envelope)
        evidence = {
            "mcp_error": result.get("isError") is True,
            "has_checkout_id": bool(checkout.get("id")),
            "buyer_present": bool(checkout.get("buyer")),
        }
        for key, source in (
            ("checkout_id", "id"),
            ("checkout_status", "status"),
            ("currency", "currency"),
            ("expires_at", "expires_at"),
        ):
            value = checkout.get(source)
            if isinstance(value, str):
                evidence[key] = value[:1024]
        continuation = checkout.get("continue_url")
        if isinstance(continuation, str):
            url = urllib.parse.urlsplit(continuation)
            if url.scheme == "https" and url.hostname and not url.username and not url.password:
                evidence["continue_url"] = continuation[:8192]
                evidence["continuation_host"] = url.hostname
        status = checkout.get("ucp", {}).get("status")
        evidence["ucp_status"] = status if status in ("success", "error") else "unknown"
        evidence["message_codes"] = [
            m["code"]
            for m in checkout.get("messages", [])
            if isinstance(m.get("code"), str) and re.fullmatch(r"[a-z_]{1,100}", m["code"])
        ][:30]
        evidence["line_items"] = [
            {"variant_id": line["item"]["id"], "quantity": line["quantity"]}
            for line in checkout.get("line_items", [])
            if VARIANT.fullmatch(str(line.get("item", {}).get("id", "")))
            and type(line.get("quantity")) is int
        ][:20]
        evidence["totals_minor"] = {
            total["type"]: total["amount"]
            for total in checkout.get("totals", [])
            if total.get("type") in ("subtotal", "fulfillment", "tax", "total", "discount")
            and type(total.get("amount")) is int
        }
        return evidence
    except KeyError, ValueError, TypeError, AttributeError, StopIteration:
        return {"malformed_response": True}


def checkout_summary(envelope, variant_id, quantity):
    """Only purchase facts leave this boundary, never buyer or payment information."""
    try:
        result, checkout = checkout_data(envelope)
        if not checkout.get("id") or checkout.get("status") not in (
            "incomplete",
            "requires_escalation",
            "ready_for_complete",
        ):
            raise ValueError()
        errors = [m.get("code") for m in checkout.get("messages", []) if m.get("type") == "error"]
        if any(code != "extension_interaction_required" for code in errors):
            raise ValueError()
        if result.get("isError") and not (
            checkout.get("ucp", {}).get("status") == "success"
            and checkout["status"] == "requires_escalation"
            and errors == ["extension_interaction_required"]
        ):
            raise ValueError()
        lines = checkout["line_items"]
        if len(lines) != 1 or lines[0]["item"]["id"] != variant_id:
            raise ValueError()
        if lines[0]["quantity"] != quantity:
            raise ValueError()
        currency = checkout["currency"]
        if currency not in ("CAD", "USD"):
            raise ValueError()
        totals = {}
        for entry in checkout.get("totals", []):
            kind, amount = entry.get("type"), entry.get("amount")
            if kind in ("subtotal", "fulfillment", "tax", "total", "discount"):
                if type(amount) is not int or amount < 0:
                    raise ValueError()
                totals[kind] = amount
        return {
            "status": "checkout_prepared",
            "app_visibility": "unverified",
            "merchant_status": checkout["status"],
            "quantity": quantity,
            "currency": currency,
            "totals_minor": totals,
            "shipping_exceeds_items": totals.get("fulfillment", 0) > totals.get("subtotal", 0),
            "review_required": checkout["status"] != "ready_for_complete",
            "message": "The merchant created an unpaid checkout using your Shop account. "
            "Visibility in the Shop app has NOT been verified. "
            "Do not claim it is in the app or cart. "
            "No payment or order was submitted. Totals may change during final review.",
        }
    except KeyError, ValueError, TypeError, AttributeError, StopIteration:
        raise ShopError("Shop did not confirm the requested unpaid checkout.") from None
