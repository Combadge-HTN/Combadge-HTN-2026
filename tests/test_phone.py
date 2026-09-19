import asyncio
import base64
import hashlib
import hmac
import json
import math
import struct
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from commbadge.audio import FRAME_BYTES, SilenceAudio
from commbadge.config import Settings
from commbadge.live import session_config
from commbadge.phone.carrier import TwilioCarrier, stream_twiml, valid_signature
from commbadge.phone.client import CallAudioRouter, call_contact
from commbadge.phone.codec import PhoneCodec, decode_sample, encode_sample
from commbadge.phone.config import PhoneSettings, RelaySettings
from commbadge.phone.relay import PhoneRelay


def settings():
    return RelaySettings(
        "https://relay.example",
        "secret-token" * 4,
        "AC" + "a" * 32,
        "auth-secret",
        "+14165550100",
        {"alex": "+14165550101"},
        30,
    )


def signature(config, path):
    return base64.b64encode(
        hmac.new(
            config.auth_token.encode(), (config.public_url + path).encode(), hashlib.sha1
        ).digest()
    ).decode()


def test_codec_vectors_and_streaming_boundaries():
    # ITU G.711 extrema, silence, and known quantization values.
    assert [decode_sample(x) for x in [0, 128, 127, 255, 206, 78]] == [
        -32124,
        32124,
        0,
        0,
        988,
        -988,
    ]
    assert [encode_sample(x) for x in [0, -1, 1000, -1000, 32767, -32768]] == [
        255,
        126,
        206,
        78,
        128,
        0,
    ]
    pcm = struct.pack(
        "<2400h", *(round(8000 * math.sin(2 * math.pi * 800 * i / 24000)) for i in range(2400))
    )
    continuous = PhoneCodec().encode(pcm)
    fragmented = PhoneCodec()
    assert continuous == b"".join(
        fragmented.encode(pcm[i : i + 14]) for i in range(0, len(pcm), 14)
    )
    assert len(continuous) == 800
    assert len(PhoneCodec().decode(continuous)) == len(pcm)
    decoder = PhoneCodec()
    assert PhoneCodec().decode(continuous) == b"".join(
        decoder.decode(continuous[i : i + 7]) for i in range(0, 800, 7)
    )
    with pytest.raises(ValueError):
        PhoneCodec().encode(b"\0")


def test_downsampling_rejects_above_nyquist():
    def energy(frequency):
        pcm = struct.pack(
            "<4800h",
            *(round(10000 * math.sin(2 * math.pi * frequency * i / 24000)) for i in range(4800)),
        )
        output = PhoneCodec().encode(pcm)[100:]
        return sum(decode_sample(v) ** 2 for v in output) / len(output)

    assert energy(6000) < energy(1000) / 1000


def test_signature_cannot_be_reused_for_other_path_or_key():
    config = settings()
    signed = signature(config, "/media/abc")
    assert valid_signature(config.auth_token, config.public_url, "/media/abc", signed)
    assert not valid_signature(config.auth_token, config.public_url, "/media/other", signed)
    assert not valid_signature("wrong", config.public_url, "/media/abc", signed)
    assert "<Hangup" in stream_twiml("wss://relay.example/media/abc")


def test_invalid_config_and_secret_repr(tmp_path, monkeypatch):
    for key in (
        "CALL_RELAY_TOKEN",
        "CALL_RELAY_URL",
        "CALL_PUBLIC_URL",
        "CALL_CONTACTS",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "CALL_MAX_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("CALL_RELAY_URL=ws://remote.example\nCALL_RELAY_TOKEN=" + "x" * 32)
    with pytest.raises(ValueError):
        PhoneSettings.load(env)
    env.write_text("CALL_RELAY_URL=wss://relay.example\nCALL_RELAY_TOKEN=" + "x" * 32)
    assert "x" * 32 not in repr(PhoneSettings.load(env))
    assert settings().auth_token not in repr(settings())
    env.write_text(
        "CALL_PUBLIC_URL=https://relay.example\nCALL_RELAY_TOKEN="
        + "x" * 32
        + "\nTWILIO_ACCOUNT_SID=AC"
        + "a" * 32
        + "\nTWILIO_AUTH_TOKEN=secret\n"
        'TWILIO_FROM_NUMBER=+14165550100\nCALL_CONTACTS={"Alex":"+14165550101"}'
    )
    assert RelaySettings.load(env).contacts == {"alex": "+14165550101"}
    env.write_text(env.read_text().replace("+14165550101", "911"))
    with pytest.raises(ValueError):
        RelaySettings.load(env)


class FakeCarrier:
    def __init__(self):
        self.created = asyncio.Event()
        self.ended = asyncio.Event()
        self.urls = []
        self.status_value = "ringing"
        self.sid = "CA" + "b" * 32

    async def create(self, number, url):
        self.urls.append(url)
        self.created.set()
        return self.sid

    async def status(self, sid):
        assert sid == self.sid
        return self.status_value

    async def hangup(self, sid):
        assert sid == self.sid
        self.ended.set()


def start_event(config, sid):
    return {
        "event": "start",
        "start": {
            "accountSid": config.account_sid,
            "callSid": sid,
            "streamSid": "MZ123",
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }


async def recv_kind(ws, kind):
    async with asyncio.timeout(3):
        while True:
            message = await ws.recv()
            if isinstance(message, str) and json.loads(message).get("type") == kind:
                return json.loads(message)


def test_real_websocket_bridge_auth_audio_and_disconnect():
    async def scenario():
        config, carrier = settings(), FakeCarrier()
        relay = PhoneRelay(config, carrier, poll_seconds=0.01)
        async with serve(relay.handle, "127.0.0.1", 0, process_request=relay.authorize) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            with pytest.raises(InvalidStatus):
                async with connect(url + "/badge"):
                    pass
            headers = {"Authorization": f"Bearer {config.token}"}
            async with connect(url + "/badge", additional_headers=headers) as badge:
                await recv_kind(badge, "contacts")
                await badge.send(json.dumps({"type": "call", "contact": "Alex"}))
                await asyncio.wait_for(carrier.created.wait(), 2)
                path = urlsplit(carrier.urls[0]).path
                with pytest.raises(InvalidStatus):
                    async with connect(url + path):
                        pass
                async with connect(
                    url + path, additional_headers={"X-Twilio-Signature": signature(config, path)}
                ) as media:
                    await media.send(json.dumps(start_event(config, carrier.sid)))
                    while (await recv_kind(badge, "status"))["status"] != "connected":
                        pass
                    await badge.send(struct.pack("<480h", *([1000] * 480)))
                    outbound = json.loads(await asyncio.wait_for(media.recv(), 2))
                    assert outbound["streamSid"] == "MZ123"
                    assert len(base64.b64decode(outbound["media"]["payload"])) == 160
                    await media.send(
                        json.dumps(
                            {
                                "event": "media",
                                "streamSid": "MZ123",
                                "media": {"payload": base64.b64encode(bytes([206]) * 160).decode()},
                            }
                        )
                    )
                    async with asyncio.timeout(2):
                        raw = await badge.recv()
                        while isinstance(raw, str):
                            raw = await badge.recv()
                    assert len(raw) == FRAME_BYTES and any(raw)
                    # A concurrent authorized client must not place a second call.
                    async with connect(url + "/badge", additional_headers=headers) as other:
                        await recv_kind(other, "contacts")
                        await other.send(json.dumps({"type": "call", "contact": "alex"}))
                        assert "already active" in (await recv_kind(other, "error"))["message"]
                    await badge.send(json.dumps({"type": "hangup"}))
                    await recv_kind(badge, "ended")
                await asyncio.wait_for(carrier.ended.wait(), 2)
                assert len(carrier.urls) == 1 and relay.call is None

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["unknown", "busy", "disconnect", "bad-frame", "bad-stream"])
def test_failure_paths_do_not_leak_calls(mode):
    async def scenario():
        config, carrier = settings(), FakeCarrier()
        relay = PhoneRelay(config, carrier, poll_seconds=0.01)
        async with serve(relay.handle, "127.0.0.1", 0, process_request=relay.authorize) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(
                url + "/badge", additional_headers={"Authorization": f"Bearer {config.token}"}
            ) as badge:
                await recv_kind(badge, "contacts")
                await badge.send(
                    json.dumps(
                        {"type": "call", "contact": "missing" if mode == "unknown" else "alex"}
                    )
                )
                if mode == "unknown":
                    await recv_kind(badge, "error")
                    assert not carrier.urls
                    return
                await asyncio.wait_for(carrier.created.wait(), 2)
                if mode == "busy":
                    carrier.status_value = "busy"
                    assert (await recv_kind(badge, "ended"))["status"] == "busy"
                elif mode == "disconnect":
                    await badge.close()
                elif mode == "bad-frame":
                    await badge.send(b"broken")
                    assert (await recv_kind(badge, "ended"))["status"] == "failed"
                else:
                    path = urlsplit(carrier.urls[0]).path
                    async with connect(
                        url + path,
                        additional_headers={"X-Twilio-Signature": signature(config, path)},
                    ) as media:
                        await media.send(json.dumps(start_event(config, "wrong-call")))
                        await recv_kind(badge, "ended")
                await asyncio.wait_for(carrier.ended.wait(), 2)
            assert relay.call is None

    asyncio.run(scenario())


def test_client_routes_audio_and_confirms_hangup():
    async def scenario():
        heard = asyncio.Event()
        ended = asyncio.Event()
        outputs = []

        class Audio(SilenceAudio):
            async def read(self):
                await asyncio.sleep(0.02)
                return bytes([1, 0]) * 480

            async def write(self, frame):
                outputs.append(frame)
                heard.set()

        async def relay(ws):
            await ws.send(json.dumps({"type": "contacts", "names": ["alex"]}))
            assert json.loads(await ws.recv())["contact"] == "alex"
            frame = await ws.recv()
            assert isinstance(frame, bytes) and any(frame)
            await ws.send(frame)
            async for message in ws:
                if isinstance(message, str) and json.loads(message)["type"] == "hangup":
                    await ws.send(json.dumps({"type": "ended", "status": "completed"}))
                    ended.set()
                    return

        async with serve(relay, "127.0.0.1", 0) as server:
            config = PhoneSettings(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "test-token"
            )
            result = await call_contact(config, "Alex", Audio(), seconds=0.1, report=lambda _: None)
            assert result["status"] == "completed" and ended.is_set() and heard.is_set()
            assert outputs == [bytes([1, 0]) * 480]

    asyncio.run(scenario())


def test_audio_router_never_sends_call_speech_to_ai_or_ai_speech_to_call():
    async def scenario():
        class Audio(SilenceAudio):
            writes = []

            async def read(self):
                return b"\x01\0" * 480

            async def write(self, data):
                self.writes.append(data)

        raw = Audio()
        router = CallAudioRouter(raw)
        assert any(await router.read())
        router.active = True
        router.forwarding = True
        assert not any(await router.read())
        assert any(await router.frames.get())
        await router.write(b"AI speech")
        assert raw.writes == []
        router.active = False
        await router.write(b"AI resumed")
        assert raw.writes == [b"AI resumed"]

    asyncio.run(scenario())


def test_call_tool_registered_alongside_snapshots():
    config = session_config(Settings(), snapshots=True, call_names=["alex"])
    tools = config["delegation"]["responses"]["tools"]
    assert {tool["name"] for tool in tools} == {"call_contact", "capture_snapshot"}
    assert tools[1]["parameters"]["properties"]["contact"]["enum"] == ["alex"]
    assert not session_config(Settings())["delegation"]["responses"].get("tools")


def test_carrier_sets_limits_and_never_retries_create(monkeypatch):
    async def scenario():
        carrier = TwilioCarrier(settings())
        sent = []

        def request(suffix="", data=None):
            sent.append(data)
            if len(sent) > 1:
                raise RuntimeError("unexpected retry")
            return {"sid": "CA" + "b" * 32}

        monkeypatch.setattr(carrier, "_request", request)
        await carrier.create("+14165550101", "wss://relay.example/media/abc")
        assert sent[0]["Record"] == "false"
        assert sent[0]["TimeLimit"] == "30"
        assert sent[0]["Timeout"] == "30"
        assert "Connect" in sent[0]["Twiml"]

    asyncio.run(scenario())


def test_cancelled_client_requests_hangup():
    async def scenario():
        requested = asyncio.Event()
        cancelled = asyncio.Event()

        async def relay(ws):
            await ws.send(json.dumps({"type": "contacts", "names": ["alex"]}))
            await ws.recv()
            requested.set()
            async for message in ws:
                if isinstance(message, str) and json.loads(message)["type"] == "hangup":
                    cancelled.set()
                    await ws.send(json.dumps({"type": "ended", "status": "canceled"}))
                    return

        async with serve(relay, "127.0.0.1", 0) as server:
            config = PhoneSettings(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "test-token"
            )
            task = asyncio.create_task(call_contact(config, "alex", SilenceAudio()))
            await asyncio.wait_for(requested.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert cancelled.is_set()

    asyncio.run(scenario())


def test_call_creation_cancellation_recovers_sid_and_ends_call():
    async def scenario():
        config, carrier = settings(), FakeCarrier()
        original = carrier.create
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_create(number, url):
            started.set()
            await release.wait()
            return await original(number, url)

        carrier.create = slow_create
        relay = PhoneRelay(config, carrier)

        class Badge:
            async def recv(self):
                return json.dumps({"type": "call", "contact": "alex"})

            async def send(self, _):
                pass

        task = asyncio.create_task(relay.badge(Badge()))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert carrier.ended.is_set() and relay.call is None

    asyncio.run(scenario())
