# Human phone calls

Combadge places outbound calls directly through Twilio Elastic SIP Trunking:

**Badge PCM helpers ↔ Python SIP/SRTP client ↔ Twilio ↔ telephone.**

No relay server, tunnel, public webhook, browser, or companion phone is required.
The user speaks directly to the other person. GPT-Live closes its session and
connection before dialing and stays disconnected after hang-up.

## Twilio configuration

In Twilio Console, open **Elastic SIP Trunking → Trunks** and create a dedicated
trunk. Configure:

1. **Secure Trunking:** enabled (TLS signaling and encrypted SRTP audio).
2. **Symmetric RTP:** enabled when the badge is behind NAT, such as a Wi-Fi router.
3. **Call Recording:** Do Not Record.
4. **Call Transfer:** disabled.
5. Under **Termination**, choose a domain ending in `.pstn.twilio.com` and attach
   a Credential List with a dedicated badge username and a strong random password.

Registration is not required. Outbound caller ID must be a Twilio number owned
by the account or an approved verified caller ID. No new number, incoming-call
routing, or Origination URL is needed for this outbound-only client. Calls use
Twilio credits and are subject to the account's geographic dialing permissions.

The network must allow outbound TCP 5061 and UDP media to Twilio. Symmetric RTP
allows Twilio to return audio to the source of the badge's outgoing media. The
client additionally authenticates every SRTP packet and checks for replay.

## Badge configuration

The runtime needs Python 3.14, Python's `ssl` and `ctypes` modules, and system
OpenSSL (`libcrypto.so.3` or `libcrypto.so`). AES uses OpenSSL; no native Python
crypto wheel or SIP SDK is required. Existing microphone/speaker helpers still
supply 20 ms mono PCM16LE frames at 24 kHz. The badge converts to/from G.711 PCMU
at 8 kHz for the phone connection.

Put these values in the ignored `.env`:

```dotenv
CALL_TRANSPORT=sip
CALL_SIP_DOMAIN=your-trunk.pstn.twilio.com
CALL_SIP_USERNAME=combadge
CALL_SIP_PASSWORD=your-dedicated-sip-password
TWILIO_FROM_NUMBER=+14165550100
CALL_CONTACTS={"edmon":"+14165550101"}
CALL_MAX_SECONDS=300
```

Use actual E.164 numbers. The client accepts contact names from this allowlist;
it does not accept arbitrary numbers from the assistant. The badge needs the SIP
password, not the Twilio account Auth Token. No `CALL_RELAY_*` values are required.

List contacts without network access or calling anyone:

```sh
commbadge call --list-contacts
```

Check system crypto and direct TLS/SIP connectivity without dialing or accessing
audio devices (this does not verify the SIP password or media path):

```sh
commbadge call --check
```

Call with the hardware team's QNX PCM helpers:

```sh
commbadge call edmon --audio-backend commands \
  --capture-command '/path/to/qnx-pcm-capture' \
  --playback-command '/path/to/qnx-pcm-playback'
```

These helper paths are placeholders. See the [QNX audio contract](QNX.md).
For voice-initiated calls, add `--calls` to the existing working `commbadge voice`
command and say **“Computer, call Edmon.”**

## Shutdown and failure behavior

Before closing GPT-Live, the client returns a truthful `handoff_requested` tool
result and continues the backend response. Dialing requires OpenAI's final
`session.closed` acknowledgment. A failed finalization never starts a call.

The other person can hang up; Ctrl+C or the configured duration limit also ends
the call. A pending call sends SIP CANCEL, an answered call sends BYE, and an
answer racing cancellation is acknowledged and then ended. Calls are never
redialed automatically. The command exits after hang-up; it does not restart the
assistant. Spoken “hang up” and a physical hang-up button are not implemented.

TLS certificates are verified and unencrypted media is rejected. The supported
media profile is PCMU with `AES_CM_128_HMAC_SHA1_80`, no MKI or key rotation.
Incoming calls, transfers, hold, and media renegotiation are not supported.
Twenty seconds without authenticated incoming audio ends the local call.

The duration limit is enforced by this client. Unlike the previous relay's REST
call setup, it is not a provider-enforced per-call time limit. If network failure
prevents confirmed hang-up, the client reports it; check the Twilio Console and
end the call there. No call audio is recorded by this code or the configured trunk.

## Verification

Tests cover SIP digest authentication, message framing, secure SDP, SRTP reference
vectors, replay/tampering rejection, packet rollover, direct two-way audio with a
simulated carrier, and cancellation/answer races. Direct TLS/SIP and encrypted
synthetic audio have also been exercised on QNX 8 with Python 3.14. Physical audio
acceptance still requires the microphone, speaker, and native helpers on the Pi.

The former relay transport remains available with `CALL_TRANSPORT=relay`; its
setup is documented in [CALLING_RELAY.md](CALLING_RELAY.md). It is optional.

References: [Twilio SIP trunk configuration](https://www.twilio.com/docs/sip-trunking),
[SIP RFC 3261](https://www.rfc-editor.org/rfc/rfc3261),
[SRTP RFC 3711](https://www.rfc-editor.org/rfc/rfc3711).
