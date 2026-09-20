"""Paid end-to-end Speechmatics/Live replay, without opening audio devices.

Supply consented enrollment WAVs with --speaker NAME=PATH and separate mono
24 kHz PCM16 test WAVs with --clip NAME=PATH (or unknown=PATH). Clips play in
order in one session. No identity labels are scripted into either service.
The report checks observed attribution against the clip schedule and prints
actual replies for semantic review; label checks alone do not validate replies.
"""

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

from websockets.asyncio.client import connect

from combadge.audio import FRAME_BYTES, RATE
from combadge.config import load_settings
from combadge.live import LIVE_URL, LiveConnection, run_session
from combadge.speakers import load_references
from combadge.speechmatics import StreamingSpeakerInput


def load_clip(spec):
    name, separator, filename = spec.partition("=")
    if not separator or not name:
        raise ValueError("Expected NAME=PATH or unknown=PATH")
    with wave.open(filename, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE):
            raise ValueError("Test clips must be mono PCM16 WAV at 24 kHz")
        if not 0 < wav.getnframes() <= 120 * RATE:
            raise ValueError("Each test clip must contain 0–120 seconds of audio")
        pcm = wav.readframes(wav.getnframes())
    return name, pcm


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--speaker", action="append", required=True)
    parser.add_argument("--clip", action="append", required=True)
    parser.add_argument("--gap", type=float, default=6)
    args = parser.parse_args()
    if not 0 <= args.gap <= 30:
        parser.error("--gap must be between 0 and 30 seconds")
    settings = load_settings(args.env_file)
    if not settings.speechmatics_api_key:
        parser.error("SPEECHMATICS_API_KEY is required")
    references = load_references(args.speaker)
    names = {r.name for r in references}
    pcm = bytearray()
    clips = []
    for spec in args.clip:
        name, data = load_clip(spec)
        if name not in names | {"unknown"}:
            parser.error("Clip names must be enrolled or unknown")
        if clips:
            pcm.extend(bytes(round(args.gap * RATE) * 2))
        start = len(pcm) // 2
        pcm.extend(data)
        clips.append({"expected": name, "start": start, "end": len(pcm) // 2})
    duration = len(pcm) / (RATE * 2) + StreamingSpeakerInput.DELAY / RATE + 12
    pipeline = StreamingSpeakerInput(settings.speechmatics_api_key, references)
    contexts, replies = [], []

    class Audio:
        async def start(self):
            self.started = time.monotonic()
            self.offset = 0

        async def read(self):
            await asyncio.sleep(max(0, self.started + self.offset / (RATE * 2) - time.monotonic()))
            frame = bytes(pcm[self.offset : self.offset + FRAME_BYTES])
            self.offset += FRAME_BYTES
            return frame.ljust(FRAME_BYTES, b"\0")

        async def write(self, data):
            pass

        async def close(self):
            pass

    audio = Audio()

    class Connection(LiveConnection):
        async def send(self, message):
            if message["type"] == "session.thinking.append":
                content = message["content"]
                payload = json.loads(content[content.index('{"block":') :])
                contexts.append(payload)
            await super().send(message)

        async def recv(self):
            event = await super().recv()
            if event.type == "session.output_transcript.delta":
                replies.append(
                    {"at": round(time.monotonic() - audio.started, 2), "said": event.delta}
                )
            return event

    async with connect(
        LIVE_URL,
        additional_headers={"Authorization": "Bearer " + settings.openai_api_key},
        close_timeout=0.25,
        max_size=1_048_576,
    ) as ws:
        stats = await run_session(
            Connection(ws),
            audio,
            settings,
            asyncio.Event(),
            seconds=duration,
            speaker_input=pipeline,
            report=lambda text: print(text, end="", flush=True),
        )
    results = []
    for clip in clips:
        observed = set()
        for context in contexts:
            base = round(context["live_input_start_seconds"] * RATE) - pipeline.DELAY
            for start, end, name in context["intervals"]:
                if base + round(start * RATE) < clip["end"] and (
                    base + round(end * RATE) > clip["start"]
                ):
                    observed.add(name)
        allowed = {clip["expected"], "unknown"}
        results.append(
            {
                "expected": clip["expected"],
                "observed": sorted(observed),
                "no_wrong_name": bool(observed) and observed <= allowed,
                "matched": clip["expected"] in observed,
                "start": clip["start"] / RATE,
                "end": clip["end"] / RATE,
            }
        )
    print(
        "\nREPLAY_RESULT "
        + json.dumps(
            {
                "clips": results,
                "replies": replies,
                "finalized": stats.finalized,
                "semantic_review_required": True,
            }
        ),
        flush=True,
    )
    return 0 if stats.finalized and all(r["no_wrong_name"] and r["matched"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
