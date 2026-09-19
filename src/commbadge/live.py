"""GPT-Live voice loop with bounded audio buffers and explicit session finalization."""

import asyncio
import base64
import binascii
import json
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from commbadge.audio import FRAME_BYTES, RATE, AlsaAudio, AudioIO, CommandAudio, SilenceAudio
from commbadge.config import Settings

Report = Callable[[str], None]
LIVE_URL = "wss://api.openai.com/v1/live/sessions"
PROMPT = (
    "You are the AI in a wearable communicator badge. Speak in brief, natural English. "
    "Listen to corrections and interruptions. Delegate reasoning questions to the backend. "
    "You currently have no external action tools; never claim to have browsed, purchased, "
    "sent messages, or changed anything."
)


@dataclass
class LiveStats:
    sent_bytes: int = 0
    received_bytes: int = 0
    speech_bytes: int = 0
    finalized: bool = False
    close_reason: str = ""


class LiveConnection:
    """Small JSON transport using the documented Live wire protocol.

    websockets ships a pure-Python wheel, avoiding the SDK's mandatory native
    dependencies on QNX. TLS and the event loop still need target verification.
    """

    def __init__(self, websocket):
        self.websocket = websocket

    async def send(self, event: dict) -> None:
        await self.websocket.send(json.dumps(event))

    async def recv(self):
        data = json.loads(
            await self.websocket.recv(), object_hook=lambda obj: SimpleNamespace(**obj)
        )
        if not isinstance(data, SimpleNamespace) or not isinstance(
            getattr(data, "type", None), str
        ):
            raise RuntimeError("Invalid GPT-Live event.")
        return data


def session_config(settings: Settings) -> dict:
    return {
        "model": settings.live_model,
        "instructions": PROMPT,
        "store": False,
        "audio": {
            "format": {"type": "audio/pcm", "rate": RATE},
            "output": {"voice": settings.live_voice},
        },
        "delegation": {
            "type": "responses",
            "responses": {
                "model": settings.backend_model,
                "instructions": (
                    "Answer concisely. You have no external action tools. Be honest about that."
                ),
            },
        },
    }


def check_error(event) -> None:
    if event.type == "error":
        raise RuntimeError(f"GPT-Live error ({event.error.code}): {event.error.message}")
    if event.type == "response.event":
        nested = event.event
        if getattr(nested, "type", None) == "response.failed":
            raise RuntimeError("The delegated backend failed. Check OPENAI_BACKEND_MODEL access.")


async def finalize(connection, stats: LiveStats, timeout: float) -> None:
    """There must be exactly one event reader; streaming tasks stop before this runs."""
    if stats.finalized:
        return
    async with asyncio.timeout(timeout):
        await connection.send({"type": "session.close"})
        while True:
            event = await connection.recv()
            if event.type == "session.closed":
                stats.finalized = True
                stats.close_reason = event.reason
                return
            # Late audio and errors are ignored while draining to the final acknowledgment.


async def run_session(
    connection,
    audio: AudioIO,
    settings: Settings,
    stop: asyncio.Event,
    *,
    check: bool = False,
    seconds: float = 300,
    captions: bool = True,
    report: Report = print,
    startup_timeout: float = 20,
    close_timeout: float = 15,
) -> LiveStats:
    """Run a connected session; injectable audio/connection enable hardware-free tests."""
    stats = LiveStats()
    tasks: list[asyncio.Task] = []
    start_sent = False
    last_speaker = ""
    playback: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)  # <= 2 seconds

    async def send_audio() -> None:
        while True:
            data = await audio.read()
            if not data or len(data) % 2:
                raise RuntimeError("Capture must provide non-empty PCM16 frames.")
            await connection.send(
                {
                    "type": "session.input_audio.append",
                    "audio": base64.b64encode(data).decode("ascii"),
                }
            )
            stats.sent_bytes += len(data)

    async def play_audio() -> None:
        while True:
            await audio.write(await playback.get())

    async def receive() -> None:
        nonlocal last_speaker
        while True:
            event = await connection.recv()
            check_error(event)
            if event.type == "session.output_audio.delta":
                try:
                    data = base64.b64decode(event.delta, validate=True)
                except (ValueError, binascii.Error) as error:
                    raise RuntimeError("GPT-Live returned invalid audio encoding.") from error
                if len(data) % 2:
                    raise RuntimeError("GPT-Live returned an incomplete PCM16 sample.")
                stats.received_bytes += len(data)
                if any(data):
                    stats.speech_bytes += len(data)
                if check:
                    if stats.speech_bytes >= FRAME_BYTES * 5:
                        stop.set()
                else:
                    for offset in range(0, len(data), FRAME_BYTES):
                        try:
                            playback.put_nowait(data[offset : offset + FRAME_BYTES])
                        except asyncio.QueueFull as error:
                            raise RuntimeError(
                                "Playback fell behind; stopping to avoid stale speech."
                            ) from error
            elif event.type in (
                "session.input_transcript.delta",
                "session.output_transcript.delta",
            ):
                if captions:
                    speaker = "You" if event.type == "session.input_transcript.delta" else "Badge"
                    if speaker != last_speaker:
                        report(f"\n{speaker}: ")
                        last_speaker = speaker
                    report(event.delta)
            elif event.type == "session.closed":
                stats.finalized = True
                stats.close_reason = event.reason
                return

    try:
        async with asyncio.timeout(startup_timeout):
            await connection.send({"type": "session.start", "session": session_config(settings)})
            start_sent = True
            while True:
                event = await connection.recv()
                check_error(event)
                if event.type == "session.started":
                    break
                if event.type == "session.closed":
                    stats.finalized = True
                    stats.close_reason = event.reason
                    raise RuntimeError("GPT-Live closed before the session was ready.")
        if stop.is_set():
            return stats
        await audio.start()
        report(
            "\nGPT-Live connected. "
            + (
                "Checking generated audio…\n"
                if check
                else "Microphone is live; speak naturally. Ctrl+C stops.\n"
            )
        )
        tasks = [
            asyncio.create_task(send_audio()),
            asyncio.create_task(receive()),
            asyncio.create_task(play_audio()),
            asyncio.create_task(stop.wait()),
            asyncio.create_task(asyncio.sleep(seconds)),
        ]
        # A greeting makes the connection observable; silence still streams in check mode.
        await connection.send(
            {
                "type": "session.instructions.append",
                "delegation_id": None,
                "content": (
                    "Greet the caller now in English. Say 'Badge ready.' Then pause and listen."
                ),
            }
        )
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # Propagate capture, playback, protocol, and network failures.
        if check and stats.speech_bytes < FRAME_BYTES * 5:
            raise RuntimeError("No usable generated audio arrived before the check ended.")
        if stats.finalized and stats.close_reason != "close_requested":
            raise RuntimeError(f"GPT-Live ended the session: {stats.close_reason}")
    finally:
        failure_in_flight = sys.exc_info()[0] is not None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await audio.close()
        finally:
            if start_sent:
                try:
                    await finalize(connection, stats, close_timeout)
                except Exception:
                    if not failure_in_flight:
                        raise RuntimeError(
                            "Session finalization was not confirmed by the server."
                        ) from None
                    report("\nWarning: server session finalization was not confirmed.\n")
    report(
        f"\nSession closed ({stats.close_reason}). "
        f"Sent {stats.sent_bytes} audio bytes; received {stats.received_bytes}.\n"
    )
    if stats.close_reason != "close_requested":
        raise RuntimeError(f"GPT-Live ended the session: {stats.close_reason}")
    return stats


async def connect_voice(
    settings: Settings,
    *,
    check: bool,
    input_device: str,
    output_device: str,
    seconds: float,
    captions: bool,
    capture_command: list[str] | None = None,
    playback_command: list[str] | None = None,
) -> LiveStats:
    # Lazy import keeps the base package usable without the voice extra.
    from websockets.asyncio.client import connect

    audio: AudioIO
    if check:
        audio = SilenceAudio()
    elif capture_command is not None and playback_command is not None:
        audio = CommandAudio(capture_command, playback_command)
        audio.preflight()
    else:
        audio = AlsaAudio(input_device, output_device)
        audio.preflight()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    try:
        async with connect(
            LIVE_URL,
            additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            open_timeout=15,
            close_timeout=5,
            max_size=1_048_576,
            max_queue=16,
        ) as websocket:
            return await run_session(
                LiveConnection(websocket),
                audio,
                settings,
                stop,
                check=check,
                seconds=seconds,
                captions=captions,
                report=lambda text: print(text, end="", flush=True),
            )
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, previous)
