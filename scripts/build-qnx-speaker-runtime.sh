#!/bin/sh
# Native QNX build, isolated from system libraries and the active application.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
prefix=${1:?usage: build-qnx-speaker-runtime.sh BUILD_DIRECTORY}
mkdir -p "$prefix"
prefix=$(CDPATH= cd -- "$prefix" && pwd)
case "$(uname -s)" in QNX) ;; *) echo 'This script builds natively on QNX.' >&2; exit 1;; esac
jobs=${COMBADGE_BUILD_JOBS:-2}
# The QNX base patch utility lacks --binary; fetch GNU patch into our prefix.
if [ ! -x "$prefix/tools/usr/bin/patch" ]; then
    mkdir -p "$prefix/tools"
    apk fetch --output "$prefix/tools" patch=2.8-r2
    (cd "$prefix/tools" && tar -xzf patch-2.8-r2.apk)
fi
if [ ! -d "$prefix/onnxruntime/.git" ]; then
    git clone --depth 1 --branch qnx-v1.23.2 \
        https://github.com/qnx-ports/onnxruntime.git "$prefix/onnxruntime"
fi
if [ "$(git -C "$prefix/onnxruntime" rev-parse HEAD)" != 1fd5b38211fc88d07fff8dbd0fe183aaa312469e ]; then
    echo 'Unexpected ONNX Runtime revision; expected pinned qnx-v1.23.2 commit.' >&2
    exit 1
fi
cmake -S "$prefix/onnxruntime/cmake" -B "$prefix/ort-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_C_FLAGS=-D_QNX_SOURCE -DCMAKE_CXX_FLAGS=-D_QNX_SOURCE \
    -Donnxruntime_BUILD_SHARED_LIB=ON -Donnxruntime_BUILD_UNIT_TESTS=OFF \
    -Donnxruntime_BUILD_BENCHMARKS=OFF -Donnxruntime_ENABLE_PYTHON=OFF \
    -Donnxruntime_USE_FULL_PROTOBUF=ON -Donnxruntime_ENABLE_CPUINFO=OFF \
    -Donnxruntime_DISABLE_CONTRIB_OPS=ON -Donnxruntime_DISABLE_ML_OPS=ON \
    -Donnxruntime_DISABLE_RTTI=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DPatch_EXECUTABLE="$prefix/tools/usr/bin/patch"
cmake --build "$prefix/ort-build" --parallel "$jobs"
cmake -S "$root/native/speaker" -B "$prefix/speaker-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
    -DORT_INCLUDE_DIR="$prefix/onnxruntime/include/onnxruntime/core/session" \
    -DORT_LIBRARY="$prefix/ort-build/libonnxruntime.so"
cmake --build "$prefix/speaker-build" --parallel "$jobs"
printf 'Speaker worker: %s\n' "$prefix/speaker-build/speaker-worker"
