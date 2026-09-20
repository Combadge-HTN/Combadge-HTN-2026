"""Paid GPT-Live handoff check with synthetic speech and scripted matcher results.

Run with --env-file PATH. No microphone, speaker, or personal recording is used.
This checks whether spoken answers use matcher results, not recognition accuracy.
Review the printed answers as well as the name-presence checks.
"""

import argparse
import asyncio
import json
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from websockets.asyncio.client import connect

from combadge.audio import FRAME_BYTES
from combadge.config import load_settings
from combadge.live import LIVE_URL, LiveConnection, run_session
from combadge.speakers import BYTES_PER_SECOND, Segment, SpeakerTracker

CASES = {
    "named": ["Edmon"],
    "samuel": ["Samuel"],
    "unknown": ["unknown"],
    "unavailable": [],
    "multiple": ["Edmon", "Samuel"],
    "overlap": ["Edmon", "ambiguous"],
    "named_then_unknown": ["unknown"],
}


def question_audio(settings, question):
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(
            {
                "model": "gpt-4o-mini-tts",
                "voice": "coral",
                "input": question,
                "response_format": "pcm",
            }
        ).encode(),
        headers={
            "Authorization": "Bearer " + settings.openai_api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(2_000_000)


class SyntheticAudio:
    def __init__(self, question, delay):
        # A tiny DC value allows the scripted matcher to exercise real feed/window
        # scheduling without inventing audible speech. Start the question before
        # the first background window to exercise the first-turn race.
        self.pcm = b"\x01\x00" * (int(BYTES_PER_SECOND // 2 * delay)) + question
        self.offset = 0

    async def start(self):
        self.started = time.monotonic()

    async def read(self):
        await asyncio.sleep(
            max(0, self.started + self.offset / BYTES_PER_SECOND - time.monotonic())
        )
        frame = self.pcm[self.offset : self.offset + FRAME_BYTES]
        self.offset += FRAME_BYTES
        return frame.ljust(FRAME_BYTES, b"\0")

    async def write(self, data):
        pass

    async def close(self):
        pass


async def check(settings, question, name, labels, delay, recognition_delay):
    requests = 0

    async def analyze(pcm):
        nonlocal requests
        current = ["Edmon"] if name == "named_then_unknown" and requests == 0 else labels
        requests += 1
        await asyncio.sleep(recognition_delay)
        if name == "unavailable":
            raise RuntimeError("Scripted recognition failure")
        width = len(pcm) / BYTES_PER_SECOND / len(current)
        return [Segment(i * width, (i + 1) * width, label) for i, label in enumerate(current)]

    tracker = SpeakerTracker(SimpleNamespace(analyze=analyze))
    answers = []
    heard = []
    audio = SyntheticAudio(question, delay)
    lookups = []
    timeline = []
    started = time.monotonic()

    class ObservedConnection(LiveConnection):
        async def send(self, message):
            item = message.get("item", {})
            if item.get("type") == "function_call_output":
                result = json.loads(item["output"])
                lookups.append(result)
                timeline.append({"at": round(time.monotonic() - started, 2), "result": result})
            await super().send(message)

        async def recv(self):
            event = await super().recv()
            if (
                event.type == "session.output_transcript.delta"
                and audio.offset >= delay * BYTES_PER_SECOND
            ):
                answers.append(event.delta)
                timeline.append({"at": round(time.monotonic() - started, 2), "said": event.delta})
            if event.type == "session.input_transcript.delta":
                heard.append(event.delta)
            return event

    async with connect(
        LIVE_URL,
        additional_headers={"Authorization": "Bearer " + settings.openai_api_key},
        open_timeout=15,
        close_timeout=0.25,
        max_size=1_048_576,
    ) as websocket:
        stats = await run_session(
            ObservedConnection(websocket),
            audio,
            settings,
            asyncio.Event(),
            seconds=30,
            report=lambda text: None,
            speaker_tracker=tracker,
        )
    answer = "".join(answers)
    named = {person for person in ("Edmon", "Samuel") if person.lower() in answer.lower()}
    expected = set() if "ambiguous" in labels else set(labels) - {"unknown"}
    result = {
        "case": name,
        "lookups": lookups,
        "names_waited_for_result": not any(
            "said" in row
            and any(person.lower() in row["said"].lower() for person in ("Edmon", "Samuel"))
            and not any("result" in prior for prior in timeline[:i])
            for i, row in enumerate(timeline)
        ),
        "timeline": timeline,
        "scripted_labels": labels,
        "answer": answer,
        "heard": "".join(heard),
        "context_sent": tracker.published,
        "context_acknowledged": tracker.acknowledged,
        "finalized": stats.finalized,
        # An uncertain multi-speaker answer need not recite all candidate names.
        # A single selected name is still a failure. Review uncertainty manually.
        "names_check": bool(answer.strip())
        and (named == expected or (len(expected) > 1 and not named)),
        "question_heard": bool("".join(heard).strip()),
    }
    print(json.dumps(result), flush=True)
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=CASES, help="repeat to select cases")
    parser.add_argument("--question-delay", type=float, default=1)
    parser.add_argument("--recognition-delay", type=float, default=3)
    parser.add_argument("--question", default="Computer, what's my name?")
    args = parser.parse_args()
    settings = load_settings(args.env_file)
    if not settings.openai_api_key:
        parser.error("OPENAI_API_KEY is required")
    question = await asyncio.to_thread(question_audio, settings, args.question)
    results = await asyncio.gather(
        *(
            check(
                settings, question, case, CASES[case], args.question_delay, args.recognition_delay
            )
            for case in args.case or CASES
        ),
    )
    return (
        0
        if all(
            r["names_check"]
            and r["question_heard"]
            and r["lookups"]
            and r["names_waited_for_result"]
            and r["finalized"]
            for r in results
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
