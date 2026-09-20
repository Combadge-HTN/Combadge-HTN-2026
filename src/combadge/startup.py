"""Convenient microphone/camera startup with transcript-only local output."""

from argparse import BooleanOptionalAction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def add_arguments(parser):
    from combadge.cli import positive_seconds
    from combadge.shop_account import DEFAULT_AUTH_FILE

    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--input-device", default="default")
    camera_unit = next((unit for unit in (3, 4) if Path(f"/dev/sensor/camera{unit}").exists()), 4)
    parser.add_argument("--camera-unit", type=int, default=camera_unit)
    parser.add_argument("--no-camera", action="store_true")
    parser.add_argument(
        "--shopify",
        action=BooleanOptionalAction,
        default=True,
        help="enable Shopify product search (default: enabled)",
    )
    parser.add_argument(
        "--shop-account",
        action=BooleanOptionalAction,
        default=None,
        help="use your Shop account (default: enabled when credentials exist)",
    )
    parser.add_argument(
        "--shop-auth-file",
        type=Path,
        default=DEFAULT_AUTH_FILE,
        help="private Shop credential file",
    )
    parser.add_argument(
        "--save-snapshots",
        type=Path,
        metavar="DIRECTORY",
        help="keep captured images in this directory",
    )
    parser.add_argument(
        "--bluetooth", action="store_true", help="play through Dan's Bluetooth example"
    )
    parser.add_argument("--bluetooth-dir", type=Path, default=ROOT.parent / "qnx-bluetooth")
    parser.add_argument(
        "--tone", action="store_true", help="run the Bluetooth example's Python tone test"
    )
    parser.add_argument(
        "--max-seconds",
        type=positive_seconds,
        default=3600,
        help="session limit (default: one hour)",
    )


def shopping_arguments(args):
    command = []
    auth_file = args.shop_auth_file.expanduser().resolve()
    use_account = args.shop_account
    if use_account is None:
        use_account = args.shopify and auth_file.is_file()
    if args.shopify or use_account:
        command.append("--shopify")
    if use_account:
        command.extend(["--shop-account", "--shop-auth-file", str(auth_file)])
    return command


def launch(args):
    if args.bluetooth or args.tone:
        from combadge.bluetooth_startup import launch as bluetooth

        return bluetooth(args)
    from combadge.cli import main

    command = [
        "voice",
        "--audio-backend",
        "console",
        "--env-file",
        str(args.env_file),
        "--input-device",
        args.input_device,
        "--max-seconds",
        str(args.max_seconds),
    ]
    if not args.no_camera:
        command.extend(["--camera", "--camera-unit", str(args.camera_unit)])
    if args.save_snapshots is not None:
        command.extend(["--save-snapshots", str(args.save_snapshots.expanduser().resolve())])
    command.extend(shopping_arguments(args))
    print(
        "Starting microphone and camera; replies appear in this console. Ctrl+C stops.", flush=True
    )
    return main(command)
