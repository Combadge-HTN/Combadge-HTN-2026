# HTN 2026 — AI comm badge

A wearable voice assistant inspired by the Star Trek communicator: chest control, haptic feedback, microphone, and speaker, powered by a Raspberry Pi 5 and OpenAI GPT-Live.

**Status:** starter repository. Local audio diagnostics are implemented; cloud voice, badge controls, and sponsor integrations are planned.

## Python setup

The application is written in Python, with Python 3.11 or later required. Run these commands from the repository root on your laptop or Pi:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
commbadge doctor
```

On Raspberry Pi OS, install `python3-venv` if virtual environment creation fails. For a device without development tools, use `python -m pip install -e .` instead. On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell; the ALSA audio script itself requires Linux.

`commbadge doctor` checks credential presence and audio tool availability locally. It never calls an API or prints credential values; it does not verify credentials or connected hardware. It reads `.env` from the current directory, with exported environment variables taking precedence. Use `commbadge doctor --env-file /path/to/.env` to choose a different file. Missing keys are allowed while working on hardware. `python -m commbadge doctor` is equivalent.

If you do not already have `.env`, copy `.env.example` to `.env` and fill in your local values. Never overwrite an existing credential file.

## Development

```bash
pytest
ruff check .
ruff format --check .
```

GitHub Actions runs these checks on Python 3.11, 3.12, and 3.13. Formatting and linting cover the new package and tests; the existing audio script is intentionally left unchanged.

```text
src/commbadge/         Python application package
  cli.py              commbadge command and local diagnostics
  config.py           .env / environment configuration
scripts/audio_check.py  Existing standalone audio tool (unchanged)
tests/                Configuration and secret-output regression checks
docs/                 Hardware notes
pyproject.toml        Package, dependency, and tooling configuration
```

## First milestone: hear yourself through the Pi

Start with Raspberry Pi OS, a USB microphone, and a supported USB audio output adapter for the 3.5 mm speaker. A USB headset can substitute for both during bench testing. See [hardware notes](docs/HARDWARE.md) before connecting the Sound Detector.

On the Pi, install the ALSA command-line tools if missing:

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

1. Record intelligible speech and play it through the actual speaker on the Pi.
2. Implement and verify a GPT-Live microphone-to-speaker session.
3. Add chest activation and haptic feedback after identifying the controller and actuator.
4. Add one verified Browserbase action, then Shopify and messy-data handling.

The camera is reserved for a later visual feature. QNX feasibility needs an early, separate boot-and-local-inference check; no QNX support is implemented here.

## Project files

- [PROJECT_PLAN.md](PROJECT_PLAN.md): architecture, sponsor strategy, and demo proposal.
- [docs/HARDWARE.md](docs/HARDWARE.md): confirmed equipment, missing pieces, and connection constraints.
- [scripts/audio_check.py](scripts/audio_check.py): Python standard-library wrapper for ALSA device listing, recording, and playback.
- [.env.example](.env.example): names of future API configuration variables, without secrets.

Keep real credentials in `.env`, which is ignored by Git. If `.env` already exists, merge missing entries manually rather than replacing it. No API calls, dependencies, or cloud credentials are needed for the audio check.
