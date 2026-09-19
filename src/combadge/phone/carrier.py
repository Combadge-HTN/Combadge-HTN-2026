"""Small Twilio REST client. Creating a call is never automatically retried."""

import asyncio
import base64
import hashlib
import hmac
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

TERMINAL = {"completed", "busy", "failed", "no-answer", "canceled"}


def valid_signature(token: str, public_url: str, path: str, signature: str) -> bool:
    # GET WebSocket upgrades have no POST parameters. Use the configured external
    # URL, never a forwarded Host supplied by a client. Twilio can normalize '/'.
    for base in (public_url, public_url.replace("https://", "wss://", 1)):
        for url in (base + path, base + path + "/"):
            expected = base64.b64encode(
                hmac.new(token.encode(), url.encode(), hashlib.sha1).digest()
            ).decode()
            if hmac.compare_digest(expected, signature):
                return True
    return False


def stream_twiml(url: str) -> str:
    root = ET.Element("Response")
    ET.SubElement(ET.SubElement(root, "Connect"), "Stream", url=url)
    ET.SubElement(root, "Hangup")
    return ET.tostring(root, encoding="unicode")


class TwilioCarrier:
    def __init__(self, settings):
        self.settings = settings

    def _request(self, suffix: str = "", data: dict | None = None):
        config = self.settings
        auth = base64.b64encode(f"{config.account_sid}:{config.auth_token}".encode()).decode()
        request = Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{config.account_sid}/Calls{suffix}.json",
            data=urlencode(data).encode() if data is not None else None,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            # Do not expose phone numbers or provider request details in errors.
            raise RuntimeError(
                f"Twilio request failed (HTTP {error.code}); check the Twilio console"
            ) from None
        except OSError:
            raise RuntimeError(
                "Twilio request timed out or failed; check the console before retrying"
            ) from None

    async def create(self, number: str, stream_url: str) -> str:
        result = await asyncio.to_thread(
            self._request,
            data={
                "To": number,
                "From": self.settings.from_number,
                "Twiml": stream_twiml(stream_url),
                "Timeout": "30",
                "TimeLimit": str(self.settings.max_seconds),
                "Record": "false",
            },
        )
        sid = result.get("sid", "")
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
            raise RuntimeError("Twilio returned an invalid call identifier")
        return sid

    async def status(self, sid: str) -> str:
        return (await asyncio.to_thread(self._request, f"/{sid}"))["status"]

    async def hangup(self, sid: str):
        status = await self.status(sid)
        if status not in TERMINAL:
            try:
                await asyncio.to_thread(
                    self._request,
                    f"/{sid}",
                    {
                        "Status": "canceled" if status in ("queued", "ringing") else "completed",
                    },
                )
            except RuntimeError:
                # It may have changed from ringing to answered while ending it.
                await asyncio.to_thread(self._request, f"/{sid}", {"Status": "completed"})
