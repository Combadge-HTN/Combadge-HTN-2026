#!/usr/bin/env python3
"""Local RVC playback server and WAV preview tool (separate Python 3.12 environment).

Uses RVC-Project's pinned MIT-licensed runtime. The streaming overlap alignment
follows its realtime reference implementation; upstream's license is retained
in the runtime checkout. See docs/MAJEL.md for setup and the model source.
"""

import argparse
import asyncio
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

RATE = 24_000
ROOT = Path(__file__).resolve().parents[1]


class Majel:
    def __init__(self, args):
        # Heavy dependencies are deliberately absent from the QNX application.
        source = args.rvc_dir.expanduser().resolve()
        os.environ["HF_HOME"] = str(ROOT / ".majel" / "cache")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"
        os.chdir(source)  # Upstream resolves its pitch weights relative to its checkout.
        sys.path.insert(0, str(source))
        import numpy as np
        import torch
        from infer.rtrvc import RVC
        from torchaudio.transforms import Resample

        torch.set_num_threads(args.threads)
        self.np, self.torch = np, torch
        device = torch.device(args.device)
        if args.index:
            with args.index.open("rb") as index:
                if index.read(128).lstrip().startswith(b"<"):
                    raise ValueError(
                        "The index is an HTML error page. Omit --index to disable retrieval."
                    )
        self.rvc = RVC(
            args.pitch,
            0,
            str(args.model.expanduser().resolve()),
            str(args.index.resolve()) if args.index else "",
            0.75 if args.index else 0,
            SimpleNamespace(device=device, is_half=False),
        )
        if getattr(self.rvc, "net_g", None) is None:
            raise RuntimeError("RVC could not load the model; inspect its error above.")
        self.block = RATE * args.block_ms // 1000
        self.block_bytes = self.block * 2
        self.overlap, self.search, self.context = 960, 240, 12_000
        self.history = torch.zeros(
            self.context + self.overlap + self.search + self.block, device=device
        )
        self.resample_in = Resample(RATE, 16_000).to(device)
        self.resample_out = Resample(self.rvc.tgt_sr, RATE).to(device)
        self.tail = torch.zeros(self.overlap, device=device)
        self.fade = torch.sin(torch.linspace(0, torch.pi / 2, self.overlap, device=device)).square()
        self.kernel = torch.ones(1, 1, self.overlap, device=device)
        self.pitch_method = args.pitch_method

    def reset(self):
        with self.torch.inference_mode():
            self.history.zero_()
            self.tail.zero_()
            self.rvc.cache_pitch.zero_()
            self.rvc.cache_pitchf.zero_()

    def convert(self, pcm):
        if len(pcm) != self.block_bytes:
            raise ValueError("Incorrect conversion block length")
        torch, np = self.torch, self.np
        samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
        self.history[: -self.block] = self.history[self.block :].clone()
        self.history[-self.block :] = torch.from_numpy(samples).to(self.history.device)
        if not torch.any(self.history):
            with torch.inference_mode():
                self.tail.zero_()
            return bytes(self.block_bytes)
        with torch.inference_mode():
            converted = self.rvc.infer(
                self.resample_in(self.history),
                self.block * 2 // 3,
                self.context // 240,
                (self.block + self.overlap + self.search) // 240,
                self.pitch_method,
            )
            converted = self.resample_out(converted)
            window = converted[: self.overlap + self.search][None, None, :]
            numerator = torch.nn.functional.conv1d(window, self.tail[None, None, :])
            denominator = torch.nn.functional.conv1d(window.square(), self.kernel).add(1e-8).sqrt()
            offset = int(torch.argmax(numerator / denominator))
            converted = converted[offset:]
            converted[: self.overlap] = converted[: self.overlap] * self.fade + self.tail * (
                1 - self.fade
            )
            self.tail = converted[self.block : self.block + self.overlap].clone()
            output = converted[: self.block].cpu().numpy()
        if len(output) != self.block or not np.isfinite(output).all():
            raise RuntimeError("RVC returned invalid audio")
        return (np.clip(output, -1, 1) * 32767).astype("<i2").tobytes()

    def warm_up(self):
        # Exercise inference before accepting a live session; silence would skip it.
        probe = self.np.sin(self.np.arange(self.block) * (2 * self.np.pi * 180 / RATE)) * 200
        self.convert(probe.astype("<i2").tobytes())
        self.reset()


async def serve(args, engine):
    from websockets.asyncio.server import serve as websocket_server
    from websockets.exceptions import ConnectionClosed

    token = os.environ.get("COMBADGE_VOICE_CONVERSION_TOKEN", "")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise ValueError("Set COMBADGE_VOICE_CONVERSION_TOKEN before listening on the network.")
    occupied = False

    async def handler(socket):
        nonlocal occupied
        if token and not hmac.compare_digest(
            socket.request.headers.get("Authorization", ""), "Bearer " + token
        ):
            await socket.close(1008, "Authentication required")
            return
        if occupied:
            await socket.close(1013, "Voice converter is already in use")
            return
        occupied = True
        pending = bytearray()
        active = False

        async def send_block(pcm, consumed):
            converted = await asyncio.to_thread(engine.convert, pcm)
            await socket.send(struct.pack("!I", consumed) + converted)

        async def flush():
            nonlocal active
            if not active:
                return
            if pending:
                count = len(pending)
                await send_block(bytes(pending).ljust(engine.block_bytes, b"\0"), count)
                pending.clear()
            # Emit the overlapping tail even when the final source block was full.
            await send_block(bytes(engine.block_bytes), 0)
            engine.reset()
            active = False

        try:
            engine.reset()
            await socket.send(json.dumps({"type": "ready", "protocol": 1, "rate": RATE}))
            while True:
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=0.12)
                except TimeoutError:
                    await flush()
                    continue
                if isinstance(message, str):
                    if json.loads(message).get("type") != "flush":
                        raise ValueError("Unknown converter command")
                    await flush()
                    await socket.send(json.dumps({"type": "flushed"}))
                    continue
                if not message or len(message) % 2:
                    raise ValueError("Expected mono PCM16LE at 24 kHz")
                pending.extend(message)
                active = True
                while len(pending) >= engine.block_bytes:
                    await send_block(bytes(pending[: engine.block_bytes]), engine.block_bytes)
                    del pending[: engine.block_bytes]
        except ConnectionClosed:
            pass
        except Exception as error:
            print(f"Conversion session failed: {error}", file=sys.stderr, flush=True)
            await socket.close(1011, "Voice conversion failed")
        finally:
            engine.reset()
            occupied = False

    async with websocket_server(
        handler, args.host, args.port, max_size=RATE * 2, max_queue=4, close_timeout=1
    ):
        print(
            f"Majel ready at ws://{args.host}:{args.port} ({args.block_ms} ms blocks)", flush=True
        )
        await asyncio.Future()


def preview(args, engine):
    import soundfile as sf

    source, rate = sf.read(args.input, dtype="int16")
    if rate != RATE or source.ndim != 1:
        raise ValueError("Preview input must be a mono 24 kHz WAV file")
    pcm = source.astype("<i2").tobytes()
    output, durations = [], []
    for offset in range(0, len(pcm) + engine.block_bytes, engine.block_bytes):
        start = time.perf_counter()
        output.append(
            engine.convert(
                pcm[offset : offset + engine.block_bytes].ljust(engine.block_bytes, b"\0")
            )
        )
        durations.append(time.perf_counter() - start)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(
        args.output, engine.np.frombuffer(b"".join(output), dtype="<i2"), RATE, subtype="PCM_16"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "block_ms": args.block_ms,
                "mean_compute_ms": round(1000 * sum(durations) / len(durations), 1),
                "max_compute_ms": round(1000 * max(durations), 1),
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rvc-dir", type=Path, default=ROOT / ".majel" / "rvc")
    parser.add_argument("--model", type=Path, default=ROOT / ".majel" / "Majel.pth")
    parser.add_argument(
        "--index", type=Path, help="optional real FAISS index; omit the broken Majel index"
    )
    parser.add_argument("--device", choices=("cpu", "cuda", "mps"), default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--block-ms", type=int, choices=(400, 800), default=400)
    parser.add_argument("--pitch-method", choices=("pm", "rmvpe"), default="pm")
    parser.add_argument("--pitch", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--input", type=Path, help="convert a WAV instead of starting the server")
    parser.add_argument("--output", type=Path, default=ROOT / "recordings" / "majel-preview.wav")
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    # Resolve before upstream changes the process working directory.
    for name in ("model", "index", "input", "output"):
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).expanduser().resolve())
    engine = Majel(args)
    engine.warm_up()
    if args.input:
        preview(args, engine)
    else:
        try:
            asyncio.run(serve(args, engine))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
