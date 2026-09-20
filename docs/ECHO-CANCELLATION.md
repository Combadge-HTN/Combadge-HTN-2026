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
measured roughly 516–541 ms. Supply the measured delay; the code advances the
reference by 50 ms so the causal 200 ms adaptive filter can cover the remaining
delay. Do not subtract that margin a second time in configuration.
The output reference is taken before Bluetooth playback gain; the adaptive filter
learns the fixed gain as part of the echo path.

Capture and playback reference positions advance by sample count within a
continuous stream, rather than realigning on every thread wakeup. After linear
cancellation, Speex's residual echo suppressor uses the canceller's echo estimate.
Speex's default noise suppression remains enabled: forcing its floor to 0 dB
also weakens residual echo suppression. Steady background tones can be attenuated;
validation checks changing speech and interruptions. This stage adds one 20 ms
frame of latency; it does not mute the microphone during playback.

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

Native QNX library loading and synthetic cancellation have been verified, including
jittery I/O and simultaneous near-end audio. The original timestamp-per-read
implementation removed almost none of the measured speaker component. Reprocessing
the same two acoustic recordings with sample-continuous timing and residual echo
suppression measured approximately 11–39 dB reduction in that correlated component.
The comparison accounts for the suppressor's 20 ms latency; it is not a measurement
of total acoustic ERLE or speech intelligibility. Calibration recordings stay on
the Pi outside the Git checkout.

The timing fix alone still produced a repeating assistant feedback loop in a live
speaker test. The combined residual-suppression revision also failed an isolated
fixed-sample test: Speechmatics transcribed "Can you recognize me" from the filtered
microphone while that sample played through the speaker. No Live session ran in
that diagnostic, so the result establishes residual audible speech without a
feedback loop. Do not treat recorded-signal attenuation as conversational acceptance.
This is not yet an effective echo solution for the complete setup.

A subsequent in-memory timing check measured 462 and 468 ms from software playback
submission to matching captured audio in two usable speech windows. That includes
capture buffering; it does not isolate the speaker's physical latency. Other
windows did not validate, so this is not a stable recalibration. The filter removed
about 12 dB of the correlated speaker component, but the phrase remained recognizable.

Offline comparison then identified the 0 dB noise-floor override as a contributor
to weak residual suppression. Restoring Speex's default improved rejection on both
existing acoustic recordings. An independent Edmon speech recording changed by
0.49 dB in overall level; mixed into recorded echo at +6 dB relative RMS, its main
sentence remained transcribable with both settings, with word errors at the
boundaries. These checks support removing the override, but do not establish
speaker-on acceptance or preservation of every quiet interruption. No calibration
or microphone recordings are committed to the repository.

If independent clocks defeat this prototype, use a shared-clock USB audio device
for microphone and speaker, or integrate an echo canceller with delay estimation
and render/capture drift compensation (such as WebRTC audio processing). QNX's
Acoustics Management Platform also offers echo cancellation as a separate product;
it is not established to be installed on this Pi.

References:
- [Speex echo cancellation and timing constraints](https://www.speex.org/docs/manual/speex-manual/node7.html)
- [QNX audio processing modules](https://qnx.com/developers/docs/7.0.0/com.qnx.doc.neutrino.audio/topic/QSA_AMP.html)
- [WebRTC processing and playback reference](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcdsp.html)
