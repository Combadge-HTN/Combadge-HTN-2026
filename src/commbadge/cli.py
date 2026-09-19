"""Command-line entry point for the Python application."""

import argparse
import asyncio
import math
import platform
import shlex
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path

from commbadge.config import load_settings


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="commbadge", description=__doc__)
    parser.add_argument("--version", action="version", version=version("htn-commbadge"))
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="show local setup status without making API calls")
    doctor.add_argument("--env-file", type=Path, default=Path(".env"))
    voice = commands.add_parser(
        "voice", help="talk to GPT-Live using Linux microphone and speakers"
    )
    voice.add_argument("--env-file", type=Path, default=Path(".env"))
    voice.add_argument(
        "--check", action="store_true", help="test generated audio without a microphone"
    )
    voice.add_argument(
        "--list-devices", action="store_true", help="list ALSA devices; no API calls"
    )
    voice.add_argument("--input-device", default="default", help="ALSA capture device")
    voice.add_argument("--output-device", default="default", help="ALSA playback device")
    voice.add_argument("--audio-backend", choices=("alsa", "commands"), default="alsa")
    voice.add_argument("--capture-command", help="raw PCM capture helper command (no shell)")
    voice.add_argument("--playback-command", help="raw PCM playback helper command (no shell)")
    voice.add_argument(
        "--max-seconds",
        type=positive_seconds,
        default=None,
        help="session limit after startup (default: 300; check: 15)",
    )
    voice.add_argument("--no-captions", action="store_true", help="hide transcript output")
    args = parser.parse_args(argv)

    if args.command == "voice" and not args.check and not args.list_devices:
        if args.audio_backend == "commands":
            if not args.capture_command or not args.playback_command:
                parser.error("commands backend needs --capture-command and --playback-command")
        elif args.capture_command or args.playback_command:
            parser.error("custom commands require --audio-backend commands")

    if args.command == "voice" and args.list_devices:
        for name in ("arecord", "aplay"):
            if not shutil.which(name):
                parser.exit(1, f"{name} is missing. On Linux: sudo apt install alsa-utils\n")
            print(f"\n{name} devices:", flush=True)
            result = subprocess.run([name, "-L"], check=False)
            if result.returncode:
                return result.returncode
        return 0

    try:
        settings = load_settings(args.env_file)
    except OSError:
        parser.exit(1, "Could not read the selected environment file. Check its permissions.\n")

    if args.command == "voice":
        if not settings.openai_api_key:
            parser.exit(
                1, "Add OPENAI_API_KEY to .env or your environment before starting voice.\n"
            )
        try:
            from commbadge.live import connect_voice

            print("Connecting to GPT-Live. This uses paid API credits; Ctrl+C ends the session.")
            asyncio.run(
                connect_voice(
                    settings,
                    check=args.check,
                    input_device=args.input_device,
                    output_device=args.output_device,
                    seconds=args.max_seconds or (15 if args.check else 300),
                    captions=not args.no_captions,
                    capture_command=shlex.split(args.capture_command)
                    if args.capture_command
                    else None,
                    playback_command=shlex.split(args.playback_command)
                    if args.playback_command
                    else None,
                )
            )
            return 0
        except ImportError:
            parser.exit(1, "Voice dependencies are missing. Run: pip install -e '.[voice]'\n")
        except KeyboardInterrupt:
            return 130
        except Exception as error:
            message = str(error) or type(error).__name__
            for secret in (settings.openai_api_key, settings.browserbase_api_key):
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            parser.exit(1, f"Voice failed: {message}\n")

    print("Local configuration (presence only; credentials are not validated):")
    for name, present in (
        ("OPENAI_API_KEY", bool(settings.openai_api_key)),
        ("BROWSERBASE_API_KEY", bool(settings.browserbase_api_key)),
        ("BROWSERBASE_PROJECT_ID", bool(settings.browserbase_project_id)),
    ):
        print(f"  {name}: {'set' if present else 'not set'}")
    host = platform.system()
    print(f"Audio tools ({host}):")
    names = ("wave", "waverec") if host == "QNX" else ("arecord", "aplay")
    for name in names:
        status = "available" if shutil.which(name) else "missing"
        print(f"  {name}: {status}")
    if host == "QNX":
        print("QNX requires a verified native PCM adapter; see docs/QNX.md.")
    print("Voice: commbadge voice --check, then commbadge voice")
    print("Standalone audio checks: python scripts/audio_check.py --help")
    return 0
