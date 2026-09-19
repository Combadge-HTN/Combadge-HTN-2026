# QNX integration

Target: Raspberry Pi 5, QNX 8.0, aarch64le, Python 3.14.

## Runtime

The application requires Python 3.11+, TLS certificates, DNS, outbound secure WebSocket access, asyncio, and subprocess support. Runtime dependencies are `python-dotenv` and `websockets`; both provide pure-Python wheels. Install the pinned dependencies using the [setup instructions](../README.md#setup).

The session layer uses the [GPT-Live WebSocket protocol](https://developers.openai.com/api/docs/guides/voice-websockets?api=live) directly. QNX runtime execution and end-to-end audio remain unvalidated.

## Audio interface

`AudioIO` defines four asynchronous methods: `start()`, `read()`, `write(data)`, and `close()`. `CommandAudio` connects the session to capture and playback executables without invoking a shell.

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

QNX capture/playback helpers are not included. Their implementation must use the selected audio interface's QNX driver. The presence of `wave` and `waverec` alone does not provide this raw streaming interface: their documented inputs and outputs are WAV files. Consult `use wave` and `use waverec` for the installed utilities' options.

## Device acceptance

For voice-triggered camera capture, configure `--snapshot-command` with a helper that writes one encoded JPEG, PNG, or WebP into the supplied `{directory}` and exits. The Python tool handler runs capture independently of the audio receiver, submits the tool result and image, then continues the Responses backend. Screen capture through `--screenshots` is a COSMIC-specific adapter; it does not provide a QNX camera driver.

Have the QNX camera helper emit JPEGs no larger than 256 KiB. Larger images require the optional `images` extra (Pillow), which has native dependencies and has not been validated on QNX. The core voice installation does not depend on Pillow. Image file reading and optional resizing run in a worker thread to keep audio flowing. The app tracks a per-session image budget and requests a new voice session when it is exhausted.

`--shopify` uses standard-library HTTPS in a worker thread, so catalog requests do not block the audio receiver. Validate HTTPS certificates and thread support on the target. Checkout returns a merchant URL without payment; a headless badge needs a companion device to open that URL. `--open-checkout` uses the host browser and is optional. No additional native Python packages are required for shopping.

1. Confirm the capture and playback devices have QNX drivers and usable PCM endpoints.
2. Record and play intelligible speech through the selected audio interface.
3. Run `commbadge voice --check` to verify TLS, authentication, and generated audio.
4. Run the voice client with both native helpers and verify conversational audio.
5. Verify session shutdown, device disconnection, and network-loss handling.
