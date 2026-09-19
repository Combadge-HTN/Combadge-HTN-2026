"""Convenient microphone/camera startup with transcript-only local output."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def add_arguments(parser):
    from combadge.cli import positive_seconds

    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--input-device", default="default")
    parser.add_argument("--camera-unit", type=int, default=4)
    parser.add_argument("--no-camera", action="store_true")
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
    print(
        "Starting microphone and camera; replies appear in this console. Ctrl+C stops.", flush=True
    )
    return main(command)
