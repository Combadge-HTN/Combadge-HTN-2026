# Human phone calls

Combadge can place a phone call to a configured contact and carry the user's
speech directly. The other person answers an ordinary phone. A voice-initiated
call closes the GPT-Live session and its connection before dialing. The phone
conversation then runs independently, with no audio sent to OpenAI. After hang-up,
the command exits; the assistant does not restart automatically. Start a new voice
session explicitly when you want the assistant again. The standalone `call`
command needs no OpenAI key.

## Architecture

Badge PCM helpers ↔ Python client ↔ authenticated WSS relay ↔ Twilio ↔ telephone.

The badge sends 20 ms frames of mono PCM16LE at 24 kHz, using the same audio helper
contract as GPT-Live. The relay converts this to/from Twilio's mono 8 kHz G.711
mu-law. Conversion is pure Python and does not depend on the removed `audioop`
module. No browser, companion phone, or Twilio SDK is needed on the badge.

The relay uses the existing `voice` dependencies, runs separately from the badge,
and requires a publicly reachable HTTPS/WSS endpoint on a server. QNX audio, TLS, and network behavior still
require device acceptance; this integration does not supply QNX audio drivers.

## Configure the relay

Use an upgraded Twilio account with Voice/Media Streams enabled and a voice-capable
Twilio number. Credits alone do not remove trial restrictions: Twilio's current
trial blocks `<Stream>`.

Install the package with its voice extra:

```sh
python3 -m pip install -e '.[voice]'
```

Create an ignored `.env` on the relay host:

```dotenv
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+14165550100
CALL_PUBLIC_URL=https://calls.example.com
CALL_RELAY_TOKEN=...
CALL_CONTACTS={"alex":"+14165550101","samuel":"+14165550102"}
CALL_MAX_SECONDS=300
```

Replace the example numbers. Contacts are an explicit allowlist; a client/model
supplies a contact name, never a destination number or TwiML. Generate the shared
relay token with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` and
store it securely on the relay and badge. Never commit credentials or contacts.

Start the relay:

```sh
commbadge phone-relay --env-file .env --host 127.0.0.1 --port 8765
```

Expose that port through a TLS reverse proxy or HTTPS tunnel that supports
WebSocket upgrades. For example, with ngrok installed and authenticated:

```sh
ngrok http 8765
```

Set `CALL_PUBLIC_URL` to the tunnel's exact HTTPS origin and restart the relay.
A changing tunnel address requires updating both relay and badge configuration.
The proxy must preserve `Authorization` and `X-Twilio-Signature` headers and allow
long-running WebSocket connections. Do not expose the plain HTTP listener directly.
Use one relay process/replica: the current implementation keeps a single active
call in memory. `/health` is a read-only health endpoint.

The relay submits inline `<Connect><Stream>` TwiML when dialing, so no incoming
number webhook configuration is required. This version implements outbound calls.

## Configure the badge

The badge only needs:

```dotenv
CALL_RELAY_URL=wss://calls.example.com
CALL_RELAY_TOKEN=...same token as relay...
```

List configured contacts (does not place a call):

```sh
commbadge call --list-contacts
```

Place a human call using native PCM helpers on QNX:

```sh
commbadge call alex --audio-backend commands \
  --capture-command '/path/to/qnx-pcm-capture' \
  --playback-command '/path/to/qnx-pcm-playback'
```

These are placeholders for the hardware team's actual helpers, not QNX commands
included in this repository. The [QNX audio contract](QNX.md) applies unchanged.
The ALSA adapter can also be selected with `--audio-backend alsa`.

To call through the assistant, add `--calls` to the working voice command, then
say **“Call Alex.”** It also works alongside `--screenshots` or a camera helper:

```sh
commbadge voice --calls --audio-backend commands \
  --capture-command '/path/to/qnx-pcm-capture' \
  --playback-command '/path/to/qnx-pcm-playback' --max-seconds 900
```

The named contact must be unambiguous. A failed call is not automatically redialed.
The other person can hang up, or Ctrl+C ends the call. Both exit the command;
the assistant stays disconnected. A physical hang-up button and spoken hang-up detection during calls
are not implemented. Call speech bypasses the AI, including a spoken “hang up.”
Use headphones or hardware echo cancellation to avoid speaker feedback.

## Failure behavior and validation

- Only one call is active at a time. Unknown contacts are rejected before dialing.
- Badge requests require the relay token; Twilio stream upgrades require a valid
  Twilio signature and matching account/call IDs.
- Audio buffers and send timeouts are bounded. Disconnects, malformed frames,
  busy/no-answer responses, and session cancellation end the call.
- Calls have a 30-second ringing timeout and a provider-enforced duration limit.
  The relay also closes calls if the media stream never arrives.
- Call creation is never automatically retried. If its HTTP response is lost,
  check Twilio's console before dialing again. The provider duration limit remains
  the fallback for a call whose ID could not be recovered.
- If hang-up cannot be confirmed, check the Twilio console and terminate the call
  there. No application can guarantee immediate remote hang-up after a network outage.
- No call recording is requested and no call audio is written to disk by this code.

The automated suite exercises actual loopback WebSocket connections with a fake
carrier, including bidirectional audio, authentication, call isolation, hang-up,
codec vectors/filtering, and closing the assistant connection before dialing. These checks do not establish
Twilio account entitlement, public tunnel routing, real telephone audio quality,
or QNX device compatibility. Accept the feature only after a live two-person call
and a subsequent QNX hardware test.

References: [Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams),
[WebSocket audio protocol](https://www.twilio.com/docs/voice/media-streams/websocket-messages),
[request signatures](https://www.twilio.com/docs/usage/security),
[trial restrictions](https://www.twilio.com/docs/usage/trials/try-out-voice).
