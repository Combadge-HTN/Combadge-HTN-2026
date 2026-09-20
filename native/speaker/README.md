# Native runtime patches

These patches apply to the pinned QNX ONNX Runtime 1.23.2 source. They are
applied idempotently by `scripts/build-qnx-speaker-runtime.sh`.

- `onnxruntime-fp16-helper.patch` includes the existing fused-activation helper
  when contrib operators are disabled. The ARM FP16 standard Conv kernel still
  depends on that helper; otherwise the shared library fails to link.
- `onnxruntime-averagepool-ceil.patch` contains the production-source changes
  from Microsoft ONNX Runtime commit
  `bead91378f878dd8fb8d125dab371620a01d1f64`,
  [upstream PR #29629](https://github.com/microsoft/onnxruntime/pull/29629).
  The old runtime counted nonexistent trailing cells when averaging a partial
  pooling window. CAM++ uses that operator configuration, causing substantially
  incorrect embeddings. The upstream fix routes this configuration through the
  correct reference implementation and prevents optimization from reintroducing
  the defect. Model weights and matching thresholds are unchanged.

ONNX Runtime and these upstream changes are Copyright Microsoft Corporation,
licensed under the [MIT license](https://github.com/microsoft/onnxruntime/blob/bead91378f878dd8fb8d125dab371620a01d1f64/LICENSE).

The optional native integration tests include a reference embedding generated
from a deterministic chirp/tone/noise signal, verified against the official
Python ONNX Runtime CPU implementation. It contains no person's voice. This
checks numerical correctness in addition to worker lifetime and resampling.
