"""Behavior tests that do not require QNX or attached hardware."""

import sys
import unittest
from unittest.mock import Mock, patch

from commbadge.touch import MPR121


class FakeBus:
    def __init__(self, reset_value=0x24):
        self.reset_value = reset_value
        self.status = 0
        self.reads = 0
        self.closes = 0
        self.error = None

    def write_byte_data(self, address, register, value):
        pass

    def read_byte_data(self, address, register):
        return self.reset_value

    def read_i2c_block_data(self, address, register, length):
        assert (address, register, length) == (0x5A, 0, 2)
        self.reads += 1
        if self.error:
            raise self.error
        return self.status.to_bytes(2, "little")

    def close(self):
        self.closes += 1


class SensorTests(unittest.TestCase):
    def setUp(self):
        sleep = patch("commbadge.touch.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        clock = patch("commbadge.touch.time.monotonic", return_value=10.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.bus = FakeBus()

    def sensor(self, **kwargs):
        sensor = MPR121(i2c=self.bus, irq_pin=None, **kwargs)
        self.addCleanup(sensor.close)
        return sensor

    def test_simultaneous_touch_release_and_cached_state(self):
        sensor = self.sensor()
        self.bus.status = (1 << 0) | (1 << 11)
        events = sensor.poll()
        self.assertEqual([(e.electrode, e.touched) for e in events], [(0, True), (11, True)])
        self.assertEqual(sensor.touched_pins, (0, 11))
        self.assertEqual(sensor.touched_mask, 0x801)
        self.assertTrue(all(e.timestamp == 10 and not e.irq_active for e in events))
        self.assertEqual(sensor.poll(force=True), ())
        self.bus.status = 1 << 11
        events = sensor.poll(force=True)
        self.assertEqual([(e.electrode, e.touched) for e in events], [(0, False)])
        self.assertEqual(sensor.touched_pins, (11,))

    def test_irq_bypasses_interval_and_fallback_detects_missing_irq(self):
        irq = Mock(return_value=False)
        sensor = self.sensor(irq_reader=irq)
        sensor.poll()
        self.clock.return_value = 10.01
        self.bus.status = 1
        self.assertEqual(sensor.poll(), ())
        self.assertEqual(self.bus.reads, 1)
        irq.return_value = True
        self.assertTrue(sensor.poll()[0].irq_active)
        irq.return_value = False
        self.bus.status = 0
        self.clock.return_value = 10.2
        (event,) = sensor.poll()
        self.assertFalse(event.touched)
        self.assertFalse(event.irq_active)

    def test_read_failure_preserves_pending_transition(self):
        sensor = self.sensor()
        self.bus.error = OSError("I2C failure")
        with self.assertRaises(OSError):
            sensor.poll()
        self.assertEqual(sensor.touched_mask, 0)
        self.bus.error = None
        self.bus.status = 1
        self.assertEqual(len(sensor.poll()), 1)

    def test_overcurrent_does_not_update_cached_state(self):
        sensor = self.sensor()
        self.bus.status = 0x8001
        with self.assertRaisesRegex(RuntimeError, "overcurrent"):
            sensor.poll()
        self.assertEqual(sensor.touched_mask, 0)

    def test_external_resources_are_not_closed(self):
        with self.sensor(irq_reader=lambda: False) as sensor:
            sensor.poll()
        sensor.close()
        self.assertEqual(self.bus.closes, 0)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sensor.poll()

    def test_owned_resources_closed_once_even_on_application_error(self):
        gpio = Mock(BCM=11, IN=1, PUD_UP=2, LOW=0)
        with (
            patch("commbadge.touch.QNXI2C", return_value=self.bus),
            patch.dict(sys.modules, rpi_gpio=gpio),
        ):
            with self.assertRaisesRegex(ValueError, "application"):
                with MPR121() as sensor:
                    raise ValueError("application")
            sensor.close()
        self.assertEqual(self.bus.closes, 1)
        gpio.setup.assert_called_once_with(4, gpio.IN, gpio.PUD_UP)
        gpio.cleanup.assert_not_called()

    def test_failed_initialization_releases_owned_resources(self):
        self.bus.reset_value = 0
        gpio = Mock()
        with (
            patch("commbadge.touch.QNXI2C", return_value=self.bus),
            patch.dict(sys.modules, rpi_gpio=gpio),
        ):
            with self.assertRaisesRegex(RuntimeError, "expected 36"):
                MPR121()
        self.assertEqual(self.bus.closes, 1)
        gpio.cleanup.assert_not_called()

    def test_reopening_sensor_keeps_shared_gpio_connection_alive(self):
        gpio = Mock(BCM=11, IN=1, PUD_UP=2, LOW=0)
        gpio.input.return_value = 1
        buses = [FakeBus(), FakeBus()]
        with (
            patch("commbadge.touch.QNXI2C", side_effect=buses),
            patch.dict(sys.modules, rpi_gpio=gpio),
        ):
            for _ in range(2):
                with MPR121() as sensor:
                    self.assertEqual(sensor.poll(), ())
        self.assertEqual([bus.closes for bus in buses], [1, 1])
        gpio.cleanup.assert_not_called()

    def test_invalid_settings_fail_before_opening_hardware(self):
        with patch("commbadge.touch.QNXI2C") as factory:
            for kwargs in (
                {"address": 0x70},
                {"poll_interval": 0},
                {"poll_interval": float("nan")},
                {"poll_interval": float("inf")},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    MPR121(**kwargs)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
