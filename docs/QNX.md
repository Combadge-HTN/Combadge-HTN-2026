# QNX Pi 5 integration

The team confirmed that the Raspberry Pi 5 runs **QNX OS**. The Linux laptop is the development host. Do not install Raspberry Pi OS or treat `apt`, ALSA, Linux wheels, or a copied laptop virtual environment as QNX deployment instructions.

## Confirmed target inventory

September 19, 2026: the user reports Python **3.14.0**, `/usr/bin/wave`, and `/usr/bin/waverec`. Read-only SSH inspection confirmed QNX **8.0.0**, RaspberryPi5 **aarch64le**, image build June 5, 2026, and pip **25.1.1**.

The installed utility help uses ALSA-style device names (`-Ddefault`, `-Dplughw:...`) and WAV file arguments. `waverec` defaults to 48 kHz stereo, with configurable rate/channels/format. `/dev/snd` listed only `controlC0`; no capture or playback PCM entries were visible at inspection time. This is an observation, not proof that audio cannot be configured.

No packages were installed, settings changed, recording/playback started, or app code executed on the Pi. The user currently permits read-only inspection only. Future installation and audio tests below require that restriction to be lifted.

## What is implemented

The Python GPT-Live session layer depends on an `AudioIO` interface: `start()`, `read()`, `write(data)`, and `close()`. Linux capture/playback and a generic native-helper transport implement that boundary. Fake transports exercise the same session logic in tests. The standalone `scripts/audio_check.py` remains unchanged and Linux-only.

The generic helper transport is an integration seam, **not a delivered QNX audio driver**. The application has not been executed on the team's QNX image.

## Facts to establish on the actual image

Have the hardware team inspect:

```sh
uname -a
python3 --version
command -v wave
command -v waverec
ls /dev/snd
```

Missing commands or device nodes are useful findings, not proof that the board cannot support audio through another driver. Confirm the image/BSP version, capture and playback driver, and interface model with the QNX sponsor.

The generic [QNX Python reference](https://www.qnx.com/developers/docs/8.0/com.qnx.doc.neutrino.utilities/topic/p/python.html) mentions Python 3.8; the actual image's reported Python 3.14 takes precedence. The application requires Python 3.11+.

The client uses the [documented GPT-Live WebSocket protocol](https://developers.openai.com/api/docs/guides/voice-websockets?api=live) directly. Its runtime dependencies are only `python-dotenv` and `websockets`, both available as pure-Python wheels. The OpenAI SDK and its mandatory native dependencies are not required. TLS certificates, event-loop support, subprocess handling, and package execution still need target verification.

`requirements-voice.txt` exports exact runtime versions and hashes from `uv.lock`. For a future authorized deployment, create a QNX-local virtual environment, install with `python -m pip install --require-hashes -r requirements-voice.txt`, then `python -m pip install --no-deps -e .`. Select the `py3-none-any` dependency wheels rather than Linux wheels. Do not perform installation under the current read-only restriction.

Actual utility help should guide adapter work rather than older online `wave` examples. Presence of the utilities does not establish continuous raw PCM streaming or driver support for the team's microphone/speaker.

## Audio boundary for teammates

The native capture helper must write **raw signed 16-bit little-endian, mono PCM at 24,000 samples/second** to stdout. The playback helper must read the same bytes from stdin and play them. No WAV header, JSON, or diagnostic text belongs in these streams. Diagnostics go to stderr. Capture must be paced by the device clock, continue through silence, and stop when terminated. Partial reads are supported; sample boundaries must remain valid. If the physical device uses 48 kHz, perform proper resampling in the adapter rather than relabeling the sample rate.

The Python client reads 960-byte frames (20 ms). Input EOF is a device failure. Playback buffering is bounded; a stalled output closes the cloud session rather than building up old speech. Helpers must terminate cleanly on SIGTERM. A native QNX helper could use the image's supported audio framework, but we cannot choose its API or build flags until that framework is identified.

Once real, tested helpers and Python dependencies exist, invoke the command backend with their paths:

```text
commbadge voice --audio-backend commands \
  --capture-command '/absolute/path/to/your-capture-helper' \
  --playback-command '/absolute/path/to/your-playback-helper'
```

Those helper names are placeholders, not commands shipped by this repo or QNX. Arguments are split into an executable and argv; no shell is invoked. `wave`/`waverec` must not simply be substituted without checking their file format and streaming behavior.

## Native deployment acceptance

1. Identify the QNX image and audio driver; record/play a local sample successfully.
2. Python 3.14 is reported present. When deployment is authorized, verify TLS certificates, DNS, network access, subprocess support, and the locked pure-Python dependencies on QNX. Do not copy `.venv` from Linux.
3. Run `commbadge voice --check` on QNX. This verifies the API path without microphones.
4. Implement and test both native PCM helpers, then run the command backend.
5. Confirm intelligible speech, natural replies, stop behavior, and an unplug/network-loss case on actual hardware.

If QNX Python networking or audio integration is unavailable, an alternative architecture is a thin QNX device process streaming audio to a Python service on another host. That relay is **not implemented** here, and would be a separate deployment decision. It would still require working QNX audio drivers. Keep this fallback distinct from claiming that the Python package runs natively on QNX.
