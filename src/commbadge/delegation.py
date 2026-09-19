"""Execute snapshot function calls without blocking the Live audio receiver."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

from commbadge.capture import SnapshotCapture

SNAPSHOT_TOOL = {
    "type": "function",
    "name": "capture_snapshot",
    "description": (
        "Capture a fresh image from the configured screen or camera when the user asks you "
        "to look, see, read, or inspect what they are showing. The application attaches the "
        "image to your context. Analyze it before replying. Do not call automatically or "
        "repeatedly; follow-up questions can use the last image unless a new view is requested."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The user's visual question."}
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    "strict": True,
}


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str


class SnapshotDelegation:
    def __init__(self, connection, capture: SnapshotCapture, report: Callable[[str], None]):
        self.connection = connection
        self.capture = capture
        self.report = report
        self.active: dict[str, str] = {}
        self.pending: dict[str, list[FunctionCall]] = {}
        self.seen: set[str] = set()
        self.queue: asyncio.Queue[list[FunctionCall]] = asyncio.Queue(maxsize=8)

    def observe(self, envelope) -> None:
        event = envelope.event
        delegation = getattr(envelope, "delegation_id", "")
        if event.type == "response.created":
            self.active[delegation] = event.response.id
            self.pending.setdefault(event.response.id, [])
        elif event.type == "response.output_item.done" and event.item.type == "function_call":
            response_id = self.active.get(delegation)
            if response_id is None:
                raise RuntimeError("Function call arrived without a response ID.")
            item = event.item
            if item.call_id not in self.seen:
                self.seen.add(item.call_id)
                self.pending[response_id].append(
                    FunctionCall(item.call_id, item.name, item.arguments)
                )
        elif event.type == "response.completed":
            calls = self.pending.pop(event.response.id, [])
            if calls:
                try:
                    self.queue.put_nowait(calls)
                except asyncio.QueueFull as error:
                    raise RuntimeError("Too many pending snapshot requests.") from error
        elif event.type in ("response.failed", "response.incomplete", "response.cancelled"):
            self.pending.pop(event.response.id, None)

    async def run(self) -> None:
        while True:
            calls = await self.queue.get()
            for call in calls:
                image = None
                try:
                    if call.name != "capture_snapshot":
                        raise ValueError("Unknown tool; no action was performed.")
                    args = json.loads(call.arguments)
                    if (
                        not isinstance(args, dict)
                        or set(args) != {"question"}
                        or not isinstance(args["question"], str)
                        or not args["question"].strip()
                        or len(args["question"]) > 4000
                    ):
                        raise ValueError(
                            "Snapshot needs a non-empty question of at most 4000 characters."
                        )
                    self.report("\nCapturing a fresh image…\n")
                    image = await self.capture.capture(args["question"])
                    result = {"status": "captured", "image_id": call.call_id}
                except (OSError, RuntimeError, ValueError) as error:
                    result = {"status": "failed", "error": str(error)}
                    self.report("\nSnapshot failed; reporting the error to the assistant.\n")
                await self.connection.send(
                    {
                        "type": "response.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps(result),
                        },
                    }
                )
                if image is not None:
                    await self.connection.send(image.event(event_id=f"image_{call.call_id}"))
                    self.report("\nImage sent for analysis.\n")
            # Every function output in this response must be submitted before continuing.
            await self.connection.send({"type": "response.create"})
