from unittest.mock import patch

import pytest

from combadge.cli import main
from combadge.startup import ROOT, launch


@pytest.fixture(autouse=True)
def one_touch_activation(monkeypatch):
    """Keep startup composition tests finite now that `start` is a daemon."""

    async def run_once(session, **kwargs):
        import asyncio

        from combadge.continuity import VoiceContinuity

        await session(asyncio.Event(), VoiceContinuity(), None, b"cue")
        return 0

    monkeypatch.setattr("combadge.device.run_badge", run_once)


def test_start_uses_console_camera_and_repo_environment_from_any_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--no-bluetooth", "--max-seconds", "30"]) == 0
    args = routed.call_args.args[0]
    with patch("combadge.cli.main", return_value=0) as voice:
        assert launch(args) == 0
    command = voice.call_args.args[0]
    assert command[:3] == ["voice", "--audio-backend", "console"]
    assert "--touch-activate" in command
    assert command[command.index("--env-file") + 1] == str(ROOT / ".env")
    assert command[command.index("--camera-unit") + 1] == str(args.camera_unit)
    assert "--shopify" in command


def test_start_can_disable_camera():
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--no-bluetooth", "--no-camera"]) == 0
    with patch("combadge.cli.main", return_value=0) as voice:
        launch(routed.call_args.args[0])
    assert "--camera" not in voice.call_args.args[0]


def test_start_forwards_saved_snapshots_as_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--no-bluetooth", "--save-snapshots", "images"]) == 0
    with patch("combadge.cli.main", return_value=0) as voice:
        launch(routed.call_args.args[0])
    command = voice.call_args.args[0]
    assert command[command.index("--save-snapshots") + 1] == str(tmp_path / "images")


@pytest.mark.parametrize("options", [[], ["--bluetooth"]])
def test_bluetooth_defaults_on_and_routes_to_supervisor(options):
    with patch("combadge.bluetooth_startup.launch", return_value=0) as bluetooth:
        assert main(["start", *options, "--max-seconds", "30"]) == 0
    assert bluetooth.call_args.args[0].bluetooth


def test_bluetooth_reconfigures_48khz_before_opening_fifo():
    from combadge.bluetooth_startup import Startup

    state = Startup()
    assert state.observe("Controller ready.") == b"a"
    state.observe("Received SBC codec configuration, sampling frequency 48000")
    assert state.observe("Stream established") == b"w"
    state.observe("Received SBC codec configuration, sampling frequency 44100")
    assert state.observe("Stream started") == b"f"
    assert not state.ready
    state.observe("PCM FIFO ready")
    assert state.ready


@pytest.mark.parametrize("option", [None, "--shopify", "--shop-account", "--no-shopify"])
def test_start_enables_requested_shopping_session(tmp_path, option):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=test-key\n")
    auth = tmp_path / "auth.json"
    command = [
        "start",
        "--no-bluetooth",
        "--no-calls",
        "--no-camera",
        "--env-file",
        str(env),
        "--shop-auth-file",
        str(auth),
    ]
    if option:
        command.append(option)
    with (
        patch("combadge.live.connect_voice") as connect,
        patch("combadge.cli.ShopAccount") as account,
    ):
        assert main(command) == 0
    shopping = connect.call_args.kwargs["shopping"]
    if option == "--no-shopify":
        assert shopping is None
        account.assert_not_called()
    else:
        assert shopping is not None
        if option == "--shop-account":
            assert shopping.account is account.return_value
            account.return_value.access_token.assert_called_once_with()
        else:
            assert shopping.account is None
            account.assert_not_called()


def test_start_automatically_uses_connected_account(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=test-key\n")
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    with (
        patch("combadge.live.connect_voice") as connect,
        patch("combadge.cli.ShopAccount") as account,
    ):
        assert (
            main(
                [
                    "start",
                    "--no-bluetooth",
                    "--no-calls",
                    "--no-camera",
                    "--env-file",
                    str(env),
                    "--shop-auth-file",
                    str(auth),
                ]
            )
            == 0
        )
    assert connect.call_args.kwargs["shopping"].account is account.return_value
    account.return_value.access_token.assert_called_once_with()


def test_start_selects_present_physical_camera():
    with (
        patch("combadge.startup.Path.exists", side_effect=lambda: True),
        patch("combadge.startup.launch", return_value=0) as routed,
    ):
        assert main(["start"]) == 0
    assert routed.call_args.args[0].camera_unit == 3


def test_bluetooth_preserves_shopping_and_user_auth_path_across_sudo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with (
        patch("combadge.bluetooth_startup.platform.system", return_value="QNX"),
        patch("combadge.bluetooth_startup.os.geteuid", return_value=1000),
        patch("combadge.bluetooth_startup.os.execvp", side_effect=SystemExit) as execute,
        pytest.raises(SystemExit),
    ):
        main(["start", "--bluetooth", "--shop-account", "--shop-auth-file", "auth.json"])
    command = execute.call_args.args[1]
    assert "--shopify" in command
    assert "--shop-account" in command
    assert command[command.index("--shop-auth-file") + 1] == str(tmp_path / "auth.json")


@pytest.mark.parametrize("enabled", [True, False])
def test_call_preference_survives_sudo_and_console(enabled):
    option = [] if enabled else ["--no-calls"]
    expected = "--calls" if enabled else "--no-calls"
    with (
        patch("combadge.bluetooth_startup.platform.system", return_value="QNX"),
        patch("combadge.bluetooth_startup.os.geteuid", return_value=1000),
        patch("combadge.bluetooth_startup.os.execvp", side_effect=SystemExit) as execute,
        pytest.raises(SystemExit),
    ):
        main(["start", *option])
    command = execute.call_args.args[1]
    assert expected in command
    # Parse the exact arguments handed to the privileged Bluetooth supervisor.
    import argparse

    from combadge.startup import add_arguments

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(command[command.index("combadge.bluetooth_startup") + 1 :])
    assert args.calls is enabled
    args.bluetooth = False
    with patch("combadge.cli.main", return_value=0) as voice:
        launch(args)
    assert expected in voice.call_args.args[0]


@pytest.mark.parametrize("enabled", [True, False])
def test_bluetooth_app_receives_call_preference(tmp_path, monkeypatch, enabled):
    import argparse
    import os
    from unittest.mock import Mock

    from combadge import bluetooth_startup
    from combadge.startup import add_arguments

    driver = tmp_path / "driver"
    for path in (
        driver / "run-radio.sh",
        driver / "play_pcm.py",
        driver / "btstack-master/port/qnx-pi5/a2dp_source_demo",
        tmp_path / ".venv/bin/combadge",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    os.mkfifo(driver / "audio.pcm")
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(
        ["--bluetooth-dir", str(driver), "--speaker", f"Edmon={tmp_path / 'edmon.wav'}"]
    )
    args.calls = enabled
    monkeypatch.setenv("SUDO_USER", "qnxuser")
    app = Mock(returncode=0)
    app.poll.return_value = 0
    with (
        patch.object(bluetooth_startup, "ROOT", tmp_path),
        patch.object(bluetooth_startup.platform, "system", return_value="QNX"),
        patch.object(bluetooth_startup.os, "geteuid", return_value=0),
        patch.object(bluetooth_startup, "idle_pins"),
        patch.object(bluetooth_startup, "Radio"),
        patch.object(bluetooth_startup, "stop_process"),
        patch.object(bluetooth_startup.subprocess, "run"),
        patch.object(bluetooth_startup.subprocess, "check_output", return_value="enabled"),
        patch.object(bluetooth_startup.subprocess, "Popen", return_value=app) as launch_app,
        patch.object(bluetooth_startup.time, "monotonic", side_effect=[0, 2]),
    ):
        bluetooth_startup.run(args)
    command = launch_app.call_args.args[0]
    assert ("--calls" if enabled else "--no-calls") in command
    assert ("--no-calls" if enabled else "--calls") not in command
    assert command[command.index("--speaker") + 1] == f"Edmon={tmp_path / 'edmon.wav'}"


def test_enrollment_survives_sudo_directory_change_and_reaches_voice(tmp_path, monkeypatch):
    import argparse

    from combadge.speakers import pcm_wav
    from combadge.startup import add_arguments

    monkeypatch.chdir(tmp_path)
    references = ["Edmon=edmon reference.wav", "Samuel=samuel.wav"]
    for reference in references:
        (tmp_path / reference.partition("=")[2]).write_bytes(pcm_wav(b"\x01\x00" * 48000))
    with (
        patch("combadge.bluetooth_startup.platform.system", return_value="QNX"),
        patch("combadge.bluetooth_startup.os.geteuid", return_value=1000),
        patch("combadge.bluetooth_startup.os.execvp", side_effect=SystemExit) as execute,
        pytest.raises(SystemExit),
    ):
        main(["start", "--speaker", references[0], "--speaker", references[1]])
    command = execute.call_args.args[1]
    monkeypatch.chdir(tmp_path.parent)
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(command[command.index("combadge.bluetooth_startup") + 1 :])
    expected = [f"{name}={tmp_path / path}" for name, path in (s.split("=", 1) for s in references)]
    assert args.speaker == expected
    args.bluetooth = False
    with patch("combadge.cli.main", return_value=0) as voice:
        launch(args)
    voice_command = voice.call_args.args[0]
    assert [
        voice_command[i + 1] for i, arg in enumerate(voice_command) if arg == "--speaker"
    ] == expected


@pytest.mark.parametrize("reference", ["Edmon", "=file.wav", "Edmon="])
def test_start_rejects_malformed_reference_before_launch(reference):
    with patch("combadge.startup.launch") as launch_start, pytest.raises(SystemExit) as error:
        main(["start", "--speaker", reference])
    assert error.value.code == 2
    launch_start.assert_not_called()


@pytest.mark.parametrize("tone", [False, True])
def test_invalid_enrollment_never_starts_bluetooth(tmp_path, tone):
    options = ["--tone"] if tone else []
    with (
        patch("combadge.bluetooth_startup.os.execvp") as sudo,
        patch("combadge.bluetooth_startup.run") as radio,
    ):
        assert main(["start", *options, "--speaker", f"Edmon={tmp_path / 'missing.wav'}"]) == 2
    sudo.assert_not_called()
    radio.assert_not_called()


def test_start_selects_streaming_speakers_without_legacy_background_tracker(tmp_path, monkeypatch):
    from combadge.speakers import pcm_wav
    from combadge.speechmatics import StreamingSpeakerInput

    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=test\nSPEECHMATICS_API_KEY=stream-test\n")
    reference = tmp_path / "edmon.wav"
    reference.write_bytes(pcm_wav(b"\x01\x00" * 48000))
    with patch("combadge.live.connect_voice") as connect:
        assert (
            main(
                [
                    "start",
                    "--no-bluetooth",
                    "--no-calls",
                    "--no-camera",
                    "--no-shopify",
                    "--env-file",
                    str(env),
                    "--speaker",
                    f"Edmon={reference}",
                ]
            )
            == 0
        )
    assert isinstance(connect.call_args.kwargs["speaker_input"], StreamingSpeakerInput)
    assert connect.call_args.kwargs["speaker_tracker"] is None


def saved_speakers(tmp_path, monkeypatch):
    import json

    from combadge.speakers import pcm_wav

    monkeypatch.delenv("COMBADGE_SPEAKERS", raising=False)
    env = tmp_path / ".env"
    mapping = {"Edmon": "edmon reference.wav", "Samuel": "samuel.wav"}
    for path in mapping.values():
        (tmp_path / path).write_bytes(pcm_wav(b"\x01\x00" * 48000))
    env.write_text(
        "OPENAI_API_KEY=test\nSPEECHMATICS_API_KEY=stream-test\n"
        + "COMBADGE_SPEAKERS='"
        + json.dumps(mapping)
        + "'\n"
    )
    return env, [f"{name}={tmp_path / path}" for name, path in mapping.items()]


def test_plain_start_loads_saved_speakers_from_default_env_before_bluetooth(tmp_path, monkeypatch):
    _, expected = saved_speakers(tmp_path, monkeypatch)
    monkeypatch.setattr("combadge.startup.ROOT", tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    with patch("combadge.bluetooth_startup.launch", return_value=0) as bluetooth:
        assert main(["start"]) == 0
    assert bluetooth.call_args.args[0].speaker == expected


@pytest.mark.parametrize("backend", ["auto", "local"])
def test_saved_speakers_and_backend_survive_sudo(tmp_path, monkeypatch, capsys, backend):
    import argparse

    from combadge.local_speakers import LocalSpeakerInput
    from combadge.speechmatics import StreamingSpeakerInput
    from combadge.startup import add_arguments

    env, expected = saved_speakers(tmp_path, monkeypatch)
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    with (
        patch("combadge.bluetooth_startup.platform.system", return_value="QNX"),
        patch("combadge.bluetooth_startup.os.geteuid", return_value=1000),
        patch("combadge.bluetooth_startup.os.execvp", side_effect=SystemExit) as execute,
        pytest.raises(SystemExit),
    ):
        main(
            [
                "start",
                "--env-file",
                str(env),
                "--no-calls",
                "--no-camera",
                "--no-shopify",
                "--speaker-backend",
                backend,
            ]
        )
    command = execute.call_args.args[1]
    monkeypatch.chdir(tmp_path.parent)
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(command[command.index("combadge.bluetooth_startup") + 1 :])
    assert args.speaker == expected
    args.bluetooth = False
    with patch("combadge.live.connect_voice") as connect:
        assert launch(args) == 0
    expected_type = LocalSpeakerInput if backend == "local" else StreamingSpeakerInput
    assert isinstance(connect.call_args.kwargs["speaker_input"], expected_type)
    assert connect.call_args.kwargs["speaker_tracker"] is None
    label = "Local CAM++" if backend == "local" else "Speechmatics streaming"
    assert f"{label} configured for Edmon, Samuel" in capsys.readouterr().out


@pytest.mark.parametrize("options", [["--no-speakers"], ["--tone"]])
def test_saved_speakers_can_be_disabled_or_ignored_for_tone(tmp_path, monkeypatch, options):
    env, _ = saved_speakers(tmp_path, monkeypatch)
    # Disabling must work even with broken saved configuration.
    env.write_text("COMBADGE_SPEAKERS=invalid-json\n")
    with patch("combadge.bluetooth_startup.launch", return_value=0) as bluetooth:
        assert main(["start", "--env-file", str(env), *options]) == 0
    assert bluetooth.call_args.args[0].speaker == []


def test_no_speakers_survives_sudo(tmp_path, monkeypatch):
    env, _ = saved_speakers(tmp_path, monkeypatch)
    with (
        patch("combadge.bluetooth_startup.platform.system", return_value="QNX"),
        patch("combadge.bluetooth_startup.os.geteuid", return_value=1000),
        patch("combadge.bluetooth_startup.os.execvp", side_effect=SystemExit) as execute,
        pytest.raises(SystemExit),
    ):
        main(["start", "--env-file", str(env), "--no-speakers"])
    command = execute.call_args.args[1]
    assert "--no-speakers" in command
    assert "--speaker" not in command


def test_explicit_speakers_replace_saved_list(tmp_path, monkeypatch):
    env, expected = saved_speakers(tmp_path, monkeypatch)
    with patch("combadge.bluetooth_startup.launch", return_value=0) as bluetooth:
        assert main(["start", "--env-file", str(env), "--speaker", expected[1]]) == 0
    assert bluetooth.call_args.args[0].speaker == [expected[1]]


@pytest.mark.parametrize("saved", ["bad-json", "[]", '{"Edmon":false}', '{"Edmon":"missing.wav"}'])
def test_invalid_saved_speakers_fail_before_bluetooth(tmp_path, monkeypatch, saved):
    monkeypatch.delenv("COMBADGE_SPEAKERS", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"COMBADGE_SPEAKERS='{saved}'\n")
    with patch("combadge.bluetooth_startup.launch") as bluetooth:
        assert main(["start", "--env-file", str(env)]) == 2
    bluetooth.assert_not_called()


def test_no_speakers_cannot_be_combined_with_explicit_enrollment():
    with pytest.raises(SystemExit) as error:
        main(["start", "--no-speakers", "--speaker", "Edmon=example.wav"])
    assert error.value.code == 2


def test_privileged_entry_point_loads_defaults_and_honors_opt_out(tmp_path, monkeypatch):
    from combadge import bluetooth_startup

    env, expected = saved_speakers(tmp_path, monkeypatch)
    for options, speakers in (([], expected), (["--no-speakers"], [])):
        monkeypatch.setattr("sys.argv", ["combadge.start", "--env-file", str(env), *options])
        with (
            patch.object(bluetooth_startup.platform, "system", return_value="QNX"),
            patch.object(bluetooth_startup.os, "geteuid", return_value=0),
            patch.object(bluetooth_startup, "run") as run,
        ):
            assert bluetooth_startup.main() == 0
        assert run.call_args.args[0].speaker == speakers


def test_local_backend_overrides_speechmatics_and_survives_start_forwarding(tmp_path, monkeypatch):
    from combadge.local_speakers import LocalSpeakerInput

    env, _ = saved_speakers(tmp_path, monkeypatch)
    with patch("combadge.live.connect_voice") as connect:
        assert (
            main(
                [
                    "start",
                    "--no-bluetooth",
                    "--no-calls",
                    "--no-camera",
                    "--no-shopify",
                    "--env-file",
                    str(env),
                    "--speaker-backend",
                    "local",
                ]
            )
            == 0
        )
    source = connect.call_args.kwargs["speaker_input"]
    assert isinstance(source, LocalSpeakerInput)
    assert connect.call_args.kwargs["speaker_tracker"] is None
    assert source.worker.model == tmp_path / "models/campplus.onnx"
