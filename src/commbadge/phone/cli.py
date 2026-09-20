"""CLI setup for the phone relay and standalone human calls."""

import argparse
import asyncio
import shlex
from pathlib import Path

from commbadge.audio import AlsaAudio, CommandAudio
from commbadge.phone.config import PhoneSettings, RelaySettings


def register(commands):
    relay = commands.add_parser("phone-relay", help="serve the Twilio audio bridge")
    relay.add_argument("--env-file", type=Path, default=Path(".env"))
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=8765)
    call = commands.add_parser("call", help="talk directly to a configured phone contact")
    call.add_argument("contact", nargs="?")
    call.add_argument("--list-contacts", action="store_true")
    call.add_argument(
        "--check", action="store_true", help="check direct SIP connectivity without dialing"
    )
    call.add_argument("--env-file", type=Path, default=Path(".env"))
    call.add_argument("--audio-backend", choices=("alsa", "commands"), default="alsa")
    call.add_argument("--capture-command")
    call.add_argument("--playback-command")
    call.add_argument("--input-device", default="default")
    call.add_argument("--output-device", default="default")
    call.add_argument(
        "--max-seconds", type=int, choices=range(10, 3601), default=300, metavar="10..3600"
    )


def run(args, parser: argparse.ArgumentParser):
    try:
        if args.command == "phone-relay":
            from commbadge.phone.relay import PhoneRelay

            settings = RelaySettings.load(args.env_file)
            asyncio.run(PhoneRelay(settings).run(args.host, args.port))
        else:
            from commbadge.phone.client import call_contact, contacts

            settings = PhoneSettings.load(args.env_file)
            if args.check:
                from commbadge.phone.config import SipSettings
                from commbadge.phone.direct import check_connection

                if not isinstance(settings, SipSettings):
                    parser.error("call --check requires CALL_TRANSPORT=sip")
                asyncio.run(check_connection(settings))
                print(
                    "Twilio TLS/SIP reachable. No call placed; "
                    "audio and credentials not yet tested."
                )
                return 0
            if args.list_contacts:
                print("\n".join(asyncio.run(contacts(settings))))
                return 0
            if not args.contact:
                parser.error("call requires a contact name or --list-contacts")
            if args.audio_backend == "commands":
                if not args.capture_command or not args.playback_command:
                    parser.error("commands backend needs --capture-command and --playback-command")
                audio = CommandAudio(
                    shlex.split(args.capture_command), shlex.split(args.playback_command)
                )
            else:
                if args.capture_command or args.playback_command:
                    parser.error("custom commands require --audio-backend commands")
                audio = AlsaAudio(args.input_device, args.output_device)
            audio.preflight()

            async def connect():
                try:
                    await audio.start()
                    return await call_contact(
                        settings, args.contact, audio, seconds=args.max_seconds
                    )
                finally:
                    await audio.close()

            print("Starting a human phone call. You speak directly; Ctrl+C hangs up.")
            result = asyncio.run(connect())
            print(f"Call ended: {result['status']}")
            return 0 if result["status"] in ("completed", "ended") else 1
        return 0
    except KeyboardInterrupt:
        return 130
    except ImportError:
        parser.exit(
            1, "Phone dependencies missing. Install the voice extra: pip install -e '.[voice]'\n"
        )
    except Exception as error:
        # Configuration and provider failures must not echo credentials/phone numbers.
        if isinstance(error, (ValueError, RuntimeError)):
            message = str(error)
        else:
            message = type(error).__name__
        parser.exit(1, f"Phone operation failed: {message}\n")
