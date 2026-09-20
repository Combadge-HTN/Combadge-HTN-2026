# Acoustic echo cancellation

The badge needs the speaker-bound PCM as a reference to remove its sound from
microphone input. Noise suppression and lower speaker volume alone do not do this.

`combadge start` and `combadge voice` support an **experimental, opt-in SpeexDSP
canceller** with the command/ALSA audio backend. It processes both assistant and
human-call microphone audio at 24 kHz in 20 ms frames. Microphone-only console
mode does not enable it. The Mac sounddevice backend does not implement it.

Install the native QNX `speexdsp` package using your configured QNX repository.
The inspected repository offers version `1.2.1-r0` for aarch64. This library is
separate from Python dependencies; Linux wheels cannot replace it on QNX.
Alternatively, point `COMBADGE_AEC_LIBRARY` to a compatible shared library.

Configure in `.env`:

```dotenv
COMBADGE_AEC=speex
# Set this from a measurement of playback submission to captured echo.
COMBADGE_AEC_DELAY_MS=500
# Optional absolute path, otherwise use the system library loader:
# COMBADGE_AEC_LIBRARY=/path/to/libspeexdsp.so.1
```

The delay shown is a calibration starting point, not a universal Bluetooth
setting. On the tested USB microphone/TWS speaker setup, a short calibration
measured roughly 516–541 ms. Set the alignment slightly before the earliest
measured echo so the 200 ms adaptive filter covers the remaining acoustic tail.
The output reference is taken before Bluetooth playback gain; the adaptive filter
learns the fixed gain as part of the echo path.

Set `COMBADGE_AEC=off` to disable it (the default). Enabling it with an absent
library or missing/invalid delay fails explicitly instead of silently claiming
that echo cancellation is active.

## Limitations and validation

Speaker submission timestamps approximate physical playback. USB capture and
Bluetooth playback have independent clocks, and A2DP latency can change. This
implementation bounds and aligns a reference buffer but does **not** compensate
for hardware clock drift or measure the actual Bluetooth playback position.
Speex specifically warns about using unrelated capture and playback clocks.
Do not enable it by default based on a synthetic test alone.

Synthetic native-library tests check echo reduction and preservation of an
independent near-end signal. Before deployment, validate on the Pi with:

- Far-end speech alone, checking echo reduction over a sustained call.
- Badge speech alone, checking intelligibility and unchanged level.
- Both people speaking together, checking that badge speech is preserved.
- Bluetooth reconnects and changing delays, checking recovery.

Native QNX library loading and synthetic cancellation have been verified. However,
two acoustic tests with the USB microphone and Bluetooth speaker showed almost
no reduction of the known speaker signal (approximately 0–0.14 dB), even after
separating that signal from room noise. This prototype is not an effective echo
solution for the tested setup yet. Capture frames arrive in bursts, so aligning
the reference to each read's wall-clock timestamp remains a suspected timing
problem in addition to independent device clocks. Calibration recordings stay
on the Pi outside the Git checkout.

If independent clocks defeat this prototype, use a shared-clock USB audio device
for microphone and speaker, or integrate an echo canceller with delay estimation
and render/capture drift compensation (such as WebRTC audio processing). QNX's
Acoustics Management Platform also offers echo cancellation as a separate product;
it is not established to be installed on this Pi.

References:
- [Speex echo cancellation and timing constraints](https://www.speex.org/docs/manual/speex-manual/node7.html)
- [QNX audio processing modules](https://qnx.com/developers/docs/7.0.0/com.qnx.doc.neutrino.audio/topic/QSA_AMP.html)
- [WebRTC processing and playback reference](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcdsp.html)
