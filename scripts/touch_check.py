#!/usr/bin/env python3
"""CLI example for the reusable MPR121 class on QNX.

Defaults: /dev/i2c1, address 0x5A, IRQ BCM GPIO 4 (physical pin 7).
Run with the QNX system Python as root. No Adafruit/Blinka packages required.
"""

import argparse
import math
import sys
import time

from commbadge.touch import MPR121, QNXI2C


def diagnose(bus):
    """Read configuration at candidate addresses without resetting devices."""
    print("Reading config registers at 0x5A-0x5D; no reset/config writes.")
    print("After a successful reset: 0x5C=0x10, 0x5D=0x24, 0x5E=0x00.")
    print("Other values may be existing configuration; reads alone do not identify a chip.")
    readable = False
    for address in range(0x5A, 0x5E):
        values = []
        for register in (0x5C, 0x5D, 0x5E):
            try:
                value = bus.read_byte_data(address, register)
                values.append(f"0x{register:02X}=0x{value:02X}")
                readable = True
            except (OSError, RuntimeError, ValueError) as error:
                values.append(f"0x{register:02X}=ERROR({error})")
        print(f"  address 0x{address:02X}: " + ", ".join(values), flush=True)
    return 0 if readable else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default: 1)")
    parser.add_argument(
        "--address",
        type=lambda x: int(x, 0),
        default=0x5A,
        choices=range(0x5A, 0x5E),
        metavar="0x5A-0x5D",
    )
    parser.add_argument("--irq", type=int, default=4, help="BCM GPIO for IRQ (default: 4)")
    parser.add_argument(
        "--poll", action="store_true", help="test I2C only, without importing or accessing GPIO"
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="read config at 0x5A-0x5D, without GPIO or sensor initialization",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="stop after this many seconds; 0 means run until Ctrl+C",
    )
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("--duration must be finite and nonnegative")

    try:
        print(
            f"Python: {sys.executable}\nI2C backend: native QNX devctl\n"
            f"I2C bus: /dev/i2c{args.bus}; selected address: 0x{args.address:02X}",
            flush=True,
        )
        if args.diagnose:
            bus = QNXI2C(args.bus)
            try:
                return diagnose(bus)
            finally:
                bus.close()

        print("Initializing MPR121; leave electrodes untouched.", flush=True)
        with MPR121(
            bus=args.bus, address=args.address, irq_pin=None if args.poll else args.irq
        ) as sensor:
            deadline = time.monotonic() + args.duration if args.duration else None
            mode = "I2C polling" if args.poll else f"IRQ on BCM GPIO {args.irq}"
            print(
                f"Ready: /dev/i2c{args.bus}, 0x{args.address:02X}, {mode}. Ctrl+C to stop.",
                flush=True,
            )
            while deadline is None or time.monotonic() < deadline:
                sensor.poll(force=True)
                print("TOUCHED" if sensor.touched_mask else "NOT TOUCHED", flush=True)
                time.sleep(0.1)
            print("Test duration complete.", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except ImportError as error:
        print(
            f"Missing QNX module: {error}\n"
            "Use the QNX system Python with rpi_gpio, or --poll for I2C only.",
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, ValueError) as error:
        print(
            f"Hardware error: {error}\n"
            "Check root access, /dev/i2c*, /dev/gpio, and wiring; use --diagnose for I2C details.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
