#!/usr/bin/env python3
"""Start the Pi's Bluetooth speaker and voice/camera app with one command."""

import argparse
import errno
import os
import platform
import pty
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Startup:
    def __init__(self):
        self.scanning = False
        self.connected = False
        self.fifo = False
        self.streaming = False
        self.sample_rate = None
        self.fifo_requested = False

    def observe(self, line):
        if match := re.search(r"sampling frequency (\d+)", line):
            self.sample_rate = int(match[1])
        if "Controller ready." in line and not self.scanning:
            self.scanning = True
            return b"a"
        if "Stream established" in line and not self.connected:
            self.connected = True
            if self.sample_rate == 44100:
                self.fifo_requested = True
                return b"f"
            return b"w"  # Ask Dan's driver to negotiate its required PCM rate.
        if "Stream already configured for 44100" in line:
            self.fifo_requested = True
            return b"f"
        if "PCM FIFO ready" in line:
            self.fifo = True
        if "Stream started" in line:
            self.streaming = True
            if self.sample_rate == 44100 and not self.fifo_requested:
                self.fifo_requested = True
                return b"f"
        return b""

    @property
    def ready(self):
        return self.connected and self.fifo and self.streaming


class Radio:
    def __init__(self, directory):
        self.master, slave = pty.openpty()
        environment = os.environ.copy()
        # User-approved full PCM amplitude; preserve explicit quieter overrides.
        environment.setdefault("QNX_PCM_VOLUME_SHIFT", "0")
        try:
            self.process = subprocess.Popen(
                ["sh", "./run-radio.sh"],
                cwd=directory,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env=environment,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.pending = ""
        self.startup = Startup()

    def poll(self):
        if self.process.poll() is not None:
            raise RuntimeError("Bluetooth launcher exited; inspect the messages above")
        if not select.select([self.master], [], [], 0.2)[0]:
            return
        try:
            chunk = os.read(self.master, 8192)
        except OSError as error:
            if error.errno == errno.EIO:
                raise RuntimeError("Bluetooth terminal closed") from error
            raise
        if not chunk:
            raise RuntimeError("Bluetooth terminal closed")
        self.pending += chunk.decode(errors="replace")
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            print(f"Bluetooth: {line}", flush=True)
            if command := self.startup.observe(line):
                os.write(self.master, command)
            if any(
                message in line
                for message in (
                    "UART differs",
                    "Controller startup timed out",
                    "Stream released",
                    "A2DP Source: Disconnected",
                    "PCM requires",
                    "Assertion failed",
                    "Stream reconfiguration failed",
                )
            ):
                raise RuntimeError(f"Bluetooth is unavailable: {line}")

    def connect(self):
        deadline = time.monotonic() + 75
        while not self.startup.ready:
            self.poll()
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "Speaker connection timed out. Turn on TWS Mini Speaker, disconnect "
                    "it from other devices, and run combadge start again."
                )

    def close(self):
        try:
            stop_process(self.process, signal.SIGTERM, 12)
        finally:
            os.close(self.master)


def stop_process(process, sig, timeout):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, sig)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Process group {process.pid} did not stop; hardware cleanup needs attention"
        ) from error


def idle_pins():
    output = subprocess.check_output(["gpio-bcm", "get", "24-29"], text=True)
    for pin in (24, 25, 26, 27, 29):
        line = next((line for line in output.splitlines() if line.startswith(f"GPIO{pin}/")), "")
        if "pull=pd" not in line or "func=INPUT" not in line:
            raise RuntimeError(f"GPIO {pin} is not idle; another Bluetooth session may own it")
        if pin == 29 and "level=lo" not in line:
            raise RuntimeError("Bluetooth power pin is not low; refusing UART preparation")


def run(args):
    from combadge.startup import shopping_arguments

    if platform.system() != "QNX" or os.geteuid() != 0 or not os.environ.get("SUDO_USER"):
        raise RuntimeError("Run combadge start on the QNX Pi as qnxuser (it invokes sudo)")
    user = os.environ["SUDO_USER"]
    if user == "root":
        raise RuntimeError("Run combadge start from the qnxuser account, not a root shell")
    directory = args.bluetooth_dir.resolve()
    for path in (
        directory / "run-radio.sh",
        directory / "play_pcm.py",
        directory / "btstack-master/port/qnx-pi5/a2dp_source_demo",
        ROOT / ".venv/bin/combadge",
    ):
        if not path.is_file():
            raise RuntimeError(f"Required file is missing: {path}")
    if not stat.S_ISFIFO((directory / "audio.pcm").lstat().st_mode):
        raise RuntimeError("The Bluetooth audio.pcm path must be a named pipe")
    if (directory / ".radio-lock").exists():
        raise RuntimeError("Bluetooth is already running. Stop its existing session first.")
    lock = directory / ".combadge-session-lock"
    lock.mkdir()  # Refuse concurrent invocations; never remove someone else's lock.
    radio = app = None
    prepared = False
    try:
        subprocess.run(
            [str(directory / "btstack-master/port/qnx-pi5/a2dp_source_demo"), "--check-board"],
            check=True,
        )
        idle_pins()
        with tempfile.TemporaryDirectory(prefix="combadge-start-") as scratch:
            helper = str(Path(scratch) / "uart-fifo")
            subprocess.run(
                ["cc", "-O2", "-o", helper, str(ROOT / "native/qnx-uart-fifo.c")], check=True
            )
            prepared = subprocess.check_output([helper, "prepare"], text=True).strip() == "enabled"
            try:
                print("Connecting TWS Mini Speaker…", flush=True)
                radio = Radio(directory)
                radio.connect()
                if args.tone:
                    command = [
                        str(ROOT / ".venv/bin/python"),
                        str(directory / "play_pcm.py"),
                        "--tone",
                    ]
                else:
                    playback = shlex.join(
                        [
                            str(ROOT / ".venv/bin/python"),
                            str(ROOT / "scripts/bluetooth_playback.py"),
                            "--bluetooth-dir",
                            str(directory),
                        ]
                    )
                    command = [
                        str(ROOT / ".venv/bin/combadge"),
                        "voice",
                        "--max-seconds",
                        str(args.max_seconds),
                        "--env-file",
                        str(args.env_file),
                        "--audio-backend",
                        "commands",
                        "--capture-command",
                        shlex.join(
                            [
                                "arecord",
                                "-D",
                                args.input_device,
                                "-q",
                                "-t",
                                "raw",
                                "-f",
                                "S16_LE",
                                "-c",
                                "1",
                                "-r",
                                "24000",
                                "--buffer-time=100000",
                            ]
                        ),
                        "--playback-command",
                        playback,
                    ]
                    if not args.no_camera:
                        command.extend(["--camera", "--camera-unit", str(args.camera_unit)])
                    if args.save_snapshots is not None:
                        command.extend(["--save-snapshots", str(args.save_snapshots)])
                    command.append("--calls" if args.calls else "--no-calls")
                    command.extend(shopping_arguments(args))
                print("Speaker stream ready. Starting test… Ctrl+C stops everything.", flush=True)
                app = subprocess.Popen(
                    ["sudo", "-u", user, "--", *command],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                while app.poll() is None:
                    radio.poll()
                if app.returncode:
                    raise RuntimeError(f"Application exited with status {app.returncode}")
                # The app has flushed its helper; allow the bounded FIFO and
                # Bluetooth transport tail to reach the speaker before teardown.
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    radio.poll()
            finally:
                try:
                    stop_process(app, signal.SIGINT, 20)
                finally:
                    if radio is not None:
                        radio.close()
                    if prepared:
                        idle_pins()
                        subprocess.run([helper, "restore"], check=True)
    finally:
        # Keep the lock if a process is still alive; a second run must not compete.
        if (radio is None or radio.process.poll() is not None) and (
            app is None or app.poll() is not None
        ):
            lock.rmdir()
            print("Stopped. Bluetooth session cleaned up.", flush=True)


def launch(args):
    from combadge.startup import shopping_arguments

    if args.max_seconds <= 0:
        print("--max-seconds must be positive", file=sys.stderr)
        return 2
    if platform.system() != "QNX":
        print("combadge start runs on the QNX Pi", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        command = [
            "sudo",
            sys.executable,
            "-m",
            "combadge.bluetooth_startup",
            "--bluetooth-dir",
            str(args.bluetooth_dir.resolve()),
            "--max-seconds",
            str(args.max_seconds),
            "--env-file",
            str(args.env_file.resolve()),
            "--input-device",
            args.input_device,
            "--camera-unit",
            str(args.camera_unit),
        ]
        if args.no_camera:
            command.append("--no-camera")
        if args.save_snapshots is not None:
            command.extend(["--save-snapshots", str(args.save_snapshots.expanduser().resolve())])
        if args.tone:
            command.append("--tone")
        command.append("--calls" if args.calls else "--no-calls")
        shopping = shopping_arguments(args)
        command.extend(shopping)
        if "--shopify" not in shopping:
            command.append("--no-shopify")
        if "--shop-account" not in shopping:
            command.append("--no-shop-account")
        # Replace this process so Ctrl+C reaches the supervisor directly.
        os.execvp(command[0], command)
    try:
        run(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        return 1
    return 0


def main():
    from combadge.startup import add_arguments

    parser = argparse.ArgumentParser(prog="combadge start", description=__doc__)
    add_arguments(parser)
    return launch(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
