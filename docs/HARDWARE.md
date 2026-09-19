# Hardware

The Raspberry Pi 5 runs QNX 8.0. Dan has written the Bluetooth and audio drivers, and speakers are available. The client uses the [PCM transport](QNX.md#audio-interface).

| Part | Role | What remains to check |
| --- | --- | --- |
| Raspberry Pi 5, QNX 8.0, Python 3.14 | Device compute and network | Audio acceptance, power, and cooling |
| LilyPad SimpleSnap Protoboard | Sewable connection/prototyping board | Whether the separate Arduino SimpleSnap controller is also present |
| IMX708 camera on Pi connector 2 | Snapshot image input | JPEG capture tested on QNX unit 4; end-to-end voice trigger remains to check |
| Speakers (available) | Voice output through Dan's audio drivers | Validate audible playback from the voice client |
| SparkFun Sound Detector | Analog microphone plus sound detection circuitry | Exact model, audio ADC/interface, levels, and coupling |
| MPR121 capacitive touch sensor | Touch input for conversation activation | QNX driver tested; connection to the voice session is not yet implemented |
| Vibration motor and driver | Haptic feedback | Not yet identified |

## Is the Sound Detector a microphone?

Yes. It contains an electret microphone and circuitry exposing three signals:

- **AUDIO:** the analog waveform needed to reconstruct speech.
- **ENVELOPE:** sound amplitude, useful for loudness detection but not recording words.
- **GATE:** a binary sound-present signal, also not speech audio.

The Pi's GPIO cannot directly sample this analog audio. Using AUDIO for speech requires an audio-capable ADC/codec or a compatible audio input interface, with the correct bias removal, signal level, and wiring. A USB adapter's microphone socket is not automatically compatible with this board's output. Check both circuits before connecting them. Likewise, do not connect a 5 V signal directly to a Pi GPIO.

A USB microphone with a compatible QNX driver is an alternative to the analog capture path.

Source: [SparkFun Sound Detector hookup guide](https://learn.sparkfun.com/tutorials/sound-detector-hookup-guide/all).

## Speaker connection

The Raspberry Pi 5 does not have a built-in 3.5 mm analog audio jack. Use a supported USB audio adapter with a 3.5 mm output for an amplified speaker, or a suitable DAC/amplifier. If the speaker is passive, it needs amplification; its connector alone does not establish that.

A USB headset is also a useful temporary way to prove recording and playback before assembling the badge.

Sources: [Raspberry Pi 5 introduction](https://www.raspberrypi.com/news/introducing-raspberry-pi-5/), [Raspberry Pi audio options whitepaper](https://pip-assets.raspberrypi.com/categories/1259-audio-camera-and-display/documents/RP-008124-WP-1-Choosing%20an%20Audio%20option.pdf).

## LilyPad distinction

The **LilyPad Arduino SimpleSnap** is the programmable controller. The **SimpleSnap Protoboard** is its companion connection board. Confirm both pieces before planning firmware. If only the protoboard is present, it can still support construction, but does not itself run button/haptic firmware. The first wired control can instead use the Pi with suitable circuitry.

Sources: [SparkFun wearable Arduino comparison](https://learn.sparkfun.com/tutorials/arduino-comparison-guide/wearable-arduinos), [SparkFun SimpleSnap introduction](https://news.sparkfun.com/910).

## Audio validation

Record and play speech on the Pi using the selected interface's QNX utilities or native helper. Check intelligibility, distortion, output volume, and speaker-to-microphone feedback before enabling continuous conversation.

## Camera

The [native snapshot helper](../native/qnx-camera/README.md) captures from the
IMX708 exposed as `/dev/sensor/camera4` on the tested QNX Pi. Build it with
`make -C native/qnx-camera`, then run it with `--output-dir` pointing to an
existing empty directory. It writes one `snapshot.jpg` and works with the
existing Python `SnapshotCapture` class and voice `--snapshot-command` option.

## MPR121 touch sensor

The reusable driver is `combadge.touch.MPR121` in `src/combadge/touch.py`.
It uses native QNX I2C calls and the system `rpi_gpio` module; no Adafruit or
pip-installed GPIO packages are needed. Importing it does not access hardware.
The driver has been tested on Raspberry Pi 5 with QNX 8 and Python 3.14,
including touch/release detection on E0 and IRQ on BCM GPIO 4.

Connect with power off, using 3.3 V power and logic:

| MPR121 breakout | Raspberry Pi header |
| --- | --- |
| VIN/VCC | 3.3 V, physical pin 1 |
| GND | Ground, physical pin 6 |
| SDA | GPIO 2, physical pin 3 |
| SCL | GPIO 3, physical pin 5 |
| IRQ | GPIO 4, physical pin 7 |

Defaults are `/dev/i2c1`, I2C address `0x5A`, and BCM GPIO 4 with an input
pull-up. Leave pads untouched during initialization, which resets and
calibrates the sensor. Run the standalone check from the repository root using
the QNX system Python (an isolated venv may hide `rpi_gpio`):

```sh
sudo env PYTHONPATH=src python3 scripts/touch_check.py
```

The check prints `TOUCHED` if any electrode is touched, otherwise `NOT TOUCHED`,
every 100 ms, including when unchanged. Ctrl+C stops it. Use `--duration 10`
for a timed run, `--poll` to skip GPIO entirely, or `--diagnose` to read
configuration at candidate addresses without resetting devices. `--bus`,
`--address`, and `--irq` override the defaults. The display forces I2C reads,
so its output alone does not verify IRQ wiring.

### Application use

```python
import time
from combadge.touch import MPR121

with MPR121(bus=1, address=0x5A, irq_pin=4) as sensor:
    while True:  # Use your application's stop condition.
        for event in sensor.poll():
            print(event.electrode, "touched" if event.touched else "released")
        time.sleep(0.005)
```

`poll()` returns a tuple of `TouchEvent` objects containing `electrode` (0–11),
`touched`, `timestamp` (monotonic seconds), and `irq_active` (the IRQ level
before the read). Unchanged state produces no events. The first poll reports
already-touched electrodes as new touches. `touched_pins` and `touched_mask`
provide cached state from the last successful poll, initially empty/zero.

Call `poll()` regularly: it reads when IRQ is low or the fallback interval
expires (`poll_interval=0.1` seconds). `poll(force=True)` reads immediately.
It does not print, sleep, or start threads, but I2C/GPIO operations are
synchronous. In an async voice application, keep sensor operations in one
worker thread and forward events to the session. Serialize accesses to each
sensor and any shared bus. Fast transitions between reads can be missed.

Use the context manager or call `close()` in a `finally` block. Hardware errors
propagate to the caller. Closing releases the owned I2C connection and leaves
the sensor configured. QNX's `rpi_gpio` connection is process-wide; the driver
does not call its global `cleanup()`, which prevented subsequent GPIO use on
the tested image. Only the application should call that at final shutdown.

For shared GPIO, supply `irq_reader=lambda: GPIO.input(4) == GPIO.LOW` after
the application configures the pin. This avoids changing global numbering
mode or configuring GPIO inside the sensor. `irq_pin=None` disables GPIO.
You can also pass `i2c=your_bus`, implementing `read_byte_data`,
`write_byte_data`, and `read_i2c_block_data` with the usual address/register
arguments. Supplied interfaces remain owned by the caller; construction still
resets/configures the sensor.

The native I2C backend bypasses the installed QNX `smbus` module, which returned
incorrect zeros on the tested target. The native backend reads both status
bytes in a single transaction and reports device I/O errors.
