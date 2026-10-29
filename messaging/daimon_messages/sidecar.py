"""Runs the Google Messages sidecar (sidecar/gmessages, a small Go program on libgm) as a child process.

They talk over its stdin/stdout, one JSON object per line (the protocol is at the top of sidecar/gmessages/main.go),
so the sidecar opens no port. The first time it's needed it goes into the home's bin/ folder: downloaded from the GitHub
release matching this package's version (checked against that release's SHA256SUMS), or built from source with Go
(from PATH or ~/sdk/go) in a source checkout, or when the download fails.

It restarts on a crash, and when it has been disconnected for a while. Its pairing keys stay in session.json;
unpairing deletes them.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
sidecar_log = logging.getLogger("daimon_messages.gmessages")

CHECKOUT_SOURCE = Path(__file__).resolve().parents[1] / "sidecar" / "gmessages"
BUNDLED_SOURCE = Path(__file__).resolve().parent / "gmessages"  # the same source, shipped inside the wheel
SOURCE = CHECKOUT_SOURCE if CHECKOUT_SOURCE.exists() else BUNDLED_SOURCE
REPO = "LegoManACM/daimon"
EXE = "daimon-messages-sidecar.exe" if sys.platform == "win32" else "daimon-messages-sidecar"
RESTART_DELAYS = (5, 15, 60, 300, 600)
DISCONNECTED_RESTART_S = 10 * 60
BUILD_TIMEOUT_S = 15 * 60
DOWNLOAD_TIMEOUT_S = 5 * 60
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EventHandler = Callable[[dict], Awaitable[None]]


def find_go() -> str:
    found = shutil.which("go")
    if found:
        return found
    home = Path.home()
    for candidate in (home / "sdk" / "go" / "bin", home / "go" / "bin", Path("C:/Program Files/Go/bin"),
                      Path("/usr/local/go/bin")):
        exe = candidate / ("go.exe" if sys.platform == "win32" else "go")
        if exe.exists():
            return str(exe)
    return ""


async def build(binary: Path) -> str:
    """Builds the sidecar from source into `binary`. Returns '' on success, or what went wrong."""
    go = find_go()
    if not go:
        return "Go isn't installed. Install it from https://go.dev/dl (or `winget install GoLang.Go`)."
    binary.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        go, "build", "-trimpath", "-o", str(binary), ".", cwd=str(SOURCE), env=os.environ | {"CGO_ENABLED": "0"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), BUILD_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return "Building the Google Messages link took too long."
    if proc.returncode != 0 or not binary.exists():
        text = out.decode(errors="replace")[-800:]
        log.error("Sidecar build failed:\n%s", text)
        return f"Building the Google Messages link failed: {text[-300:]}"
    log.info("Built the sidecar at %s", binary)
    return ""


def asset_name(system: str | None = None, machine: str | None = None) -> str:
    """The release asset with the prebuilt sidecar for this OS/CPU, or '' if there isn't one."""
    goos = {"windows": "windows", "darwin": "darwin", "linux": "linux"}.get((system or platform.system()).lower())
    goarch = {"x86_64": "amd64", "amd64": "amd64", "x64": "amd64", "arm64": "arm64", "aarch64": "arm64",
              "armv8": "arm64"}.get((machine or platform.machine()).lower())
    if not goos or not goarch:
        return ""
    return f"daimon-messages-sidecar-{goos}-{goarch}" + (".exe" if goos == "windows" else "")


def installed_version() -> str:
    try:
        return metadata.version("daimon-messages")
    except metadata.PackageNotFoundError:
        return ""


def parse_sums(text: str) -> dict[str, str]:
    """`sha256sum` output -> {file name: hex digest}."""
    sums = {}
    for line in text.splitlines():
        digest, _, name = line.strip().partition(" ")
        if digest and name:
            sums[name.strip().lstrip("*")] = digest.lower()
    return sums


async def download(binary: Path, version: str | None = None, name: str | None = None,
                   transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Downloads the prebuilt sidecar from the release for `version` (default: this package's) into `binary`.
    Returns '' on success, or what went wrong."""
    version = version if version is not None else installed_version()
    name = name if name is not None else asset_name()
    if not name:
        return f"There's no prebuilt Google Messages link for this computer ({platform.system()} {platform.machine()})."
    if not version:
        return "Couldn't tell which version of daimon-messages is installed, so there's nothing to download."
    base = f"https://github.com/{REPO}/releases/download/v{version}"
    part = binary.with_name(binary.name + ".part")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_S, transport=transport) as http:
            r = await http.get(f"{base}/SHA256SUMS")
            r.raise_for_status()
            want = parse_sums(r.text).get(name)
            if not want:
                return f"The v{version} release has no checksum for {name}."
            binary.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            async with http.stream("GET", f"{base}/{name}") as r:
                r.raise_for_status()
                with part.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        digest.update(chunk)
                        f.write(chunk)
    except (httpx.HTTPError, OSError) as e:
        part.unlink(missing_ok=True)
        return f"Couldn't download the Google Messages link ({name}, v{version}): {e}"
    if digest.hexdigest() != want:
        part.unlink(missing_ok=True)
        return f"The downloaded Google Messages link ({name}) didn't match its checksum, so it wasn't installed."
    try:
        part.chmod(0o755)
        os.replace(part, binary)
    except OSError as e:
        part.unlink(missing_ok=True)
        return f"Couldn't install the Google Messages link at {binary}: {e}"
    log.info("Downloaded the sidecar (v%s) to %s", version, binary)
    return ""


async def install(binary: Path) -> str:
    """Puts the sidecar at `binary`: built in a source checkout with Go, else downloaded, else built from the bundled
    source if Go is around. Returns '' on success, or what went wrong."""
    go = find_go()
    if go and CHECKOUT_SOURCE.exists():
        return await build(binary)
    err = await download(binary)
    if not err:
        return ""
    log.warning("%s", err)
    if go and SOURCE.exists():
        log.info("Building the sidecar from source instead")
        return await build(binary)
    return f"{err} Installing Go (https://go.dev/dl) lets it be built from source instead."


class Sidecar:
    def __init__(self, bin_dir: Path, session: Path, on_event: EventHandler):
        self.binary = bin_dir / EXE
        self.session = session
        self.on_event = on_event
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"  # stopped | building | build_failed | + the sidecar's own states
        self.detail = ""
        self.since = time.time()
        self.qr = ""
        self.emoji = ""
        self.pair_error = ""
        self._runner: asyncio.Task | None = None
        self._stopping = False
        self._build_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._waiting: dict[str, asyncio.Future] = {}

    # ---- building ----

    def built(self) -> bool:
        return self.binary.exists()

    async def build(self) -> str:
        async with self._build_lock:
            if self.built():
                return ""
            self._set("building", "Setting up the Google Messages link (first time only, up to a minute or two)...")
            if err := await install(self.binary):
                self._set("build_failed", err)
                return err
            self._set("stopped", "")
            return ""

    # ---- running ----

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._stopping = False
            self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
        self._set("stopped", "")

    async def _run(self) -> None:
        failures = 0
        while not self._stopping:
            if not self.built() and await self.build():
                return  # nothing to run; pairing again retries the build
            started = time.monotonic()
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Sidecar failed")
            if self._stopping:
                return
            failures = 0 if time.monotonic() - started > 600 else failures + 1
            delay = RESTART_DELAYS[min(failures, len(RESTART_DELAYS) - 1)]
            self._set("disconnected", f"The Google Messages link stopped; restarting in {delay}s.")
            await asyncio.sleep(delay)

    async def _run_once(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            str(self.binary), "-session", str(self.session),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024, creationflags=NO_WINDOW)
        log.info("Sidecar started (pid %s)", self.proc.pid)
        logs = asyncio.create_task(self._pipe_logs(self.proc.stderr))
        watchdog = asyncio.create_task(self._watchdog())
        try:
            while line := await self.proc.stdout.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    await self._handle(event)
        finally:
            watchdog.cancel()
            with contextlib.suppress(ProcessLookupError):
                if self.proc.returncode is None:
                    self.proc.kill()
            await self.proc.wait()
            logs.cancel()
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_result({"error": "the Google Messages link stopped"})
            self._waiting.clear()
            log.info("Sidecar exited (%s)", self.proc.returncode)

    async def _watchdog(self) -> None:
        """A sidecar stuck disconnected gets a fresh start."""
        while True:
            await asyncio.sleep(60)
            if self.state == "disconnected" and time.time() - self.since > DISCONNECTED_RESTART_S:
                log.info("Sidecar disconnected for a while; restarting it")
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                return

    @staticmethod
    async def _pipe_logs(stream) -> None:
        while line := await stream.readline():
            text = line.decode(errors="replace").rstrip()
            if text:
                level = logging.WARNING if (" WRN " in text or " ERR " in text) else logging.DEBUG
                sidecar_log.log(level, "%s", text)

    async def _handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "status":
            self._set(str(event.get("state", "")), str(event.get("detail", "")))
            if self.state != "pairing":
                self.qr = self.emoji = ""
        elif kind == "qr":
            self.qr, self.pair_error = str(event.get("url", "")), ""
        elif kind == "emoji":
            self.emoji, self.pair_error = str(event.get("emoji", "")), ""
        elif kind == "pair_error":
            self.pair_error = str(event.get("error", ""))
            self.qr = self.emoji = ""
        elif kind == "paired":
            self.qr = self.emoji = self.pair_error = ""
        if (req := event.get("req_id")) and (fut := self._waiting.pop(str(req), None)) and not fut.done():
            fut.set_result(event)
        await self.on_event(event)

    def _set(self, state: str, detail: str) -> None:
        if state != self.state:
            self.since = time.time()
            log.info("Link: %s %s", state, detail)
        self.state, self.detail = state, detail

    async def send(self, command: dict) -> bool:
        if self.proc is None or self.proc.returncode is not None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write((json.dumps(command) + "\n").encode())
            await self.proc.stdin.drain()
            return True
        except (ConnectionError, RuntimeError, BrokenPipeError):
            return False

    async def request(self, command: dict, timeout: float = 60) -> dict:
        """Sends a command that gets a reply (send, contacts) and waits for it. Returns the reply event, or
        {"error": ...}."""
        req_id = f"r{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._waiting[req_id] = fut
        if not await self.send(command | {"req_id": req_id}):
            self._waiting.pop(req_id, None)
            return {"error": "the Google Messages link isn't running"}
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"error": "the phone didn't answer in time"}
        finally:
            self._waiting.pop(req_id, None)

    async def running(self, wait_s: float = 20) -> bool:
        """Starts it if needed and waits until it's taking commands."""
        self.start()
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if self.proc is not None and self.proc.returncode is None and self.state not in ("stopped", "building"):
                return True
            if self.state == "build_failed":
                return False
            await asyncio.sleep(0.3)
        return False

    def since_iso(self) -> str:
        return datetime.fromtimestamp(self.since, timezone.utc).isoformat(timespec="seconds")


def backfill_since(last: datetime | None, days: int, now: datetime | None = None) -> int:
    """Unix ms to fetch from: a few minutes before the newest stored message, never more than `days` back."""
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(days=max(days, 0))
    start = floor if last is None else max(floor, last - timedelta(minutes=5))
    return int(start.timestamp() * 1000)
