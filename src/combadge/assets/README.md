# Activation sound

`tng_chirp_clean.pcm` is the user-selected TNG communicator sound from:
https://www.trekcore.com/audio/communicator/tng_chirp_clean.mp3

Converted from MP3 to signed 16-bit little-endian, mono, 24,000 Hz PCM
using FFmpeg, without trimming or normalization. Duration: 0.456 seconds.
The asset is local so badge activation needs no download or MP3 decoder.
It plays on double-tap activation and normal session exit, not individual taps.

`tng_chirp_stereo.pcm` is the same clip converted to 44,100 Hz stereo PCM16
with a 0.6 gain for direct Bluetooth FIFO playback. This avoids process startup
and live resampling delays for acknowledgement sounds.

Original MP3 SHA256:
`8af14ed5cdd3efb89f889c0303082dd46472574beb2bd05ab5d734290bd80df7`

PCM SHA256:
`837746606f55fa3b6e36dfcce20bcff424cd08fe0bb9d8d80d59c190fbae02b0`

This is third-party audio, not an original project sound or an asset covered
by the project's code license. Verify redistribution rights before publishing.
