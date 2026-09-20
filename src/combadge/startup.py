"""Start microphone, camera, Bluetooth replies, and human calling."""

import json
import sys
from argparse import ArgumentTypeError, BooleanOptionalAction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def speaker_reference(value):
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise ArgumentTypeError("use NAME=FILE.wav for each --speaker")
    return f"{name}={Path(path).expanduser().resolve()}"


def add_arguments(parser):
    from combadge.cli import positive_seconds
    from combadge.shop_account import DEFAULT_AUTH_FILE

    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--input-device", default="default")
    speakers = parser.add_mutually_exclusive_group()
    speakers.add_argument(
        "--speaker",
        action="append",
        type=speaker_reference,
        default=[],
        metavar="NAME=FILE.wav",
        help="override configured speakers with a 2–10s reference; repeat for up to four people",
    )
    speakers.add_argument(
        "--no-speakers", action="store_true", help="disable configured speaker identification"
    )
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
        "--bluetooth",
        action=BooleanOptionalAction,
        default=True,
        help="play through the Bluetooth speaker (default: enabled)",
    )
    parser.add_argument(
        "--calls",
        action=BooleanOptionalAction,
        default=True,
        help="enable human phone calls (default: enabled; requires configured contacts)",
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
    parser.add_argument("--idle-seconds", type=positive_seconds, default=2.0)
    parser.add_argument("--touch-bus", type=int, default=1)
    parser.add_argument("--touch-address", type=lambda value: int(value, 0), default=0x5A)
    parser.add_argument("--touch-irq", type=int, default=4)
    parser.add_argument("--touch-electrode", type=int, default=0)
    parser.add_argument("--double-tap-window", type=positive_seconds, default=0.6)
    parser.add_argument("--session-light-pin", type=int, default=14)
    parser.add_argument("--haptic-pin", type=int, default=15)


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


def speaker_arguments(args):
    return [item for reference in args.speaker for item in ("--speaker", reference)]


def touch_arguments(args):
    return [
        "--touch-activate",
        "--idle-seconds",
        str(args.idle_seconds),
        "--touch-bus",
        str(args.touch_bus),
        "--touch-address",
        hex(args.touch_address),
        "--touch-irq",
        str(args.touch_irq),
        "--touch-electrode",
        str(args.touch_electrode),
        "--double-tap-window",
        str(args.double_tap_window),
        "--session-light-pin",
        str(args.session_light_pin),
        "--haptic-pin",
        str(args.haptic_pin),
    ]


def configured_speakers(args):
    """Resolve saved references relative to the selected env file, before sudo."""
    from combadge.config import load_settings
    from combadge.speakers import load_references

    references = args.speaker
    if args.no_speakers:
        return []
    if not references and not args.tone:
        saved = load_settings(args.env_file).speaker_references
        if saved:
            try:
                mapping = json.loads(saved)
            except json.JSONDecodeError:
                raise ValueError(
                    "COMBADGE_SPEAKERS must be a JSON name-to-WAV-path object"
                ) from None
            if not isinstance(mapping, dict) or any(
                not name or not isinstance(path, str) or not path for name, path in mapping.items()
            ):
                raise ValueError("COMBADGE_SPEAKERS must map speaker names to nonempty WAV paths")
            base = args.env_file.expanduser().resolve().parent
            references = [
                f"{name}={(base / Path(path).expanduser()).resolve()}"
                for name, path in mapping.items()
            ]
    if references:
        if args.tone:
            raise ValueError("--speaker requires a voice session, not --tone")
        load_references(references)
    return references


def launch(args):
    try:
        args.speaker = configured_speakers(args)
    except (OSError, ValueError) as error:
        print(f"Cannot load speaker references: {error}", file=sys.stderr)
        return 2
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
    command.append("--calls" if args.calls else "--no-calls")
    command.extend(shopping_arguments(args))
    command.extend(speaker_arguments(args))
    command.extend(touch_arguments(args))
    print(
        "Starting badge controller; double tap electrode 0 to activate. Ctrl+C stops.", flush=True
    )
    return main(command)
