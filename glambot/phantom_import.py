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
import subprocess
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

# Code values a full-scale header would report, by bit depth. When a .cine says
# black 0 / white one of these it is describing the sensor's nominal range and
# telling us nothing about the decode, so we measure instead.
_FULL_SCALE = {255, 1023, 4095, 16383, 65535}


def _sidecar_is_current(sidecar: Path) -> bool:
    """Whether an existing sidecar already carries everything the renderer wants.

    A sidecar written before the black reference existed is missing the one
    field that stops the render coming out flat, so the backfill has to rewrite
    it rather than skip it. An unreadable sidecar counts as out of date - worst
    case it gets rewritten.
    """
    if not sidecar.exists():
        return False
    try:
        look = json.loads(sidecar.read_text(encoding="utf-8")).get("look")
    except (OSError, ValueError):
        return False
    return isinstance(look, dict) and "levels_source" in look


def _levels_from_header(look: dict) -> tuple[float, float] | None:
    """Black/white levels from the .cine header, as 0..1 fractions.

    Returns None when the header is full-scale, which is the usual case: on a
    VEO 4K it reports 0 / 4095 and normalising to that is an identity op.
    """
    try:
        black = int(look.get("black_level"))
        white = int(look.get("white_level"))
    except (TypeError, ValueError):
        return None
    if white <= black or white <= 0:
        return None
    if black <= 0 and white in _FULL_SCALE:
        return None
    # The header's levels are in its own bit depth - infer the scale from the
    # white level rather than trusting real_bpp, which reports 10 on files whose
    # levels are quoted in 12 bits.
    full = next((f for f in sorted(_FULL_SCALE) if white <= f), None)
    if not full:
        return None
    return black / full, white / full


def _measure_levels(clip: Path) -> tuple[float, float] | None:
    """The darkest and brightest luma actually present, as 0..1 fractions.

    One decoded frame through ffmpeg's `signalstats`. This is what catches the
    fixed pedestal the header hides: measured at 7283-7316 of 65535 across six
    clips from two different days.
    """
    from .processor import _NO_WINDOW_FLAGS, _resolve_ffmpeg
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        logger.warning("no ffmpeg available - %s gets no black reference", clip.name)
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(clip), "-frames:v", "1",
             "-vf", "format=gbrp16le,signalstats,metadata=print:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
            creationflags=_NO_WINDOW_FLAGS)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not measure the black reference of %s: %s", clip.name, exc)
        return None
    stats: dict[str, float] = {}
    for line in proc.stdout.splitlines():
        key, _, val = line.strip().partition("=")
        key = key.rsplit(".", 1)[-1]
        if key in ("YMIN", "YMAX"):
            try:
                stats[key] = float(val)
            except ValueError:
                pass
    if "YMIN" not in stats or "YMAX" not in stats:
        logger.warning("signalstats gave no levels for %s", clip.name)
        return None
    lo, hi = stats["YMIN"] / 65535.0, stats["YMAX"] / 65535.0
    # Don't stretch highlights that are already near clipping - a shoulder pulled
    # up is its own kind of wrong. Only the floor is reliably a fixed offset.
    if hi > 0.95:
        hi = 1.0
    if hi - lo < 0.1:
        return None
    return round(lo, 6), round(hi, 6)


def _clip_levels(clip: Path, look: dict) -> tuple[float | None, float | None, str]:
    """Black/white points for a clip: the header when it says something real,
    otherwise measured from the footage."""
    from_header = _levels_from_header(look)
    if from_header:
        return from_header[0], from_header[1], "header"
    measured = _measure_levels(clip)
    if measured:
        return measured[0], measured[1], "measured"
    return None, None, "none"


_CINE_SUFFIX = ".cine"
_PART_SUFFIX = ".part"
_LOOK_SUFFIX = ".look.json"   # must match effects.LOOK_SUFFIX
_SAVE_TIMEOUT = 1800.0     # ceiling for one transfer; see _await_save
# A whole idle cycle (connect + get_state + disconnect) measures well under
# 100 ms against a VEO 4K, so the record badge's freshness is set by this sleep
# and nothing else. Kept independent of the operator's poll_seconds, which is
# about how often to do the *heavy* reads.
_IDLE_POLL_SECONDS = 2.0
_STALE_PART_AGE = 300.0    # a .part older than this can only be an orphan
_SWEEP_EVERY = 60.0        # how often the idle loop looks for orphaned partials


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
        self._bridge = PhantomBridge(on_event=self._on_bridge_event,
                                     on_disconnect=self._on_bridge_disconnect)
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

        self._owner = "chataigne"          # who may hold TCP 7115 right now
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._camera_info: dict[str, Any] = {}
        self._live_settings: dict[str, Any] = {}
        self._state: dict[str, Any] = {"partitions": [], "record_state": "unknown"}
        self._state_at: float = 0.0            # monotonic stamp of the last camera read
        # Takes already pulled, by camera-reported fingerprint (trigger time +
        # frame count). Keyed by take rather than by partition number: the ring
        # reuses partition numbers every `partition_count` takes, so a
        # partition-keyed set silently stops downloading once it has wrapped.
        self._downloaded: deque[str] = deque(maxlen=200)
        # Fallback for firmware that won't report a fingerprint: a partition
        # number is only "already done" until we next see that slot leave the
        # stored state, which is the transition that precedes a re-record.
        self._downloaded_slots: set[int] = set()
        # Takes the operator stopped mid-download. Same two-tier keying as
        # _downloaded, and for the same reason - without it the auto-poll sees
        # the take still `stored` and restarts the transfer seconds later,
        # which makes the Stop button pointless.
        self._skipped: deque[str] = deque(maxlen=200)
        self._skipped_slots: set[int] = set()
        self._last_take_ids: dict[int, str] = {}   # display only; see _already_downloaded
        self._pending_settings: dict[str, Any] = {}
        self._queue: deque[int | None] = deque()  # partition ints, or None = "scan all stored"
        self._downloads: deque[dict[str, Any]] = deque(maxlen=50)
        self._active_save: dict[str, Any] | None = None
        self._save_events: dict[str, threading.Event] = {}
        self._save_results: dict[str, dict[str, Any]] = {}
        self._cancel_job: str | None = None
        self._last_sweep: float = 0.0
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
            self._sweep_stale_parts(dest)

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
        # An explicit pull is the escape hatch for anything the operator
        # previously stopped - otherwise a cancelled take sits on the camera
        # with no way to fetch it.
        self._skipped.clear()
        self._skipped_slots.clear()
        self._queue.append(None)
        self._wake.set()

    def cancel_active_save(self) -> dict[str, Any]:
        """Abort the transfer in progress and discard the partial file.

        The take stays on the camera, so it can be pulled again later - it is
        still protected by the partition ring until that slot is re-recorded.
        """
        active = self._active_save
        if not active:
            return {"ok": False, "error": "no download in progress"}
        job_id = str(active.get("job_id"))
        self._cancel_job = job_id
        active["cancelling"] = True
        try:
            res = self._bridge.request("cancel_save", {"job_id": job_id}, timeout=20)
        except BridgeError as exc:
            # The worker's wait loop still ends the job, so report and move on.
            logger.warning("cancel_save failed: %s", exc)
            res = {"aborted": False}

        method = "in-band"
        if not res.get("aborted"):
            # The SDK didn't take the hint, so the save thread is still writing.
            # Releasing the waiter now would race the cleanup against a live
            # writer - which is exactly how a 1.17 GB orphan was left behind on
            # 2026-09-08. Stopping the bridge is the one thing guaranteed to end
            # the write and release the file handle; _on_bridge_disconnect frees
            # every waiter and _acquire_port respawns it on the next cycle.
            method = "bridge restart"
            logger.warning("Phantom: in-band cancel of %s not honoured - stopping the "
                           "camera bridge to guarantee the transfer ends", job_id)
            self._bridge.stop()
        else:
            self._save_results.setdefault(job_id, {"error": "cancelled by operator"})
            ev = self._save_events.get(job_id)
            if ev:
                ev.set()
        logger.info("Phantom: cancelled download %s via %s", job_id, method)
        return {"ok": True, "job_id": job_id, "method": method,
                "sdk_stopped": bool(res.get("aborted"))}

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
            "state_age_s": self._state_age(),
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

    def _state_age(self) -> float | None:
        """Seconds since the camera state was last actually read. The page uses
        it to mark a reading stale rather than showing a frozen one as current -
        while show control holds the port, Glambot cannot read the camera at
        all, and a confidently wrong READY badge is worse than a greyed one."""
        if not self._state_at:
            return None
        return round(time.monotonic() - self._state_at, 1)

    def camera_json(self) -> dict[str, Any]:
        return {
            "port_owner": self._owner,
            "camera": self._camera_info,
            "record_state": self._state.get("record_state"),
            "state_age_s": self._state_age(),
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
            if p.get("state") == "stored":
                if self._already_downloaded(p, remembered=True):
                    row["downloaded"] = True
                elif self._is_skipped(p, remembered=True):
                    row["skipped"] = True
            out.append(row)
        return out

    # -- worker loop --------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self._poll_interval())
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._maybe_sweep_parts()
                # Explicit work (take-complete / download-now / settings) grabs
                # the port assertively - it may ask Chataigne to release it.
                if self._queue or self._pending_settings:
                    self._do_cycle(assertive=True)
                # In Auto mode we also poll the camera on our own, but only
                # opportunistically: take the port when it's already free, never
                # interrupt a recording or nag show-control.
                elif self._settings.get("auto_download", True) and self._active_save is None:
                    self._do_cycle(assertive=False)
                # In Manual mode nobody would otherwise read the camera, so the
                # record badge would sit frozen. Take a status-only look.
                elif self._active_save is None:
                    self._status_cycle()
            except Exception:  # noqa: BLE001
                logger.exception("phantom import cycle failed")
                self._last_error = "cycle error - see logs"

    def _poll_interval(self) -> float:
        """How long to sleep between cycles.

        ``poll_seconds`` is the operator's setting for how hard to work the
        camera, but the record badge is only as fresh as this interval, so idle
        cycles run faster than the configured value. During a transfer nothing
        useful happens here anyway.
        """
        configured = max(2.0, float(self._settings.get("poll_seconds", 5) or 5))
        if self._active_save is not None:
            return configured
        return min(configured, _IDLE_POLL_SECONDS)

    def _status_cycle(self) -> None:
        """Refresh the camera state and nothing else, without disturbing show
        control - if the port isn't free right now, try again next tick."""
        if not self._acquire_port(assertive=False):
            return
        try:
            self._refresh_camera_state(full=False)
        finally:
            self._release_port(assertive=False)

    def _maybe_sweep_parts(self) -> None:
        """Clear orphaned partials while idle.

        Sweeping only at start() meant a partial stranded by a failed cleanup
        sat there until the next restart. The mtime check in
        _sweep_stale_parts keeps a live transfer's file safe, and the
        _active_save guard makes that doubly true.
        """
        if self._active_save is not None:
            return
        now = time.monotonic()
        if now - self._last_sweep < _SWEEP_EVERY:
            return
        self._last_sweep = now
        dest = Path(str(self._settings.get("dest_dir", "")).strip())
        if dest.is_dir():
            self._sweep_stale_parts(dest)

    def _do_cycle(self, assertive: bool = True) -> None:
        if not self._acquire_port(assertive=assertive):
            if assertive:
                self._last_error = "could not acquire camera port (Chataigne still holding it?)"
            return
        try:
            if self._pending_settings:
                self._apply_pending_settings()
            self._refresh_camera_state(take_ids=True)

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

            by_n = {int(p["n"]): p for p in self._state.get("partitions", [])
                    if p.get("state") == "stored"}
            # An explicit request names partitions; otherwise take every stored
            # one. Either way the pending filter decides what's actually new.
            candidates = wanted if explicit else sorted(by_n)
            for n in candidates:
                part = by_n.get(n)
                if part is None:
                    continue  # asked for a slot that holds nothing
                if self._already_downloaded(part):
                    continue
                # Only the automatic poll honours a stop; an explicit request is
                # the operator asking for it again, which overrides.
                if not explicit and self._is_skipped(part):
                    continue
                self._download_partition(n, part.get("take_id"))
        finally:
            self._release_port(assertive=assertive)

    def _already_downloaded(self, part: dict[str, Any], remembered: bool = False) -> bool:
        """Whether this stored take has been pulled.

        ``remembered`` lets the *display* fall back to the last fingerprint seen
        for the slot, because the cheap status read doesn't ask for take ids.
        The download decision never does that: acting on a stale fingerprint
        would skip a real take, which is the failure this whole scheme exists
        to prevent, so an unknown id there means "download it".
        """
        take_id = part.get("take_id")
        if take_id is None and remembered:
            take_id = self._last_take_ids.get(int(part["n"]))
        if take_id:
            return str(take_id) in self._downloaded
        return int(part["n"]) in self._downloaded_slots

    def _note_downloaded(self, partition: int, take_id: str | None) -> None:
        if take_id:
            self._downloaded.append(str(take_id))
        else:
            self._downloaded_slots.add(partition)

    def _forget_downloaded(self, partition: int, take_id: str | None) -> None:
        if take_id:
            try:
                self._downloaded.remove(str(take_id))
            except ValueError:
                pass
        else:
            self._downloaded_slots.discard(partition)

    def _is_skipped(self, part: dict[str, Any], remembered: bool = False) -> bool:
        take_id = part.get("take_id")
        if take_id is None and remembered:
            take_id = self._last_take_ids.get(int(part["n"]))
        if take_id:
            return str(take_id) in self._skipped
        return int(part["n"]) in self._skipped_slots

    def _note_skipped(self, partition: int, take_id: str | None) -> None:
        """Don't offer this take to the automatic poll again. Deliberately
        applied even when the SDK abort failed: the operator asked to stop, so
        Glambot must not restart it on their behalf. ``Download now`` clears
        this, which is how a stopped take is still retrievable."""
        if take_id:
            self._skipped.append(str(take_id))
        else:
            self._skipped_slots.add(partition)

    def _download_partition(self, partition: int, take_id: str | None = None) -> None:
        cfg = self._settings
        dest_dir = Path(cfg["dest_dir"])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        serial = self._camera_info.get("serial") or "phantom"
        final = dest_dir / f"{serial}_p{partition}_{stamp}{_CINE_SUFFIX}"
        # The stamp only resolves to the second, so two takes pulled from the
        # same partition inside one second would otherwise silently overwrite.
        dedupe = 1
        while final.exists():
            final = dest_dir / f"{serial}_p{partition}_{stamp}-{dedupe}{_CINE_SUFFIX}"
            dedupe += 1
        tmp = dest_dir / f".{final.stem}{_PART_SUFFIX}"
        job_id = f"p{partition}-{stamp}"
        ev = threading.Event()
        self._save_events[job_id] = ev
        self._active_save = {"partition": partition, "job_id": job_id, "pct": 0,
                             "path": str(final), "cancelling": False}
        rec = {"partition": partition, "name": final.name, "t": time.time(), "ok": False, "error": None}
        started = time.monotonic()
        try:
            started_info = self._bridge.request("save_cine", {
                "partition": partition,
                "dest_path": str(tmp),
                "file_type": cfg.get("file_type", "SVV_RAWCINE"),
                "job_id": job_id,
            }, timeout=30)
            rec["file_type"] = started_info.get("file_type")
            w, h = started_info.get("width"), started_info.get("height")
            if w and h:
                rec["resolution"] = f"{w}x{h}"
            rec["frames"] = started_info.get("frames")
            self._await_save(job_id, ev)
            result = self._save_results.get(job_id, {})
            if result.get("error"):
                raise BridgeError(result["error"])
            os.replace(tmp, final)   # atomic - watcher only ever sees a complete file
            elapsed = max(time.monotonic() - started, 1e-6)
            try:
                size = final.stat().st_size
                rec["size_mb"] = round(size / 1e6, 1)
                rec["mb_per_s"] = round(size / 1e6 / elapsed, 1)
                # Bits per pixel is the one number that identifies the format at
                # a glance: the raw packed cine lands on exactly 10.0, anything
                # processed lands far above it.
                if w and h and rec.get("frames"):
                    rec["bpp"] = round(size * 8 / (w * h * rec["frames"]), 2)
            except OSError:
                pass
            self._write_look_sidecar(final)
            rec["ok"] = True
            self._note_downloaded(partition, take_id)
            self._downloads.appendleft(rec)
            logger.info(
                "Phantom: downloaded partition %s -> %s  (%.0f MB in %.1fs = %s MB/s; "
                "%s %s %sf %s bits/px)",
                partition, final.name, rec.get("size_mb") or 0, elapsed,
                rec.get("mb_per_s", "?"), rec.get("file_type") or "?",
                rec.get("resolution") or "?", rec.get("frames") or "?",
                rec.get("bpp") or "?")
            self._notify_watcher()
            if cfg.get("delete_after_import"):
                try:
                    self._bridge.request("delete_cine", {"partition": partition})
                    self._forget_downloaded(partition, take_id)
                except BridgeError as exc:
                    logger.warning("delete_cine %s failed: %s", partition, exc)
        # OSError matters as much as BridgeError here: os.replace routinely
        # fails on Windows when AV or the indexer holds the new file for a
        # moment, and letting it escape leaves the .part behind and re-pulls the
        # whole multi-GB cine on the next cycle.
        except (BridgeError, OSError) as exc:
            cancelled = self._cancel_job == job_id
            rec["error"] = "cancelled by operator" if cancelled else str(exc)
            rec["cancelled"] = cancelled
            self._downloads.appendleft(rec)
            if cancelled:
                # Suppress the auto-retry, or the next poll restarts the very
                # transfer the operator just stopped.
                self._note_skipped(partition, take_id)
                logger.info("Phantom: download of partition %s cancelled - it will not "
                            "download automatically; use Download now to fetch it", partition)
            else:
                self._last_error = f"partition {partition}: {exc}"
                logger.error("Phantom download failed: %s", exc)
            rec["partial_removed"] = _remove_with_retry(tmp)
        finally:
            self._save_events.pop(job_id, None)
            self._save_results.pop(job_id, None)
            if self._cancel_job == job_id:
                self._cancel_job = None
            self._active_save = None

    def _await_save(self, job_id: str, ev: threading.Event) -> None:
        """Block until the save reports back, but never past the point where a
        report can still arrive.

        The completion event is only ever set by a ``save_done``/``error``
        message from the bridge. If the bridge process dies mid-transfer no such
        message is ever sent, so waiting the full ceiling in one call would park
        the single worker thread - and with it all polling and every later
        download - until it expired. Short waits, re-checking liveness.
        """
        deadline = time.monotonic() + _SAVE_TIMEOUT
        while not ev.wait(timeout=1.0):
            if self._stop.is_set():
                raise BridgeError("shutting down")
            if not self._bridge.running:
                raise BridgeError("camera bridge exited during save")
            if time.monotonic() >= deadline:
                raise BridgeError(f"save timed out after {_SAVE_TIMEOUT / 60:.0f} min")

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
                    start_err = self._bridge.start()
                    if start_err:
                        # A bridge that can no longer start never recovers on
                        # its own, so this must be visible even on the quiet
                        # opportunistic path - otherwise auto-download just
                        # stops with nothing on the page to say why.
                        self._last_error = f"camera bridge: {start_err}"
                        logger.warning("camera bridge failed to start: %s", start_err)
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

    def _refresh_camera_state(self, take_ids: bool = False, full: bool = True) -> None:
        """Read the camera.

        ``full=False`` is the status path: one round-trip for the partition
        states behind the record badge. The settings and camera-info reads cost
        two more and only change when the operator changes them, so the routine
        poll skips them - that is most of the latency between pressing record
        and the page showing it.
        """
        try:
            self._state = self._bridge.request("get_state", {"take_ids": take_ids})
            self._state_at = time.monotonic()
            # A slot that is no longer stored is about to hold a different take,
            # so anything we remembered against its number is spent. Only
            # matters when the camera won't give us a take_id.
            for p in self._state.get("partitions", []):
                n = int(p["n"])
                if p.get("state") != "stored":
                    self._downloaded_slots.discard(n)
                    self._skipped_slots.discard(n)
                    self._last_take_ids.pop(n, None)
                elif p.get("take_id"):
                    self._last_take_ids[n] = str(p["take_id"])
        except BridgeError as exc:
            logger.debug("get_state failed: %s", exc)
        if not full:
            return
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

    def _write_look_sidecar(self, clip: Path) -> bool:
        """Record the camera's colour description beside the clip.

        Most of it - the tone curve above all - never reaches ffprobe, so
        without this the renderer has no way to reproduce the camera's look and
        falls back to the legacy wbgain+gamma approximation.

        The black reference is added here too. It is the one number the header
        does not usefully carry (it reports the sensor's nominal 0..4095, not
        what the decode produces) and the one the renderer cannot afford to
        guess, so it is measured from the footage once, here, rather than on
        every render.
        """
        try:
            res = self._bridge.request("read_look", {"path": str(clip)}, timeout=60)
        except BridgeError as exc:
            logger.warning("could not read the colour profile of %s: %s", clip.name, exc)
            return False
        look = res.get("look")
        if isinstance(look, dict):
            lo, hi, source = _clip_levels(clip, look)
            if lo is not None:
                look["black_floor"], look["white_ceiling"] = lo, hi
                look["levels_source"] = source
        try:
            Path(str(clip.with_suffix("")) + _LOOK_SUFFIX).write_text(
                json.dumps({"clip": clip.name, "look": look}, indent=2),
                encoding="utf-8")
            return True
        except OSError as exc:
            logger.warning("could not write the colour sidecar for %s: %s", clip.name, exc)
            return False

    def backfill_looks(self, folder: Path | None = None) -> dict[str, Any]:
        """Generate colour sidecars for clips already on disk.

        Reading a look needs no camera - the SDK opens the file directly - so
        this works while show control holds the port.
        """
        base = Path(folder) if folder else Path(str(self._settings.get("dest_dir", "")).strip())
        if not base.is_dir():
            return {"ok": False, "error": f"not a folder: {base}"}
        started = self._bridge.running
        if not started:
            err = self._bridge.start()
            if err:
                return {"ok": False, "error": f"camera bridge: {err}"}
        done, skipped, failed = 0, 0, 0
        try:
            for clip in sorted(base.rglob(f"*{_CINE_SUFFIX}")):
                if _sidecar_is_current(Path(str(clip.with_suffix("")) + _LOOK_SUFFIX)):
                    skipped += 1
                    continue
                if self._write_look_sidecar(clip):
                    done += 1
                else:
                    failed += 1
        finally:
            if not started:
                self._bridge.stop()
        logger.info("Phantom colour backfill: %s written, %s already had one, %s failed",
                    done, skipped, failed)
        return {"ok": True, "written": done, "skipped": skipped, "failed": failed}

    @staticmethod
    def _sweep_stale_parts(dest: Path) -> None:
        """Drop partial downloads orphaned by a killed process. Only ones old
        enough that no live transfer could still be writing them."""
        cutoff = time.time() - _STALE_PART_AGE
        for leftover in dest.glob(f".*{_PART_SUFFIX}"):
            try:
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink()
                    logger.info("Phantom: removed orphaned partial %s", leftover.name)
            except OSError as exc:
                logger.warning("could not remove %s: %s", leftover, exc)

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

    def _on_bridge_disconnect(self, reason: str) -> None:
        """The bridge process is gone. Anything waiting on a save event from it
        will otherwise wait for the full ceiling, which parks the worker thread
        and stops auto-download until Glambot is restarted."""
        for job_id, ev in list(self._save_events.items()):
            self._save_results.setdefault(job_id, {"error": reason})
            ev.set()
        if self._save_events:
            logger.error("camera bridge went away mid-save (%s)", reason)


def _remove_with_retry(path: Path, attempts: int = 6, delay: float = 1.0) -> bool:
    """Delete a partial download, retrying while the writer lets go of it.

    A single immediate attempt is not enough: this runs the moment the transfer
    ends, and on Windows the SDK's (or a dying bridge process's) handle can
    outlive that by a second or two, so the unlink fails with a sharing
    violation and the partial is stranded until the next restart.
    """
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            if attempt == attempts - 1:
                logger.warning("could not remove partial download %s: %s "
                               "(it will be swept once it goes stale)", path, exc)
                return False
            time.sleep(delay)
    return False


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if str(v)]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
