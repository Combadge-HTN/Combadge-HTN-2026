# Combadge-HTN-2026

A wearable voice assistant for Raspberry Pi 5 running QNX 8.0. The Python application streams microphone audio to OpenAI GPT-Live and plays spoken responses through the badge.

## Requirements

- QNX 8.0 on Raspberry Pi 5 (aarch64le), with Python 3.11+ and pip.
- Network access to OpenAI and an `OPENAI_API_KEY`.
- QNX audio drivers and capture/playback helpers implementing the [PCM interface](docs/QNX.md#audio-interface).

The voice client and command transport are implemented. Native QNX audio helpers are still required; end-to-end operation on the Pi has not been validated.

## Setup

From the repository root on the Pi:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-voice.txt
python -m pip install --no-deps -e .
```

Create `.env` using [.env.example](.env.example) and set `OPENAI_API_KEY`. Environment variables override `.env` values. Credentials belong outside source control.

Runtime dependencies are pinned in `requirements-voice.txt`, exported from `uv.lock`. QNX networking and dependency execution require target validation; see [QNX integration](docs/QNX.md).

## Voice

With native audio helpers installed, supply their executable paths:

```sh
commbadge voice --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper'
```

These paths are placeholders for the required helpers, which are not included in this repository.

Press **Ctrl+C** to end the session. Sessions default to five minutes; use `--max-seconds 60` to change the limit. Add `--no-captions` to hide transcripts. The client saves no audio or transcript files. Acoustic echo cancellation must be handled by the audio path.

`commbadge doctor` reports configuration and audio utility availability. `commbadge voice --check` verifies API access and generated audio without opening audio devices; it consumes API credits.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required | OpenAI authentication |
| `OPENAI_LIVE_MODEL` | `gpt-live-1` | Voice model |
| `OPENAI_LIVE_VOICE` | `marin` | Response voice |
| `OPENAI_BACKEND_MODEL` | `gpt-5.6-luna` | Delegated reasoning model |
| `BROWSERBASE_API_KEY` | Unset | Reserved for browser integration |
| `BROWSERBASE_PROJECT_ID` | Unset | Reserved for browser integration |

The client uses the GPT-Live WebSocket protocol with Responses delegation. External action tools are not registered yet. Voice sessions and delegated inference incur separate charges.

## Documentation

- [QNX integration](docs/QNX.md): runtime and audio interface.
- [Hardware](docs/HARDWARE.md): components and electrical requirements.
- [Project plan](PROJECT_PLAN.md): architecture and upcoming features.
