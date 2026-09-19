"""Command-line entry point for the Python application."""

import argparse
import shutil
from importlib.metadata import version
from pathlib import Path

from commbadge.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="commbadge", description=__doc__)
    parser.add_argument("--version", action="version", version=version("htn-commbadge"))
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="show local setup status without making API calls")
    doctor.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except OSError:
        parser.exit(1, "Could not read the selected environment file. Check its permissions.\n")

    print("Local configuration (presence only; credentials are not validated):")
    for name, present in (
        ("OPENAI_API_KEY", bool(settings.openai_api_key)),
        ("BROWSERBASE_API_KEY", bool(settings.browserbase_api_key)),
        ("BROWSERBASE_PROJECT_ID", bool(settings.browserbase_project_id)),
    ):
        print(f"  {name}: {'set' if present else 'not set'}")
    print("Audio tools:")
    for name in ("arecord", "aplay"):
        status = "available" if shutil.which(name) else "missing (Pi: install alsa-utils)"
        print(f"  {name}: {status}")
    print("Cloud voice is not implemented yet. Audio checks: python scripts/audio_check.py --help")
    return 0
