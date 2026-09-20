# Majel computer voice

The badge can send GPT-Live's generated speech to a separate RVC converter and
play the converted speech. Set `COMBADGE_VOICE_CONVERSION_URL` once in the badge's
`.env`; subsequent audible voice sessions use it automatically, including after
returning from a phone call. Microphone input and human phone calls are not
converted. Console-only mode and `voice --check` skip conversion.

This uses the community [Majel Barrett Computer Voice model](https://voice-models.com/model/1qQohBdPu8g)
by MrM0dZ, described by its author as trained on *Star Trek Generations* using
Ov2 pretraining. It is a synthetic recreation, not an official Roddenberry voice
service. The inspected checkpoint is RVC v2, 32 kHz, marked `500epoch`; its SHA-256
is `516427666b544728891069bce24561928a236d2d15c7b1bd35ef860fc551df8c`.

The original archive's `added_IVF430_Flat_nprobe_1_Majel_v2.index` is only 1,648
bytes and contains a Google 403 error page. Leave index retrieval disabled.
The `.pth` works without it; do not pass the broken file to `--index`.

## Linux setup

Run the converter on the laptop or another Linux host. This PyTorch runtime is
separate from the QNX badge's Python environment. It requires Python 3.12, `uv`
(the checkout's `.tools/bin/uv` is accepted), network access, and several GB of
disk space. It downloads the pinned RVC source and its inference dependencies:

```sh
bash scripts/setup-majel.sh /home/alexhuang/Downloads/Majel/Majel.pth
```

On the same laptop, put this in `.env`:

```dotenv
COMBADGE_VOICE_CONVERSION_URL=ws://127.0.0.1:8765
COMBADGE_VOICE_CONVERSION_AUTOSTART=true
```

Then run your normal `combadge voice`. It starts and warms up the converter before
connecting to GPT-Live, reuses it across phone-call handoffs, and stops its owned
server on exit. Startup logs go to `.majel/server.log`. A server already running
at that address is reused and left running. Console-only startup intentionally
has no speaker output; use `voice` for local audio or `start --bluetooth` on QNX.
There is no need to upload the checkpoint to OpenAI or change `OPENAI_LIVE_VOICE`.

To keep a server running independently, use
`.majel/env/bin/python scripts/majel_server.py`. It prints `Majel ready` after
warm-up. Autostart only supports local addresses and requires the installed
Linux runtime.

## QNX badge and Bluetooth speaker

For a converter on the laptop and playback on the Pi, choose a shared token,
export it in the server terminal, and listen on the local network:

```sh
export COMBADGE_VOICE_CONVERSION_TOKEN='your-shared-token'
.majel/env/bin/python scripts/majel_server.py --host 0.0.0.0
```

Configure the **Pi's** `.env` with the laptop's reachable address and the same
token, then use the existing Bluetooth startup:

```dotenv
COMBADGE_VOICE_CONVERSION_URL=ws://LAPTOP_LAN_IP:8765
COMBADGE_VOICE_CONVERSION_TOKEN=your-shared-token
COMBADGE_VOICE_CONVERSION_AUTOSTART=false
```

```sh
combadge start --bluetooth
```

The laptop must stay running and be reachable from the Pi. The server defaults
to loopback and requires a token for network listening. Plain `ws://` is for a
trusted local network; use a TLS proxy (`wss://`) for other networks. This path
sends only assistant output audio to the converter. No API keys, camera images,
tool results, or microphone recordings are sent there. Neither side records
live audio. One badge session is accepted at a time.

The app stops on converter failure or excessive backlog instead of switching
voices. Remove/empty `COMBADGE_VOICE_CONVERSION_URL` to restore native playback.

## Preview and performance

Convert a mono 24 kHz WAV without microphone, speaker, or OpenAI calls:

```sh
.majel/env/bin/python scripts/majel_server.py \
  --input recordings/majel-source.wav --output recordings/majel-preview.wav
```

The default uses 400 ms blocks and Praat (`pm`) pitch tracking. An initial test on
the Ryzen 5 PRO 5650U laptop averaged about 327 ms computation per 400 ms block.
This excludes network and speaker buffering and is not a latency guarantee.
For RMVPE pitch tracking, use `--pitch-method rmvpe --block-ms 800`; the measured
mean was about 695 ms per 800 ms block. RMVPE at 400 ms could not sustain real
time on this CPU. First-use warm-up can take substantially longer.

Replies carry buffering and conversion delay. The server crossfades overlapping
blocks, flushes partial replies after a brief input gap, and emits the final tail
before shutdown. The application bounds unacknowledged input to three seconds.
The existing Live client does not flush already queued playback on user speech;
voice conversion therefore does not guarantee immediate barge-in cancellation.
Physical Pi/Bluetooth playback still needs target testing.

RVC source is pinned to `81eed5e8f68b6bed1789f682fe78cdd324495afc` and helper
weights to `lj1995/VoiceConversionWebUI` revision
`e6d0c1a17da07c33557852f9dfa2bd44cc75737d`. The upstream MIT license remains in
`.majel/rvc/LICENSE`. Runtime files and checkpoints stay in the git-ignored
`.majel` directory. Checkpoints are loaded in PyTorch's weights-only mode.
