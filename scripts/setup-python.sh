#!/usr/bin/env bash
# Local desktop setup; QNX uses the manual installation in README.md.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

# Keep downloaded tools, Python, and caches inside the project.
export UV_CACHE_DIR="$project_root/.tools/cache"
export UV_PYTHON_INSTALL_DIR="$project_root/.tools/python"
export UV_PYTHON_BIN_DIR="$project_root/.tools/bin"
export UV_PROJECT_ENVIRONMENT="$project_root/.venv"
uv_bin="$project_root/.tools/bin/uv"

if [[ ! -x "$uv_bin" ]]; then
    command -v curl >/dev/null || {
        echo 'curl is required. Install it with: sudo apt install curl' >&2
        exit 1
    }
    mkdir -p "$project_root/.tools/bin"
    installer="$(mktemp)"
    trap 'rm -f "$installer"' EXIT
    curl --fail --location --show-error --silent https://astral.sh/uv/install.sh -o "$installer"
    UV_UNMANAGED_INSTALL="$project_root/.tools/bin" sh "$installer"
fi

python_version="$(tr -d '[:space:]' < .python-version)"
"$uv_bin" python install "$python_version"
"$uv_bin" sync --locked --python "$python_version" --extra voice --extra dev "$@"
# Include pip for the manual installation commands documented in the README.
"$uv_bin" pip install --python "$project_root/.venv/bin/python" pip

"$project_root/.venv/bin/python" --version
printf '\nSetup complete. From the repository root, run:\n  source .venv/bin/activate\n'
