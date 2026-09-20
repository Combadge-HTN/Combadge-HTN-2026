"""Local ringback for SIP provisional responses, without microphone capture."""

import asyncio
import math
import struct

from combadge.audio import FRAME_BYTES, RATE


def ring_frame(index):
    """Quiet 440/480 Hz ringback: two seconds on, four seconds off."""
    if index % 300 >= 100:
        return bytes(FRAME_BYTES)
    samples = []
    for offset in range(FRAME_BYTES // 2):
        sample = (index * (FRAME_BYTES // 2) + offset) % (RATE * 6)
        # Five-millisecond fades prevent clicks at the cadence edges.
        envelope = min(1.0, sample / 120, (RATE * 2 - 1 - sample) / 120)
        value = 1800 * envelope * sum(math.sin(2 * math.pi * f * sample / RATE) for f in (440, 480))
        samples.append(round(value))
    return struct.pack(f"<{len(samples)}h", *samples)


async def invite_with_ringback(call, audio, *, enabled):
    start_playback = getattr(audio, "start_playback", None)
    if not enabled or start_playback is None:
        return await call.invite()
    ringing = asyncio.Event()
    call.on_ringing = ringing.set

    async def play():
        await ringing.wait()
        await start_playback()
        index = 0
        while True:
            await audio.write(ring_frame(index))
            index += 1
            # Pace even when the helper accepts writes faster than real time.
            await asyncio.sleep(0.02)

    invite = asyncio.create_task(call.invite())
    player = asyncio.create_task(play())
    try:
        done, _ = await asyncio.wait((invite, player), return_when=asyncio.FIRST_COMPLETED)
        if player in done:
            player.result()  # Surface playback failure and cancel the pending call.
        return await invite
    finally:
        call.on_ringing = None
        for task in (invite, player):
            task.cancel()
        await asyncio.gather(invite, player, return_exceptions=True)
