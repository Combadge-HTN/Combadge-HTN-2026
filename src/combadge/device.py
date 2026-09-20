"""Long-lived touch-activated badge controller for QNX hardware."""

import asyncio
import signal
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from importlib.resources import files

from combadge.continuity import VoiceContinuity
from combadge.touch import MPR121, TouchEvent

WAKE_HAPTIC = (0.250,)
SLEEP_HAPTIC = (0.150, 0.150)
FAILURE_HAPTIC = (0.120, 0.120, 0.120)


def activation_chirp() -> bytes:
    """Return the selected TNG communicator clip as 24 kHz mono PCM16."""
    return files("combadge").joinpath("assets/tng_chirp_clean.pcm").read_bytes()


class DoubleTap:
    """Recognize two completed taps on one MPR121 electrode."""

    def __init__(
        self,
        electrode: int = 0,
        *,
        window: float = 0.6,
        minimum_touch: float = 0.02,
        maximum_touch: float = 0.5,
    ):
        self.electrode = electrode
        self.window = window
        self.minimum_touch = minimum_touch
        self.maximum_touch = maximum_touch
        self._touched_at: float | None = None
        self._first_tap: float | None = None

    def feed(self, event: TouchEvent) -> bool:
        if event.electrode != self.electrode:
            return False
        if event.touched:
            if self._touched_at is None:
                self._touched_at = event.timestamp
            return False
        if self._touched_at is None:
            return False
        duration = event.timestamp - self._touched_at
        self._touched_at = None
        if not self.minimum_touch <= duration <= self.maximum_touch:
            self._first_tap = None
            return False
        if self._first_tap is not None and event.timestamp - self._first_tap <= self.window:
            self._first_tap = None
            return True
        self._first_tap = event.timestamp
        return False


class BadgeHardware:
    """Serialized access to MPR121 and the process-wide QNX GPIO module."""

    def __init__(
        self,
        *,
        bus: int = 1,
        address: int = 0x5A,
        irq_pin: int = 4,
        light_pin: int = 14,
        haptic_pin: int = 15,
    ):
        self.bus = bus
        self.address = address
        self.irq_pin = irq_pin
        self.light_pin = light_pin
        self.haptic_pin = haptic_pin
        self._lock = threading.RLock()
        self._gpio = None
        self._sensor = None

    def configure(self) -> None:
        import rpi_gpio

        with self._lock:
            self._gpio = rpi_gpio
            rpi_gpio.setmode(rpi_gpio.BCM)
            rpi_gpio.setup(self.irq_pin, rpi_gpio.IN, rpi_gpio.PUD_UP)
            rpi_gpio.setup(self.light_pin, rpi_gpio.OUT)
            rpi_gpio.setup(self.haptic_pin, rpi_gpio.OUT)
            rpi_gpio.output(self.light_pin, rpi_gpio.LOW)
            rpi_gpio.output(self.haptic_pin, rpi_gpio.LOW)

    def _ensure_sensor(self) -> None:
        if self._sensor is None:
            gpio = self._gpio
            self._sensor = MPR121(
                bus=self.bus,
                address=self.address,
                irq_reader=lambda: gpio.input(self.irq_pin) == gpio.LOW,
            )

    def poll(self) -> tuple[TouchEvent, ...]:
        with self._lock:
            self._ensure_sensor()
            return self._sensor.poll()

    def reset_sensor(self) -> None:
        with self._lock:
            if self._sensor is not None:
                self._sensor.close()
                self._sensor = None

    def output(self, pin: int, active: bool) -> None:
        with self._lock:
            if self._gpio is None:
                return
            self._gpio.output(pin, self._gpio.HIGH if active else self._gpio.LOW)

    def safe(self) -> None:
        try:
            self.output(self.light_pin, False)
        finally:
            self.output(self.haptic_pin, False)

    def close(self) -> None:
        try:
            self.reset_sensor()
        finally:
            self.safe()


async def _pulse(hardware: BadgeHardware, pattern: tuple[float, ...]) -> None:
    try:
        for index, duration in enumerate(pattern):
            await asyncio.to_thread(hardware.output, hardware.haptic_pin, True)
            await asyncio.sleep(duration)
            await asyncio.to_thread(hardware.output, hardware.haptic_pin, False)
            if index + 1 < len(pattern):
                await asyncio.sleep(0.060)
    finally:
        await asyncio.to_thread(hardware.output, hardware.haptic_pin, False)


async def run_badge(
    session: Callable[[asyncio.Event, VoiceContinuity, str | None, bytes], Awaitable[object]],
    *,
    hardware: BadgeHardware | None = None,
    electrode: int = 0,
    double_tap_window: float = 0.6,
    report: Callable[[str], None] = print,
    shutdown: asyncio.Event | None = None,
    describe_error: Callable[[Exception], str] = str,
    end_cue: Callable[[], Awaitable[None]] | None = None,
) -> int:
    """Run until SIGINT/SIGTERM, creating one fresh Live session per activation."""
    hardware = hardware or BadgeHardware()
    gestures: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
    terminate = shutdown or asyncio.Event()
    continuity = VoiceContinuity()
    recognizer = DoubleTap(electrode, window=double_tap_window)
    loop = asyncio.get_running_loop()
    sensor_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="badge-touch")
    worker_stop = threading.Event()
    monitor_task = None
    active = None
    wake = None

    async def sensor_call(function):
        return await loop.run_in_executor(sensor_worker, partial(function))

    def poll_changes():
        # Keep 200 Hz GPIO polling off the latency-sensitive audio event loop.
        while not worker_stop.is_set():
            events = hardware.poll()
            if events:
                return events
            worker_stop.wait(0.005)
        return ()
    installed = []
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, terminate.set)
            installed.append(name)
        except (NotImplementedError, RuntimeError):
            pass

    async def monitor() -> None:
        backoff = 1.0
        failed = False
        while not terminate.is_set():
            try:
                events = await sensor_call(poll_changes)
                if failed:
                    report("Touch sensor recovered; badge idle.")
                failed = False
                backoff = 1.0
                for event in events:
                    if recognizer.feed(event):
                        if gestures.full():
                            gestures.get_nowait()
                        gestures.put_nowait("toggle")
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                raise
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                await sensor_call(hardware.reset_sensor)
                recognizer._touched_at = None
                recognizer._first_tap = None
                if not failed:
                    failed = True
                    report(f"Touch sensor unavailable: {error}; retrying.")
                    if gestures.empty():
                        gestures.put_nowait("hardware_error")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    try:
        setup_backoff = 1.0
        while not terminate.is_set():
            try:
                await asyncio.to_thread(hardware.configure)
                break
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                report(f"Badge GPIO unavailable: {error}; retrying.")
                await asyncio.sleep(setup_backoff)
                setup_backoff = min(setup_backoff * 2, 30.0)
        if terminate.is_set():
            return 0
        monitor_task = asyncio.create_task(monitor())
        report("Badge idle; double tap electrode 0 to activate.")
        while not terminate.is_set():
            gesture_task = asyncio.create_task(gestures.get())
            shutdown_task = asyncio.create_task(terminate.wait())
            done, pending = await asyncio.wait(
                (gesture_task, shutdown_task), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if shutdown_task in done and shutdown_task.result():
                break
            gesture = gesture_task.result()
            if gesture == "hardware_error":
                await _pulse(hardware, FAILURE_HAPTIC)
                continue

            await asyncio.to_thread(hardware.output, hardware.light_pin, True)
            wake = asyncio.create_task(_pulse(hardware, WAKE_HAPTIC))
            stop = asyncio.Event()
            context = continuity.context() if continuity.entries else None
            active = asyncio.create_task(session(stop, continuity, context, activation_chirp()))
            toggle = asyncio.create_task(gestures.get())
            shutdown = asyncio.create_task(terminate.wait())
            session_stop = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait(
                (active, toggle, shutdown, session_stop), return_when=asyncio.FIRST_COMPLETED
            )
            explicit_stop = toggle in done and toggle.result() == "toggle"
            hardware_failed = toggle in done and toggle.result() == "hardware_error"
            shutting_down = shutdown in done and shutdown.result()
            # Visible feedback must not wait for audio/cloud teardown.
            await asyncio.to_thread(hardware.output, hardware.light_pin, False)
            if explicit_stop or hardware_failed or shutting_down:
                stop.set()
                active.cancel()
            for task in (toggle, shutdown, session_stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(active, toggle, shutdown, session_stop, return_exceptions=True)
            await wake
            failure = None
            if active.done():
                try:
                    active.result()
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    failure = error
            if hardware_failed:
                failure = RuntimeError("touch sensor became unavailable")
            if shutting_down:
                break
            if failure is not None:
                report(f"Interactive session failed: {describe_error(failure)}; returning to idle.")
                await _pulse(hardware, FAILURE_HAPTIC)
            else:
                if end_cue is not None:
                    results = await asyncio.gather(
                        end_cue(), _pulse(hardware, SLEEP_HAPTIC), return_exceptions=True
                    )
                    for error in results:
                        if isinstance(error, Exception):
                            report(f"Exit feedback failed: {describe_error(error)}")
                else:
                    await _pulse(hardware, SLEEP_HAPTIC)
            report("Badge idle; double tap electrode 0 to activate.")
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        return 0
    finally:
        worker_stop.set()
        if wake is not None and not wake.done():
            wake.cancel()
            await asyncio.gather(wake, return_exceptions=True)
        if active is not None and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if monitor_task is not None:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        for name in installed:
            loop.remove_signal_handler(name)
        try:
            await sensor_call(hardware.close)
        finally:
            sensor_worker.shutdown(wait=False)
