# Combadge-HTN-2026

A wearable voice assistant named **Computer** for Raspberry Pi 5 running QNX 8.0. The Python application streams microphone audio to OpenAI GPT-Live and plays spoken responses through the badge.

## Requirements

- QNX 8.0 on Raspberry Pi 5 (aarch64le), with Python 3.14 and pip.
- Network access to OpenAI and an `OPENAI_API_KEY`.
- QNX audio drivers and `arecord`/`aplay`, or custom helpers implementing the [PCM interface](docs/QNX.md#audio-interface).

The voice client, camera capture, and network integrations run on QNX. The default audio adapter uses the QNX ports of `arecord` and `aplay`. Physical microphone/speaker acceptance is required for each audio device.

Dan has written the Bluetooth and audio drivers, and speakers are available. Bluetooth development lives in [Combadge-HTN/qnx-bluetooth](https://github.com/Combadge-HTN/qnx-bluetooth).

## Setup

From the repository root on the Pi:

```sh
python3 --version  # Must be Python 3.14.x
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-voice.txt
python -m pip install --no-deps -e .
```

Create `.env` using [.env.example](.env.example) and set `OPENAI_API_KEY`. Environment variables override `.env` values. Credentials belong outside source control.

Runtime dependencies are pinned in `requirements-voice.txt`, exported from `uv.lock`. QNX networking and dependency execution have been exercised on the target; see [QNX integration](docs/QNX.md) for remaining hardware requirements.

## Voice on macOS

Install the optional Mac audio support in the project environment:

```sh
uv sync --extra voice --extra mac
.venv/bin/combadge voice
```

`voice` automatically uses CoreAudio on macOS and ALSA on Linux. The Mac adapter
uses [sounddevice raw streams](https://python-sounddevice.readthedocs.io/en/latest/api/raw-streams.html)
at 24 kHz, mono PCM16. PortAudio is included in the macOS wheel; NumPy is not needed.
Allow microphone access for your terminal when macOS prompts. If denied, enable it
in **System Settings → Privacy & Security → Microphone**, then restart the terminal.
Use headphones to prevent the assistant's voice from feeding back into the microphone;
this adapter does not provide acoustic echo cancellation.

List devices and optionally select the numeric IDs or device names:

```sh
.venv/bin/combadge voice --list-devices
.venv/bin/combadge voice --input-device 'MacBook Pro Microphone' --output-device 'MacBook Pro Speakers'
```

Use the actual device names from the listing. The default uses the system devices.
`--audio-backend mac` explicitly selects this adapter. To activate the environment
in fish, use `source .venv/bin/activate.fish`; bash/zsh use `source .venv/bin/activate`.
Calling `.venv/bin/combadge` directly requires no activation.

## Voice on QNX

For microphone input, physical camera snapshots, and replies printed in the
console, run on the Pi:

```sh
combadge start
```

This does not open a playback device or require Bluetooth. It uses the checkout's
`.env` from any working directory. Shopify search is enabled, and an existing
Shop login is used automatically. Startup selects physical camera unit 3 when
present, otherwise unit 4; `--camera-unit N` overrides this choice.
Say **“Computer, look at this”** to capture a
camera image. Press **Ctrl+C** to stop. The default session limit is one hour;
use `combadge start --max-seconds 180` for a short test, or `--no-camera` for
microphone-only use. GPT-Live still generates audio server-side; this mode
discards playback and displays the transcripts, and still uses API credits.

The prepared Pi has `~/bin/combadge` linked to this checkout's `.venv/bin/combadge`.
On a new checkout, activate `.venv` first or run `.venv/bin/combadge start`.

To retain camera images for inspection, run:

```sh
combadge start --save-snapshots ~/combadge-snapshots
```

Each capture prints its saved path. Files have unique UTC timestamps and contain
the encoded image prepared for analysis, including any resizing. They remain
after the session ends until you delete them. This option also works with
`start --bluetooth` and `voice --camera`; snapshots are otherwise temporary.

For speaker output through Dan's existing Bluetooth example, turn on the TWS
Mini Speaker, disconnect it from other devices, and run:

```sh
combadge start --bluetooth
```

This starts `~/projects/qnx-bluetooth/run-radio.sh`, connects the speaker, and
feeds this app's converted audio through the example's `audio.pcm` FIFO. It does
not change the Pi's system audio output. Ctrl+C stops the app and Bluetooth
session. `combadge start --tone` runs the example's three-second PCM tone test
without using the API. See [Bluetooth startup](docs/QNX.md#bluetooth-example).

With a microphone and speaker connected to the QNX audio device:

```sh
combadge voice --list-devices
combadge voice
```

Use `--input-device` and `--output-device` to select devices other than `default`. For a board image without `arecord`/`aplay`, supply custom raw PCM helpers:

```sh
combadge voice --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper'
```

These paths are placeholders for custom helpers; they are unnecessary when the native `arecord`/`aplay` tools are available.

Press **Ctrl+C** to end the session. Sessions default to five minutes; use `--max-seconds 60` to change the limit. Add `--no-captions` to hide transcripts. The client saves no audio or transcript files. Acoustic echo cancellation must be handled by the audio path.

`combadge doctor` reports configuration and audio utility availability. `combadge voice --check` verifies API access and generated audio without opening audio devices; it consumes API credits.

## Speaker identification (prototype)

Add `--speaker Edmon=/path/to/edmon.wav --speaker Samuel=/path/to/samuel.wav` to identify enrolled voices alongside the live conversation. This uses the existing voice installation. Labels arrive asynchronously and can be wrong; overlapping speech remains experimental. See [speaker setup and limitations](docs/SPEAKERS.md).

### Custom speech voices

`OPENAI_LIVE_VOICE` selects the voice at the start of every GPT-Live session.
It accepts a built-in name such as `marin` or an approved OpenAI custom voice ID
such as `voice_123abc`. The app sends custom IDs in the required object format.
Set the value in `.env` and start a new `combadge voice` session to use it.
An unavailable custom voice produces an API error; there is no silent fallback.

This does not include the original Star Trek computer voice. A startup prompt
cannot install a voice from an audio clip, and a recording URL is not a voice ID.
OpenAI's [custom voice setup](https://developers.openai.com/api/docs/guides/custom-voices#use-a-custom-voice-with-gpt-live)
requires project access, a speaker-consent recording and a matching reference
sample. A voice model hosted by another provider would need a separate audio
conversion or speech-generation integration; its ID cannot be used here.

## Live web research with Browserbase

Set `BROWSERBASE_API_KEY` in `.env` or the environment. Normal `combadge voice`
sessions automatically enable web research. `BROWSERBASE_PROJECT_ID` optionally
selects the browser project; otherwise Browserbase infers it from the API key.
No local Chromium, Node.js, or additional package is required beyond the voice
extra. Add `--no-web` to disable research; `--check` also disables it.

The conversation loop is:

1. GPT-Live hears a question and delegates it to `OPENAI_BACKEND_MODEL`.
2. That model chooses a search, page fetch, or real browser navigation.
3. The app executes the operation on Browserbase and sends the result back to the
   backend as a function result. Browserbase does not decide when the answer is sufficient.
4. The backend can follow a returned link, read more, or stop with findings and source URLs.
5. GPT-Live speaks those findings while the microphone/audio loop remains active.

The tools are `search_web` (up to five candidate URLs), `read_web_page` (fast static
Markdown), `browse_web_page` (remote Chromium with JavaScript), `follow_web_link`
(a link from the current rendered page), and `read_more_web_page` (the next excerpt).
Rendered pages return readable text, title, public metadata and labeled links to the
backend, not screenshots. Link IDs are tied to the current page, so old-page links
cannot accidentally navigate the new page. The browser supports public navigation,
not logins, arbitrary button clicks, form submissions, account changes, or purchases.

The browsing strategy is general: identify the requested facts and constraints, find
an appropriate source, inspect it, then choose the next useful action. It can follow
a documentation reference, move from a listing to an item, compare relevant sources,
read a long article, or follow pagination on a JavaScript-rendered page. There are no
site-specific query templates or answer rules. It should change strategy when a
source is unhelpful and stop when the evidence answers the question. When evidence
is insufficient or conflicting, it should give a qualified or partial answer instead
of inferring that an inaccessible fact does not exist. Login walls, arbitrary button
interactions and forms remain outside this read-only adapter's capabilities.

Each Live delegation has a hard limit of **two searches, eight web tool calls, and
60 seconds**. Tool continuations share that budget; a new delegation gets a new one.
Repeated identical searches/static page reads in a delegation reuse their results,
but still consume an action so duplicates cannot create an endless loop. Model
routing remains probabilistic; these bounds limit network work, not guarantee
answer accuracy. The whole voice session has a separate limit of 24 web operations
(including browser session creation/navigation). Cleanup bypasses that limit.

The terminal prints queries, candidate source titles/URLs, opened pages, and
`Read source [browser]` or `Read source [fetch]` after successful retrieval. A read
confirms access, not the factual answer. These progress messages remain visible
with `--no-captions`, which hides speech transcripts only.

Test Search/Fetch without microphone or OpenAI inference:

```sh
combadge web-search 'Browserbase documentation' --read-first
```

This uses Browserbase credits. Search, Fetch, and cloud browser sessions are billed
by Browserbase; delegated inference uses OpenAI credits. `combadge doctor` checks
credential presence, not access. HTTP 403 can indicate unavailable project/API
access; 402 indicates credits; 429 indicates a rate limit.

Requests/URLs go to Browserbase and retrieved content goes to OpenAI. The app does
not persist web lookups locally. Browser recording/logging are disabled in the
created session. The browser is released on voice shutdown; disconnects release it
and a 180-second server TTL bounds orphaned sessions. REST requests use a 20-second
socket timeout and 25-second async deadline; cancelling cannot kill an already
running HTTP worker. API responses are capped at 2 MiB, rendered text at 60,000
characters with 12,000-character excerpts, and page metadata/links are bounded.
Treat all website content as untrusted evidence, never instructions for badge tools.

References: [Browserbase Search](https://docs.browserbase.com/reference/api/web-search),
[Fetch](https://docs.browserbase.com/reference/api/fetch-a-page),
[cloud sessions](https://docs.browserbase.com/reference/api/create-a-session), and
[GPT-Live delegation](https://developers.openai.com/api/docs/guides/live-delegation).

## Gmail and Google Calendar with Composio

Connect Gmail and Google Calendar in the same Composio Platform project, using
Composio-managed OAuth2 and completing Google's sign-in and consent flow for each.
An auth configuration alone is not a connected account. The project API key needs
**Tools: Read only**, **Connected accounts: Read only**, and
**Tool execution (Legacy): Write only**. Google permissions must also cover the
actions you want (reading email, sending/drafting, reading/editing events).

Set `COMPOSIO_API_KEY` in `.env`, then inspect the connected account owners:

```sh
.venv/bin/combadge composio accounts
```

Set `COMPOSIO_USER_ID` to the exact `user_id` returned for your accounts, which
must share one owner. This is the connected-account owner, not the Composio project
ID. The `accounts` command lists supported connections across the project for
setup; runtime execution is restricted to `COMPOSIO_USER_ID`. If that user has
multiple Gmail or Calendar accounts, select one with `COMPOSIO_GMAIL_ACCOUNT_ID`
or `COMPOSIO_GOOGLECALENDAR_ACCOUNT_ID`. Set `COMBADGE_TIMEZONE` for relative dates
and event times (default `America/Toronto`).

```sh
.venv/bin/combadge composio status
.venv/bin/combadge composio tools --app gmail
.venv/bin/combadge composio tools --app googlecalendar
.venv/bin/combadge voice
```

Normal voice sessions automatically enable Gmail and Calendar when both
`COMPOSIO_API_KEY` and `COMPOSIO_USER_ID` are configured. The startup banner reads
`Connected apps: Composio enabled (Gmail, Google Calendar).` Use `--no-composio`
to disable them for a session, or `--composio` to require valid configuration.
Audio check and device-listing modes do not automatically enable connected apps.
Keep your usual camera, shopping, call, and audio options. GPT-Live delegates inbox
and schedule questions to its backend,
which discovers supported actions, inspects their input schemas, and executes them
through Composio. Browserbase can supply public research in the same request;
private email and calendar data use the connected Google APIs. Results used to
answer your question are passed to the model. No Composio SDK is required.

Try: "Summarize my five most recent unread emails", "Find the email with my hotel
booking", "Draft a reply to that email saying I'll arrive at 3 PM", "What is on my
calendar tomorrow?", or "Find a free 30-minute slot tomorrow afternoon". You can
also request a specific email send/reply/forward, or create, move, and delete calendar
events. Resolve unclear recipients, event times, and attendees before making changes.
A request to draft does not authorize sending. An email's contents cannot authorize
actions. Unknown write outcomes are not retried automatically.

Email summaries use `search_gmail_messages` with an explicit read/unread/all filter.
The app searches message IDs, opens each result with Gmail's full-message API,
checks its current `UNREAD` label, and decodes the actual body (nested MIME,
plain text, or HTML). Subjects and inbox previews are not used as body substitutes.
Up to five messages are opened per search page; longer bodies have continuation
offsets for `read_gmail_message`. The assistant can open an individual message for
follow-up questions and must report unavailable or incomplete content. These API
reads do **not** mark emails read in Gmail; they leave mailbox labels unchanged.

This add-on supports Gmail and Google Calendar only. It has no SMS tools, background
inbox monitoring, arbitrary Google app access, or email attachment-download tool.
`status` checks connection metadata and `tools` checks discovery; neither proves
all Google scopes work. Start with a read request in voice, then try a draft or
calendar change you actually want. Reconnect in Composio if access expires.

## Image questions

Add a still image and question to the voice command:

```sh
combadge voice --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper' \
  --image /path/to/image.png \
  --question 'What is shown in this image?'
```

The application sends the image once to the configured vision-capable Responses backend. GPT-Live speaks about the findings, and subsequent voice questions can refer to the same image. This uses a still image, not a continuous camera feed. JPEG, PNG, and WebP source files up to 20 MiB are accepted. With the optional `images` extra installed (`python -m pip install -e '.[images]'`), files larger than 256 KiB are resized without cropping and compressed to JPEG before upload. Without that extra, the capture helper must supply an encoded image no larger than 256 KiB. Image contents are sent to OpenAI; no public image URL is required.

GPT-Live limits backend input history to 4 MiB per session, including base64 image data. The app limits each encoded image to 256 KiB and reserves at most 2 MiB of history for image messages, leaving space for conversation and tool results. When the image budget fills, further captures return an error to the assistant without uploading another image; restart the voice session to continue capturing. Long conversations can also reach the service's item/history limits.

To check image delegation without opening audio devices:

```sh
combadge voice --check --image /path/to/image.png --question 'Describe this image.'
```

The image check defaults to 45 seconds and finishes when backend analysis completes and non-silent audio arrives afterward. It checks the API path, not answer accuracy, complete speech, or physical playback. Timings report backend completion and first non-silent audio received after completion, measured from image submission; acknowledgments can affect the audio measurement. Use a spoken conversation to verify the answer itself.

The camera integration boundary is `ImageInput.from_bytes(encoded_image, question)` in `src/combadge/vision.py`. It accepts the same encoded image bytes as file input.

## Voice-triggered capture

Enable a capture helper, then say **“Hey, look at this”** or **“Take another picture and tell me what you see.”** The agent requests a fresh snapshot, the application captures it, and the vision backend returns findings to the spoken conversation. Follow-up questions can use the last image.

For the QNX Pi camera, build the helper once and enable camera capture:

```sh
make -C native/qnx-camera
combadge voice --camera --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper'
```

Say **“Computer, look at this”**. The backend captures a fresh physical camera photo and analyzes it. `--camera` selects unit 4; use `--camera-unit N` to select another unit. It finds the built helper independently of the current directory, or uses `combadge-camera` from `PATH`. Camera and desktop screenshot modes are mutually exclusive. Capture still happens only on request.

For a different device camera, add this option to the voice command:

```sh
--snapshot-command '/path/to/camera-helper --output-dir {directory}'
```

The helper must write exactly one JPEG, PNG, or WebP into `{directory}` and exit. The application supplies a fresh temporary directory, reads the image, and removes that directory. Commands run without a shell; the model cannot choose executable paths or filenames.

For the QNX Pi, the included [camera helper](native/qnx-camera/README.md) captures a JPEG from the IMX708 on unit 4. Build it on the Pi with `make -C native/qnx-camera`, then use:

```sh
--snapshot-command '/absolute/path/to/Combadge-HTN-2026/native/qnx-camera/combadge-camera --unit 4 --output-dir {directory}'
```

For COSMIC screen capture:

```sh
python -m pip install -e '.[images]'
combadge voice --screenshots
```

This uses `cosmic-screenshot` through the desktop screenshot portal. Allow its screen-capture permission prompt if shown. Each requested screenshot is sent to OpenAI for analysis. Capture is opt-in for the session; it is not continuous recording. Capture failures are returned to the assistant, and capture helpers time out after 30 seconds. `--screenshots` and `--snapshot-command` are mutually exclusive and require a voice session rather than `--check`.

## Human phone calls

Configure a Twilio SIP trunk to speak directly to another person through the badge.
Use `combadge call alex` for a standalone call, or run `combadge voice` and say
“Call Alex.” Voice sessions enable calling automatically when SIP or relay settings
are configured. Startup reports the calling transport and, for SIP, contact names.
Use `--no-calls` to disable it or `--calls` to require valid calling configuration.
Contact names and numbers are configured on the badge.

The assistant disconnects during the human call and reconnects after confirmed
hang-up or an unanswered/busy call. Recent conversation and tool results carry
forward; phone-call audio stays outside the assistant. Ctrl+C ends the whole
command. Standalone `combadge call` exits after the call.

The badge connects directly to Twilio using TLS and encrypted SRTP audio; no relay
server or tunnel is required. It keeps its 24 kHz PCM helpers and handles telephone
audio conversion itself. See [calling setup](docs/CALLING.md) for credentials,
QNX requirements, hang-up behavior, and validation limits. Calls use Twilio credits.

## Text messaging

Set `TWILIO_FROM_NUMBER_TXT` to your dedicated SMS-capable number and provide
`TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` for the account that owns it.
`TWILIO_FROM_NUMBER` continues to be used for calls. SMS reuses `CALL_CONTACTS`
unless you provide a separate `SMS_CONTACTS` JSON map.

```sh
combadge sms check
combadge sms contacts
combadge voice
```

Say **“Computer, text Alex that I'm running five minutes late”** or
**“Computer, read my recent texts from Alex.”** SMS is enabled automatically when
the three Twilio settings are present. Startup prints `Text messaging: Twilio enabled`
and the configured contact names. Use `--no-sms` to disable texting for a session,
or `--sms` to require SMS configuration. It works alongside `--calls` and your
existing app/audio/camera options. For standalone use:

```sh
combadge sms send alex "I'm running five minutes late."
combadge sms read --contact alex --limit 5
```

`sms check` verifies account access, number ownership and SMS capability without
sending a message. Sending uses Twilio credits; queued/sent does not prove delivery.
Recent incoming texts are fetched on request, with no webhook or background polling.
See [SMS setup](docs/SMS.md) for account requirements and failure handling.

## Shopping with Shopify

On the prepared Pi, shopping is enabled by default:

```sh
combadge start
```

For account-based merchant checkout, run `combadge shop-account login` once.
Future starts detect the saved credentials automatically. Without credentials,
search and guest checkout links remain available. Invalid or expired credentials
that cannot be refreshed produce a login error; use `--no-shop-account` for guest
mode. Use `--no-shopify` to disable shopping entirely, or `--shop-account` to require
account mode explicitly. These options also work with `--bluetooth`;
`--shop-auth-file PATH` selects a custom credential file.
Account checkout does not confirm that the item appears in the Shop app cart.

Enable product discovery and checkout handoff with `--shopify`:

```sh
combadge voice --shopify --snapshot-command '/path/to/camera-helper --output-dir {directory}'
```

Say **“Find something like this on Shopify under fifty dollars.”** The badge captures the requested view, searches Shopify's Global Catalog using the image and your preferences, and compares relevant offers. Follow up with **“Is the first one available in blue?”** or **“Find a cheaper one.”** The most recent image is reused until you request a new capture. A shopping image is sent to both OpenAI and Shopify.

Connect your personal Shop account once:

```sh
combadge shop-account login
combadge shop-account status
```

Open the sign-in link on your phone and approve the connection. The command waits for approval. Then add `--shop-account` to the voice command:

```sh
combadge voice --shopify --shop-account \
  --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper' \
  --snapshot-command '/path/to/camera-helper --output-dir {directory}'
```

Say **“Computer, find something like this under fifty dollars.”** Select a specific offer, then say **“Add that to my Shop account.”** Computer rechecks the variant and prepares an **unpaid merchant checkout** using your connected account. This response does **not** verify that the checkout appears in the Shop app cart. Direct app visibility was observed in two initial merchant tests but did not reproduce in a later shopping session; phone handoff remains unresolved. Computer reports checkout creation separately from app visibility. Different merchants have separate checkouts. Each save creates a checkout; it does not combine items into a shared cart or update a previous checkout.

Computer reports returned checkout totals and warns when shipping exceeds the item subtotal. Missing shipping/tax amounts are not assumed to be zero. Changed offers require a new confirmation. If a request fails with an uncertain outcome, inspect the trace before retrying to avoid duplicate checkouts. The app exposes no payment or order-completion operation.

For screen capture, use `combadge voice --shopify --shop-account --screenshots`. Account mode does not open a browser. The separate guest flow remains available with `--shopify --open-checkout`, which opens a merchant checkout link instead; these modes cannot be combined.

Credentials are stored in `~/.local/state/commbadge/shop-auth.json`, with owner-only file permissions, outside the repository. This is a plaintext credential file, not a keychain. Tokens are refreshed when needed. Use `--auth-file PATH` on `shop-account` and `--shop-auth-file PATH` on `voice` to choose another private location. `combadge shop-account logout` deletes local credentials; revoke the agent in Shop to remove its account access. Credentials, addresses and payment details are never sent to the voice model. The merchant receives a scoped token and the buyer's public network address (resolved through ipify) for checkout authentication and risk checks.

The account adapter uses Python's standard library and needs no Node.js runtime. Target networking and TLS have been verified on QNX. This integration uses Shopify's personal-agent flow for an individual's connected account; broader product distribution requires confirming Shopify's applicable terms and access requirements.

### Checkout diagnostics

Every account checkout attempt prints a `Shop trace: <id>` and records an owner-only JSON file in `shop-traces/` alongside the selected Shop credential file (default `~/.local/state/commbadge/shop-traces/`). Traces retain the merchant, requested variant and quantity, stage, timestamp, merchant checkout ID, checkout status, diagnostic codes, totals and continuation URL when provided. No audio, screenshots, credentials, buyer contact details, addresses or payment objects are recorded. Checkout IDs and continuation URLs are private and omitted from terminal trace output.

```sh
combadge shop-account trace
combadge shop-account trace --trace-id TRACE_ID --refresh
```

The first command lists the ten most recent traces without network access. `--refresh` calls only `get_checkout` on the recorded merchant; it does not create a checkout, alter a cart, or submit payment. Creation evidence is preserved separately from the refreshed state. Neither result proves visibility in the phone app. Raw trace files contain private checkout access information; avoid sharing them publicly. Attempts made before tracing was implemented cannot be recovered from the console transcript alone.

Search defaults to products shipping to Canada with CAD prices. `SHOPIFY_COUNTRY` supports `CA` or `US`; `SHOPIFY_CURRENCY` supports `CAD` or `USD`. Prices exclude shipping and tax. Results are candidates, not proof of an exact match or the lowest price across all stores. Catalog availability and final checkout totals can change.

You can also use a supplied photo or search by description:

```sh
combadge voice --shopify --image /path/to/product.jpg --question 'Find a similar item under CAD 50'
combadge shop 'blue insulated bottle' --max-price 5000
combadge shop 'a bottle like this' --image /path/to/product.jpg
```

`shop` outputs JSON; its price limit is in cents. It does not require an OpenAI key. Shopify discovery uses a public UCP capability profile and does not require a merchant Admin API token. The default profile is an immutable copy of `docs/ucp-agent.json` served as JSON from the project's public repository through jsDelivr. Override `SHOPIFY_AGENT_PROFILE_URL` to host your own profile at an HTTPS URL serving `application/json`; GitHub raw's `text/plain` response is rejected by the catalog. Shopping tools are opt-in and cannot be combined with the voice `--check` flag.

API references: [Shopify Global Catalog](https://shopify.dev/docs/agents/catalog/global-catalog), [agent profiles](https://shopify.dev/docs/agents/get-started/profile), and [checkout handoff](https://shopify.dev/docs/agents/carts-and-checkout), and [Shop personal agents](https://help.shop.app/en/shop/shopping/personal-agents).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required | OpenAI authentication |
| `OPENAI_LIVE_MODEL` | `gpt-live-1` | Voice model |
| `OPENAI_LIVE_VOICE` | `marin` | Built-in response voice name or approved OpenAI custom voice ID (`voice_...`) |
| `OPENAI_BACKEND_MODEL` | `gpt-5.6-luna` | Delegated reasoning model |
| `BROWSERBASE_API_KEY` | Unset | Enables automatic Search/Fetch tools in voice sessions |
| `BROWSERBASE_PROJECT_ID` | Unset | Optional cloud browser project; inferred from API key if unset |
| `TWILIO_ACCOUNT_SID` | Unset | Twilio account owning the SMS number; enables texting with token and SMS sender |
| `TWILIO_AUTH_TOKEN` | Unset | Twilio account authentication for SMS and the optional call relay |
| `TWILIO_FROM_NUMBER_TXT` | Unset | Dedicated SMS sender in E.164 format; separate from the calling number |
| `SMS_CONTACTS` | `CALL_CONTACTS` | Optional JSON name-to-number map for texting |

The client uses the GPT-Live WebSocket protocol with Responses delegation. Configured calling adds `call_contact` automatically (disable with `--no-calls`); configured Twilio SMS adds `send_text` and `read_texts` automatically (disable with `--no-sms`). Enabling capture registers `capture_snapshot`; `--shopify` adds catalog search, product details, and merchant checkout handoff. A configured Browserbase key adds web search, static page reading, and rendered browser navigation. Arbitrary browser actions and merchant inventory actions are not implemented. Voice sessions and delegated inference incur separate charges.

## Documentation

- [MPR121 touch input](docs/HARDWARE.md#mpr121-touch-sensor): reusable `combadge.touch` driver and continuous-state hardware check.
- [QNX integration](docs/QNX.md): runtime and audio interface.
- [Hardware](docs/HARDWARE.md): components and electrical requirements.
- [Project plan](PROJECT_PLAN.md): architecture and upcoming features.
