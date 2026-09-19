import asyncio
import struct

import pytest

from commbadge.phone.sip import Message, answer, digest, offer, read_message
from commbadge.phone.srtp import Srtp, aes_ctr, derive

MASTER = bytes.fromhex("E1F97A0D3E018BE0D64FA32C06DE41390EC675AD498AFEEBB6960B3AABE6")


def test_rfc3711_key_derivation_vectors():
    assert derive(MASTER, 0, 16).hex() == "c61e7a93744f39ee10734afe3ff7a087"
    assert derive(MASTER, 1, 20).hex() == "cebe321f6ff7716b6fd4ab49af256a156d38baa4"
    assert derive(MASTER, 2, 14).hex() == "30cbbc08863d8c85d49db34a9ae1"


def test_nist_aes_ctr_vector():
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    assert aes_ctr(key, iv, plain).hex() == "874d6191b620e3261bef6864990db6ce"


def rtp(seq):
    return struct.pack("!BBHII", 128, 0, seq, seq * 160, 123) + b"\xff" * 160


def test_srtp_auth_replay_reordering_and_rollover():
    sender, receiver = Srtp(MASTER), Srtp(MASTER)
    packets = [sender.protect(rtp(n)) for n in (65533, 65534, 65535, 0, 1)]
    assert receiver.unprotect(packets[0]) == rtp(65533)
    damaged = bytearray(packets[1])
    damaged[15] ^= 1
    with pytest.raises(ValueError, match="authentication"):
        receiver.unprotect(bytes(damaged))
    # An unauthenticated packet must not advance the replay window.
    assert receiver.highest == 65533
    assert receiver.unprotect(packets[2]) == rtp(65535)
    assert receiver.unprotect(packets[1]) == rtp(65534)
    assert receiver.unprotect(packets[3]) == rtp(0)
    assert receiver.highest == 65536
    with pytest.raises(ValueError, match="Replayed"):
        receiver.unprotect(packets[1])
    assert receiver.unprotect(packets[4]) == rtp(1)
    with pytest.raises(ValueError):
        sender.protect(rtp(1))


def test_rfc2617_digest_vector_and_unsupported_challenges():
    challenge = (
        'Digest realm="testrealm@host.com", qop="auth,auth-int", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
    )
    result = digest(
        challenge, "Mufasa", "Circle Of Life", "GET", "/dir/index.html", cnonce="0a4f113b"
    )
    assert 'response="6629fae49393a05397450978507c4ef1"' in result
    with pytest.raises(ValueError):
        digest(challenge.replace("auth,auth-int", "auth-int"), "user", "secret", "INVITE", "sip:x")


def test_sdp_requires_secure_pcmu_and_valid_sdes_key():
    sdp = offer("127.0.0.1", 12345, MASTER, 123)
    media = answer(sdp)
    assert (media.ip, media.port, media.key) == ("127.0.0.1", 12345, MASTER)
    for broken in (
        sdp.replace(b"RTP/SAVP", b"RTP/AVP"),
        sdp.replace(b" 12345 ", b" 0 "),
        sdp.replace(b"a=crypto:1", b"a=crypto:2"),
        sdp.replace(b"sendrecv", b"sendonly"),
    ):
        with pytest.raises(ValueError):
            answer(broken)


def test_stream_parser_preserves_coalesced_messages_and_fragmented_body():
    async def run():
        one = Message("SIP/2.0 200 OK", [("Via", "one"), ("Via", "two")], b"v=0\r\n")
        two = Message("BYE sip:x SIP/2.0", [("CSeq", "2 BYE")])
        reader = asyncio.StreamReader()
        data = one.encode() + two.encode()

        async def feed():
            for i in range(0, len(data), 3):
                reader.feed_data(data[i : i + 3])
                await asyncio.sleep(0)
            reader.feed_eof()

        task = asyncio.create_task(feed())
        result = await read_message(reader)
        assert result.status == 200 and result.all("Via") == ["one", "two"]
        assert result.body == one.body
        assert (await read_message(reader)).method == "BYE"
        await task

    asyncio.run(run())


@pytest.mark.parametrize("length", ["-1", "32769", "1\r\nContent-Length: 2"])
def test_sip_parser_rejects_ambiguous_or_large_lengths(length):
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(f"SIP/2.0 200 OK\r\nContent-Length: {length}\r\n\r\n".encode())
        reader.feed_eof()
        with pytest.raises(ValueError):
            await read_message(reader)

    asyncio.run(run())


def test_direct_settings_do_not_need_relay_or_account_credentials(tmp_path, monkeypatch):
    from commbadge.phone.client import contacts
    from commbadge.phone.config import PhoneSettings, SipSettings

    for key in (
        "CALL_TRANSPORT",
        "CALL_SIP_DOMAIN",
        "CALL_SIP_USERNAME",
        "CALL_SIP_PASSWORD",
        "CALL_CONTACTS",
        "TWILIO_FROM_NUMBER",
        "CALL_MAX_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "CALL_TRANSPORT=sip\nCALL_SIP_DOMAIN=badge.pstn.twilio.com\n"
        "CALL_SIP_USERNAME=badge\nCALL_SIP_PASSWORD=secret-password\n"
        "TWILIO_FROM_NUMBER=+14165550100\n"
        'CALL_CONTACTS={"Edmon":"+14165550101"}\n'
    )
    config = PhoneSettings.load(path)
    assert isinstance(config, SipSettings)
    assert asyncio.run(contacts(config)) == ["edmon"]
    assert "secret-password" not in repr(config) and "+14165550101" not in repr(config)
    path.write_text(path.read_text().replace("badge.pstn.twilio.com", "example.com"))
    with pytest.raises(ValueError, match="Twilio"):
        PhoneSettings.load(path)


def test_dialog_tags_and_combined_record_routes():
    from commbadge.phone.sip import routes, tag

    assert tag("<sip:peer@example.com>;tag=abcd") == "abcd"
    assert tag("<sip:peer@example.com>;tag=abc") != "abcd"
    assert routes(
        ['"One, proxy" <sip:one.example;lr>, <sip:two.example;lr>', "<sip:three.example;lr>"]
    ) == ["<sip:three.example;lr>", "<sip:two.example;lr>", '"One, proxy" <sip:one.example;lr>']
