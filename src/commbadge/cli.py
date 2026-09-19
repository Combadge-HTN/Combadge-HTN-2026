"""Command-line entry point for the Python application."""

import argparse
import asyncio
import json
import math
import platform
import shlex
import shutil
import subprocess
from importlib.metadata import version
from pathlib import Path

from commbadge.capture import COSMIC_SCREENSHOT, SnapshotCapture
from commbadge.config import load_settings
from commbadge.shop_account import DEFAULT_AUTH_FILE, ShopAccount, TokenStore
from commbadge.shopify import CatalogClient, ShoppingSession
from commbadge.vision import DEFAULT_QUESTION, ImageInput


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="commbadge", description=__doc__)
    parser.add_argument("--version", action="version", version=version("htn-commbadge"))
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="show configuration and audio tool availability")
    doctor.add_argument("--env-file", type=Path, default=Path(".env"))
    voice = commands.add_parser("voice", help="stream speech with GPT-Live")
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
        help="session limit after startup (default: 300; check: 15; image check: 45)",
    )
    voice.add_argument("--no-captions", action="store_true", help="hide transcript output")
    voice.add_argument(
        "--speaker",
        action="append",
        default=[],
        metavar="NAME=FILE.wav",
        help="enroll a speaker using a 2–10s WAV; repeat for up to four people",
    )
    analyze = commands.add_parser("speakers", help="analyze a WAV with enrolled speaker references")
    analyze.add_argument("audio", type=Path, help="mono PCM16 24kHz WAV, up to 30 seconds")
    analyze.add_argument("--speaker", action="append", required=True, metavar="NAME=FILE.wav")
    analyze.add_argument("--env-file", type=Path, default=Path(".env"))
    voice.add_argument("--image", type=Path, help="send a JPEG, PNG, or WebP to the vision backend")
    voice.add_argument("--question", help="question about --image (default: describe the image)")
    snapshots = voice.add_mutually_exclusive_group()
    snapshots.add_argument(
        "--screenshots", action="store_true", help="enable voice-triggered COSMIC screenshots"
    )
    snapshots.add_argument(
        "--snapshot-command", help="image capture helper with {directory} placeholder (no shell)"
    )
    voice.add_argument("--calls", action="store_true", help="enable human phone calls via a relay")
    from commbadge.phone.cli import register
    from commbadge.phone.cli import run as run_phone

    register(commands)
    voice.add_argument(
        "--shopify", action="store_true", help="enable Shopify product search and checkout links"
    )
    voice.add_argument(
        "--open-checkout",
        action="store_true",
        help="open requested Shopify checkout links in this device's browser",
    )
    voice.add_argument(
        "--shop-account",
        action="store_true",
        help="prepare merchant checkouts using your connected Shop account",
    )
    voice.add_argument(
        "--shop-auth-file",
        type=Path,
        default=DEFAULT_AUTH_FILE,
        help="private Shop credential file",
    )
    account_parser = commands.add_parser("shop-account", help="connect your personal Shop account")
    account_parser.add_argument("action", choices=("login", "status", "logout", "trace"))
    account_parser.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    account_parser.add_argument("--trace-id", help="inspect one recorded checkout attempt")
    account_parser.add_argument(
        "--refresh",
        action="store_true",
        help="read the recorded checkout from its merchant; no mutation",
    )
    shop = commands.add_parser(
        "shop", help="search Shopify products by description and optional image"
    )
    shop.add_argument("query", help="product description and preferences")
    shop.add_argument("--image", type=Path, help="JPEG, PNG, or WebP for visual product search")
    shop.add_argument("--env-file", type=Path, default=Path(".env"))
    shop.add_argument("--max-price", type=int, help="maximum item price in cents (CAD by default)")
    args = parser.parse_args(argv)
    if args.command in ("call", "phone-relay"):
        return run_phone(args, parser)
    speaker_tracker = None
    if args.command in ("voice", "speakers") and args.speaker:
        if args.command == "voice" and (args.check or args.list_devices):
            parser.error("--speaker requires a voice session")
        from commbadge.speakers import SpeakerTracker, Transcriber, load_references, read_wav

        try:
            references = load_references(args.speaker)
            import httpx  # noqa: F401
        except ImportError:
            parser.exit(1, "Speaker dependencies are missing. Install the speakers extra.\n")
        except (OSError, ValueError) as error:
            parser.error(f"Cannot load speaker references: {error}")
    phone_settings = None
    if args.command == "voice" and args.calls:
        if args.check or args.list_devices:
            parser.error("--calls requires a voice session")
        from commbadge.phone.config import PhoneSettings

        try:
            phone_settings = PhoneSettings.load(args.env_file)
        except (OSError, ValueError) as error:
            parser.error(str(error))

    if args.command == "shop-account":
        if (args.trace_id or args.refresh) and args.action != "trace":
            parser.error("--trace-id and --refresh require shop-account trace")
        account = ShopAccount(TokenStore(args.auth_file))
        try:
            if args.action == "trace":
                print(json.dumps(account.traces(args.trace_id, refresh=args.refresh), indent=2))
                return 0
            if args.action == "login":
                account.login()
            elif args.action == "status":
                account.access_token()
                print("Shop account connected.")
            else:
                account.store.clear()
                print("Local Shop credentials removed. Revoke agent access in Shop to disconnect.")
            return 0
        except KeyboardInterrupt:
            return 130
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Shop account: {error}\n")

    image = None
    snapshot_capture = None
    shopping = None
    if args.command == "voice":
        if args.shop_account and not args.shopify:
            parser.error("--shop-account requires --shopify")
        if args.shop_account and args.open_checkout:
            parser.error("--shop-account cannot be combined with --open-checkout")
        if args.open_checkout and not args.shopify:
            parser.error("--open-checkout requires --shopify")
        if args.shopify and (args.check or args.list_devices):
            parser.error("--shopify requires a voice session, not --check or --list-devices")
        if args.screenshots or args.snapshot_command:
            if args.check or args.list_devices:
                parser.error(
                    "snapshot tools require a voice session, not --check or --list-devices"
                )
            try:
                snapshot_capture = SnapshotCapture(
                    COSMIC_SCREENSHOT if args.screenshots else shlex.split(args.snapshot_command)
                )
                snapshot_capture.preflight()
            except ValueError as error:
                parser.error(str(error))
        if args.question is not None and args.image is None:
            parser.error("--question requires --image")
        if args.image is not None:
            if args.list_devices:
                parser.error("--image cannot be combined with --list-devices")
            try:
                image = ImageInput.from_file(
                    args.image, args.question if args.question is not None else DEFAULT_QUESTION
                )
            except (OSError, ValueError) as error:
                parser.error(f"Cannot load image: {error}")

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

    if args.command in ("voice", "speakers") and args.speaker:
        if not settings.openai_api_key:
            parser.exit(1, "Add OPENAI_API_KEY before using speaker identification.\n")
        transcriber = Transcriber(settings.openai_api_key, references)
        speaker_tracker = SpeakerTracker(transcriber)
        if args.command == "speakers":
            import time
            from dataclasses import asdict

            try:
                pcm = read_wav(args.audio, minimum=0.1, maximum=30)
                started = time.monotonic()
                segments = asyncio.run(transcriber.analyze(pcm))
                print(
                    json.dumps(
                        {
                            "analysis_seconds": round(time.monotonic() - started, 3),
                            "segments": [asdict(s) for s in segments],
                        },
                        indent=2,
                    )
                )
                return 0
            except KeyboardInterrupt:
                return 130
            except Exception:
                parser.exit(
                    1, "Speaker analysis failed; check WAV format, API access and connection.\n"
                )

    if args.command == "shop" or (args.command == "voice" and args.shopify):
        try:
            account = None
            if args.command == "voice" and args.shop_account:
                account = ShopAccount(TokenStore(args.shop_auth_file))
                account.access_token()
            shopping = ShoppingSession(
                CatalogClient(
                    settings.shopify_agent_profile_url,
                    country=settings.shopify_country,
                    currency=settings.shopify_currency,
                ),
                account=account,
                open_checkout=args.command == "voice" and args.open_checkout,
                report=(lambda text: print(text, end="", flush=True))
                if args.command == "voice"
                else (lambda _: None),
            )
            if args.command == "shop":
                if args.image:
                    shopping.image = ImageInput.from_file(args.image, args.query)
                result = asyncio.run(
                    shopping.search(args.query, args.image is not None, args.max_price)
                )
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Shopify failed: {error}\n")

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
                    seconds=args.max_seconds or ((45 if image else 15) if args.check else 300),
                    captions=not args.no_captions,
                    capture_command=shlex.split(args.capture_command)
                    if args.capture_command
                    else None,
                    image=image,
                    snapshot_capture=snapshot_capture,
                    phone_settings=phone_settings,
                    shopping=shopping,
                    speaker_tracker=speaker_tracker,
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
            for secret in (
                settings.openai_api_key,
                settings.browserbase_api_key,
                phone_settings.token if phone_settings else "",
            ):
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            parser.exit(1, f"Voice failed: {message}\n")

    print("Configuration (presence only; credentials are not validated):")
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
        print("Audio: configure native PCM helpers with --audio-backend commands; see docs/QNX.md.")
    print("API check: commbadge voice --check")
    print("Voice options: commbadge voice --help")
    return 0
