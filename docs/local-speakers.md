# On-device speaker identification

The experimental `local` backend runs CAM++ with the QNX port of ONNX Runtime.
The Python 3.14 application talks to a persistent native C++ worker over private
pipes. Audio and voice embeddings stay on the device for speaker matching.
GPT Live still receives microphone audio for the conversation.

This is **windowed identification**, not full diarization. It compares 1.5 seconds
of recent audio with enrolled references, at most once every 0.5 seconds during
continuous speech. After 0.4 seconds of quiet, a shorter final window is also
analyzed if at least 0.5 seconds of audible input was collected. A match
requires both a minimum cosine similarity and a margin over the next candidate.
Silence is screened with an energy gate. This does not separate simultaneous
speakers, and background noise can affect both detection and matching.

Audio continues directly to Live while inference runs. Only identity changes
are added as context; no transcript is copied into the conversation. A new turn
clears the previous match until new evidence arrives. Slow inference never builds
an unbounded queue. Results from a previous turn or an old audio window are discarded.
Short questions can finish before identification; this backend does not guarantee
that the first reply already has the speaker's name. Voice estimates are not authentication.

## Build on QNX

Requires the QNX native `clang`, `clang++`, `cmake`, `ninja`, `git`, `apk`, and Python
3.14 tools. The script builds in a separate directory and does not install system
libraries. Internet access is needed to fetch pinned sources and dependencies.
The build applies the [documented runtime corrections](../native/speaker/README.md),
including an upstream fix for CAM++'s partial-window average pooling.

```sh
sh scripts/build-qnx-speaker-runtime.sh "$HOME/local-speakers"
.venv/bin/python scripts/fetch-speaker-model.py
```

Set these values in the project's private `.env`, using the build directory's
absolute path for the worker:

```dotenv
COMBADGE_SPEAKER_BACKEND=local
COMBADGE_SPEAKER_WORKER=/data/home/qnxuser/local-speakers/speaker-build/speaker-worker
COMBADGE_SPEAKER_MODEL=models/campplus.onnx
COMBADGE_SPEAKERS='{"Edmon":"recordings/edmon-pi-mic.wav","Samuel":"recordings/samuel.wav"}'
```

References use the existing 2–10 second PCM16 WAV enrollment format. The first
four seconds are used for enrollment. Choose speech with one person and minimal
background noise, preferably recorded with the badge microphone. Use separate
speech for validation. Neither recordings nor model weights belong in Git.

```sh
combadge speakers recordings/heldout.wav --speaker-backend local \
  --speaker Edmon=recordings/edmon-pi-mic.wav --speaker Samuel=recordings/samuel.wav
combadge start --speaker-backend local
```

The `speakers` command performs offline windowed matching and reports measured
inference time. It needs no cloud key. `start --no-speakers` disables matching.
A missing or invalid local model fails explicitly rather than silently switching
to a cloud service. The default remains `auto` until this prototype is validated.

## Pinned components

- [QNX ONNX Runtime port](https://github.com/qnx-ports/onnxruntime/tree/qnx-v1.23.2),
  commit `1fd5b38211fc88d07fff8dbd0fe183aaa312469e`, MIT license.
- [kaldi-native-fbank](https://github.com/csukuangfj/kaldi-native-fbank/tree/v1.22.3),
  commit `b09e686fe2084732ddd30d1ef80acfc0f13eaf01`, Apache-2.0 license.
- [English VoxCeleb CAM++ ONNX export](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models),
  from [3D-Speaker](https://github.com/modelscope/3D-Speaker).
  SHA-256 `357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b`.

The feature pipeline follows sherpa-onnx: normalized 16 kHz samples, 80-bin Kaldi
filterbanks, no dither, reflected frame edges, a 7.6 kHz upper filterbank limit,
and per-bin mean subtraction. The worker uses the same resampling and extraction
for enrollment and incoming speech.

## QNX Pi 5 validation

Tested with Python 3.14.0, QNX 8.0.0 aarch64le, the patched QNX ONNX Runtime
1.23.2 port, and two inference threads. Model loading took 0.88 seconds; enrolling
both four-second references took 0.49 seconds. The persistent worker processed
each 1.5-second audio window in 93–97 ms, including resampling and feature extraction.
The window collection time is additional; these numbers are not first-reply latency.

With disjoint enrollment and held-out speech, Edmon matched in 6/6 overlapping
windows and Samuel in 9/9. A synthetic unknown remained unknown in 9/9 windows.
This small recorded-data check is not a population accuracy estimate or a
microphone/speaker feedback test.

Native checks verify process reuse across idle gaps, bounded request sizes,
resampling consistency, and a generated signal's embedding against a known
reference. The reference check caught incorrect pooling in the older runtime;
the patched build passes it without changing model weights or match thresholds.
