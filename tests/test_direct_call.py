import asyncio
from contextlib import suppress

import pytest

from commbadge.audio import SilenceAudio
from commbadge.phone import direct
from commbadge.phone.config import SipSettings
from commbadge.phone.sip import Message, answer, digest, offer, read_message
from commbadge.phone.srtp import Srtp


def settings():
    return SipSettings(
        "test.pstn.twilio.com",
        "badge",
        "secret-password",
        "+14165550100",
        {"edmon": "+14165550101"},
    )


@pytest.mark.parametrize(
    "ending", ["remote", "local", "cancel-ringing", "race-answer", "busy", "auth-denied"]
)
def test_direct_call_auth_audio_and_hangup(monkeypatch, ending):
    async def run():
        seen = []
        media_seen = asyncio.Event()
        ringing = asyncio.Event()
        finished = asyncio.get_running_loop().create_future()
        master = bytes(range(30))
        udp = None
        peer_tasks = []
        challenge = 'Digest realm="twilio.com", nonce="test-nonce", algorithm=MD5'

        class Echo(asyncio.DatagramProtocol):
            def __init__(self, incoming):
                self.receiver, self.sender = Srtp(incoming), Srtp(master)
                self.count = 0

            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                packet = self.receiver.unprotect(data)
                self.transport.sendto(self.sender.protect(packet), addr)
                self.count += 1
                if self.count >= 5:
                    media_seen.set()

        async def server(reader, writer):
            nonlocal udp
            try:
                invite = await read_message(reader)
                seen.append(invite.method)

                def response(request, status, body=b""):
                    h = [
                        (k, v)
                        for k, v in request.headers
                        if k.lower() in ("via", "from", "to", "call-id", "cseq")
                    ]
                    h = [
                        (k, v + ";tag=peer" if k.lower() == "to" and ";tag=" not in v else v)
                        for k, v in h
                    ]
                    h.append(("Contact", "<sip:peer@127.0.0.1;transport=tls>"))
                    if status == 407:
                        h.append(("Proxy-Authenticate", challenge))
                    writer.write(Message(f"SIP/2.0 {status} Test", h, body).encode())

                response(invite, 407)
                assert (await read_message(reader)).method == "ACK"
                invite = await read_message(reader)
                seen.append(invite.method)
                expected = digest(
                    challenge,
                    settings().username,
                    settings().password,
                    "INVITE",
                    invite.start.split()[1],
                )
                assert invite.get("Proxy-Authorization") == expected
                if ending in ("busy", "auth-denied"):
                    response(invite, 486 if ending == "busy" else 407)
                    assert (await read_message(reader)).method == "ACK"
                    finished.set_result(True)
                    return
                response(invite, 100)
                response(invite, 180)
                ringing.set()
                if ending in ("cancel-ringing", "race-answer"):
                    cancel = await read_message(reader)
                    seen.append(cancel.method)
                    assert cancel.method == "CANCEL"
                    assert cancel.get("Via") == invite.get("Via")
                    response(cancel, 200)
                    if ending == "cancel-ringing":
                        response(invite, 487)
                        assert (await read_message(reader)).method == "ACK"
                        finished.set_result(True)
                        return
                peer_key = answer(invite.body).key
                udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: Echo(peer_key), local_addr=("127.0.0.1", 0)
                )
                sdp = offer("127.0.0.1", udp.get_extra_info("sockname")[1], master, 456)
                response(invite, 200, sdp)
                assert (await read_message(reader)).method == "ACK"
                if ending == "remote":
                    await media_seen.wait()
                    headers = [
                        ("Via", "SIP/2.0/TLS 127.0.0.1;branch=z9hG4bKpeer"),
                        ("From", invite.get("To") + ";tag=peer"),
                        ("To", invite.get("From")),
                        ("Call-ID", invite.get("Call-ID")),
                        ("CSeq", "19 BYE"),
                    ]
                    writer.write(Message("BYE sip:badge SIP/2.0", headers).encode())
                    assert (await read_message(reader)).status == 200
                else:
                    bye = await read_message(reader)
                    seen.append(bye.method)
                    assert bye.method == "BYE"
                    response(bye, 200)
                finished.set_result(True)
            except BaseException as e:
                if not finished.done():
                    finished.set_exception(e)
            finally:
                writer.close()
                await writer.wait_closed()

        def accept(reader, writer):
            peer_tasks.append(asyncio.create_task(server(reader, writer)))

        listener = await asyncio.start_server(accept, "127.0.0.1", 0)
        real_open = asyncio.open_connection

        async def open_connection(host, port, **kwargs):
            assert host == settings().domain and port == 5061
            assert kwargs["ssl"].check_hostname
            return await real_open("127.0.0.1", listener.sockets[0].getsockname()[1])

        monkeypatch.setattr(asyncio, "open_connection", open_connection)
        audio = SilenceAudio()
        await audio.start()
        task = asyncio.create_task(
            direct.call_contact(settings(), "edmon", audio, report=lambda _: None)
        )
        try:
            async with asyncio.timeout(5):
                if ending in ("local", "cancel-ringing", "race-answer"):
                    await (media_seen if ending == "local" else ringing).wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                elif ending in ("busy", "auth-denied"):
                    with pytest.raises(RuntimeError, match="rejected"):
                        await task
                else:
                    result = await task
                    assert result["transport"] == "direct-sip"
                    assert result["sent_packets"] >= 5
                    assert result["received_packets"] >= 4
                assert await finished
                assert seen.count("INVITE") == 2  # Digest challenge, never a redial.
        finally:
            task.cancel()
            with suppress(BaseException):
                await task
            listener.close()
            await listener.wait_closed()
            for peer in peer_tasks:
                peer.cancel()
            await asyncio.gather(*peer_tasks, return_exceptions=True)
            if udp:
                udp.close()

    asyncio.run(run())
