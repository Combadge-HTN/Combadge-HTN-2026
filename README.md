# HTN 2026 — AI comm badge

A wearable voice assistant inspired by the Star Trek communicator: chest control, haptic feedback, microphone, and speaker, powered by a Raspberry Pi 5 and OpenAI GPT-Live.

**Status:** starter repository. Local audio diagnostics are implemented; cloud voice, badge controls, and sponsor integrations are planned.

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
