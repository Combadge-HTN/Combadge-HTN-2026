# Speaker identification prototype

Add named speaker estimates to GPT-Live while microphone capture and spoken replies continue. Identification uses a separate `gpt-4o-transcribe-diarize` request; it is not native Live diarization. This feature is opt-in and does not grant permissions or change tool authorization.

## Setup

Use Python 3.14 and install the pinned voice dependencies:

```sh
python -m pip install --require-hashes -r requirements-voice.txt
python -m pip install --no-deps -e .
```

Prepare one clean, single-person WAV reference for each speaker: 2–10 seconds, PCM16, mono or stereo, 8–96 kHz. The application uses the first four seconds (or the whole clip if shorter), mixes stereo to mono, and preserves the sample rate. This keeps reference uploads below the API form-part size limit. Choose clearly audible speech from the start, with no other voices or music. Use recordings the speakers have agreed to enroll. Keep them outside source control; WAV files and `recordings/` are ignored by Git. No persistent voice profile is created remotely by this application.

Add these options to your normal `commbadge voice` command:

```sh
--speaker Edmon=/path/to/edmon.wav --speaker Samuel=/path/to/samuel.wav
```

Up to four unique names are supported. References are loaded before the microphone opens. `--speaker` cannot be combined with `--check` or `--list-devices`.

For QNX, retain the command audio interface:

```sh
commbadge voice --audio-backend commands \
  --capture-command '/path/to/capture-helper' \
  --playback-command '/path/to/playback-helper' \
  --speaker Edmon=/path/to/edmon.wav \
  --speaker Samuel=/path/to/samuel.wav
```

Speaker analysis uses standard-library HTTPS in a background thread, matching the catalog adapter. No extra HTTP package, subprocess transport, or runtime patch is needed. The capture contract remains 24 kHz mono PCM16. Speaker behavior with the physical microphone still requires validation; installing this prototype does not install audio drivers or helpers.

## Behavior and latency

Microphone audio goes to Live first. A background worker analyzes six-second windows every three seconds, with one request in flight and at most one pending window. When analysis falls behind, the newest pending window replaces the older one. This bounds buffering but can leave some speech unidentified.

Expect the first identity update after six seconds of captured audio **plus API processing time**. Ordinary replies do not wait for it and may begin before identification finishes. This is unsuitable when every reply must know the speaker immediately. Requests have an eight-second total deadline, and results more than eight seconds behind the captured audio or wall clock are discarded. Recognition does not become immediate once enrolled.

Each result describes individual speech intervals, so two people speaking consecutively within a single turn can receive different labels. Names appear as separate timestamped estimates in the console; existing transcript captions are not rewritten. `--no-captions` hides both transcripts and speaker estimates while retaining failure notices.

The worker sends compact observations using `session.thinking.append`. Offsets count input samples from the first microphone frame, not transcript event timestamps. The assistant is instructed to use names only when it can unambiguously match the relevant speech, never to treat a historical label as the current speaker. Server acknowledgments confirm context acceptance, not that a particular reply used the label correctly.

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

Enabling this feature sends overlapping microphone windows and every enrolled reference clip to OpenAI's transcription endpoint. Reference WAV metadata is stripped. Audio is held in bounded memory; this feature saves no recordings or transcripts. It does not send the transcription text back as trusted instructions—only validated speaker names and intervals. Provider data handling is governed by the account's API settings and policies.

Diarization adds paid requests alongside Live. Overlapping windows and repeated reference uploads increase usage. All-zero digital silence is skipped; ordinary microphone noise may still trigger requests. The normal voice session duration limit also bounds this feature's run time.

## Check recognition separately

Use a different utterance from enrollment when evaluating accuracy. Analysis input must be mono PCM16 WAV at 24 kHz, between 0.1 and 30 seconds:

```sh
commbadge speakers /path/to/conversation.wav \
  --speaker Edmon=/path/to/edmon.wav \
  --speaker Samuel=/path/to/samuel.wav
```

This prints segment names/times and elapsed analysis time, using API credits without opening audio devices. Test each speaker individually, sequential speakers, an unenrolled person, simultaneous speech, background noise, and short interjections. Then verify spoken attribution in a real voice session; accepted context alone does not prove the assistant used it correctly.

References: [transcription API](https://developers.openai.com/api/reference/python/resources/audio/subresources/transcriptions/methods/create), [Live context injection](https://developers.openai.com/api/docs/guides/live-conversations#add-context-during-the-conversation).
