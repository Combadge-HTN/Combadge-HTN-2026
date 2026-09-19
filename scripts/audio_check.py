#!/usr/bin/env python3
"""Local Raspberry Pi audio diagnostics using ALSA; no network or API keys."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def run(command: list[str]) -> None:
    if shutil.which(command[0]) is None:
        raise RuntimeError(
            f"{command[0]} is missing. On Raspberry Pi OS, install alsa-utils."
        )
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list hardware and logical ALSA audio devices")
    record = commands.add_parser("record", help="record speech to a new local WAV")
    record.add_argument("--input-device", default="default")
    record.add_argument("--seconds", type=positive_int, default=5)
    record.add_argument("--rate", type=positive_int, default=48000)
    record.add_argument("--file", type=Path, default=Path("recordings/check.wav"))
    play = commands.add_parser("play", help="play a previously recorded WAV")
    play.add_argument("--output-device", default="default")
    play.add_argument("--file", type=Path, default=Path("recordings/check.wav"))
    args = parser.parse_args()

    try:
        if args.command == "list":
            for executable in ("arecord", "aplay"):
                for flag in ("-l", "-L"):
                    print(f"\n{executable} {flag}", flush=True)
                    run([executable, flag])
        elif args.command == "record":
            # Check tooling before reserving a new file; never overwrite audio.
            if shutil.which("arecord") is None:
                raise RuntimeError("arecord is missing. Install alsa-utils on the Pi.")
            args.file.parent.mkdir(parents=True, exist_ok=True)
            with args.file.open("xb") as output:
                print(f"Recording {args.seconds}s to {args.file}. Speak now.", flush=True)
                subprocess.run(
                    ["arecord", "-D", args.input_device, "-t", "wav", "-f", "S16_LE",
                     "-c", "1", "-r", str(args.rate), "-d", str(args.seconds)],
                    stdout=output, check=True,
                )
            print("Saved. Use the play command to listen.")
        else:
            if not args.file.is_file():
                raise RuntimeError(f"No recording at {args.file}; run record first.")
            run(["aplay", "-D", args.output_device, str(args.file.resolve())])
    except FileExistsError:
        print("Recording already exists. Choose a new --file path.", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Audio check failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. A partial recording may remain.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
