# QNX integration

Target: Raspberry Pi 5, QNX 8.0, aarch64le, Python 3.14.

## Runtime

The application requires Python 3.14, TLS certificates, DNS, outbound secure WebSocket access, asyncio, and subprocess support. Runtime dependencies are `python-dotenv` and `websockets`; both provide pure-Python wheels. Install the pinned dependencies using the [setup instructions](../README.md#setup).



The session layer uses the [GPT-Live WebSocket protocol](https://developers.openai.com/api/docs/guides/voice-websockets?api=live) directly. The QNX runtime has been exercised with prerecorded PCM: session connection, generated audio reception, camera tool calls, speaker analysis, and catalog search. Physical USB microphone input has also been exercised in a live session with transcription and generated responses. Audible speaker playback remains unvalidated.

Speaker identification uses standard-library HTTPS in a background thread; see [speaker setup](SPEAKERS.md).

The installed QNX Python 3.14 reports `SC_IOV_MAX=-1`, which breaks asyncio's
buffered `sendmsg` path during larger TLS writes, including camera uploads.
The application applies a guarded, process-local fallback of one buffer per
`sendmsg` call when this limit is invalid on QNX. Valid limits and other platforms
are unchanged; no system Python files are edited.

## Audio interface

For the microphone/camera test without speakers, use `combadge start`. It enables
physical camera snapshots and prints GPT-Live transcripts in the console. Only
`arecord` is started; `aplay` and the Bluetooth launcher are not needed. The
underlying command is `combadge voice --camera --audio-backend console`.

Dan has written the Bluetooth and audio drivers, and speakers are available. See [Combadge-HTN/qnx-bluetooth](https://github.com/Combadge-HTN/qnx-bluetooth) for the Bluetooth work. Validate audible playback with the selected speakers using the native audio adapter below.

`AudioIO` defines four asynchronous methods: `start()`, `read()`, `write(data)`, and `close()`. `CommandAudio` connects the session to capture and playback executables without invoking a shell. The default adapter uses the QNX ports of `arecord` and `aplay`. Ordinary pipes carry PCM; blocking reads and writes run in background threads. This avoids the QNX Python asynchronous write-pipe disconnect observed while the playback process was still running. Shutdown terminates the helpers to release blocked I/O.

| Property | Contract |
| --- | --- |
| Format | Raw signed 16-bit little-endian PCM |
| Channels | Mono |
| Sample rate | 24,000 Hz |
| Capture | Write audio to stdout, paced by the device clock, including silence |
| Playback | Read audio from stdin |
| Diagnostics | Write to stderr; keep stdout free of text and WAV headers |
| Shutdown | Exit cleanly on SIGTERM |

The client reads 960-byte frames (20 ms). Helpers may emit partial frames; input EOF is treated as a device failure. Playback buffering is bounded, and stalled output closes the session. Resample in the helper when hardware uses another sample rate.

Check `arecord --help`, `aplay --help`, and `combadge voice --list-devices` on the target. The supplied QNX USB audio image supports raw 24 kHz mono PCM through these tools. Run `combadge voice` with the default backend. If these tools are absent, custom capture/playback helpers must use the selected audio interface's QNX driver. The presence of `wave` and `waverec` alone does not provide this raw streaming interface: their documented inputs and outputs are WAV files. Consult `use wave` and `use waverec` for the installed utilities' options.

## Bluetooth example

`combadge start --bluetooth` uses Dan's existing example in the sibling
`qnx-bluetooth` checkout. It starts the guarded `run-radio.sh`, scans for the TWS
Mini Speaker, selects 44.1 kHz if the speaker reconnects at 48 kHz, opens the PCM
FIFO, and starts microphone/camera input with speaker output. The speaker stays
connected for this application session. This does not register a system audio
device or reroute unrelated applications.

The startup command requires the driver's built executable, firmware,
`play_pcm.py`, and existing `audio.pcm` FIFO. Use `--bluetooth-dir PATH` for a
different driver location. The Pi needs `sudo`, `cc`, and `gpio-bcm`. Only the
Bluetooth supervisor runs as root; the application runs as the invoking user.
The supervisor preserves the driver's ownership checks and prepares the
Bluetooth UART FIFO only on a verified idle controller. Ctrl+C stops the
application, stops the example, and restores any FIFO initialization it made.

The playback helper converts 24 kHz mono to 44.1 kHz stereo with continuous
resampling state and uses the driver's bounded FIFO writer. The example's
existing 1/32 volume attenuation remains in effect. Its Python PCM path can be
tested with `combadge start --tone`. Audible live speech still needs human
confirmation; successful connection and PCM submission alone do not prove it.

On the prepared Pi, `~/bin/combadge` points to the checkout's `.venv/bin/combadge`,
so startup works from any directory. On another Pi, activate the virtual
environment first or run `.venv/bin/combadge start --bluetooth` from the checkout.

## Camera capture

The native [camera snapshot helper](../native/qnx-camera/README.md) is included
in `native/qnx-camera`. Build it on the Pi with `make -C native/qnx-camera`.
It has been tested with the physical IMX708 camera on unit 4, producing a
1152 × 648 JPEG from the configured NV12 stream without sudo. It uses the
installed Sensor Framework and TurboJPEG libraries; no Pillow is needed.

```sh
mkdir -p /tmp/badge-photo
native/qnx-camera/combadge-camera --unit 4 --output-dir /tmp/badge-photo
```

For voice-triggered photos, add `--camera` to the voice command after building the helper. Say “Computer, look at this.” This selects the physical unit 4 camera and describes it as such to the model. Use `--camera-unit N` to override the unit.

Use a fresh output directory for each capture. For voice integration, append
`--snapshot-command '/absolute/path/to/native/qnx-camera/combadge-camera --unit 4 --output-dir {directory}'`
to your voice command. See the helper documentation for configuration and SDK
compatibility requirements. Voice-triggered capture and image analysis have been exercised on QNX using prerecorded voice input and the physical camera. Combined camera use with the physical microphone and audible speaker playback still require acceptance.

For voice-triggered camera capture, configure `--snapshot-command` with a helper that writes one encoded JPEG, PNG, or WebP into the supplied `{directory}` and exits. The Python tool handler runs capture independently of the audio receiver, submits the tool result and image, then continues the Responses backend. Screen capture through `--screenshots` is a COSMIC-specific adapter; it does not provide a QNX camera driver.

The included helper emits JPEGs no larger than 256 KiB. Larger images from other helpers require the optional `images` extra (Pillow), which has native dependencies and has not been validated on QNX. The core voice installation does not depend on Pillow. Image file reading and optional resizing run in a worker thread to keep audio flowing. The app tracks a per-session image budget and requests a new voice session when it is exhausted.

## Device acceptance

`--shopify` uses standard-library HTTPS in a worker thread, so catalog requests do not block the audio receiver. HTTPS certificates and thread support have been exercised on the target. Checkout returns a merchant URL without payment; a headless badge needs a companion device to open that URL. `--open-checkout` uses the host browser and is optional. No additional native Python packages are required for shopping.

1. Confirm the capture and playback devices have QNX drivers and usable PCM endpoints.
2. Record and play intelligible speech through the selected audio interface.
3. Run `combadge voice --check` to verify TLS, authentication, and generated audio.
4. Run the voice client with both native helpers and verify conversational audio.
5. Verify session shutdown, device disconnection, and network-loss handling.

## Telephone calls

The [phone client](CALLING.md) uses the same native PCM helpers and `voice` dependencies.
Direct calls use a dedicated SIP credential, TLS, and SRTP on the badge. G.711
conversion is Python; encryption uses the system OpenSSL library through `ctypes`.
The QNX Python build must include `ssl` and `ctypes`, and the system must provide
`libcrypto.so.3` or `libcrypto.so`. No relay is required. QNX device
acceptance must include a two-way call, confirmed hang-up, and verifying that the
assistant stays disconnected after the call.
