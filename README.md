# HTN 2026 — AI comm badge

A wearable voice assistant inspired by the Star Trek communicator: chest control, haptic feedback, microphone, and speaker, powered by a Raspberry Pi 5 and OpenAI GPT-Live.

**Status:** GPT-Live voice client implemented and tested on the Linux development laptop, including a real API check and microphone/playback streaming. The Pi 5 runs **QNX**, with Python 3.14.0 and `wave`/`waverec` present; native audio and dependency execution remain unverified. Badge controls and sponsor tools remain planned. See [QNX integration](docs/QNX.md).

## Run GPT-Live on the development laptop

From the repository root, activate your Python environment and install the voice extra:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev,voice]'
commbadge voice --check
commbadge voice
```

Set `OPENAI_API_KEY` in the ignored `.env` file first; keep the existing Browserbase entry. `--check` sends synthetic silence, requests a greeting, verifies non-silent generated PCM, and closes the session without opening a microphone or speaker. It checks audio generation and API access, not speech recognition or device quality. Both commands use paid API credits.

`commbadge voice` streams microphone audio and plays the assistant's response. It greets you with “Badge ready”; speak normally and pause for a reply. Use headphones to avoid microphone/speaker feedback: this adapter does not implement acoustic echo cancellation. GPT-Live manages conversation overlap; the client does not implement separate speech detection or force-cut playback on transcript events.

Press **Ctrl+C** to stop capture and playback, then wait for the server's close acknowledgment. The default session limit is five minutes; use `--max-seconds 60` for a shorter run. Add `--no-captions` to hide speech transcripts. No audio recordings or transcript files are saved by this client.

Choose Linux devices if the defaults are wrong:

```bash
commbadge voice --list-devices
commbadge voice --input-device 'plughw:CARD=Device,DEV=0' --output-device default
```

Replace `Device` with a name from your device list; `plughw` permits conversion when the hardware does not accept 24 kHz directly. The ALSA adapter requires `arecord` and `aplay` (`sudo apt install alsa-utils` on Debian/Ubuntu). These commands are **Linux-only**, not QNX instructions.

Optional `.env` settings are `OPENAI_LIVE_MODEL=gpt-live-1`, `OPENAI_LIVE_VOICE=marin`, and `OPENAI_BACKEND_MODEL=gpt-5.6-luna`. Responses delegation handles reasoning; no external action tools are registered yet. Backend inference is billed separately from the voice session.

## Reproduce the software environment

`uv.lock` records exact package versions and artifact hashes. On supported development hosts, install uv 0.12.17 and use the same lock as CI:

```bash
uv sync --locked --extra dev --extra voice --python 3.12
uv run --no-sync commbadge doctor
uv run --no-sync commbadge voice --check
uv run --no-sync commbadge voice
```

CI checks Python 3.11 through 3.14. `--locked` rejects a stale lock rather than resolving new versions. Commit lock updates along with dependency changes. The pip editable install above is convenient but does not enforce the lock. `requirements-voice.txt` exports runtime versions and hashes for pip deployment. Both runtime dependencies have pure-Python wheels; the client uses the GPT-Live WebSocket protocol without requiring the OpenAI SDK. QNX drivers and Python networking require separate validation in [QNX.md](docs/QNX.md).

## Python setup

The application is written in Python, with Python 3.11 or later required. Run these commands from the repository root on the development laptop. QNX deployment has separate prerequisites:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
commbadge doctor
```

On Debian/Ubuntu, install `python3-venv` if virtual environment creation fails. The setup above installs the base package and development tools; add the `voice` extra to run GPT-Live. The current microphone/speaker adapter and original audio script require Linux.

`commbadge doctor` checks credential presence and audio tool availability locally. It never calls an API or prints credential values; it does not verify credentials or connected hardware. It reads `.env` from the current directory, with exported environment variables taking precedence. Use `commbadge doctor --env-file /path/to/.env` to choose a different file. Missing keys are allowed while working on hardware. `python -m commbadge doctor` is equivalent.

If you do not already have `.env`, copy `.env.example` to `.env` and fill in your local values. Never overwrite an existing credential file.

## Development

```bash
pytest
ruff check .
ruff format --check .
```

GitHub Actions runs these checks with locked dependencies on Linux/Python 3.11 through 3.14, without API keys or hardware. Formatting and linting cover the new package and tests; the existing audio script is intentionally left unchanged. The fake audio/connection tests check byte flow, startup ordering, errors, timeouts, and shutdown; process-helper tests check the raw PCM contract.

```text
src/commbadge/         Python application package
  cli.py              commbadge command and local diagnostics
  config.py           .env / environment configuration
  live.py             GPT-Live session and streaming lifecycle
  audio.py            Linux, native-helper, and synthetic audio adapters
scripts/audio_check.py  Existing standalone audio tool (unchanged)
tests/                Configuration and secret-output regression checks
docs/                 Hardware notes
pyproject.toml        Package, dependency, and tooling configuration
uv.lock               Reproducible development dependency versions
requirements-voice.txt  Hash-verified runtime export for pip deployments
```

## Standalone audio check (Linux development only)

The original audio tool is unchanged. It can test a Linux laptop with a microphone and speakers; it does not operate the team's QNX Pi. See [hardware notes](docs/HARDWARE.md) before connecting the Sound Detector.

On Debian/Ubuntu, install the ALSA command-line tools if missing:

```bash
sudo apt update
sudo apt install -y alsa-utils python3
python3 scripts/audio_check.py list
python3 scripts/audio_check.py record --seconds 5
python3 scripts/audio_check.py play
```

Record only when ready to speak; playback is a separate command. Recordings stay local under the ignored `recordings/` directory. The script refuses to overwrite an existing recording; choose another `--file` for the next test.

If the default devices are incorrect, use device names from `arecord -L` / `aplay -L`:

```bash
python3 scripts/audio_check.py record --input-device plughw:CARD=Device,DEV=0 --file recordings/second.wav
python3 scripts/audio_check.py play --output-device plughw:CARD=Device,DEV=0 --file recordings/second.wav
```

`Device` above is an example; replace it with your actual card name. Recording defaults to 48 kHz mono PCM16. This bench-test WAV is not a GPT-Live stream; the later voice client must convert to its configured stream format.

## Next milestones

1. Verify the QNX image's Python version, audio framework, and driver support.
2. Provide QNX capture/playback adapters and validate the same voice loop on the Pi.
3. Add chest activation and haptic feedback after identifying the controller and actuator.
4. Add one verified Browserbase action, then Shopify and messy-data handling.

The camera is reserved for a later visual feature. Native QNX runtime and audio-driver integration are not implemented or tested yet. The generic command adapter provides the PCM boundary for that work. A qualifying local AI module is a separate QNX prize requirement.

## Project files

- [PROJECT_PLAN.md](PROJECT_PLAN.md): architecture, sponsor strategy, and demo proposal.
- [docs/HARDWARE.md](docs/HARDWARE.md): confirmed equipment, missing pieces, and connection constraints.
- [docs/QNX.md](docs/QNX.md): platform differences, integration contract, and deployment checks.
- [scripts/audio_check.py](scripts/audio_check.py): Python standard-library wrapper for ALSA device listing, recording, and playback.
- [.env.example](.env.example): API configuration variables, without secrets.

Keep real credentials in `.env`, which is ignored by Git. If `.env` already exists, merge missing entries manually rather than replacing it. No API calls, dependencies, or cloud credentials are needed for the audio check.
