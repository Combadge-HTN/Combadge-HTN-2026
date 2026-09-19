# Project plan

## Architecture

The application targets Raspberry Pi 5 running QNX 8.0. Audio helpers connect the microphone and speaker to the Python GPT-Live client using mono PCM16 at 24 kHz. Responses delegation handles backend reasoning.

```text
Microphone → QNX capture helper → Python client ⇄ GPT-Live
Speaker   ← QNX playback helper ← Python client
```

Chest activation, haptic feedback, and external action tools are planned. The LilyPad SimpleSnap Protoboard requires a separate controller for firmware-driven controls.

Still images and questions can be submitted to the managed Responses backend through the Live connection. GPT-Live uses the backend findings in its spoken response. Camera capture will supply encoded image bytes through the same interface.

## Milestones

1. Complete a spoken conversation on the Pi with the available speakers and Dan's Bluetooth/audio drivers; native QNX capture/playback streaming is implemented.
2. Add chest activation and distinct haptic feedback for listening, completion, and failure.
3. Add a Browserbase workflow that executes a task and returns its result by voice.
4. Add Shopify product matching, clarification of ambiguous requests, and draft orders.
5. Add on-device inference using a QNX-compatible AI module for a defined offline feature.

## Tool execution

Browser and commerce operations must run independently of audio capture and playback. Track each task as running, awaiting clarification, completed, failed, or cancelled. Ending speech does not cancel a business action. Report completion only after checking the tool result, and check for an existing result before retrying writes.
