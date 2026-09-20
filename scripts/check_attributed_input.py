"""Paid Live test: context acknowledgment precedes each synthetic microphone turn.

Tests first-turn attribution, an immediate new identity on a later turn, and
unknown after a named speaker, within one session. No microphone is opened.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from check_speaker_context import question_audio
from websockets.asyncio.client import connect

from combadge.audio import FRAME_BYTES, RATE
from combadge.config import load_settings
from combadge.live import LIVE_URL, LiveConnection, run_session
from combadge.speaker_input import INPUT_INSTRUCTIONS, AttributedAudio, ContextBeforeAudio


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings(args.env_file)
    pcm = await asyncio.to_thread(question_audio, settings, "Computer, who is speaking right now?")
    gate = ContextBeforeAudio()
    timeline = []
    labels = ["Edmon", "Samuel", None]
    answers = ["", "", ""]

    class Audio:
        def __init__(self):
            self.turn = -1
            self.frame = 0
            self.pending = None
            self.offset = len(pcm)
            self.next_turn = 1.0
            self.connection = None

        async def start(self):
            self.started = time.monotonic()

        async def read(self):
            await asyncio.sleep(max(0, self.started + self.frame * 0.02 - time.monotonic()))
            self.frame += 1
            elapsed = time.monotonic() - self.started
            if (
                self.turn < 2
                and elapsed >= self.next_turn
                and self.pending is None
                and self.offset >= len(pcm)
            ):
                self.turn += 1
                chunk = AttributedAudio(0, len(pcm) // 2, labels[self.turn], pcm)
                self.pending = asyncio.create_task(gate.prepare(self.connection, [chunk]))
                timeline.append({"at": round(elapsed, 2), "context_sent_for": labels[self.turn]})
            if self.pending is not None:
                if not self.pending.done():
                    return bytes(FRAME_BYTES)
                self.pending.result()
                self.pending = None
                self.offset = 0
                self.next_turn = elapsed + len(pcm) / (RATE * 2) + 12
                timeline.append({"at": round(elapsed, 2), "audio_released_for": labels[self.turn]})
            frame = pcm[self.offset : self.offset + FRAME_BYTES]
            self.offset += len(frame)
            return frame.ljust(FRAME_BYTES, b"\0")

        async def write(self, data):
            pass

        async def close(self):
            if self.pending is not None:
                self.pending.cancel()
                await asyncio.gather(self.pending, return_exceptions=True)

    audio = Audio()

    class Connection(LiveConnection):
        async def send(self, message):
            if message["type"] == "session.start":
                message["session"]["instructions"] += INPUT_INSTRUCTIONS
            await super().send(message)

        async def recv(self):
            event = await super().recv()
            gate.observe(event)
            if event.type == "session.output_transcript.delta":
                timeline.append(
                    {"at": round(time.monotonic() - audio.started, 2), "said": event.delta}
                )
                if audio.turn >= 0:
                    answers[audio.turn] += event.delta
            return event

    async with connect(
        LIVE_URL,
        additional_headers={"Authorization": "Bearer " + settings.openai_api_key},
        close_timeout=0.25,
        max_size=1_048_576,
    ) as ws:
        connection = Connection(ws)
        audio.connection = connection
        stats = await run_session(
            connection, audio, settings, asyncio.Event(), seconds=58, report=lambda _: None
        )
    checks = [
        "edmon" in answers[0].lower() and "samuel" not in answers[0].lower(),
        "samuel" in answers[1].lower() and "edmon" not in answers[1].lower(),
        bool(answers[2].strip()) and not any(n in answers[2].lower() for n in ["edmon", "samuel"]),
    ]
    print(
        json.dumps(
            {
                "answers": answers,
                "checks": checks,
                "timeline": timeline,
                "finalized": stats.finalized,
            }
        ),
        flush=True,
    )
    return 0 if all(checks) and stats.finalized else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
