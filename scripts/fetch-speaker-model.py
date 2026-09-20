#!/usr/bin/env python3
"""Download the pinned CAM++ model; verify before replacing any existing file."""
import argparse
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

from combadge.local_speakers import MODEL_SHA256, MODEL_URL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?", default=Path("models/campplus.onnx"))
    args = parser.parse_args()
    target = args.destination.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temp = Path(output.name)
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 40 * 1024 * 1024:
                        raise ValueError("Model download exceeds size limit")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise ValueError("Model checksum mismatch; existing file was preserved")
        os.replace(temp, target)
        print(target)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
