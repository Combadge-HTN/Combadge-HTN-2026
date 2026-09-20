import asyncio

import pytest

from combadge.device import DoubleTap, run_badge
from combadge.touch import TouchEvent


def test_activation_chirp_is_the_selected_tng_clip():
    import hashlib

    from combadge.device import activation_chirp

    cue = activation_chirp()
    assert len(cue) == 21888  # 0.456 s, mono PCM16 at 24 kHz
    assert hashlib.sha256(cue).hexdigest() == (
        "837746606f55fa3b6e36dfcce20bcff424cd08fe0bb9d8d80d59c190fbae02b0"
    )


def test_haptic_uses_confirmed_pin_and_longer_wake_pulse(monkeypatch):
    from combadge.device import BadgeHardware, WAKE_HAPTIC, _pulse

    hardware = BadgeHardware()
    outputs = []
    durations = []
    monkeypatch.setattr(hardware, "output", lambda pin, active: outputs.append((pin, active)))

    async def sleep(duration):
        durations.append(duration)

    monkeypatch.setattr("combadge.device.asyncio.sleep", sleep)
    asyncio.run(_pulse(hardware, WAKE_HAPTIC))
    assert hardware.light_pin == 14
    assert hardware.haptic_pin == 15
    assert durations == [0.250]
    assert outputs == [(15, True), (15, False), (15, False)]


def test_cancelled_haptic_always_returns_motor_low():
    from combadge.device import _pulse

    async def scenario():
        outputs = []

        class Hardware:
            haptic_pin = 15

            def output(self, pin, active):
                outputs.append((pin, active))

        task = asyncio.create_task(_pulse(Hardware(), (10,)))
        while not outputs:
            await asyncio.sleep(0.001)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert outputs[-1] == (15, False)

    asyncio.run(scenario())


def touch(at, touched, electrode=0):
    return TouchEvent(electrode, touched, at, False)


def test_double_tap_requires_two_complete_short_taps_on_selected_electrode():
    gesture = DoubleTap(window=0.6)
    assert not gesture.feed(touch(1.00, True))
    assert not gesture.feed(touch(1.10, False))
    assert not gesture.feed(touch(1.30, True, electrode=1))
    assert not gesture.feed(touch(1.38, False, electrode=1))
    assert not gesture.feed(touch(1.40, True))
    assert gesture.feed(touch(1.50, False))


def test_slow_or_held_touches_do_not_activate():
    gesture = DoubleTap(window=0.6)
    for event in (
        touch(1.0, True),
        touch(1.8, False),
        touch(2.0, True),
        touch(2.1, False),
        touch(3.0, True),
        touch(3.1, False),
    ):
        assert not gesture.feed(event)


def test_controller_lights_the_full_session_and_returns_outputs_low(monkeypatch):
    class Hardware:
        light_pin = 15
        haptic_pin = 18

        def __init__(self):
            self.outputs = []
            self.polls = 0
            self.closed = False

        def configure(self):
            pass

        def poll(self):
            self.polls += 1
            if self.polls == 1:
                return (touch(1.0, True), touch(1.1, False))
            if self.polls == 2:
                return (touch(1.3, True), touch(1.4, False))
            return ()

        def output(self, pin, active):
            self.outputs.append((pin, active))

        def reset_sensor(self):
            pass

        def close(self):
            self.closed = True
            self.output(15, False)
            self.output(18, False)

    async def scenario():
        hardware = Hardware()
        shutdown = asyncio.Event()
        contexts = []

        async def no_pulse(*args):
            pass

        async def session(stop, continuity, context, cue):
            contexts.append(context)
            continuity.add("user", "status")
            assert cue
            assert (15, True) in hardware.outputs
            shutdown.set()

        monkeypatch.setattr("combadge.device._pulse", no_pulse)
        assert (
            await run_badge(
                session,
                hardware=hardware,
                shutdown=shutdown,
                report=lambda _: None,
            )
            == 0
        )
        assert contexts == [None]
        assert hardware.closed
        assert hardware.outputs[-2:] == [(15, False), (18, False)]

    asyncio.run(scenario())


@pytest.mark.parametrize("idle_exit", [False, True])
def test_light_off_precedes_cleanup_and_exit_chirp_follows_cleanup(monkeypatch, idle_exit):
    async def scenario():
        shutdown = asyncio.Event()
        sequence = []
        started = False
        cleaned = False

        class Hardware:
            light_pin = 14
            haptic_pin = 15
            polls = 0
            light = False

            def configure(self):
                pass

            def poll(self):
                if self.polls == 0:
                    self.polls = 1
                    return (touch(1, True), touch(1.1, False),
                            touch(1.2, True), touch(1.3, False))
                if started and not idle_exit and self.polls == 1:
                    self.polls = 2
                    return (touch(2, True), touch(2.1, False),
                            touch(2.2, True), touch(2.3, False))
                return ()

            def output(self, pin, active):
                if pin == 14:
                    self.light = active

            def close(self):
                self.light = False

        hardware = Hardware()

        async def pulse(*args):
            sequence.append("pulse-start")
            await asyncio.sleep(0.03)
            sequence.append("pulse-end")

        async def session(stop, *args):
            nonlocal started, cleaned
            assert "pulse-end" not in sequence
            started = True
            try:
                if idle_exit:
                    stop.set()
                else:
                    await asyncio.Future()
            finally:
                await asyncio.sleep(0.05)  # Simulate slow cloud/audio cleanup.
                assert not hardware.light
                cleaned = True

        async def exit_chirp():
            assert cleaned
            assert not hardware.light
            sequence.append("exit-chirp")
            shutdown.set()

        monkeypatch.setattr("combadge.device._pulse", pulse)
        await asyncio.wait_for(run_badge(
            session, hardware=hardware, shutdown=shutdown,
            end_cue=exit_chirp, report=lambda _: None,
        ), 2)
        assert sequence.count("exit-chirp") == 1

    asyncio.run(scenario())
