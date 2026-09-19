"""Single-badge relay: authenticated PCM WebSocket to signed Twilio Media Streams.

Run behind an HTTPS/WSS reverse proxy. One process owns the active call.
"""

import asyncio
import base64
import json
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from http import HTTPStatus

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from commbadge.audio import FRAME_BYTES
from commbadge.phone.carrier import TERMINAL, TwilioCarrier, valid_signature
from commbadge.phone.codec import PhoneCodec


async def send(ws, message):
    if isinstance(message, dict):
        message = json.dumps(message)
    await asyncio.wait_for(ws.send(message), 2)


@dataclass
class Call:
    badge: object
    key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    sid: str | None = None
    media: object = None
    stream_sid: str | None = None
    created: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    codec: PhoneCodec = field(default_factory=PhoneCodec)
    reason: str = "ended"


class PhoneRelay:
    def __init__(self, settings, carrier=None, *, poll_seconds=2):
        self.settings = settings
        self.carrier = carrier or TwilioCarrier(settings)
        self.call: Call | None = None
        self.poll_seconds = poll_seconds

    def authorize(self, connection, request):
        path = request.path
        if path == "/health":
            return connection.respond(HTTPStatus.OK, "ok\n")
        if path == "/badge":
            auth = request.headers.get_all("Authorization")
            if len(auth) == 1 and secrets.compare_digest(auth[0], f"Bearer {self.settings.token}"):
                return None
        elif self.call is not None and path == f"/media/{self.call.key}":
            signatures = request.headers.get_all("X-Twilio-Signature")
            if len(signatures) == 1 and valid_signature(
                self.settings.auth_token, self.settings.public_url, path, signatures[0]
            ):
                return None
        return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

    async def handle(self, ws):
        try:
            if ws.request.path == "/badge":
                await self.badge(ws)
            else:
                await self.media(ws)
        except (ConnectionClosed, TimeoutError, ValueError, KeyError, TypeError, RuntimeError):
            with suppress(ConnectionClosed, TimeoutError):
                await send(
                    ws,
                    {
                        "type": "error",
                        "message": "Call connection failed; inspect relay logs/console.",
                    },
                )
        finally:
            await ws.close()

    async def badge(self, ws):
        # The badge learns only names. Destinations and carrier secrets stay here.
        await send(ws, {"type": "contacts", "names": list(self.settings.contacts)})
        raw = await asyncio.wait_for(ws.recv(), 15)
        request = json.loads(raw)
        if not isinstance(request, dict) or request.get("type") != "call":
            raise ValueError("Expected call request")
        name = request.get("contact")
        if not isinstance(name, str) or name.casefold() not in self.settings.contacts:
            await send(
                ws, {"type": "error", "message": "Unknown contact; configure it on the relay."}
            )
            return
        # No await between check and reservation: a second badge cannot race the dial.
        if self.call is not None:
            await send(ws, {"type": "error", "message": "Another call is already active."})
            return
        call = self.call = Call(ws)
        creation = None
        tasks = []
        try:
            await send(ws, {"type": "status", "status": "dialing"})
            url = self.settings.public_url.replace("https://", "wss://", 1) + f"/media/{call.key}"
            creation = asyncio.create_task(
                self.carrier.create(self.settings.contacts[name.casefold()], url)
            )
            # Keep call creation alive during cancellation so we can recover its SID
            # and hang it up. Network requests have their own bounded timeout.
            call.sid = await asyncio.shield(creation)
            call.created.set()
            tasks = [
                asyncio.create_task(self.from_badge(call)),
                asyncio.create_task(self.watch_status(call)),
                asyncio.create_task(call.ended.wait()),
                asyncio.create_task(asyncio.sleep(self.settings.max_seconds + 45)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as error:
            call.reason = "failed"
            with suppress(ConnectionClosed, TimeoutError):
                await send(
                    ws,
                    {
                        "type": "error",
                        "message": str(error)
                        if isinstance(error, RuntimeError)
                        else "Invalid call data",
                    },
                )
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if creation is not None and call.sid is None:
                with suppress(Exception):
                    call.sid = await creation
            call.created.set()
            try:
                if call.media is not None:
                    await call.media.close()
                if call.sid is not None:
                    await self.carrier.hangup(call.sid)
            except Exception:
                call.reason = "hangup-unconfirmed"
                print(
                    "Call hang-up was not confirmed; check Twilio. "
                    "Provider duration limit remains active.",
                    flush=True,
                )
            finally:
                self.call = None
                with suppress(ConnectionClosed, TimeoutError):
                    await send(ws, {"type": "ended", "status": call.reason})

    async def from_badge(self, call):
        async for data in call.badge:
            if isinstance(data, str):
                control = json.loads(data)
                if isinstance(control, dict) and control.get("type") == "hangup":
                    return
                raise ValueError("Unknown control message")
            if len(data) != FRAME_BYTES:
                raise ValueError("Expected a 20 ms PCM frame")
            # Discard speech while ringing; never queue pre-answer microphone audio.
            if call.media is not None and call.stream_sid is not None:
                payload = base64.b64encode(call.codec.encode(data)).decode()
                await send(
                    call.media,
                    {"event": "media", "streamSid": call.stream_sid, "media": {"payload": payload}},
                )

    async def watch_status(self, call):
        previous = ""
        connected_deadline = asyncio.get_running_loop().time() + 50
        while True:
            status = await self.carrier.status(call.sid)
            if status != previous:
                await send(call.badge, {"type": "status", "status": status})
                previous = status
            if status in TERMINAL:
                call.reason = status
                return
            if call.stream_sid is None and asyncio.get_running_loop().time() > connected_deadline:
                call.reason = "media-timeout"
                return
            await asyncio.sleep(self.poll_seconds)

    async def media(self, ws):
        call = self.call
        if call is None or ws.request.path != f"/media/{call.key}" or call.media is not None:
            return
        # Reserve before waiting for REST's response, which can race this handshake.
        call.media = ws
        try:
            await asyncio.wait_for(call.created.wait(), 20)
            async with asyncio.timeout(10):
                event = json.loads(await ws.recv())
                if not isinstance(event, dict):
                    raise ValueError("Expected stream event")
                if event.get("event") == "connected":
                    event = json.loads(await ws.recv())
                if not isinstance(event, dict):
                    raise ValueError("Expected stream start")
                start = event.get("start", {})
                if (
                    event.get("event") != "start"
                    or not isinstance(start, dict)
                    or start.get("callSid") != call.sid
                    or start.get("accountSid") != self.settings.account_sid
                    or start.get("mediaFormat")
                    != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
                    or not isinstance(start.get("streamSid"), str)
                ):
                    raise ValueError("Unexpected Twilio stream")
                call.stream_sid = start["streamSid"]
            await send(call.badge, {"type": "status", "status": "connected"})
            async for raw in ws:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("Expected stream event")
                if event.get("streamSid") != call.stream_sid:
                    raise ValueError("Wrong stream identifier")
                kind = event.get("event")
                if kind == "stop":
                    return
                if kind == "media":
                    payload = base64.b64decode(event["media"]["payload"], validate=True)
                    if not payload or len(payload) > 8000:
                        raise ValueError("Invalid media size")
                    await send(call.badge, call.codec.decode(payload))
        finally:
            call.ended.set()

    async def run(self, host="127.0.0.1", port=8765):
        async with serve(
            self.handle,
            host,
            port,
            process_request=self.authorize,
            max_size=65536,
            max_queue=16,
            compression=None,
            ping_interval=10,
            ping_timeout=10,
            close_timeout=2,
        ):
            print(f"Phone relay listening on {host}:{port}; HTTPS/WSS proxy required.", flush=True)
            await asyncio.Future()
