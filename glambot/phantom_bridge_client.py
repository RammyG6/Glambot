"""Talk to the Python 3.11 ``camera_bridge`` subprocess from Glambot (3.12).

The bridge owns the single real connection to a Phantom camera's PH16 control
server. This module manages that child process and exposes a small blocking
request/response API plus an event callback for async save progress.

See ``camera_bridge/bridge.py`` for the wire protocol.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Matches app.py: a frozen build extracts next to the exe, not as a package.
_REPO_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
_BRIDGE_DIR = _REPO_ROOT / "camera_bridge"
_BRIDGE_SCRIPT = _BRIDGE_DIR / "bridge.py"


class BridgeError(RuntimeError):
    """A command the bridge rejected, or a transport failure."""


def find_python311() -> str | None:
    """Locate an interpreter that can import pyphantom (Python 3.11)."""
    override = os.environ.get("GLAMBOT_PY311", "").strip()
    if override and Path(override).exists():
        return override
    for candidate in (
        _BRIDGE_DIR / "runtime" / "python.exe",
        _BRIDGE_DIR / "runtime" / "Scripts" / "python.exe",
        _BRIDGE_DIR / "runtime" / "bin" / "python3.11",
    ):
        if candidate.exists():
            return str(candidate)
    for name in ("python3.11", "python3.11.exe"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt" and shutil.which("py"):
        try:
            out = subprocess.run(
                ["py", "-3.11", "-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class PhantomBridge:
    """A managed camera-bridge subprocess.

    Not thread-safe for concurrent ``request`` calls - callers should serialise
    (the PhantomImportServer state machine does).
    """

    def __init__(self, on_event: Callable[[dict[str, Any]], None] | None = None):
        self._proc: subprocess.Popen[str] | None = None
        self._on_event = on_event
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._replies: dict[int, "threading.Event"] = {}
        self._results: dict[int, dict[str, Any]] = {}
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._ready_info: dict[str, Any] = {}
        self._start_error: str | None = None

    # -- lifecycle --------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> str | None:
        """Spawn the bridge. Returns None on success, else an error string."""
        if self.running:
            return None
        if not _BRIDGE_SCRIPT.exists():
            return f"camera bridge script missing: {_BRIDGE_SCRIPT}"
        py = find_python311()
        if py is None:
            return (
                "No Python 3.11 found for the camera bridge. Set GLAMBOT_PY311, "
                "or create camera_bridge/runtime (see camera_bridge/README.md)."
            )
        try:
            self._proc = subprocess.Popen(
                [py, "-u", str(_BRIDGE_SCRIPT)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, cwd=str(_REPO_ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as exc:
            self._proc = None
            return f"could not start camera bridge: {exc}"

        self._ready.clear()
        self._reader = threading.Thread(target=self._read_loop, name="phantom-bridge", daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, name="phantom-bridge-err", daemon=True).start()

        if not self._ready.wait(timeout=20):
            self.stop()
            return "camera bridge did not report ready within 20s"
        if not self._ready_info.get("pyphantom", False):
            err = self._ready_info.get("import_error") or "pyphantom import failed"
            self.stop()
            return f"camera bridge runtime cannot load pyphantom: {err}"
        return None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(json.dumps({"id": 0, "cmd": "shutdown"}) + "\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Fail any in-flight waiters.
        with self._pending_lock:
            for rid, ev in list(self._replies.items()):
                self._results[rid] = {"ok": False, "error": "bridge stopped"}
                ev.set()

    # -- requests --------------------------------------------------

    def request(self, cmd: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        if not self.running:
            raise BridgeError("camera bridge is not running")
        with self._pending_lock:
            rid = self._next_id
            self._next_id += 1
            ev = threading.Event()
            self._replies[rid] = ev
        payload = json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n"
        try:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except (OSError, ValueError, AssertionError) as exc:
            raise BridgeError(f"failed to send {cmd}: {exc}") from exc
        if not ev.wait(timeout=timeout):
            with self._pending_lock:
                self._replies.pop(rid, None)
            raise BridgeError(f"timed out waiting for {cmd} after {timeout:.0f}s")
        with self._pending_lock:
            res = self._results.pop(rid, {"ok": False, "error": "no result"})
            self._replies.pop(rid, None)
        if not res.get("ok"):
            raise BridgeError(res.get("error") or f"{cmd} failed")
        return res.get("result") or {}

    # -- internals ------------------------------------------------

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("bridge non-JSON line: %s", line[:200])
                continue
            if "event" in msg:
                self._handle_event(msg)
                continue
            rid = msg.get("id")
            if rid is None:
                continue
            with self._pending_lock:
                self._results[rid] = msg
                ev = self._replies.get(rid)
            if ev is not None:
                ev.set()
        logger.info("camera bridge stdout closed")

    def _handle_event(self, msg: dict[str, Any]) -> None:
        kind = msg.get("event")
        if kind == "ready":
            self._ready_info = msg
            self._ready.set()
            return
        if kind == "log":
            logger.log(
                getattr(logging, str(msg.get("level", "info")).upper(), logging.INFO),
                "[bridge] %s", msg.get("msg"),
            )
            return
        if self._on_event is not None:
            try:
                self._on_event(msg)
            except Exception:
                logger.exception("phantom bridge event handler failed")

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            if line.strip():
                logger.debug("[bridge stderr] %s", line.rstrip())
