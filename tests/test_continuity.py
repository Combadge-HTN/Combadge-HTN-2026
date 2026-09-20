import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from test_live import FakeAudio, FakeConnection, event
from test_phone_live import call_events

from combadge.config import Settings
from combadge.continuity import MAX_CONTEXT_CHARS, VoiceContinuity
from combadge.live import LiveStats, connect_voice, run_session, session_config
from combadge.phone import direct
from combadge.phone.config import SipSettings
from combadge.speakers import SpeakerTracker


def phone_settings():
    return SipSettings(
        "test.pstn.twilio.com",
        "badge",
        "private-password",
        "+14165550100",
        {"alex": "+14165550101"},
    )


def test_recent_context_includes_transcripts_and_tool_receipt_even_without_captions():
    async def scenario():
        history = VoiceContinuity()
        connection = FakeConnection(
            [
                event("session.input_transcript.delta", delta="Call Alex"),
                event("session.output_transcript.delta", delta="Calling Alex"),
                *call_events(),
            ]
        )
        stop = asyncio.Event()
        stats = await run_session(
            connection,
            FakeAudio(stop),
            Settings(),
            stop,
            continuity=history,
            phone_settings=phone_settings(),
            captions=False,
            seconds=1,
            report=lambda _: None,
        )
        assert stats.phone_contact == "alex"
        context = json.loads(history.context())["previous_session_history"]
        assert context[0] == {"kind": "user", "text": "Call Alex"}
        assert context[1] == {"kind": "assistant", "text": "Calling Alex"}
        receipt = json.loads(context[2]["text"])
        assert receipt["tool"] == "call_contact"
        assert receipt["result"] == {
            "status": "handoff_requested",
            "contact": "alex",
            "dialed": False,
        }
        resumed = session_config(Settings(), resume_context=history.context())
        assert resumed["input"][0]["type"] == "message"  # Data, never replayed function calls.
        assert "Wait for a new user request" in resumed["instructions"]

    asyncio.run(scenario())


def test_context_is_bounded_and_discards_older_history():
    history = VoiceContinuity()
    history.add("user", "old request")
    for _ in range(100):
        history.add("user", "large turn " * 1000)
        history.add("assistant", "large reply " * 1000)
    history.add("call_outcome", '{"status":"no-answer"}')
    assert len(history.context()) <= MAX_CONTEXT_CHARS
    assert "old request" not in history.context()
    assert "no-answer" in history.context()
    # Adversarial control characters expand when JSON-encoded; still bounded.
    history.add("user", "\x01" * 20000)
    assert len(history.context()) <= MAX_CONTEXT_CHARS


@pytest.mark.parametrize("backend", ["commands", "console", "alsa", "mac"])
def test_call_returns_without_reopening_audio_tools_or_speaker_tracker(monkeypatch, backend):
    import websockets.asyncio.client

    from combadge import live
    from combadge.phone import client

    async def scenario():
        audio_factory = Mock(side_effect=lambda *args, **kwargs: NS(preflight=Mock()))
        for name in ("CommandAudio", "AlsaAudio", "MacAudio"):
            monkeypatch.setattr(live, name, audio_factory)

        class Context:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return NS()

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(websockets.asyncio.client, "connect", Context)
        caller = AsyncMock(return_value={"status": "completed"})
        monkeypatch.setattr(client, "call_until_stopped", caller)
        session = AsyncMock(
            side_effect=[
                LiveStats(finalized=True, close_reason="close_requested", phone_contact="alex"),
                LiveStats(finalized=True, close_reason="close_requested"),
            ]
        )
        monkeypatch.setattr(live, "run_session", session)
        tracker = SpeakerTracker(NS(references=["enrolled speaker"]))
        tracker.feed(b"\x01\x00" * 24_000 * 6)
        tracker.awaiting["old_context"] = 0
        features = dict(
            snapshot_capture=Mock(), shopping=Mock(), web=Mock(), composio=Mock(), sms=Mock()
        )
        commands = (
            dict(capture_command=["capture-helper"], playback_command=["playback-helper"])
            if backend == "commands"
            else {}
        )
        await connect_voice(
            Settings(),
            check=False,
            input_device="mic",
            output_device="speaker",
            seconds=1,
            captions=False,
            backend=backend,
            phone_settings=phone_settings(),
            speaker_tracker=tracker,
            **commands,
            **features,
        )
        assert caller.await_count == 1 and session.await_count == 1
        assert audio_factory.call_count == 1
        first = session.call_args
        for name, value in features.items():
            assert first.kwargs[name] is value
        assert first.kwargs["speaker_tracker"] is tracker

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["stop", "none", "unconfirmed", "raises", "completed"])
def test_call_stop_or_uncertain_teardown_never_redials_or_reopens_voice(monkeypatch, mode):
    async def scenario():
        import websockets.asyncio.client

        from combadge import live
        from combadge.phone import client

        opened = []

        class Context:
            def __init__(self, *args, **kwargs):
                opened.append(self)

            async def __aenter__(self):
                if len(opened) == 2:
                    raise ConnectionError("Reconnect failed")
                return NS()

            async def __aexit__(self, *args):
                pass

        async def phone(settings, contact, audio, stop, **kwargs):
            if mode == "raises":
                raise RuntimeError("Hang-up unconfirmed")
            if mode == "stop":
                stop.set()
            if mode == "none":
                return None
            return {"status": "hangup-unconfirmed" if mode == "unconfirmed" else "completed"}

        caller = AsyncMock(side_effect=phone)
        monkeypatch.setattr(websockets.asyncio.client, "connect", Context)
        monkeypatch.setattr(live, "AlsaAudio", lambda *_: NS(preflight=Mock()))
        monkeypatch.setattr(
            live,
            "run_session",
            AsyncMock(
                return_value=LiveStats(
                    finalized=True,
                    close_reason="close_requested",
                    phone_contact="alex",
                )
            ),
        )
        monkeypatch.setattr(client, "call_until_stopped", caller)
        session = connect_voice(
            Settings(),
            check=False,
            input_device="default",
            output_device="default",
            seconds=1,
            captions=False,
            backend="alsa",
            phone_settings=phone_settings(),
        )
        if mode in ("unconfirmed", "raises"):
            with pytest.raises(RuntimeError, match="unconfirmed"):
                await session
        else:
            await session
        assert len(opened) == 1
        assert caller.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed", [True, False])
def test_unanswered_sip_call_returns_only_after_confirmed_hangup(monkeypatch, confirmed):
    class Call:
        def __init__(self, *args):
            self.invited = self.established = self.ended = False
            self.sent_packets = self.received_packets = self.rejected_packets = 0

        async def open(self):
            pass

        async def invite(self):
            self.invited = True
            raise TimeoutError()

        async def hangup(self):
            if not confirmed:
                raise TimeoutError()
            self.ended = True

        async def close(self):
            pass

    monkeypatch.setattr(direct, "SipCall", Call)
    monkeypatch.setattr(direct, "crypto", lambda: None)
    call = direct.call_contact(phone_settings(), "alex", None, report=lambda _: None)
    if confirmed:
        assert asyncio.run(call)["status"] == "no-answer"
    else:
        with pytest.raises(RuntimeError, match="hang-up unconfirmed"):
            asyncio.run(call)
