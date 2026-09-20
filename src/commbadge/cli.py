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

from commbadge.audio import MacAudio, audio_backend
from commbadge.browserbase import BrowserbaseClient
from commbadge.capture import COSMIC_SCREENSHOT, SnapshotCapture
from commbadge.composio import ComposioClient
from commbadge.config import load_settings
from commbadge.shop_account import DEFAULT_AUTH_FILE, ShopAccount, TokenStore
from commbadge.shopify import CatalogClient, ShoppingSession
from commbadge.sms import SmsClient, SmsSettings
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
        "--list-devices", action="store_true", help="list audio devices; no API calls"
    )
    voice.add_argument(
        "--input-device", default="default", help="input device name or index (Mac), or ALSA device"
    )
    voice.add_argument(
        "--output-device",
        default="default",
        help="output device name or index (Mac), or ALSA device",
    )
    voice.add_argument(
        "--audio-backend", choices=("auto", "mac", "alsa", "commands"), default="auto"
    )
    voice.add_argument("--capture-command", help="raw PCM capture helper command (no shell)")
    voice.add_argument("--playback-command", help="raw PCM playback helper command (no shell)")
    voice.add_argument(
        "--max-seconds",
        type=positive_seconds,
        default=None,
        help="limit per assistant session, including after calls (default: 300; check: 15/45)",
    )
    voice.add_argument(
        "--no-web", action="store_true", help="disable automatic Browserbase online lookups"
    )
    voice.add_argument(
        "--composio",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Gmail and Google Calendar tools (automatic when key and User ID are configured)",
    )
    voice.add_argument("--no-captions", action="store_true", help="hide transcript output")
    voice.add_argument("--image", type=Path, help="send a JPEG, PNG, or WebP to the vision backend")
    voice.add_argument("--question", help="question about --image (default: describe the image)")
    snapshots = voice.add_mutually_exclusive_group()
    snapshots.add_argument(
        "--screenshots", action="store_true", help="enable voice-triggered COSMIC screenshots"
    )
    snapshots.add_argument(
        "--snapshot-command", help="image capture helper with {directory} placeholder (no shell)"
    )
    voice.add_argument(
        "--calls",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="human phone calls (automatic when SIP or relay settings are configured)",
    )
    voice.add_argument(
        "--sms",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Twilio texting (automatic when account credentials and SMS number are configured)",
    )
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
    web_search = commands.add_parser("web-search", help="check Browserbase search without audio")
    web_search.add_argument("query", help="search terms (up to 200 characters)")
    web_search.add_argument("--env-file", type=Path, default=Path(".env"))
    web_search.add_argument(
        "--read-first",
        action="store_true",
        help="also fetch the first result to verify page access",
    )
    apps = commands.add_parser("composio", help="inspect connected app setup without audio")
    apps.add_argument("action", choices=("accounts", "status", "tools"))
    apps.add_argument("--app", choices=("gmail", "googlecalendar"))
    apps.add_argument("--env-file", type=Path, default=Path(".env"))
    texts = commands.add_parser("sms", help="send or read texts using the dedicated SMS number")
    texts.add_argument("--env-file", type=Path, default=Path(".env"))
    sms_commands = texts.add_subparsers(dest="sms_action", required=True)
    sms_commands.add_parser("check", help="verify credentials and sender capability; sends no text")
    sms_commands.add_parser("contacts", help="list configured SMS contact names offline")
    send = sms_commands.add_parser("send", help="send an SMS (uses Twilio credits)")
    send.add_argument("recipient", help="configured contact name or E.164 number")
    send.add_argument("body", help="text to send, in quotes")
    read = sms_commands.add_parser("read", help="read recent incoming texts")
    read.add_argument("--contact", help="filter by contact name or E.164 sender")
    read.add_argument("--limit", type=int, choices=range(1, 11), default=5, metavar="1..10")
    args = parser.parse_args(argv)
    sms = None
    if args.command == "voice" and args.sms and (args.check or args.list_devices):
        parser.error("--sms requires a voice session; use sms check to verify SMS setup")
    if args.command == "sms" or (
        args.command == "voice"
        and args.sms is not False
        and not args.check
        and not args.list_devices
    ):
        try:
            sms_settings = SmsSettings.load(
                args.env_file, optional=args.command == "voice" and args.sms is None
            )
            sms = SmsClient(sms_settings) if sms_settings is not None else None
            if args.command == "sms":
                if args.sms_action == "contacts":
                    print("\n".join(sorted(sms.settings.contacts)))
                    return 0
                if args.sms_action == "check":
                    result = asyncio.run(sms.check())
                elif args.sms_action == "send":
                    result = asyncio.run(sms.send(args.recipient, args.body))
                else:
                    result = asyncio.run(sms.read(args.contact, args.limit))
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 1 if result["status"] in ("failed", "undelivered", "unknown") else 0
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"SMS: {error}\n")
        except KeyboardInterrupt:
            parser.exit(130, "SMS interrupted; check Twilio message logs before retrying a send.\n")
    if args.command == "voice" and args.composio and (args.check or args.list_devices):
        parser.error(
            "--composio requires a voice session; use composio status to check connections"
        )
    if args.command in ("call", "phone-relay"):
        return run_phone(args, parser)
    from commbadge.phone.config import PhoneSettings, SipSettings

    phone_settings = None
    if args.command == "voice" and args.calls and (args.check or args.list_devices):
        parser.error("--calls requires a voice session")
    if (
        args.command == "voice"
        and args.calls is not False
        and not args.check
        and not args.list_devices
    ):
        try:
            phone_settings = PhoneSettings.load(args.env_file, optional=args.calls is None)
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
        if audio_backend(args.audio_backend) == "mac":
            try:
                devices = MacAudio.driver().query_devices()
                if not devices:
                    raise RuntimeError(
                        "No audio devices are visible. Run from your Mac terminal and "
                        "check System Settings > Sound."
                    )
                print(devices)
            except Exception as error:
                parser.exit(1, f"Audio devices unavailable: {error}\n")
            return 0
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

    if args.command == "composio":
        if args.action == "tools" and not args.app:
            parser.error("composio tools requires --app")
        try:
            apps_client = ComposioClient.from_settings(settings)
            if args.action == "accounts":
                # Setup diagnostics must reveal owners even when the configured ID is wrong.
                result = asyncio.run(ComposioClient(settings.composio_api_key).connected_accounts())
            elif args.action == "status":
                result = asyncio.run(apps_client.connection_status())
            else:
                result = asyncio.run(apps_client.list_tools(args.app))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Composio: {error}\n")

    if args.command == "web-search":
        try:
            web = BrowserbaseClient(settings.browserbase_api_key, settings.browserbase_project_id)

            async def lookup():
                result = await web.search(args.query)
                if args.read_first and result["sources"]:
                    result["page"] = await web.read_page(result["sources"][0]["url"])
                return result

            print(json.dumps(asyncio.run(lookup()), indent=2, ensure_ascii=False))
            return 0
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Web lookup failed: {error}\n")

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

            enable_composio = args.composio
            if enable_composio is None:
                enable_composio = bool(
                    settings.composio_api_key and settings.composio_user_id and not args.check
                )
            composio = ComposioClient.from_settings(settings) if enable_composio else None
            if composio is not None:
                composio.require_user()
                print("Connected apps: Composio enabled (Gmail, Google Calendar).")
            elif not args.check:
                print(
                    "Connected apps: disabled (--no-composio)."
                    if args.composio is False
                    else "Connected apps: unavailable (set COMPOSIO_API_KEY and COMPOSIO_USER_ID)."
                )
            if sms is not None:
                print("Text messaging: Twilio enabled (separate SMS sender).")
                print(
                    "SMS contacts: "
                    + (", ".join(sorted(sms.settings.contacts)) or "none configured")
                )
            elif not args.check:
                print(
                    "Text messaging: disabled (--no-sms)."
                    if args.sms is False
                    else "Text messaging: unavailable (set TWILIO_ACCOUNT_SID, "
                    "TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER_TXT)."
                )
            if phone_settings is not None:
                if isinstance(phone_settings, SipSettings):
                    print("Calling: Twilio SIP enabled.")
                    print("Call contacts: " + ", ".join(sorted(phone_settings.contacts)))
                else:
                    print("Calling: relay enabled; contacts will be loaded from the relay.")
            elif not args.check:
                print(
                    "Calling: disabled (--no-calls)."
                    if args.calls is False
                    else "Calling: unavailable (configure SIP or relay; see docs/CALLING.md)."
                )
            web = (
                BrowserbaseClient(settings.browserbase_api_key, settings.browserbase_project_id)
                if settings.browserbase_api_key and not args.no_web and not args.check
                else None
            )
            print(
                "Web access: Browserbase enabled (uses API credits)."
                if web is not None
                else "Web access: disabled (--no-web/--check or BROWSERBASE_API_KEY not set)."
            )
            print("Connecting to GPT-Live. This uses paid API credits; Ctrl+C ends the session.")
            asyncio.run(
                connect_voice(
                    settings,
                    check=args.check,
                    backend=args.audio_backend,
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
                    web=web,
                    composio=composio,
                    sms=sms,
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
                settings.composio_api_key,
                getattr(phone_settings, "token", ""),
                getattr(phone_settings, "password", ""),
                sms.settings.auth_token if sms else "",
            ):
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            parser.exit(1, f"Voice failed: {message}\n")

    print("Configuration (presence only; credentials are not validated):")
    for name, present in (
        ("OPENAI_API_KEY", bool(settings.openai_api_key)),
        ("BROWSERBASE_API_KEY", bool(settings.browserbase_api_key)),
        ("BROWSERBASE_PROJECT_ID", bool(settings.browserbase_project_id)),
        ("COMPOSIO_API_KEY", bool(settings.composio_api_key)),
        ("COMPOSIO_USER_ID", bool(settings.composio_user_id)),
    ):
        print(f"  {name}: {'set' if present else 'not set'}")
    host = platform.system()
    if host == "Darwin":
        try:
            MacAudio.driver()
            print("Mac audio: CoreAudio adapter available")
        except RuntimeError as error:
            print(f"Mac audio: {error}")
    else:
        print(f"Audio tools ({host}):")
        names = ("wave", "waverec") if host == "QNX" else ("arecord", "aplay")
        for name in names:
            status = "available" if shutil.which(name) else "missing"
            print(f"  {name}: {status}")
    if host == "QNX":
        print("Audio: configure native PCM helpers with --audio-backend commands; see docs/QNX.md.")
    print(
        "Web access: enabled for voice sessions"
        if settings.browserbase_api_key
        else "Web access: unavailable; set BROWSERBASE_API_KEY"
    )
    print("Web check: commbadge web-search 'Browserbase documentation' --read-first")
    print("Connected app check: commbadge composio status")
    print("API check: commbadge voice --check")
    print("Voice options: commbadge voice --help")
    return 0
