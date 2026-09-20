# Text messaging

Combadge sends SMS through Twilio's HTTPS Messages API using Python's standard
library. No Twilio SDK, relay, tunnel, or public webhook is needed. The SMS sender
is independent of the SIP calling number.

## Configuration

Add these to the ignored `.env` using the account that owns the texting number:

```dotenv
TWILIO_ACCOUNT_SID=ACyour-account-sid
TWILIO_AUTH_TOKEN=your-account-auth-token
TWILIO_FROM_NUMBER_TXT=+15484900711
```

Keep the existing `TWILIO_FROM_NUMBER` for calls. SMS needs the account SID and
Auth Token, not the SIP trunk username/password. Existing credentials can be
reused if both numbers belong to the same account. A Messaging Service SID is
not required. Environment variables take precedence over the selected `.env`.

Recipient names reuse `CALL_CONTACTS`. Optionally replace that mapping for SMS:

```dotenv
SMS_CONTACTS={"alex":"+14165550123"}
```

Use actual recipients. An explicit `{}` disables named contacts; a blank/unset
`SMS_CONTACTS` uses `CALL_CONTACTS`. Names are case-insensitive. Explicit E.164
phone numbers also work. The assistant is instructed never to invent a number.

## Usage

```sh
# Read-only remote check; verifies credentials, ownership and SMS capability.
combadge sms check

# Offline contact names, without exposing their numbers.
combadge sms contacts

# SMS loads automatically when its account credentials and sender are configured.
combadge voice

# Sending immediately submits a real SMS and uses Twilio credits.
combadge sms send alex "I'm running five minutes late."

# Fetch recent incoming texts to the SMS number, optionally by sender.
combadge sms read --contact alex --limit 5

# Select a different environment file before the action.
combadge sms --env-file /path/to/.env check
```

Voice examples: “Computer, text Alex that I'm running five minutes late” and
“Computer, read my recent texts from Alex.” An explicit, unambiguous send request
does not require a second confirmation. Requests to draft do not send. The voice
assistant receives the configured contact names, whose numbers are resolved
locally; it does not need to ask for a stored contact's number. Recipient
corrections retain the previously requested message.

Normal voice sessions enable SMS when all three Twilio settings are present and
print the loaded contact names at startup. `--no-sms` disables texting for that
session. `--sms` explicitly requires SMS configuration and reports missing
settings as an error. Audio checks and device listing never enable SMS. Texting
works alongside calling, connected apps, shopping, web, and camera tools.

The read command fetches up to ten recent incoming messages addressed to the
configured SMS number. It is not an unread-message tracker and does not mark
messages read. There are no automatic replies or background notifications.
Inbound SMS bodies are untrusted data, not instructions for further actions.
MMS attachments are not sent or fetched by this integration.

## Twilio requirements and results

The number must be SMS-capable and owned by the configured account. On trial
accounts, recipients must be verified with Twilio. Destination permissions and
applicable sender registration requirements still apply. `sms check` does not
prove carrier delivery or validate every destination restriction.

Messages may be up to 1,600 characters; Twilio can split them into separately
charged segments. A successful submission returns the message SID and Twilio's
current status. `queued`, `sending`, and `sent` do not confirm delivery. Check
Twilio Console messaging logs for final delivery and provider error codes.

Sends are never automatically retried. Identical sends within one voice
delegation are deduplicated, while a later explicit request can send again.
If the request times out, the response is malformed, or a send is cancelled,
its outcome is uncertain: the server may already have accepted it. Identical
uncertain sends are blocked for the rest of that client session. Check Twilio
logs before retrying; the duplicate guard is in memory and does not persist
across process restarts. CLI failures or uncertain results use a nonzero exit
code. Provider errors expose only HTTP/numeric codes, not message contents or
credentials.

Tests use simulated Twilio responses and voice events; they do not send real
messages. Hardware audio and end-to-end SMS delivery require a real-device test.

References: [Twilio Messages API](https://www.twilio.com/docs/messaging/api/message-resource),
[Messaging authentication](https://www.twilio.com/docs/messaging/api).
