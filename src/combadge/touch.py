"""Reusable MPR121 touch detection for QNX 8 on Raspberry Pi.

Importing this module does not open hardware. MPR121 construction resets and
configures the sensor, so leave electrodes untouched during construction.
"""

import ctypes
import math
import os
import struct
import sys
import time
from dataclasses import dataclass


class QNXI2C:
    """QNX 8 hw/i2c.h ABI; call libc directly, avoiding the SMBus wrapper.

    Fixed-width headers and command encodings were checked against the target's
    hw/i2c.h and devctl.h. SENDRECV returns data at the same offset as send data.
    """

    SEND = 0x80100505  # __DIOT(_DCMD_MISC, 5, i2c_send_t)
    SENDRECV = 0xC0140507  # __DIOTF(_DCMD_MISC, 7, i2c_sendrecv_t)
    ADDRESS_7BIT = 2

    def __init__(self, bus):
        if not sys.platform.startswith("qnx"):
            raise RuntimeError("The native I2C backend requires QNX")
        self._libc = ctypes.CDLL(None)
        self._devctl = self._libc.devctl
        self._devctl.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int),
        ]
        self._devctl.restype = ctypes.c_int
        self.path = f"/dev/i2c{bus}"
        self.fd = os.open(self.path, os.O_RDWR)

    def _transfer(self, command, header, data):
        if self.fd is None:
            raise RuntimeError("I2C connection is closed")
        message = ctypes.create_string_buffer(header + data, len(header) + len(data))
        result = self._devctl(self.fd, ctypes.c_int(command).value, message, len(message), None)
        # devctl returns an error number, not -1/errno.
        if result != 0:
            raise OSError(result, os.strerror(result), self.path)
        return message.raw[len(header) :]

    def read_i2c_block_data(self, address, register, length):
        if not 1 <= length <= 42:
            raise ValueError("Read length must be between 1 and 42 bytes")
        header = struct.pack("=5I", address, self.ADDRESS_7BIT, 1, length, 1)
        return self._transfer(self.SENDRECV, header, bytes([register]) + bytes(length - 1))

    def read_byte_data(self, address, register):
        return self.read_i2c_block_data(address, register, 1)[0]

    def write_byte_data(self, address, register, value):
        header = struct.pack("=4I", address, self.ADDRESS_7BIT, 2, 1)
        self._transfer(self.SEND, header, bytes([register, value]))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _initialize(bus, address):
    """Reset, configure in stop mode, then enable all twelve electrodes."""

    def write(register, value):
        bus.write_byte_data(address, register, value)

    write(0x80, 0x63)  # Soft reset.
    time.sleep(0.01)
    write(0x5E, 0x00)  # Stop mode allows configuration writes.
    config2 = bus.read_byte_data(address, 0x5D)
    if config2 != 0x24:
        raise RuntimeError(
            f"MPR121 at 0x{address:02X}: after reset, register 0x5D "
            f"returned {config2!r} (expected 36 / 0x24).\n"
            "Check the bus, address, and wiring."
        )

    # Rising, falling, and touched baseline filter settings.
    for register, value in (
        (0x2B, 1),
        (0x2C, 1),
        (0x2D, 14),
        (0x2E, 0),
        (0x2F, 1),
        (0x30, 5),
        (0x31, 1),
        (0x32, 0),
        (0x33, 0),
        (0x34, 0),
        (0x35, 0),
    ):
        write(register, value)
    for electrode in range(12):
        write(0x41 + 2 * electrode, 12)  # Touch threshold.
        write(0x42 + 2 * electrode, 6)  # Release threshold.
    write(0x5B, 0x00)  # No additional touch/release debounce.
    write(0x5C, 0x10)  # 16 uA charge current.
    write(0x5D, 0x20)  # 0.5 us charge time, 1 ms sample interval.
    write(0x5E, 0x8C)  # Initialize baseline, enable E0-E11, no proximity.
    time.sleep(0.1)


@dataclass(frozen=True)
class TouchEvent:
    """An electrode transition from one status snapshot.

    timestamp uses time.monotonic(), in seconds. irq_active records the GPIO
    level sampled before the I2C read; it is not a hardware interrupt timestamp.
    """

    electrode: int
    touched: bool
    timestamp: float
    irq_active: bool


class MPR121:
    """Initialize the sensor and produce events through poll().

    Defaults use QNX I2C bus 1 and BCM GPIO 4. irq_pin=None selects I2C-only
    polling. poll_interval sets the fallback I2C interval in seconds; call
    poll() frequently (e.g. every 5 ms) to also respond promptly to IRQ.

    An optional i2c object must provide read_byte_data, write_byte_data, and
    read_i2c_block_data. An optional irq_reader() returns True when IRQ is
    asserted (LOW). Supplied interfaces remain owned by the caller; close()
    never closes them. With irq_reader supplied, irq_pin is ignored and this
    class never imports, configures, or cleans up rpi_gpio.

    Default GPIO management sets the process-wide rpi_gpio mode to BCM. Its
    module-level connection is shared and stays open for the process lifetime;
    the sensor never calls global GPIO cleanup. Applications sharing GPIO
    should supply irq_reader and manage GPIO themselves. Calls on an instance,
    and accesses to a shared bus, must be serialized by the application.
    """

    def __init__(
        self, bus=1, address=0x5A, irq_pin=4, *, poll_interval=0.1, i2c=None, irq_reader=None
    ):
        if address not in range(0x5A, 0x5E):
            raise ValueError("MPR121 address must be 0x5A through 0x5D")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive")
        if irq_reader is not None and not callable(irq_reader):
            raise TypeError("irq_reader must be callable")
        self.address = address
        self.poll_interval = poll_interval
        self._bus = i2c
        self._owns_bus = i2c is None
        self._gpio = None
        self._irq_reader = irq_reader
        self._closed = False
        self._touched_mask = 0
        self._next_check = 0.0
        try:
            if self._bus is None:
                self._bus = QNXI2C(bus)
            if irq_reader is None and irq_pin is not None:
                import rpi_gpio

                self._gpio = rpi_gpio
                self._gpio.setmode(self._gpio.BCM)
                self._gpio.setup(irq_pin, self._gpio.IN, self._gpio.PUD_UP)
                self._irq_reader = lambda: self._gpio.input(irq_pin) == self._gpio.LOW
            _initialize(self._bus, self.address)
        except BaseException:
            self.close()
            raise

    @property
    def touched_mask(self):
        """Cached 12-bit state from the last successful poll; initially zero."""
        return self._touched_mask

    @property
    def touched_pins(self):
        """Tuple of currently touched electrode numbers from cached state."""
        return tuple(pin for pin in range(12) if self._touched_mask & (1 << pin))

    def poll(self, *, force=False):
        """Return a tuple of TouchEvent objects without sleeping or printing.

        Reads I2C when IRQ is asserted, the fallback interval is due, or force
        is True. The first call always reads; initially touched electrodes
        produce touch events. Unchanged state produces no events. Each read
        returns a coherent two-byte snapshot and acknowledges sensor IRQ.

        Hardware errors propagate, leaving the previous event state intact.
        This call performs synchronous GPIO/I2C operations, so it can block on
        the driver. It starts no threads and cannot recover transitions that
        happen completely between reads.
        """
        if self._closed:
            raise RuntimeError("MPR121 is closed")
        now = time.monotonic()
        irq_active = bool(self._irq_reader()) if self._irq_reader is not None else False
        if not (force or irq_active or now >= self._next_check):
            return ()

        low, high = self._bus.read_i2c_block_data(self.address, 0x00, 2)
        if high & 0x80:
            raise RuntimeError("MPR121 overcurrent fault; check the breakout's REXT circuit")
        current = ((high << 8) | low) & 0x0FFF
        changed = self._touched_mask ^ current
        events = tuple(
            TouchEvent(pin, bool(current & (1 << pin)), now, irq_active)
            for pin in range(12)
            if changed & (1 << pin)
        )
        self._touched_mask = current
        self._next_check = now + self.poll_interval
        return events

    def close(self):
        """Close owned I2C once; leave sensor configuration and shared GPIO live.

        rpi_gpio.cleanup() closes a module-global descriptor that the tested
        QNX module cannot reopen. Only the application should call it, after
        all GPIO users have finished (normally at process shutdown).
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_bus and self._bus is not None:
            self._bus.close()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("MPR121 is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
