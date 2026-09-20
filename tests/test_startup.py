from unittest.mock import patch

import pytest

from combadge.cli import main
from combadge.startup import ROOT, launch


def test_start_uses_console_camera_and_repo_environment_from_any_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--max-seconds", "30"]) == 0
    args = routed.call_args.args[0]
    with patch("combadge.cli.main", return_value=0) as voice:
        assert launch(args) == 0
    command = voice.call_args.args[0]
    assert command[:3] == ["voice", "--audio-backend", "console"]
    assert command[command.index("--env-file") + 1] == str(ROOT / ".env")
    assert command[command.index("--camera-unit") + 1] == str(args.camera_unit)
    assert "--shopify" in command


def test_start_can_disable_camera():
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--no-camera"]) == 0
    with patch("combadge.cli.main", return_value=0) as voice:
        launch(routed.call_args.args[0])
    assert "--camera" not in voice.call_args.args[0]


def test_start_forwards_saved_snapshots_as_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("combadge.startup.launch", return_value=0) as routed:
        assert main(["start", "--save-snapshots", "images"]) == 0
    with patch("combadge.cli.main", return_value=0) as voice:
        launch(routed.call_args.args[0])
    command = voice.call_args.args[0]
    assert command[command.index("--save-snapshots") + 1] == str(tmp_path / "images")


def test_bluetooth_is_explicit_and_routes_to_the_example():
    with patch("combadge.bluetooth_startup.launch", return_value=0) as bluetooth:
        assert main(["start", "--bluetooth", "--max-seconds", "30"]) == 0
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
    command = ["start", "--no-camera", "--env-file", str(env), "--shop-auth-file", str(auth)]
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
            main(["start", "--no-camera", "--env-file", str(env), "--shop-auth-file", str(auth)])
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
