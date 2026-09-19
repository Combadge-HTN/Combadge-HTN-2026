"""Outbound human calling directly to Twilio over TLS and SRTP; no relay."""

import asyncio
import secrets
import socket
import ssl
import struct
from contextlib import suppress

from commbadge.audio import FRAME_BYTES
from commbadge.phone.codec import PhoneCodec
from commbadge.phone.sip import Message, answer, digest, offer, read_message, routes, tag, uri
from commbadge.phone.srtp import Srtp, crypto, header_size


class SipCall:
    def __init__(self, settings, number, report=print):
        self.settings = settings
        self.number = number
        self.report = report
        self.call_id = secrets.token_hex(16)
        self.tag = secrets.token_hex(12)
        self.cseq = 1
        self.branch = "z9hG4bK" + secrets.token_hex(12)
        self.target = f"sip:{number}@{settings.domain};transport=tls"
        self.remote_target = self.target
        self.to = f"<{self.target}>"
        self.original_to = self.to
        self.routes = []
        self.writer = None
        self.reader = None
        self.reader_task = None
        self.messages = asyncio.Queue(maxsize=32)
        self.udp = None
        self.master = secrets.token_bytes(30)
        self.sender = Srtp(self.master)
        self.receiver = None
        self.media = None
        self.invited = False
        self.provisional = False
        self.established = False
        self.ended = False
        self.last_response = None
        self.sent_packets = 0
        self.received_packets = 0
        self.rejected_packets = 0

    async def open(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.settings.domain,
                5061,
                ssl=context,
                server_hostname=self.settings.domain,
                family=socket.AF_INET,
                limit=65536,
            ),
            15,
        )
        self.ip, self.port = self.writer.get_extra_info("sockname")[:2]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((self.ip, 0))
        self.udp.setblocking(False)
        self.from_header = (
            f"<sip:{self.settings.from_number}@{self.settings.domain}>;tag={self.tag}"
        )
        self.contact = f"<sip:combadge@{self.ip}:{self.port};transport=tls>"
        self.sdp = offer(self.ip, self.udp.getsockname()[1], self.master, secrets.randbits(48))
        self.reader_task = asyncio.create_task(self.read_messages())

    async def read_messages(self):
        # Keep framing alive when a call task is cancelled during a partial message.
        try:
            while True:
                await self.messages.put(await read_message(self.reader))
        except (OSError, ValueError, asyncio.IncompleteReadError) as error:
            await self.messages.put(error)

    def request(self, method, *, branch=None, cseq=None, dialog=False, body=b"", extra=()):
        target = self.remote_target if dialog else self.target
        routes = list(self.routes) if dialog else []
        if routes and ";lr" not in routes[0]:
            target = uri(routes.pop(0))
            routes.append(f"<{self.remote_target}>")
        branch = branch or "z9hG4bK" + secrets.token_hex(12)
        headers = [
            (
                "Via",
                f"SIP/2.0/TLS {self.ip}:{self.port};branch={branch};rport",
            ),
            ("Max-Forwards", "70"),
            ("From", self.from_header),
            ("To", self.to if dialog or method == "ACK" else self.original_to),
            ("Call-ID", self.call_id),
            ("CSeq", f"{cseq or self.cseq} {method}"),
            ("Contact", self.contact),
            ("User-Agent", "Combadge/0.1"),
            ("Allow", "INVITE, ACK, CANCEL, BYE, OPTIONS"),
        ]
        headers.extend(("Route", value) for value in routes)
        headers.extend(extra)
        if body:
            headers.append(("Content-Type", "application/sdp"))
        return Message(f"{method} {target} SIP/2.0", headers, body)

    async def send(self, message):
        self.writer.write(message.encode())
        await asyncio.wait_for(self.writer.drain(), 3)

    async def reply(self, request, status, reason):
        headers = [
            (name, value)
            for name, value in request.headers
            if name.lower() in ("via", "from", "to", "call-id", "cseq")
        ]
        await self.send(Message(f"SIP/2.0 {status} {reason}", headers))

    async def receive(self):
        while True:
            message = await self.messages.get()
            if isinstance(message, Exception):
                raise message
            if message.get("Call-ID") != self.call_id:
                if not message.status:
                    await self.reply(message, 481, "Call Does Not Exist")
                continue
            if message.status:
                return message
            # Requests must match both dialog tags, not merely the Call-ID.
            if tag(message.get("To")) != self.tag or tag(message.get("From")) != tag(self.to):
                await self.reply(message, 481, "Call Does Not Exist")
            elif message.method == "BYE":
                await self.reply(message, 200, "OK")
                self.ended = True
                return message
            elif message.method == "OPTIONS":
                await self.reply(message, 200, "OK")
            elif message.method == "INVITE":
                # No transfers, hold, renegotiation, or new media destinations.
                await self.reply(message, 488, "Not Acceptable Here")
            elif message.method != "ACK":
                await self.reply(message, 501, "Not Implemented")

    async def acknowledge(self, response):
        self.to = response.get("To")
        if 200 <= response.status < 300:
            self.established = True
            self.remote_target = uri(response.get("Contact"))
            self.routes = routes(response.all("Record-Route"))
            await self.send(self.request("ACK", dialog=True))
        else:
            await self.send(self.request("ACK", branch=self.branch))

    async def invite(self):
        self.invited = True
        await self.send(self.request("INVITE", branch=self.branch, body=self.sdp))
        authenticated = False
        async with asyncio.timeout(30):
            while True:
                message = await self.receive()
                if self.ended:
                    return False
                if message.get("CSeq") != f"{self.cseq} INVITE":
                    continue
                self.last_response = message
                if message.status < 200:
                    self.provisional = True
                    if message.status in (180, 183):
                        self.report("\nCall: ringing\n")
                    continue
                await self.acknowledge(message)
                if message.status in (401, 407):
                    if authenticated:
                        self.ended = True
                        raise RuntimeError("SIP credentials were rejected")
                    challenge = (
                        "Proxy-Authenticate" if message.status == 407 else "WWW-Authenticate"
                    )
                    header = "Proxy-Authorization" if message.status == 407 else "Authorization"
                    auth = digest(
                        message.get(challenge),
                        self.settings.username,
                        self.settings.password,
                        "INVITE",
                        self.target,
                    )
                    self.cseq += 1
                    self.branch = "z9hG4bK" + secrets.token_hex(12)
                    self.provisional = False
                    self.last_response = None
                    authenticated = True
                    await self.send(
                        self.request(
                            "INVITE", branch=self.branch, body=self.sdp, extra=[(header, auth)]
                        )
                    )
                elif 200 <= message.status < 300:
                    self.media = answer(message.body)
                    self.receiver = Srtp(self.media.key)
                    self.report("\nCall: connected directly to Twilio\n")
                    return True
                else:
                    self.ended = True
                    # Do not reflect provider headers, dialed numbers, or credentials.
                    raise RuntimeError(f"SIP call rejected (status {message.status})")

    async def signaling(self):
        async def keepalive():
            while True:
                await asyncio.sleep(20)
                self.writer.write(b"\r\n\r\n")
                await asyncio.wait_for(self.writer.drain(), 3)

        tasks = [asyncio.create_task(keepalive()), asyncio.create_task(self._signaling())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _signaling(self):
        while not self.ended:
            message = await self.receive()
            if message.status == 200 and message.get("CSeq") == f"{self.cseq} INVITE":
                await self.acknowledge(message)

    async def hangup(self):
        if self.ended or not self.invited:
            return
        if self.last_response is not None and self.last_response.status >= 300:
            self.ended = True
            return
        async with asyncio.timeout(8):
            if not self.established:
                # CANCEL must follow a provisional response; handle an answer racing it.
                while not self.provisional:
                    message = await self.receive()
                    if self.ended:
                        return
                    if message.get("CSeq") != f"{self.cseq} INVITE":
                        continue
                    if message.status < 200:
                        self.provisional = True
                    else:
                        await self.acknowledge(message)
                        if message.status >= 300:
                            self.ended = True
                            return
                        break
                if not self.established:
                    await self.send(self.request("CANCEL", branch=self.branch))
                    while not self.ended:
                        message = await self.receive()
                        if message.method == "INVITE" and message.status >= 200:
                            await self.acknowledge(message)
                            if message.status >= 300:
                                self.ended = True
                                return
                            break
            if self.established and not self.ended:
                self.cseq += 1
                await self.send(self.request("BYE", dialog=True))
                while not self.ended:
                    message = await self.receive()
                    if message.get("CSeq") == f"{self.cseq} BYE" and (
                        200 <= message.status < 300 or message.status == 481
                    ):
                        self.ended = True

    async def media_loop(self, audio):
        loop = asyncio.get_running_loop()
        encoder, decoder = PhoneCodec(), PhoneCodec()
        sequence = secrets.randbelow(32768)
        timestamp, ssrc = secrets.randbits(32), secrets.randbits(32)
        endpoint = (self.media.ip, self.media.port)
        playback = asyncio.Queue(maxsize=10)
        highest = -1
        last_received = loop.time()

        async def transmit():
            nonlocal sequence, timestamp
            while True:
                frame = await audio.read()
                if len(frame) != FRAME_BYTES:
                    raise RuntimeError("Call audio needs 20 ms PCM16 frames")
                packet = struct.pack("!BBHII", 128, 0, sequence, timestamp, ssrc)
                packet += encoder.encode(frame)
                await loop.sock_sendto(self.udp, self.sender.protect(packet), endpoint)
                self.sent_packets += 1
                sequence = (sequence + 1) & 65535
                timestamp = (timestamp + 160) & 0xFFFFFFFF

        async def receive():
            nonlocal highest, last_received
            while True:
                packet, source = await loop.sock_recvfrom(self.udp, 2048)
                if source != endpoint:
                    self.rejected_packets += 1
                    continue
                try:
                    plain = self.receiver.unprotect(packet)
                    size = header_size(plain)
                    if plain[1] & 127 != 0:
                        continue
                    payload = plain[size:]
                    if plain[0] & 32:
                        padding = payload[-1] if payload else 0
                        if not padding or padding > len(payload):
                            raise ValueError("Invalid RTP padding")
                        payload = payload[:-padding]
                    if not payload or len(payload) > 1600:
                        raise ValueError("Invalid PCMU payload")
                except ValueError:
                    self.rejected_packets += 1
                    continue
                index = self.receiver.highest
                # Drop late packets instead of playing old speech after newer speech.
                if index <= highest:
                    continue
                highest = index
                last_received = loop.time()
                self.received_packets += 1
                pcm = decoder.decode(payload)
                for offset in range(0, len(pcm), FRAME_BYTES):
                    frame = pcm[offset : offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
                    if playback.full():
                        playback.get_nowait()
                    playback.put_nowait(frame)

        async def play():
            while True:
                if loop.time() - last_received > 20:
                    raise RuntimeError("No authenticated phone audio for 20 seconds")
                try:
                    # Keep the queue wait in this task: Python 3.11 wait_for can
                    # swallow cancellation when its child finishes concurrently.
                    async with asyncio.timeout(0.1):
                        frame = await playback.get()
                except TimeoutError:
                    continue
                await audio.write(frame)

        tasks = [asyncio.create_task(f()) for f in (transmit, receive, play)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self.udp:
            self.udp.close()
        if self.writer:
            self.writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(self.writer.wait_closed(), 1)


async def call_contact(settings, contact, audio, *, seconds=300, report=print):
    contact = contact.casefold()
    if contact not in settings.contacts:
        raise ValueError("Unknown contact; configure CALL_CONTACTS")
    crypto()  # Fail before opening a call if the system crypto library is missing.
    call = SipCall(settings, settings.contacts[contact], report)
    tasks = []
    confirmed = False
    try:
        await call.open()
        if await call.invite():
            tasks = [
                asyncio.create_task(call.signaling()),
                asyncio.create_task(call.media_loop(audio)),
                asyncio.create_task(asyncio.sleep(min(seconds, settings.max_seconds))),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await call.hangup()
            confirmed = call.ended or not call.invited
        except (OSError, TimeoutError, ValueError, RuntimeError):
            report("\nHang-up unconfirmed; check Twilio console.\n")
        finally:
            await call.close()
    if not confirmed:
        raise RuntimeError("SIP hang-up unconfirmed; check Twilio console")
    return {
        "status": "completed",
        "contact": contact,
        "transport": "direct-sip",
        "sent_packets": call.sent_packets,
        "received_packets": call.received_packets,
        "rejected_packets": call.rejected_packets,
    }


async def check_connection(settings):
    """Check TLS, SIP OPTIONS, local UDP binding, and crypto without dialing."""
    crypto()
    call = SipCall(settings, settings.from_number)
    try:
        await call.open()
        await call.send(call.request("OPTIONS"))
        response = await asyncio.wait_for(call.receive(), 10)
        if response.status != 200:
            raise RuntimeError(f"SIP connectivity check failed (status {response.status})")
    finally:
        await call.close()
