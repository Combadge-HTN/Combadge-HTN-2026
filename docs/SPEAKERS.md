# Speaker identification prototype

Add named speaker estimates to GPT-Live while microphone capture and spoken replies continue. Identification uses a separate `gpt-4o-transcribe-diarize` request; it is not native Live diarization. This feature is opt-in and does not grant permissions or change tool authorization.

## Setup

Use Python 3.14 and install the pinned voice dependencies:

```sh
python -m pip install --require-hashes -r requirements-voice.txt
python -m pip install --no-deps -e .
```

Prepare one clean, single-person WAV reference for each speaker: 2–10 seconds, PCM16, mono or stereo, 8–96 kHz. The application uses the first four seconds (or the whole clip if shorter), mixes stereo to mono, and preserves the sample rate. This keeps reference uploads below the API form-part size limit. Choose clearly audible speech from the start, with no other voices or music. Use recordings the speakers have agreed to enroll. Keep them outside source control; WAV files and `recordings/` are ignored by Git. No persistent voice profile is created remotely by this application.

Start the Pi with Bluetooth replies and enrolled speakers:

```sh
combadge start \
  --speaker Edmon=recordings/edmon.wav \
  --speaker Samuel=recordings/samuel.wav
```

Up to four unique names are supported. Relative paths resolve from the directory where you run the command, including across Bluetooth's sudo handoff. References are validated before Bluetooth starts or the microphone opens. The same options work with `combadge voice`. `--speaker` cannot be combined with `--tone`, `--check`, or `--list-devices`.

For QNX, retain the command audio interface:

```sh
combadge voice --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper' \
  --speaker Edmon=/path/to/edmon.wav \
  --speaker Samuel=/path/to/samuel.wav
```

Speaker analysis uses standard-library HTTPS in a background thread, matching the catalog adapter. No extra HTTP package, subprocess transport, or runtime patch is needed. The capture contract remains 24 kHz mono PCM16. Speaker behavior with the physical microphone still requires validation; installing this prototype does not install audio drivers or helpers.

## Streaming attribution before Live input

With `SPEECHMATICS_API_KEY` in the selected `.env`, `--speaker` selects the streaming
Speechmatics path. Without it, the older OpenAI background prototype below remains
available. The streaming path is experimental and is not yet accepted for deployment.

At startup, each supplied reference is enrolled with Speechmatics. Microphone PCM
then goes to Speechmatics first. Final word-level speaker results resolve exact
sample intervals in a bounded buffer. Only registered names survive; unregistered,
missing, or overlapping identities become unknown. Unresolved intervals expire to
unknown after two seconds of captured audio; late results cannot relabel released
speech. Audio uses a fixed five-second staging delay. One-second attribution packets
are prepared concurrently, with source-aligned word snippets and their scheduled
Live input times. A Live context acknowledgment is required before releasing the
matching PCM. If context misses its scheduled deadline, voice stops rather than
shifting the stream or playing audio with stale labels. The legacy background labels and `identify_speaker` tool are not used in this
mode. Pure digital silence does not generate identity updates.

There is no required warm-up utterance or six-second recording minimum. Enrollment
runs before the microphone opens. Processing and buffering still introduce latency.
Long audio is processed incrementally with bounded memory; if the pipeline falls
behind its buffer capacity it stops rather than dropping or misattributing speech.
Context acceptance is not proof the model used it correctly: first questions,
speaker switches, short input, unknown speakers, overlap, and speaker echo all need
end-to-end acceptance tests. Current acceptance is incomplete.

This mode sends the supplied reference clips and microphone stream to Speechmatics,
and sends attributed microphone audio to OpenAI. Speaker identifiers are held in
memory for the session. No new native QNX dependency is required; the existing
WebSocket transport and PCM format are used.

## OpenAI background prototype: behavior and latency

Microphone audio goes to Live first. A background worker analyzes six-second windows every three seconds, with one request in flight and at most one pending window. When analysis falls behind, the newest pending window replaces the older one. This bounds buffering but can leave some speech unidentified.

The first background identity update arrives after six seconds of captured audio **plus API processing time**. Ordinary replies do not wait for it and may begin before identification finishes.

Recognition questions such as “Can you recognize me?” use the `identify_speaker` backend tool. It immediately snapshots up to twelve seconds of recent microphone audio, including a first question shorter than six seconds. The backend waits for analysis before continuing, and Live is instructed to wait for that result before answering the identity question. It may acknowledge the request while waiting; there is no prescribed answer wording. The ten-second lookup deadline includes waiting for an in-flight background analysis. Lookup failure or timeout returns `unavailable`, distinct from a completed `unknown` result. Mixed known/unknown speakers or multiple names return `ambiguous`. This adds recognition and delegation latency to identity questions, not every conversation turn. Model delegation still requires live acceptance testing; this is not a hard generation gate for all replies.

Background requests have an eight-second total deadline, and results more than eight seconds behind the captured audio or wall clock are discarded. Recognition does not become immediate once enrolled.

Each result describes individual speech intervals, so two people speaking consecutively within a single turn can receive different labels. Names appear as separate timestamped estimates in the console; existing transcript captions are not rewritten. `--no-captions` hides both transcripts and speaker estimates while retaining failure notices.

The worker sends a plain-language match summary and the complete analyzed window using `session.thinking.append`. Console output still omits repeated intervals, but that must not remove another speaker from the model's evidence. Offsets count input samples from the first microphone frame, not transcript event timestamps.

Speaker observations are background context, not a request to speak. A clear match supplies the name for ordinary conversation. There is no name-answer template, required length, or prescribed confidence wording; responses follow the user's question and available context. Names the user gives or confirms remain conversational context; unknown audio alone does not contradict a self-introduction. The assistant must not announce label updates or repeat earlier answers when an update arrives. Multiple names or overlap still require uncertainty, and the estimates never authorize actions. Server acknowledgments confirm context acceptance, not that a particular reply used the label correctly.

## Uncertainty and failure handling

- Unenrolled API speaker labels become `unknown`; labels such as A/B are not identities across windows.
- Overlapping intervals with different labels become `ambiguous`.
- Three speaker changes within two seconds cause the result to be marked ambiguous. This conservative rule can also reject fast, legitimate exchanges.
- The model may miss overlap or confidently misidentify someone. The API provides no calibrated confidence score here; the application cannot guarantee rejection of every uncertain result. A single mixed microphone is not source separation.
- Reference quality, similar voices, short utterances, room noise, and speaker playback leaking into the microphone affect accuracy. Echo cancellation remains the audio path's responsibility.
- Authentication rejection or three consecutive analysis failures disable new identification until the voice session restarts. Other failures back off; normal voice continues. Rejected or unacknowledged context updates also disable labels rather than stopping voice.
- The speaker task is cancelled when the voice session ends, including phone handoff. An already-started HTTP request may finish under its socket timeout; its result is discarded and it receives no further audio. Process exit may wait for this request to finish. It does not listen to the subsequent call or restart the assistant after hang-up.

Names are conversational hints, not authentication. Do not use this prototype to decide who may place a call, access an account, or approve a purchase.

## Data and cost

Enabling this feature sends overlapping microphone windows, requested identity lookups, and every enrolled reference clip to OpenAI's transcription endpoint. Reference WAV metadata is stripped. Audio is held in bounded memory; this feature saves no recordings or transcripts. It does not send the transcription text back as trusted instructions—only validated speaker names and intervals. Provider data handling is governed by the account's API settings and policies.

Diarization adds paid requests alongside Live. Overlapping windows and repeated reference uploads increase usage. All-zero digital silence is skipped; ordinary microphone noise may still trigger requests. The normal voice session duration limit also bounds this feature's run time.

## Check recognition separately

Use a different utterance from enrollment when evaluating accuracy. Analysis input must be mono PCM16 WAV at 24 kHz, between 0.1 and 30 seconds:

```sh
combadge speakers /path/to/conversation.wav \
  --speaker Edmon=/path/to/edmon.wav \
  --speaker Samuel=/path/to/samuel.wav
```

This prints segment names/times and elapsed analysis time, using API credits without opening audio devices. Test each speaker individually, sequential speakers, an unenrolled person, simultaneous speech, background noise, and short interjections. Then verify spoken attribution in a real voice session; accepted context alone does not prove the assistant used it correctly.

To check the model handoff separately from recognition, run:

```sh
python scripts/check_speaker_context.py --env-file .env
```

The question starts after one second by default, before the first background result, with a scripted three-second analysis delay. Use `--question-delay` and `--recognition-delay` to change those timings. It logs tool results and spoken output timestamps so premature identity answers are visible.

This opt-in check uses paid TTS and Live API requests with generated speech and scripted matcher results. It opens no audio devices and uses no personal recordings. It checks two enrolled names, unknown speech, multiple speakers, overlap, and a named-then-unknown transition. Use `--case overlap`, for example, to rerun one case. Review the printed answers for appropriate uncertainty; automated name-presence checks alone do not establish semantic correctness or recognition accuracy.

References: [transcription API](https://developers.openai.com/api/reference/python/resources/audio/subresources/transcriptions/methods/create), [Live context injection](https://developers.openai.com/api/docs/guides/live-conversations#add-context-during-the-conversation).
