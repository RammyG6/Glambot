"""Phantom camera bridge - runs under an embedded CPython 3.11.

Glambot's main app runs on Python 3.12, but the official ``pyphantom`` wheel
ships a ``PhPy.pyd`` linked against ``python311.dll`` and cannot be imported
there. This script is the other side of that gap: a long-lived child process,
spoken to over stdin/stdout with newline-delimited JSON, that owns the one
real connection to the camera's PH16 control server (TCP 7115).

Protocol
--------
Request  (one JSON object per line on stdin):
    {"id": <int>, "cmd": "<name>", "args": {...}}
Response (one JSON object per line on stdout):
    {"id": <int>, "ok": true, "result": {...}}
    {"id": <int>, "ok": false, "error": "<message>"}
Event    (unsolicited, no "id"):
    {"event": "save_progress", "job_id": "...", "pct": 42}
    {"event": "save_done", "job_id": "...", "path": "..."}
    {"event": "ready"}            emitted once at startup
    {"event": "log", "level": "info", "msg": "..."}

Every line on stdout is one JSON object. Anything the SDK prints to the real
stdout is redirected to stderr so it can't corrupt the stream.

Commands
--------
discover                      -> {"cameras": [{"name","serial","model","cn"}]}
connect {serial?|ip?}         -> {"cn", "serial", "ip", "model"}
disconnect                    -> {}
get_camera_info               -> {"ip","model","serial","firmware","partition_count", ...}
get_state {take_ids?}         -> {"partitions": [{"n","state","take_id"?}], "record_state": "..."}
get_settings                  -> {"fields": {name: {"value","writable","choices"?,"min"?,"max"?}}}
set_settings {fields:{...}}   -> {"applied": {...}, "errors": {name: "msg"}}
set_partitions {count}        -> {"partition_count": N}
save_cine {partition, dest_path, file_type?, first?, last?, job_id?}
                             -> {"job_id"}  (completion via save_done event)
cancel_save {job_id}         -> {"stopped"}  (aborts an in-flight save)
save_progress {job_id}       -> {"pct"}
save_nvm {partition}         -> {}
delete_cine {partition}      -> {}
shutdown                     -> process exits
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import traceback
from typing import Any

# The SDK and pyphantom both like to print. Keep stdout pristine for JSON.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

try:  # pyphantom is only importable under the bundled 3.11 runtime
    import pyphantom
    from pyphantom import Phantom, Camera, Cine, utils
    _IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - depends on runtime packaging
    pyphantom = None
    _IMPORT_ERROR = f"{exc.__class__.__name__}: {exc}"

# UC_VIEW=1 / UC_SAVE=2 (PhFile.h). A cine handle defaults to UC_VIEW, whose read
# pipeline is tuned for interactive playback rather than a bulk camera->disk copy.
# PCC calls PhSetUseCase(hC, UC_SAVE) before writing; pyphantom has no wrapper for
# it, so we poke PhFile.Dll directly. Measured worth ~3% (528 -> 542 MB/s), not
# the large win it was once assumed to be - see camera_bridge/README.md.
UC_SAVE = 2

# Cine info selectors (GCI_* in PhCon.h, mirrored in pyphantom.utils).
GCI_TRIGTIMESEC = 10        # trigger time, whole seconds
GCI_TRIGTIMEFR = 11         # trigger time, fractions
GCI_TOTALIMAGECOUNT = 30    # frames actually recorded
GCI_WRITEERR = 109          # last error from a save on this cine

_PHFILE: Any = None
_PHFILE_TRIED = False


def _phfile() -> Any:
    """Lazily load PhFile.Dll (the one pyphantom already bundles). Returns None
    if it can't be loaded - callers must treat every entry point here as
    best-effort; pyphantom wraps none of them."""
    global _PHFILE, _PHFILE_TRIED
    if _PHFILE_TRIED:
        return _PHFILE
    _PHFILE_TRIED = True
    if pyphantom is None:
        return None
    try:
        dll_dir = os.path.join(os.path.dirname(pyphantom.__file__), "data")
        try:
            os.add_dll_directory(dll_dir)  # let dependent Ph*.Dll resolve
        except (AttributeError, OSError):
            pass
        lib = ctypes.WinDLL(os.path.join(dll_dir, "PhFile.Dll"))
        lib.PhSetUseCase.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.PhSetUseCase.restype = ctypes.c_int
        lib.PhGetUseCase.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.PhGetUseCase.restype = ctypes.c_int
        # Aborts an in-flight SaveNonBlocking. Optional: older SDK builds may
        # not export it, in which case a download simply can't be cancelled.
        try:
            lib.PhStopWriteCineFileAsync.argtypes = [ctypes.c_void_p]
            lib.PhStopWriteCineFileAsync.restype = ctypes.c_int
        except AttributeError:
            _log("PhFile.Dll has no PhStopWriteCineFileAsync - "
                 "downloads cannot be cancelled", "warning")
        _PHFILE = lib
    except Exception as exc:  # noqa: BLE001
        _log(f"could not load PhFile.Dll: {exc}", "warning")
        _PHFILE = None
    return _PHFILE


def _set_save_use_case(cine_handle: Any) -> bool:
    """Put the cine handle on the bulk camera->disk pipeline. Returns whether the
    readback confirms it - a silent no-op here is the difference between PCC's
    throughput and a fraction of it, so the caller logs the answer either way."""
    lib = _phfile()
    if lib is None:
        return False
    try:
        h = ctypes.c_void_p(int(cine_handle))
        hres = lib.PhSetUseCase(h, UC_SAVE)
        cur = ctypes.c_int(-1)
        lib.PhGetUseCase(h, ctypes.byref(cur))
        ok = hres == 0 and cur.value == UC_SAVE
        _log(f"PhSetUseCase(UC_SAVE) hres={hres} use_case_now={cur.value}"
             + ("" if ok else "  <-- NOT applied; transfer will run at UC_VIEW speed"),
             "info" if ok else "warning")
        return ok
    except Exception as exc:  # noqa: BLE001
        _log(f"PhSetUseCase failed: {exc}", "warning")
        return False


_out_lock = threading.Lock()


def _emit(obj: dict[str, Any]) -> None:
    line = json.dumps(obj, default=str)
    with _out_lock:
        _REAL_STDOUT.write(line + "\n")
        _REAL_STDOUT.flush()


def _event(event: str, **kw: Any) -> None:
    _emit({"event": event, **kw})


def _log(msg: str, level: str = "info") -> None:
    _event("log", level=level, msg=msg)


class Bridge:
    def __init__(self) -> None:
        self._ph: Any = None
        self._cam: Any = None
        self._cn: int | None = None
        self._saves: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------

    def _require_pyphantom(self) -> None:
        if pyphantom is None:
            raise RuntimeError(
                f"pyphantom is not available in this runtime ({_IMPORT_ERROR}). "
                "The camera bridge needs the bundled Python 3.11 + pyphantom wheel."
            )

    def _phantom(self) -> Any:
        if self._ph is None:
            self._require_pyphantom()
            self._ph = Phantom()
        return self._ph

    def _require_cam(self) -> Any:
        if self._cam is None:
            raise RuntimeError("Not connected to a camera - call connect first.")
        return self._cam

    @staticmethod
    def _file_type(name: str | None) -> Any:
        """Map a friendly name to utils.FileTypeEnum (default raw packed cine)."""
        if not name:
            return utils.FileTypeEnum.SVV_RAWCINE
        try:
            return utils.FileTypeEnum[name]
        except KeyError:
            raise RuntimeError(f"Unknown file_type {name!r}")

    # -- commands ---------------------------------------------------

    def discover(self, _args: dict) -> dict:
        ph = self._phantom()
        cams = []
        for entry in ph.discover(print_list=False):
            cams.append({
                "name": getattr(entry, "name", None),
                "serial": getattr(entry, "serial", None),
                "model": getattr(entry, "model", None),
                "cn": _cn(entry),
            })
        return {"cameras": cams}

    def connect(self, args: dict) -> dict:
        ph = self._phantom()
        want_serial = args.get("serial")
        want_ip = str(args.get("ip") or "").strip()
        entries = list(ph.discover(print_list=False))
        chosen = None
        for entry in entries:
            if want_serial is not None and int(getattr(entry, "serial", -1)) == int(want_serial):
                chosen = entry
                break
        if chosen is None and want_ip:
            for entry in entries:
                cam = ph.Camera(_cn(entry))
                if str(getattr(cam, "ip_address", "")).strip() == want_ip:
                    chosen = entry
                    break
        if chosen is None and not want_serial and not want_ip and entries:
            chosen = entries[0]
        if chosen is None:
            raise RuntimeError("Camera not found on the network (checked serial and IP).")
        cn = _cn(chosen)
        self._cam = ph.Camera(cn)
        self._cn = cn
        return {
            "cn": cn,
            "serial": getattr(chosen, "serial", None),
            "ip": getattr(self._cam, "ip_address", None),
            "model": getattr(chosen, "model", None),
        }

    def disconnect(self, _args: dict) -> dict:
        cam = self._cam
        self._cam = None
        self._cn = None
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass
        return {}

    def get_camera_info(self, _args: dict) -> dict:
        cam = self._require_cam()
        # Selectors from PhCon.h: 1097 gsEthernetAdapterName, 1098 gsEthernetLinkSpeed,
        # 1093 gsEthernet10GAddress. If the 10G address is blank, or the control IP
        # and the SDK's connection are on the 1G NIC, downloads cap ~940 Mbps.
        return {
            "ip": _safe(lambda: cam.ip_address),
            "model": _safe(lambda: cam.model),
            "serial": _safe(lambda: cam.serial),
            "firmware": _safe(lambda: cam.hardware_version),
            "partition_count": _safe(lambda: cam.partition_count),
            "has_10g": _safe(lambda: bool(cam.has_10g)),
            "adapter_name": _safe(lambda: cam.get_selector_string(1097)),
            "link_speed": _safe(lambda: cam.get_selector_string(1098))
            or _safe(lambda: cam.get_selector_int(1098)),
            "ethernet_10g_ip": _safe(lambda: cam.get_selector_string(1093)),
        }

    def get_state(self, args: dict) -> dict:
        """Partition states, and optionally a fingerprint of each stored take.

        ``take_ids`` is opt-in because it costs one cine handle per stored
        partition. The importer asks for them only when it is about to decide
        what to download; the status poll that drives the UI badge doesn't.
        """
        cam = self._require_cam()
        want_ids = bool(args.get("take_ids"))
        parts = []
        record_state = "unknown"
        try:
            states = cam.get_partition_state(-1)  # [(cine_nr, PartitionStateEnum)]
            for cine_nr, st in states:
                n = int(cine_nr)
                name = getattr(st, "name", str(st)).lower()
                row: dict[str, Any] = {"n": n, "state": name}
                if want_ids and name == "stored":
                    row["take_id"] = self._take_id(n)
                parts.append(row)
                if name == "recording":
                    record_state = "recording"
            if record_state != "recording":
                record_state = "stored" if any(p["state"] == "stored" for p in parts) else "ready"
        except Exception as exc:
            _log(f"get_state failed: {exc}", "warning")
        return {"partitions": parts, "record_state": record_state}

    def _take_id(self, partition: int) -> str | None:
        """Fingerprint the take currently stored in ``partition``.

        Partition numbers are reused - the ring wraps every ``partition_count``
        takes - so the slot number alone cannot distinguish a take we already
        downloaded from a fresh one recorded into the same slot. The trigger
        time can. Deliberately *not* cached: Glambot can't watch the slot while
        show control holds the port, so a cached value could easily outlive the
        take it describes, which is the exact bug this exists to prevent.
        """
        cine = None
        try:
            cine = Cine.from_camera(self._cam, partition)
            # pyphantom hands these back signed, so mask to the UINT the SDK
            # actually returned - only cosmetic, but this string is the take's
            # identity and shows up in logs.
            sec = int(cine.get_selector_uint(GCI_TRIGTIMESEC)) & 0xFFFFFFFF
            frac = int(cine.get_selector_uint(GCI_TRIGTIMEFR)) & 0xFFFFFFFF
            count = _safe(lambda: cine.get_selector_uint(GCI_TOTALIMAGECOUNT))
            return f"{sec}.{frac}.{int(count or 0) & 0xFFFFFFFF}"
        except Exception as exc:  # noqa: BLE001
            # Older firmware may not answer these selectors. The importer falls
            # back to watching slot state transitions.
            _log(f"take_id for partition {partition} unavailable: {exc}", "debug")
            return None
        finally:
            if cine is not None:
                try:
                    cine.close()
                except Exception:
                    pass

    # --- settings ------------------------------------------------

    _LIVE_FIELDS = {
        # name: (getter, setter or None, kind)
        "partition_count": ("partition_count", "partition_count", "int"),
        "exp_index": ("exp_index", "exp_index", "int"),
        "frame_rate": ("frame_rate", "frame_rate", "int"),
        "exposure": ("exposure", "exposure", "int"),
        "edr_exposure": ("edr_exposure", "edr_exposure", "int"),
        "sync_mode": ("sync_mode", "sync_mode", "sync"),
        "trigger_edge_and_voltage": ("trigger_edge_and_voltage", "trigger_edge_and_voltage", "int"),
        "trigger_filter": ("trigger_filter", "trigger_filter", "float"),
        "trigger_delay": ("trigger_delay", "trigger_delay", "float"),
        "enable_auto_trigger": ("enable_auto_trigger", "enable_auto_trigger", "bool"),
        "shutter_off": ("shutter_off", "shutter_off", "bool"),
        "quiet": ("quiet", "quiet", "bool"),
        "nvm_auto_save": ("nvm_auto_save", "nvm_auto_save", "bool"),
    }

    def get_settings(self, _args: dict) -> dict:
        cam = self._require_cam()
        fields: dict[str, Any] = {}
        for name, (getter, setter, kind) in self._LIVE_FIELDS.items():
            val = _safe(lambda g=getter: getattr(cam, g))
            entry: dict[str, Any] = {"value": val, "writable": setter is not None, "kind": kind}
            # gsExpIndexPresets (1091) returns a typed ISO table the generic
            # pyphantom accessors can't unpack (comes back as a bare int), so
            # exp_index stays a plain number field.
            if kind == "sync":
                entry["choices"] = [m.name for m in utils.SyncModeEnum]
            fields[name] = entry
        return {"fields": fields}

    def set_settings(self, args: dict) -> dict:
        cam = self._require_cam()
        applied: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name, value in (args.get("fields") or {}).items():
            spec = self._LIVE_FIELDS.get(name)
            if spec is None or spec[1] is None:
                errors[name] = "not a writable field"
                continue
            _getter, setter, kind = spec
            try:
                setattr(cam, setter, _coerce(value, kind))
                applied[name] = _safe(lambda g=spec[0]: getattr(cam, g))
            except Exception as exc:
                errors[name] = str(exc)
        return {"applied": applied, "errors": errors}

    def set_partitions(self, args: dict) -> dict:
        cam = self._require_cam()
        count = int(args["count"])
        if count < 1:
            raise RuntimeError("partition count must be >= 1")
        # Writing PartitionsCount re-partitions camera RAM and ERASES every stored
        # cine. Only touch it when the count actually changes, and refuse while a
        # take is still stored (the caller must download/clear it first).
        current = int(cam.partition_count)
        if current == count:
            return {"partition_count": current, "changed": False}
        for cine_nr, st in cam.get_partition_state(-1):
            if getattr(st, "name", str(st)).lower() == "stored":
                raise RuntimeError(
                    f"refusing to change partition count {current}->{count}: "
                    f"partition {cine_nr} still holds an un-downloaded take")
        cam.partition_count = count
        return {"partition_count": int(cam.partition_count), "changed": True}

    # --- save --------------------------------------------------

    def save_cine(self, args: dict) -> dict:
        cam = self._require_cam()
        partition = int(args["partition"])
        dest = str(args["dest_path"])
        ftype = self._file_type(args.get("file_type"))
        job_id = str(args.get("job_id") or f"save-{partition}-{len(self._saves)}")

        cine = Cine.from_camera(cam, partition)
        _set_save_use_case(cine._cine_handle)  # bulk-transfer pipeline, not UC_VIEW
        cine.save_name = dest
        cine.save_type = ftype
        first = args.get("first")
        last = args.get("last")
        if first is not None and last is not None:
            cine.save_range = utils.FrameRange(int(first), int(last))
        else:
            # Pin the range to what was actually recorded rather than trusting
            # the SDK default (which can be the full partition capacity).
            rng = _safe(lambda: cine.recorded_range)
            if rng is not None:
                try:
                    cine.save_range = rng
                except Exception as exc:  # noqa: BLE001
                    _log(f"save_range=recorded_range failed: {exc}", "warning")
        bake = args.get("bake") or {}
        for k, v in bake.items():
            try:
                setattr(cine, k, v)
            except Exception as exc:
                _log(f"bake {k} failed: {exc}", "warning")

        rec: dict[str, Any] = {"cine": cine, "pct": 0, "path": dest,
                               "done": False, "error": None, "cancel": False}
        self._saves[job_id] = rec
        tick = threading.Event()   # reused for the poll sleep; set() wakes it early

        def _run() -> None:
            try:
                cine.save_non_blocking()
                last_pct = -1
                stalled_for = 0.0
                while True:
                    if rec["cancel"]:
                        raise RuntimeError("cancelled by operator")
                    pct = int(getattr(cine, "save_percentage", -1))
                    if pct >= 0:
                        rec["pct"] = pct
                        _event("save_progress", job_id=job_id, pct=pct)
                    if pct >= 100:
                        break
                    # Stall detector: pyphantom's save runs on its own thread and
                    # a failure there won't raise here - it just stops advancing.
                    if pct == last_pct:
                        stalled_for += 0.5
                        if stalled_for >= 90:
                            raise RuntimeError(
                                f"save stalled at {pct}% for 90s - no progress from the SDK"
                                + _write_err_suffix(cine))
                    else:
                        stalled_for = 0.0
                        last_pct = pct
                    tick.wait(0.5)
                    tick.clear()
                rec["done"] = True
                _event("save_done", job_id=job_id, path=dest)
            except Exception as exc:  # noqa: BLE001
                rec["error"] = str(exc)
                rec["done"] = True
                _event("error", job_id=job_id,
                       error=f"save failed: {exc}", cancelled=bool(rec["cancel"]))
            finally:
                try:
                    cine.close()
                except Exception:
                    pass
                rec["cine"] = None   # drop the SDK handle; save_progress still answers

        rec["tick"] = tick
        # Keep the finished-job tail bounded.
        for old in [k for k, v in list(self._saves.items())
                    if v.get("done") and k != job_id][:-20]:
            self._saves.pop(old, None)
        threading.Thread(target=_run, name=f"save-{job_id}", daemon=True).start()
        return {"job_id": job_id}

    def cancel_save(self, args: dict) -> dict:
        """Abort an in-flight save. The take stays on the camera; the caller
        discards the partial file."""
        job_id = str(args.get("job_id") or "")
        rec = self._saves.get(job_id)
        if rec is None or rec.get("done"):
            # Already finished - nothing to stop, and the caller's own wait loop
            # is what actually unblocks the operator.
            return {"job_id": job_id, "stopped": False, "reason": "no such active save"}
        rec["cancel"] = True
        stopped = False
        lib = _phfile()
        cine = rec.get("cine")
        stop_fn = getattr(lib, "PhStopWriteCineFileAsync", None) if lib else None
        if stop_fn is not None and cine is not None:
            try:
                hres = stop_fn(ctypes.c_void_p(int(cine._cine_handle)))
                stopped = hres == 0
                _log(f"PhStopWriteCineFileAsync({job_id}) hres={hres}",
                     "info" if stopped else "warning")
            except Exception as exc:  # noqa: BLE001
                _log(f"PhStopWriteCineFileAsync failed: {exc}", "warning")
        else:
            _log("no PhStopWriteCineFileAsync in this SDK build - the transfer "
                 "will run to completion in the background", "warning")
        # Wake the poll loop so it reports the cancellation without waiting out
        # its 0.5s sleep (or, if the SDK ignored us, the 90s stall detector).
        try:
            rec["tick"].set()
        except Exception:
            pass
        return {"job_id": job_id, "stopped": stopped}

    def save_progress(self, args: dict) -> dict:
        rec = self._saves.get(str(args.get("job_id")))
        if rec is None:
            raise RuntimeError("unknown job_id")
        return {"pct": rec["pct"], "done": rec["done"], "error": rec["error"]}

    def save_nvm(self, args: dict) -> dict:
        cam = self._require_cam()
        cine = Cine.from_camera(cam, int(args["partition"]))
        cine.save_nvm()
        return {}

    def delete_cine(self, args: dict) -> dict:
        cam = self._require_cam()
        cam.delete(int(args["partition"]))
        return {}


def _cn(entry: Any) -> int:
    """CameraDiscoverInfo names the index 'camera_number'."""
    for attr in ("camera_number", "cn", "cameraNumber"):
        val = getattr(entry, attr, None)
        if val is not None:
            return int(val)
    return 0


def _safe(fn: Any) -> Any:
    try:
        return fn()
    except Exception:
        return None


def _write_err_suffix(cine: Any) -> str:
    """GCI_WRITEERR holds the SDK's own reason a save died - far more useful in
    a log than the percentage it happened to stop at."""
    err = _safe(lambda: cine.get_selector_int(GCI_WRITEERR))
    return f" (SDK write error {err})" if err else ""


def _coerce(value: Any, kind: str) -> Any:
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        return bool(value) if not isinstance(value, str) else value.lower() in {"1", "true", "on", "yes"}
    if kind == "sync":
        return utils.SyncModeEnum[value] if isinstance(value, str) else utils.SyncModeEnum(int(value))
    return value


def _reader(q: "queue.Queue[str]") -> None:
    for line in sys.stdin:
        q.put(line)
    q.put("")  # EOF sentinel


def main() -> None:
    bridge = Bridge()
    _event("ready", pyphantom=_IMPORT_ERROR is None, import_error=_IMPORT_ERROR)

    q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_reader, args=(q,), name="stdin", daemon=True).start()

    while True:
        line = q.get()
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _emit({"ok": False, "error": "malformed JSON request"})
            continue
        req_id = req.get("id")
        cmd = req.get("cmd")
        args = req.get("args") or {}
        if cmd == "shutdown":
            _emit({"id": req_id, "ok": True, "result": {}})
            break
        handler = getattr(bridge, str(cmd), None)
        if handler is None or cmd.startswith("_"):
            _emit({"id": req_id, "ok": False, "error": f"unknown command {cmd!r}"})
            continue
        try:
            result = handler(args)
            _emit({"id": req_id, "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            _emit({"id": req_id, "ok": False, "error": str(exc)})
            _log(traceback.format_exc(), "error")


if __name__ == "__main__":
    main()
