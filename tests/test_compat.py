import asyncio
import socket
import sys
from asyncio import selector_events
from collections import deque
from types import SimpleNamespace

import pytest

from combadge import compat


@pytest.mark.parametrize("limit", [-1, 0, None, sys.maxsize + 1])
def test_qnx_invalid_iovec_limit_is_repaired(monkeypatch, limit):
    monkeypatch.setattr(compat, "sys", SimpleNamespace(platform="qnx8", maxsize=sys.maxsize))
    monkeypatch.setattr(selector_events, "SC_IOV_MAX", limit)
    assert compat.configure_asyncio()
    transport = SimpleNamespace(_buffer=deque([b"first", b"second"]))
    assert list(selector_events._SelectorSocketTransport._get_sendmsg_buffer(transport)) == [
        b"first"
    ]
    assert not compat.configure_asyncio()


@pytest.mark.parametrize("platform,limit", [("linux", -1), ("qnx8", 16)])
def test_other_platforms_and_valid_limits_are_preserved(monkeypatch, platform, limit):
    monkeypatch.setattr(compat, "sys", SimpleNamespace(platform=platform, maxsize=sys.maxsize))
    monkeypatch.setattr(selector_events, "SC_IOV_MAX", limit)
    assert not compat.configure_asyncio()
    assert selector_events.SC_IOV_MAX == limit


def test_buffered_sendmsg_preserves_large_payload_under_backpressure(monkeypatch):
    monkeypatch.setattr(compat, "sys", SimpleNamespace(platform="qnx8", maxsize=sys.maxsize))
    monkeypatch.setattr(selector_events, "SC_IOV_MAX", -1)
    compat.configure_asyncio()
    writes = []
    original = selector_events._SelectorSocketTransport._write_sendmsg

    def observed_write(transport):
        writes.append(True)
        return original(transport)

    monkeypatch.setattr(selector_events._SelectorSocketTransport, "_write_sendmsg", observed_write)

    async def scenario():
        left, right = socket.socketpair()
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        right.setblocking(False)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_connection(asyncio.Protocol, sock=left)
        payload = bytes(range(256)) * 4096
        try:
            for offset in range(0, len(payload), 1024):
                transport.write(payload[offset : offset + 1024])
            received = bytearray()
            async with asyncio.timeout(5):
                while len(received) < len(payload):
                    chunk = await loop.sock_recv(right, 8192)
                    assert chunk
                    received.extend(chunk)
            assert received == payload
            assert writes, "Test must exercise the formerly failing buffered sendmsg path"
        finally:
            transport.close()
            right.close()
            await asyncio.sleep(0)

    asyncio.run(scenario())
