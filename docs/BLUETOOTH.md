# AI speech through the QNX Bluetooth speaker

From the Pi's application checkout:

```sh
cd ~/projects/Combadge-HTN-2026
.venv/bin/combadge start --no-camera --no-shopify --no-shop-account
```

This launches the sibling `~/projects/qnx-bluetooth` driver under sudo,
connects TWS Mini Speaker, negotiates 44.1 kHz, enables its PCM FIFO, and
starts the voice application as the original user. Ctrl-C stops both and
restores Bluetooth hardware state. For a bounded test add `--max-seconds 20`.
Microphone audio is sent to OpenAI; AI audio replies play through the speaker.
Credentials stay in the application's existing `.env` file.

Plain `combadge start` enables Bluetooth speech, calling, camera, and shopping.
Use `--no-bluetooth` for transcript-only console output and `--no-calls` to disable
calling. The explicit `--bluetooth` and `--calls` options remain supported. Camera
and shopping were disabled for the original speaker integration test.

For a human call, say “Computer, call Edmon.” The assistant closes before dialing;
your microphone and the Bluetooth speaker carry the call audio. The calling
configuration and contacts must be present in `.env`.

## Driver compatibility and volume

Use the updated sibling driver source, including the discovery-stop fix,
idle-FIFO initialization and `QNX_PCM_VOLUME_SHIFT` support. Rebuild it with:

```sh
cd ~/projects/qnx-bluetooth/btstack-master/port/qnx-pi5
make
```

The application supervisor defaults the driver to shift 0: full PCM amplitude,
the level approved during the speaker tests. The driver alone
still defaults to divide-by-32. `QNX_PCM_VOLUME_SHIFT` supports integer values
0 through 8; larger numbers are quieter. It must be present in the supervisor's
environment after sudo to override its default. No firmware or system audio
configuration is changed.

The playback adapter resamples 24 kHz mono signed 16-bit PCM to 44.1 kHz
stereo and writes through the driver's bounded FIFO sender. It no longer
boosts samples before attenuation, avoiding clipping of louder AI speech.
Only one app/session may own the radio and PCM FIFO at a time.

## Validation

44 audio, adapter, startup and Live-session tests passed on the Pi. A real
20-second session connected the speaker, opened the FIFO at gain 1/4, received
897600 bytes of AI audio, and closed normally. The AI generated its greeting
and counted to ten. Acoustic confirmation is separate from these counters.
No camera or shopping activity was enabled in that test.

Shutdown now drains received speech for up to four seconds, lets the playback
helper flush at EOF, and gives Bluetooth a one-second tail before disconnecting.
This drains already received audio, not speech the AI has yet to generate.
The user subsequently confirmed full-amplitude speech at speaker volume 100%.
