# Hardware inventory and next bench test

Updated September 19, 2026 from the team's equipment list. Audio and hardware execution have not yet been tested.

| Part | Role | What remains to check |
| --- | --- | --- |
| Raspberry Pi 5 development kit | Device compute and network | Installed OS, power supply, cooling, and SSH access |
| LilyPad SimpleSnap Protoboard | Sewable connection/prototyping board | Whether the separate Arduino SimpleSnap controller is also present |
| Raspberry Pi camera | Later image input | Exact model and Pi 5-compatible ribbon cable |
| Speaker with 3.5 mm plug | Voice output | Whether it is powered/amplified; exact plug and power requirements |
| SparkFun Sound Detector | Analog microphone plus sound detection circuitry | Exact model, audio ADC/interface, levels, and coupling |
| Chest button/touch sensor | Conversation activation | Not yet identified |
| Vibration motor and driver | Haptic feedback | Not yet identified |

## Is the Sound Detector a microphone?

Yes. It contains an electret microphone and circuitry exposing three signals:

- **AUDIO:** the analog waveform needed to reconstruct speech.
- **ENVELOPE:** sound amplitude, useful for loudness detection but not recording words.
- **GATE:** a binary sound-present signal, also not speech audio.

The Pi's GPIO cannot directly sample this analog audio. Using AUDIO for speech requires an audio-capable ADC/codec or a compatible audio input interface, with the correct bias removal, signal level, and wiring. A USB adapter's microphone socket is not automatically compatible with this board's output. Check both circuits before connecting them. Likewise, do not connect a 5 V signal directly to a Pi GPIO.

The simplest proposed MVP path is to borrow a supported USB microphone. Keep the Sound Detector available for a later analog-audio experiment or sound-presence feature.

Source: [SparkFun Sound Detector hookup guide](https://learn.sparkfun.com/tutorials/sound-detector-hookup-guide/all).

## Speaker connection

The Raspberry Pi 5 does not have a built-in 3.5 mm analog audio jack. Use a supported USB audio adapter with a 3.5 mm output for an amplified speaker, or a suitable DAC/amplifier. If the speaker is passive, it needs amplification; its connector alone does not establish that.

A USB headset is also a useful temporary way to prove recording and playback before assembling the badge.

Sources: [Raspberry Pi 5 introduction](https://www.raspberrypi.com/news/introducing-raspberry-pi-5/), [Raspberry Pi audio options whitepaper](https://pip-assets.raspberrypi.com/categories/1259-audio-camera-and-display/documents/RP-008124-WP-1-Choosing%20an%20Audio%20option.pdf).

## LilyPad distinction

The **LilyPad Arduino SimpleSnap** is the programmable controller. The **SimpleSnap Protoboard** is its companion connection board. Confirm both pieces before planning firmware. If only the protoboard is present, it can still support construction, but does not itself run button/haptic firmware. The first wired control can instead use the Pi with suitable circuitry.

Sources: [SparkFun wearable Arduino comparison](https://learn.sparkfun.com/tutorials/arduino-comparison-guide/wearable-arduinos), [SparkFun SimpleSnap introduction](https://news.sparkfun.com/910).

## Immediate completion criterion

On the Pi, list audio devices, record five seconds of speech, then play the recording through the intended output using `scripts/audio_check.py`. Check intelligibility, distortion, and output volume. Only after this passes should the voice client be connected to GPT-Live. This avoids debugging audio hardware and the cloud API at the same time.
