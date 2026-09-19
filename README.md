# Combadge-HTN-2026

A wearable voice assistant for Raspberry Pi 5 running QNX 8.0. The Python application streams microphone audio to OpenAI GPT-Live and plays spoken responses through the badge.

## Requirements

- QNX 8.0 on Raspberry Pi 5 (aarch64le), with Python 3.11+ and pip.
- Network access to OpenAI and an `OPENAI_API_KEY`.
- QNX audio drivers and capture/playback helpers implementing the [PCM interface](docs/QNX.md#audio-interface).

The voice client and command transport are implemented. Native QNX audio helpers are still required; end-to-end operation on the Pi has not been validated.

## Setup

From the repository root on the Pi:

```sh
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

The helper must write exactly one JPEG, PNG, or WebP into `{directory}` and exit. The application supplies a fresh temporary directory, reads the image, and removes that directory. Commands run without a shell; the model cannot choose executable paths or filenames. A QNX camera helper is still required.

For COSMIC screen capture:

```sh
python -m pip install -e '.[images]'
commbadge voice --screenshots
```

This uses `cosmic-screenshot` through the desktop screenshot portal. Allow its screen-capture permission prompt if shown. Each requested screenshot is sent to OpenAI for analysis. Capture is opt-in for the session; it is not continuous recording. Capture failures are returned to the assistant, and capture helpers time out after 30 seconds. `--screenshots` and `--snapshot-command` are mutually exclusive and require a voice session rather than `--check`.

## Shopping with Shopify

Enable product discovery and checkout handoff with `--shopify`:

```sh
commbadge voice --shopify --snapshot-command '/path/to/camera-helper --output-dir {directory}'
```

Say **“Find something like this on Shopify under fifty dollars.”** The badge captures the requested view, searches Shopify's Global Catalog using the image and your preferences, and compares relevant offers. Follow up with **“Is the first one available in blue?”** or **“Find a cheaper one.”** The most recent image is reused until you request a new capture. A shopping image is sent to both OpenAI and Shopify.

On a COSMIC desktop, screen capture and browser checkout can be enabled together:

```sh
commbadge voice --shopify --screenshots --open-checkout
```

After selecting an offer, say **“Open checkout for that one.”** The app refreshes that variant's price and availability, then opens its merchant checkout when `--open-checkout` is enabled. Otherwise it prints and returns the checkout link. Changed offers require a new confirmation. Payment happens at the merchant; the badge does not place orders or process payment. On QNX, deliver the returned link to a companion device; a companion link transport is not included.

Search defaults to products shipping to Canada with CAD prices. `SHOPIFY_COUNTRY` supports `CA` or `US`; `SHOPIFY_CURRENCY` supports `CAD` or `USD`. Prices exclude shipping and tax. Results are candidates, not proof of an exact match or the lowest price across all stores. Catalog availability and final checkout totals can change.

You can also use a supplied photo or search by description:

```sh
commbadge voice --shopify --image /path/to/product.jpg --question 'Find a similar item under CAD 50'
commbadge shop 'blue insulated bottle' --max-price 5000
commbadge shop 'a bottle like this' --image /path/to/product.jpg
```

`shop` outputs JSON; its price limit is in cents. It does not require an OpenAI key. Shopify discovery uses a public UCP capability profile and does not require a merchant Admin API token. The default profile is an immutable copy of `docs/ucp-agent.json` served as JSON from the project's public repository through jsDelivr. Override `SHOPIFY_AGENT_PROFILE_URL` to host your own profile at an HTTPS URL serving `application/json`; GitHub raw's `text/plain` response is rejected by the catalog. Shopping tools are opt-in and cannot be combined with the voice `--check` flag.

API references: [Shopify Global Catalog](https://shopify.dev/docs/agents/catalog/global-catalog), [agent profiles](https://shopify.dev/docs/agents/get-started/profile), and [checkout handoff](https://shopify.dev/docs/agents/carts-and-checkout).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required | OpenAI authentication |
| `OPENAI_LIVE_MODEL` | `gpt-live-1` | Voice model |
| `OPENAI_LIVE_VOICE` | `marin` | Response voice |
| `OPENAI_BACKEND_MODEL` | `gpt-5.6-luna` | Delegated reasoning model |
| `BROWSERBASE_API_KEY` | Unset | Reserved for browser integration |
| `BROWSERBASE_PROJECT_ID` | Unset | Reserved for browser integration |

The client uses the GPT-Live WebSocket protocol with Responses delegation. Enabling capture registers `capture_snapshot`; `--shopify` adds catalog search, product details, and merchant checkout handoff. General browser automation and merchant inventory actions are not implemented. Voice sessions and delegated inference incur separate charges.

## Documentation

- [QNX integration](docs/QNX.md): runtime and audio interface.
- [Hardware](docs/HARDWARE.md): components and electrical requirements.
- [Project plan](PROJECT_PLAN.md): architecture and upcoming features.
