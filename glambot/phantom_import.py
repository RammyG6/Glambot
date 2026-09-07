"""Built-in Phantom camera importer - pulls ``.cine`` takes off a VEO into a
watched folder, the pull-side counterpart to ``ftp_import.py``.

Phantom cameras don't push over FTP: a take lives in volatile camera RAM until
a client copies it out over the SDK. A new record/trigger wipes that RAM, so a
save has to be protected. Two mechanisms, both configured here:

* **Partition ring.** The camera is set to N partitions (``partition_count``,
  default 4). Each take records into the next free partition, so a download of
  partition P never races a recording into partition P+1.
* **Cooperative port ownership.** The camera's PH16 control server (TCP 7115)
  allows one client at a time. Chataigne holds it while arming/recording; after
  a take it calls ``POST /phantom-import/take-complete`` and drops the socket.
  Glambot then grabs the port, downloads the stored partition(s), and hands the
  port back. ``/phantom-import/port-owner`` tells Chataigne whose turn it is.

The SDK connection itself runs in a Python 3.11 subprocess - see
``phantom_bridge_client`` and ``camera_bridge/``.

Settings are UI-managed and persisted to ``inbox_dir/.glambot/phantom_import.json``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from .phantom_bridge_client import BridgeError, PhantomBridge

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "camera_ip": "172.16.37.56",
    "camera_serial": "",
    "partition_count": 4,          # operator-editable; the partition ring depth
    "quick_settings": [],          # live-camera field names to surface in the quick-settings card
    "file_type": "SVV_RAWCINE",    # raw packed cine - matches effects.build_cine_source_filter
    "dest_dir": r"D:\GlambotAuto_Import",
    "auto_download": True,
    "delete_after_import": False,
    "handoff_mode": "notify",      # "notify" (call handoff_notify_url) | "idle" (just grab)
    "handoff_notify_url": "",      # Chataigne/show-control endpoint: POST {"action":"release"|"resume"}
    "poll_seconds": 5,
}

_CINE_SUFFIX = ".cine"


def _settings_path(inbox_dir: Path) -> Path:
    return Path(inbox_dir) / ".glambot" / "phantom_import.json"


def load_phantom_settings(inbox_dir: Path) -> dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    path = _settings_path(inbox_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read %s - using defaults", path)
    try:
        merged["partition_count"] = max(1, int(merged["partition_count"]))
    except (TypeError, ValueError):
        merged["partition_count"] = 4
    merged["quick_settings"] = _as_str_list(merged.get("quick_settings"))
    return merged


def save_phantom_settings(inbox_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    current = load_phantom_settings(inbox_dir)
    current.update({k: v for k, v in updates.items() if k in DEFAULT_SETTINGS})
    try:
        current["partition_count"] = max(1, int(current["partition_count"]))
    except (TypeError, ValueError):
        current["partition_count"] = 4
    current["quick_settings"] = _as_str_list(current.get("quick_settings"))
    path = _settings_path(inbox_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


class PhantomImportServer:
    """Owns the camera bridge subprocess and the port-handoff state machine.

    Duck-types the surface Glambot expects from ``FtpImportServer``:
    ``start`` / ``stop`` / ``restart`` / ``running`` / ``status``.
    """

    def __init__(self, inbox_dir: Path, watcher: Any = None):
        self.inbox_dir = Path(inbox_dir)
        self.watcher = watcher
        self._bridge = PhantomBridge(on_event=self._on_bridge_event)
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

        self._owner = "chataigne"          # who may hold TCP 7115 right now
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._camera_info: dict[str, Any] = {}
        self._live_settings: dict[str, Any] = {}
        self._state: dict[str, Any] = {"partitions": [], "record_state": "unknown"}
        self._downloaded: set[int] = set()     # partitions already pulled this session
        self._pending_settings: dict[str, Any] = {}
        self._queue: deque[int | None] = deque()  # partition ints, or None = "scan all stored"
        self._downloads: deque[dict[str, Any]] = deque(maxlen=50)
        self._active_save: dict[str, Any] | None = None
        self._save_events: dict[str, threading.Event] = {}
        self._save_results: dict[str, dict[str, Any]] = {}
        self._last_error: str | None = None

    # -- lifecycle -----------------------------------------------------

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start(self, settings: dict[str, Any] | None = None) -> str | None:
        with self._lock:
            if self.running:
                return None
            cfg = settings or load_phantom_settings(self.inbox_dir)
            self._settings = cfg
            dest = Path(str(cfg.get("dest_dir", "")).strip())
            if not str(dest):
                return "No download folder set."
            if not dest.is_dir():
                return f"Download folder does not exist: {dest}"

            err = self._bridge.start()
            if err:
                return err
            try:
                conn = self._bridge.request("connect", {
                    "serial": _int_or_none(cfg.get("camera_serial")),
                    "ip": str(cfg.get("camera_ip", "")).strip(),
                }, timeout=25)
            except BridgeError as exc:
                self._bridge.stop()
                return f"Could not connect to the camera: {exc}"
            self._camera_info = conn
            try:
                self._bridge.request("set_partitions", {"count": int(cfg["partition_count"])})
            except BridgeError as exc:
                logger.warning("set_partitions failed: %s", exc)
            self._refresh_camera_state()
            # Glambot is holding the port right now (bridge is connected); hand
            # it back to Chataigne and let the worker re-acquire per cycle.
            self._release_port()

            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="phantom-import", daemon=True)
            self._worker.start()
            logger.info("Phantom import started (camera %s, %s partitions)",
                        self._camera_info.get("ip"), cfg["partition_count"])
            return None

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._wake.set()
            worker = self._worker
            self._worker = None
        if worker is not None:
            worker.join(timeout=10)
        self._bridge.stop()
        self._owner = "chataigne"
        logger.info("Phantom import stopped")

    def restart(self, settings: dict[str, Any] | None = None) -> str | None:
        self.stop()
        return self.start(settings)

    # -- external triggers ------------------------------------------

    def note_take_complete(self, partition: int | None) -> None:
        """Chataigne calls this after ``trig`` + ``STR``. Enqueues a download
        when auto-download is on; otherwise it's a no-op (use Download now)."""
        if not self._settings.get("auto_download", True):
            return
        self._queue.append(_int_or_none(partition))
        self._wake.set()

    def download_now(self) -> None:
        self._queue.append(None)
        self._wake.set()

    def queue_settings(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Store live-camera changes to apply in the next Glambot-owned window.

        If Glambot already holds the port, apply immediately.
        """
        self._pending_settings.update(fields)
        if self._owner == "glambot" and self._bridge.running:
            return self._apply_pending_settings()
        self._wake.set()
        return {"queued": list(fields)}

    def set_auto_download(self, auto: bool) -> None:
        """Flip auto vs. manual download on a running server without a restart."""
        self._settings["auto_download"] = bool(auto)

    def set_quick_settings(self, fields: list[str]) -> None:
        """Update which live-camera fields the quick-settings card shows."""
        self._settings["quick_settings"] = _as_str_list(fields)

    # -- status ---------------------------------------------------

    @property
    def port_owner(self) -> str:
        return self._owner

    def status(self) -> dict[str, Any]:
        cfg = self._settings if self.running else load_phantom_settings(self.inbox_dir)
        return {
            "running": self.running,
            "bridge_running": self._bridge.running,
            "port_owner": self._owner,
            "camera": self._camera_info,
            "record_state": self._state.get("record_state"),
            "partitions": self._mark_partitions(),
            "live_settings": self._live_settings,
            "pending_settings": dict(self._pending_settings),
            "active_save": self._active_save,
            "downloads": list(self._downloads),
            "last_error": self._last_error,
            "auto_download": bool(cfg.get("auto_download", True)),
            "quick_settings": _as_str_list(cfg.get("quick_settings")),
            "settings": {k: cfg.get(k) for k in DEFAULT_SETTINGS if k != "camera_serial" or cfg.get(k)},
        }

    def camera_json(self) -> dict[str, Any]:
        return {
            "port_owner": self._owner,
            "camera": self._camera_info,
            "record_state": self._state.get("record_state"),
            "partitions": self._mark_partitions(),
            "live_settings": self._live_settings,
            "pending_settings": dict(self._pending_settings),
            "active_save": self._active_save,
            "quick_settings": _as_str_list(self._settings.get("quick_settings")),
        }

    def _mark_partitions(self) -> list[dict[str, Any]]:
        out = []
        for p in self._state.get("partitions", []):
            row = dict(p)
            if int(p.get("n", -1)) in self._downloaded:
                row["downloaded"] = True
            out.append(row)
        return out

    # -- worker loop --------------------------------------------

    def _run(self) -> None:
        poll = max(2, int(self._settings.get("poll_seconds", 5)))
        while not self._stop.is_set():
            self._wake.wait(timeout=poll)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                # Explicit work (take-complete / download-now / settings) grabs
                # the port assertively - it may ask Chataigne to release it.
                if self._queue or self._pending_settings:
                    self._do_cycle(assertive=True)
                # In Auto mode we also poll the camera on our own, but only
                # opportunistically: take the port when it's already free, never
                # interrupt a recording or nag show-control.
                elif self._settings.get("auto_download", True) and self._active_save is None:
                    self._do_cycle(assertive=False)
                else:
                    continue
            except Exception:  # noqa: BLE001
                logger.exception("phantom import cycle failed")
                self._last_error = "cycle error - see logs"

    def _do_cycle(self, assertive: bool = True) -> None:
        if not self._acquire_port(assertive=assertive):
            if assertive:
                self._last_error = "could not acquire camera port (Chataigne still holding it?)"
            return
        try:
            if self._pending_settings:
                self._apply_pending_settings()
            self._refresh_camera_state()

            if not assertive and self._state.get("record_state") == "recording":
                return  # don't pull mid-record; the finally still releases the port

            wanted: list[int] = []
            explicit = False
            while self._queue:
                item = self._queue.popleft()
                if item is None:
                    continue
                explicit = True
                wanted.append(int(item))
            stored = [int(p["n"]) for p in self._state.get("partitions", [])
                      if p.get("state") == "stored" and int(p["n"]) not in self._downloaded]
            targets = [n for n in (wanted or stored) if n not in self._downloaded]
            if explicit:
                targets = [n for n in targets if n in wanted or n in stored]
            for n in targets:
                self._download_partition(n)
        finally:
            self._release_port(assertive=assertive)

    def _download_partition(self, partition: int) -> None:
        cfg = self._settings
        dest_dir = Path(cfg["dest_dir"])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        serial = self._camera_info.get("serial") or "phantom"
        final = dest_dir / f"{serial}_p{partition}_{stamp}{_CINE_SUFFIX}"
        tmp = dest_dir / f".{final.stem}.part"
        job_id = f"p{partition}-{stamp}"
        ev = threading.Event()
        self._save_events[job_id] = ev
        self._active_save = {"partition": partition, "job_id": job_id, "pct": 0, "path": str(final)}
        rec = {"partition": partition, "name": final.name, "t": time.time(), "ok": False, "error": None}
        started = time.monotonic()
        try:
            self._bridge.request("save_cine", {
                "partition": partition,
                "dest_path": str(tmp),
                "file_type": cfg.get("file_type", "SVV_RAWCINE"),
                "job_id": job_id,
            }, timeout=30)
            # Wait for the save_done / error event (large cines take a while).
            if not ev.wait(timeout=1800):
                raise BridgeError("save timed out after 30 min")
            result = self._save_results.get(job_id, {})
            if result.get("error"):
                raise BridgeError(result["error"])
            os.replace(tmp, final)   # atomic - watcher only ever sees a complete file
            elapsed = max(time.monotonic() - started, 1e-6)
            try:
                size = final.stat().st_size
                rec["size_mb"] = round(size / 1e6, 1)
                rec["mb_per_s"] = round(size / 1e6 / elapsed, 1)
            except OSError:
                pass
            rec["ok"] = True
            self._downloaded.add(partition)
            self._downloads.appendleft(rec)
            logger.info("Phantom: downloaded partition %s -> %s  (%.0f MB in %.1fs = %s MB/s)",
                        partition, final.name, rec.get("size_mb") or 0, elapsed,
                        rec.get("mb_per_s", "?"))
            self._notify_watcher()
            if cfg.get("delete_after_import"):
                try:
                    self._bridge.request("delete_cine", {"partition": partition})
                    self._downloaded.discard(partition)
                except BridgeError as exc:
                    logger.warning("delete_cine %s failed: %s", partition, exc)
        except BridgeError as exc:
            rec["error"] = str(exc)
            self._downloads.appendleft(rec)
            self._last_error = f"partition {partition}: {exc}"
            logger.error("Phantom download failed: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            self._save_events.pop(job_id, None)
            self._save_results.pop(job_id, None)
            self._active_save = None

    # -- port handoff ------------------------------------------

    def _acquire_port(self, assertive: bool = True) -> bool:
        if self._owner == "glambot" and self._bridge.running:
            return True
        cfg = self._settings
        # Only an assertive grab asks show-control to drop the socket. An
        # opportunistic scan takes the port only if it's already free.
        if assertive and cfg.get("handoff_mode") == "notify":
            self._handoff_notify("release")
        attempts = 6 if assertive else 1
        for attempt in range(attempts):
            try:
                if not self._bridge.running:
                    if self._bridge.start():
                        if assertive:
                            time.sleep(1.0)
                            continue
                        return False
                conn = self._bridge.request("connect", {
                    "serial": _int_or_none(cfg.get("camera_serial")),
                    "ip": str(cfg.get("camera_ip", "")).strip(),
                }, timeout=15 if assertive else 8)
                self._camera_info = conn
                self._owner = "glambot"
                return True
            except BridgeError as exc:
                logger.info("acquire port attempt %s: %s", attempt + 1, exc)
                if assertive:
                    time.sleep(1.5)
        return False

    def _release_port(self, assertive: bool = True) -> None:
        try:
            if self._bridge.running:
                self._bridge.request("disconnect", timeout=10)
        except BridgeError:
            pass
        self._owner = "chataigne"
        # Mirror _acquire_port: only resume show-control if we told it to release.
        if assertive and self._settings.get("handoff_mode") == "notify":
            self._handoff_notify("resume")

    def _handoff_notify(self, action: str) -> None:
        url = str(self._settings.get("handoff_notify_url", "")).strip()
        if not url:
            return
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"action": action}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=5).close()
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("handoff notify (%s) failed: %s", action, exc)
        if action == "release":
            time.sleep(1.0)  # give show control a beat to drop the socket

    # -- camera reads -----------------------------------------

    def _refresh_camera_state(self) -> None:
        try:
            self._state = self._bridge.request("get_state")
        except BridgeError as exc:
            logger.debug("get_state failed: %s", exc)
        try:
            self._live_settings = self._bridge.request("get_settings").get("fields", {})
        except BridgeError as exc:
            logger.debug("get_settings failed: %s", exc)
        try:
            self._camera_info.update(self._bridge.request("get_camera_info"))
        except BridgeError:
            pass

    def _apply_pending_settings(self) -> dict[str, Any]:
        fields, self._pending_settings = dict(self._pending_settings), {}
        if not fields:
            return {"applied": {}, "errors": {}}
        try:
            res = self._bridge.request("set_settings", {"fields": fields})
        except BridgeError as exc:
            self._pending_settings.update(fields)  # keep for retry
            return {"applied": {}, "errors": {"_": str(exc)}}
        if "partition_count" in (res.get("applied") or {}):
            save_phantom_settings(self.inbox_dir, {"partition_count": res["applied"]["partition_count"]})
        self._refresh_camera_state()
        return res

    def _notify_watcher(self) -> None:
        if self.watcher is not None:
            try:
                self.watcher.rescan_now()
            except Exception:
                logger.exception("watcher.rescan_now() after phantom download failed")

    # -- bridge events --------------------------------------

    def _on_bridge_event(self, msg: dict[str, Any]) -> None:
        kind = msg.get("event")
        if kind == "save_progress":
            if self._active_save and self._active_save.get("job_id") == msg.get("job_id"):
                self._active_save["pct"] = int(msg.get("pct", 0))
        elif kind == "save_done":
            job_id = str(msg.get("job_id"))
            self._save_results[job_id] = {"path": msg.get("path")}
            ev = self._save_events.get(job_id)
            if ev:
                ev.set()
        elif kind == "error":
            job_id = str(msg.get("job_id") or "")
            if job_id:
                self._save_results[job_id] = {"error": msg.get("error")}
                ev = self._save_events.get(job_id)
                if ev:
                    ev.set()
            self._last_error = str(msg.get("error"))


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if str(v)]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
