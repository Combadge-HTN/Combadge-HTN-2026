# Combadge-HTN-2026

A wearable voice assistant named **Computer** for Raspberry Pi 5 running QNX 8.0. The Python application streams microphone audio to OpenAI GPT-Live and plays spoken responses through the badge.

## Requirements

- QNX 8.0 on Raspberry Pi 5 (aarch64le), with Python 3.14 and pip.
- Network access to OpenAI and an `OPENAI_API_KEY`.
- QNX audio drivers and capture/playback helpers implementing the [PCM interface](docs/QNX.md#audio-interface).

The voice client and command transport are implemented. Native QNX audio helpers are still required; end-to-end operation on the Pi has not been validated.

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

Runtime dependencies are pinned in `requirements-voice.txt`, exported from `uv.lock`. QNX networking and dependency execution require target validation; see [QNX integration](docs/QNX.md).

## Voice

With native audio helpers installed, supply their executable paths:

```sh
commbadge voice --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper'
```

These paths are placeholders for the required helpers, which are not included in this repository.

Press **Ctrl+C** to end the session. Sessions default to five minutes; use `--max-seconds 60` to change the limit. Add `--no-captions` to hide transcripts. The client saves no audio or transcript files. Acoustic echo cancellation must be handled by the audio path.

`commbadge doctor` reports configuration and audio utility availability. `commbadge voice --check` verifies API access and generated audio without opening audio devices; it consumes API credits.

## Image questions

Add a still image and question to the voice command:

```sh
commbadge voice --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper' \
  --image /path/to/image.png \
  --question 'What is shown in this image?'
```

The application sends the image once to the configured vision-capable Responses backend. GPT-Live speaks about the findings, and subsequent voice questions can refer to the same image. This uses a still image, not a continuous camera feed. JPEG, PNG, and WebP source files up to 20 MiB are accepted. With the optional `images` extra installed (`python -m pip install -e '.[images]'`), files larger than 256 KiB are resized without cropping and compressed to JPEG before upload. Without that extra, the capture helper must supply an encoded image no larger than 256 KiB. Image contents are sent to OpenAI; no public image URL is required.

GPT-Live limits backend input history to 4 MiB per session, including base64 image data. The app limits each encoded image to 256 KiB and reserves at most 2 MiB of history for image messages, leaving space for conversation and tool results. When the image budget fills, further captures return an error to the assistant without uploading another image; restart the voice session to continue capturing. Long conversations can also reach the service's item/history limits.

To check image delegation without opening audio devices:

```sh
commbadge voice --check --image /path/to/image.png --question 'Describe this image.'
```

The image check defaults to 45 seconds and finishes when backend analysis completes and non-silent audio arrives afterward. It checks the API path, not answer accuracy, complete speech, or physical playback. Timings report backend completion and first non-silent audio received after completion, measured from image submission; acknowledgments can affect the audio measurement. Use a spoken conversation to verify the answer itself.

The camera integration boundary is `ImageInput.from_bytes(encoded_image, question)` in `src/commbadge/vision.py`. It accepts the same encoded image bytes as file input.

## Voice-triggered capture

Enable a capture helper, then say **“Hey, look at this”** or **“Take another picture and tell me what you see.”** The agent requests a fresh snapshot, the application captures it, and the vision backend returns findings to the spoken conversation. Follow-up questions can use the last image.

For a device camera, add this option to the voice command:

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
commbadge voice --screenshots
```

This uses `cosmic-screenshot` through the desktop screenshot portal. Allow its screen-capture permission prompt if shown. Each requested screenshot is sent to OpenAI for analysis. Capture is opt-in for the session; it is not continuous recording. Capture failures are returned to the assistant, and capture helpers time out after 30 seconds. `--screenshots` and `--snapshot-command` are mutually exclusive and require a voice session rather than `--check`.

## Human phone calls

Configure a Twilio SIP trunk to speak directly to another person through the badge.
Use `commbadge call alex` for a standalone call, or add `--calls` to a voice session
and say “Call Alex.” Contact names and numbers are configured on the badge.

The badge connects directly to Twilio using TLS and encrypted SRTP audio; no relay
server or tunnel is required. It keeps its 24 kHz PCM helpers and handles telephone
audio conversion itself. See [calling setup](docs/CALLING.md) for credentials,
QNX requirements, hang-up behavior, and validation limits. Calls use Twilio credits.

## Shopping with Shopify

Enable product discovery and checkout handoff with `--shopify`:

```sh
commbadge voice --shopify --snapshot-command '/path/to/camera-helper --output-dir {directory}'
```

Say **“Find something like this on Shopify under fifty dollars.”** The badge captures the requested view, searches Shopify's Global Catalog using the image and your preferences, and compares relevant offers. Follow up with **“Is the first one available in blue?”** or **“Find a cheaper one.”** The most recent image is reused until you request a new capture. A shopping image is sent to both OpenAI and Shopify.

Connect your personal Shop account once:

```sh
commbadge shop-account login
commbadge shop-account status
```

Open the sign-in link on your phone and approve the connection. The command waits for approval. Then add `--shop-account` to the voice command:

```sh
commbadge voice --shopify --shop-account \
  --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper' \
  --snapshot-command '/path/to/camera-helper --output-dir {directory}'
```

Say **“Computer, find something like this under fifty dollars.”** Select a specific offer, then say **“Add that to my Shop account.”** Computer rechecks the variant and prepares an **unpaid merchant checkout** using your connected account. This response does **not** verify that the checkout appears in the Shop app cart. Direct app visibility was observed in two initial merchant tests but did not reproduce in a later shopping session; phone handoff remains unresolved. Computer reports checkout creation separately from app visibility. Different merchants have separate checkouts. Each save creates a checkout; it does not combine items into a shared cart or update a previous checkout.

Computer reports returned checkout totals and warns when shipping exceeds the item subtotal. Missing shipping/tax amounts are not assumed to be zero. Changed offers require a new confirmation. If a request fails with an uncertain outcome, inspect the trace before retrying to avoid duplicate checkouts. The app exposes no payment or order-completion operation.

For screen capture, use `commbadge voice --shopify --shop-account --screenshots`. Account mode does not open a browser. The separate guest flow remains available with `--shopify --open-checkout`, which opens a merchant checkout link instead; these modes cannot be combined.

Credentials are stored in `~/.local/state/commbadge/shop-auth.json`, with owner-only file permissions, outside the repository. This is a plaintext credential file, not a keychain. Tokens are refreshed when needed. Use `--auth-file PATH` on `shop-account` and `--shop-auth-file PATH` on `voice` to choose another private location. `commbadge shop-account logout` deletes local credentials; revoke the agent in Shop to remove its account access. Credentials, addresses and payment details are never sent to the voice model. The merchant receives a scoped token and the buyer's public network address (resolved through ipify) for checkout authentication and risk checks.

The account adapter uses Python's standard library and needs no Node.js runtime. Target networking and TLS still require QNX verification. This integration uses Shopify's personal-agent flow for an individual's connected account; broader product distribution requires confirming Shopify's applicable terms and access requirements.

### Checkout diagnostics

Every account checkout attempt prints a `Shop trace: <id>` and records an owner-only JSON file in `shop-traces/` alongside the selected Shop credential file (default `~/.local/state/commbadge/shop-traces/`). Traces retain the merchant, requested variant and quantity, stage, timestamp, merchant checkout ID, checkout status, diagnostic codes, totals and continuation URL when provided. No audio, screenshots, credentials, buyer contact details, addresses or payment objects are recorded. Checkout IDs and continuation URLs are private and omitted from terminal trace output.

```sh
commbadge shop-account trace
commbadge shop-account trace --trace-id TRACE_ID --refresh
```

The first command lists the ten most recent traces without network access. `--refresh` calls only `get_checkout` on the recorded merchant; it does not create a checkout, alter a cart, or submit payment. Creation evidence is preserved separately from the refreshed state. Neither result proves visibility in the phone app. Raw trace files contain private checkout access information; avoid sharing them publicly. Attempts made before tracing was implemented cannot be recovered from the console transcript alone.

Search defaults to products shipping to Canada with CAD prices. `SHOPIFY_COUNTRY` supports `CA` or `US`; `SHOPIFY_CURRENCY` supports `CAD` or `USD`. Prices exclude shipping and tax. Results are candidates, not proof of an exact match or the lowest price across all stores. Catalog availability and final checkout totals can change.

You can also use a supplied photo or search by description:

```sh
commbadge voice --shopify --image /path/to/product.jpg --question 'Find a similar item under CAD 50'
commbadge shop 'blue insulated bottle' --max-price 5000
commbadge shop 'a bottle like this' --image /path/to/product.jpg
```

`shop` outputs JSON; its price limit is in cents. It does not require an OpenAI key. Shopify discovery uses a public UCP capability profile and does not require a merchant Admin API token. The default profile is an immutable copy of `docs/ucp-agent.json` served as JSON from the project's public repository through jsDelivr. Override `SHOPIFY_AGENT_PROFILE_URL` to host your own profile at an HTTPS URL serving `application/json`; GitHub raw's `text/plain` response is rejected by the catalog. Shopping tools are opt-in and cannot be combined with the voice `--check` flag.

API references: [Shopify Global Catalog](https://shopify.dev/docs/agents/catalog/global-catalog), [agent profiles](https://shopify.dev/docs/agents/get-started/profile), and [checkout handoff](https://shopify.dev/docs/agents/carts-and-checkout), and [Shop personal agents](https://help.shop.app/en/shop/shopping/personal-agents).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required | OpenAI authentication |
| `OPENAI_LIVE_MODEL` | `gpt-live-1` | Voice model |
| `OPENAI_LIVE_VOICE` | `marin` | Response voice |
| `OPENAI_BACKEND_MODEL` | `gpt-5.6-luna` | Delegated reasoning model |
| `BROWSERBASE_API_KEY` | Unset | Reserved for browser integration |
| `BROWSERBASE_PROJECT_ID` | Unset | Reserved for browser integration |

The client uses the GPT-Live WebSocket protocol with Responses delegation. `--calls` registers `call_contact`. Enabling capture registers `capture_snapshot`; `--shopify` adds catalog search, product details, and merchant checkout handoff. General browser automation and merchant inventory actions are not implemented. Voice sessions and delegated inference incur separate charges.

## Documentation

- [MPR121 touch input](docs/HARDWARE.md#mpr121-touch-sensor): reusable `commbadge.touch` driver and continuous-state hardware check.
- [QNX integration](docs/QNX.md): runtime and audio interface.
- [Hardware](docs/HARDWARE.md): components and electrical requirements.
- [Project plan](PROJECT_PLAN.md): architecture and upcoming features.
