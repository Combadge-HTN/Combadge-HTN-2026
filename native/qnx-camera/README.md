# QNX camera snapshot helper

`combadge-camera` captures one fresh camera frame and writes `snapshot.jpg` to
an existing output directory. It connects directly to QNX's Sensor Framework
and uses the installed TurboJPEG library. No Python imaging package is needed.

## Build and take a picture

Run on the QNX Pi, from the repository root:

```sh
make -C native/qnx-camera
mkdir -p /tmp/badge-photo
native/qnx-camera/combadge-camera --unit 4 --output-dir /tmp/badge-photo
```

The helper refuses to overwrite an existing `snapshot.jpg`; use a fresh
directory for another photo. Diagnostics go to stderr. Successful capture exits
with status 0; errors and cancellation exit nonzero.

Requirements: a C11 compiler, `libcamapi`, `libturbojpeg.so.0`, and a running
`sensor` service with an accessible NV12 camera. On the tested Pi 5/QNX 8 image,
the IMX708 on camera connector 2 is **unit 4** (`/dev/sensor/camera4`), and
`qnxuser` can capture without sudo. Units 1 and 2 are simulated sources. Use
`--unit N` for a different configured camera; the helper does not auto-select one
or change its configuration.

The helper waits one second for camera warmup, copies a viewfinder frame, stops
the stream, and encodes a JPEG. The default 2304 × 1296 NV12 source becomes
1152 × 648 using integer downsampling. JPEG quality is reduced if needed to
stay at or below 256 KiB. If that is insufficient, capture fails; lower
`--max-dimension` and retry. This captures a preview frame, not the camera's
full-resolution still-photo mode. Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--output-dir DIR` | Required | Existing directory for the single JPEG |
| `--unit N` | 4 | Sensor Framework camera unit |
| `--timeout N` | 10 | Frame wait including warmup, 2–20 seconds |
| `--max-dimension N` | 1280 | Maximum output width/height, 2–4096 pixels |

SIGINT/SIGTERM stop capture and remove incomplete output. The frame timeout
does not bound a stalled native driver call; the Python integration provides
an outer process timeout and kill fallback.

## Use from Python or a voice session

The existing `SnapshotCapture` class manages the temporary output directory and
loads the encoded image:

```python
from combadge.capture import SnapshotCapture

camera = SnapshotCapture([
    "/absolute/path/to/Combadge-HTN-2026/native/qnx-camera/combadge-camera",
    "--unit", "4", "--output-dir", "{directory}",
])
image = await camera.capture("What is in front of me?")
```

Or append this option to your configured `combadge voice` command:

```sh
--snapshot-command '/absolute/path/to/Combadge-HTN-2026/native/qnx-camera/combadge-camera --unit 4 --output-dir {directory}'
```

The standalone helper only writes a local file. A voice session sends requested
snapshots to the configured vision backend through the existing capture flow.

## Compatibility and checks

Matching `<camera/camera_api.h>` SDK headers are used when installed. The tested
quick-start image has the runtime libraries but lacks those headers, so
`camera_compat.h` includes a minimal fallback for its observed 64-bit QNX 8
buffer ABI (104-byte descriptor, NV12 fields at offset 72). This is specific to
that image, not a general QNX ABI guarantee. A mismatched descriptor size is
rejected; for other images, build with their matching SDK headers.

The callback pattern follows the public
[QNX camera example](https://gitlab.com/qnx/projects/camera-projects/applications/camera_example1_callback).
The older public QNX 7.1 buffer layout differs from the tested image. JPEG
encoding uses the stable
[TurboJPEG API](https://github.com/libjpeg-turbo/libjpeg-turbo/blob/3.1.0/src/turbojpeg.h).

Run the synthetic NV12 conversion tests on QNX or a development machine:

```sh
make -C native/qnx-camera test
```

These check padding, separate chroma strides, downsampling, and invalid geometry.
Hardware acceptance also requires capturing and decoding a JPEG on the target.
The helper has been tested against the physical unit 4 camera on the Pi.
