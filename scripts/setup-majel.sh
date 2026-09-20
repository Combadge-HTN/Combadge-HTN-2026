#!/usr/bin/env bash
# Install the optional Linux CPU converter without changing the badge environment.
set -euo pipefail
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
runtime="$task_root/.majel"
model_file=${1:?Usage: bash scripts/setup-majel.sh /path/to/Majel.pth}
uv_bin="$task_root/.tools/bin/uv"
if [[ ! -x "$uv_bin" ]]; then uv_bin=$(command -v uv); fi
if [[ ! -f "$model_file" ]]; then printf 'Model not found: %s\n' "$model_file" >&2; exit 1; fi
mkdir -p "$runtime"
if [[ ! -x "$runtime/env/bin/python" ]]; then
  "$uv_bin" venv --python python3.12 "$runtime/env"
fi
"$uv_bin" pip install --python "$runtime/env/bin/python" \
  --index-url https://download.pytorch.org/whl/cpu 'torch==2.7.1+cpu' 'torchaudio==2.7.1+cpu'
"$uv_bin" pip install --python "$runtime/env/bin/python" -r "$task_root/requirements-majel.txt"
revision=81eed5e8f68b6bed1789f682fe78cdd324495afc
assets_revision=e6d0c1a17da07c33557852f9dfa2bd44cc75737d
if [[ ! -d "$runtime/rvc" ]]; then
  curl --fail --location --connect-timeout 15 --max-time 120 \
    "https://api.github.com/repos/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/zipball/$revision" \
    --output "$runtime/rvc.zip"
  "$runtime/env/bin/python" - "$runtime" <<'PY'
import pathlib, zipfile
import sys
runtime = pathlib.Path(sys.argv[1])
with zipfile.ZipFile(runtime / 'rvc.zip') as archive:
    root = archive.namelist()[0].split('/')[0]
    for name in archive.namelist():
        if pathlib.Path(name).is_absolute() or '..' in pathlib.Path(name).parts:
            raise ValueError('Invalid runtime archive path')
    archive.extractall(runtime / 'source')
(runtime / 'source' / root).rename(runtime / 'rvc')
PY
fi
mkdir -p "$runtime/rvc/assets/hubert_base" "$runtime/rvc/assets/rmvpe"
for name in config.json preprocessor_config.json pytorch_model.bin; do
  if [[ ! -f "$runtime/rvc/assets/hubert_base/$name" ]]; then
    curl --fail --location --connect-timeout 15 --max-time 180 \
      "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/$assets_revision/hubert_base/$name" \
      --output "$runtime/rvc/assets/hubert_base/$name.part"
    mv "$runtime/rvc/assets/hubert_base/$name.part" "$runtime/rvc/assets/hubert_base/$name"
  fi
done
# RMVPE is optional for the default fast CPU profile.
if [[ ! -f "$runtime/rvc/assets/rmvpe/rmvpe.pt" ]]; then
  curl --fail --location --connect-timeout 15 --max-time 180 \
    "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/$assets_revision/rmvpe.pt" \
    --output "$runtime/rvc/assets/rmvpe/rmvpe.pt.part"
  mv "$runtime/rvc/assets/rmvpe/rmvpe.pt.part" "$runtime/rvc/assets/rmvpe/rmvpe.pt"
fi
if [[ ! -f "$runtime/Majel.pth" ]]; then cp -- "$model_file" "$runtime/Majel.pth"; fi
printf 'Ready. Start with: %s/env/bin/python %s/scripts/majel_server.py\n' "$runtime" "$task_root"
