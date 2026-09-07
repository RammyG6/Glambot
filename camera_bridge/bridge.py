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
get_state                     -> {"partitions": [{"n","state"}], "record_state": "..."}
get_settings                  -> {"fields": {name: {"value","writable","choices"?,"min"?,"max"?}}}
set_settings {fields:{...}}   -> {"applied": {...}, "errors": {name: "msg"}}
set_partitions {count}        -> {"partition_count": N}
save_cine {partition, dest_path, file_type?, first?, last?, job_id?}
                             -> {"job_id"}  (completion via save_done event)
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
# pipeline is tuned for interactive playback, not a bulk camera->disk copy. PCC
# calls PhSetUseCase(hC, UC_SAVE) before writing; pyphantom never does, which is
# why our downloads ran at a fraction of PCC's speed. We poke PhFile.Dll directly.
UC_SAVE = 2
_PHFILE: Any = None
_PHFILE_TRIED = False


def _phfile() -> Any:
    """Lazily load PhFile.Dll (the one pyphantom already bundles). Returns None
    if it can't be loaded - callers must treat the use-case hint as best-effort."""
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
        _PHFILE = lib
    except Exception as exc:  # noqa: BLE001
        _log(f"could not load PhFile.Dll for PhSetUseCase: {exc}", "warning")
        _PHFILE = None
    return _PHFILE


def _set_save_use_case(cine_handle: Any) -> None:
    lib = _phfile()
    if lib is None:
        return
    try:
        h = ctypes.c_void_p(int(cine_handle))
        hres = lib.PhSetUseCase(h, UC_SAVE)
        cur = ctypes.c_int(-1)
        lib.PhGetUseCase(h, ctypes.byref(cur))
        _log(f"PhSetUseCase(UC_SAVE) hres={hres} use_case_now={cur.value}",
             "info" if hres == 0 and cur.value == UC_SAVE else "warning")
    except Exception as exc:  # noqa: BLE001
        _log(f"PhSetUseCase failed: {exc}", "warning")


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

    def get_state(self, _args: dict) -> dict:
        cam = self._require_cam()
        parts = []
        record_state = "unknown"
        try:
            states = cam.get_partition_state(-1)  # [(cine_nr, PartitionStateEnum)]
            for cine_nr, st in states:
                name = getattr(st, "name", str(st)).lower()
                parts.append({"n": int(cine_nr), "state": name})
                if name == "recording":
                    record_state = "recording"
            if record_state != "recording":
                record_state = "stored" if any(p["state"] == "stored" for p in parts) else "ready"
        except Exception as exc:
            _log(f"get_state failed: {exc}", "warning")
        return {"partitions": parts, "record_state": record_state}

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

        rec = {"cine": cine, "pct": 0, "path": dest, "done": False, "error": None}
        self._saves[job_id] = rec

        def _run() -> None:
            try:
                cine.save_non_blocking()
                last_pct = -1
                stalled_for = 0.0
                while True:
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
                                f"save stalled at {pct}% for 90s - no progress from the SDK")
                    else:
                        stalled_for = 0.0
                        last_pct = pct
                    threading.Event().wait(0.5)
                rec["done"] = True
                _event("save_done", job_id=job_id, path=dest)
            except Exception as exc:  # noqa: BLE001
                rec["error"] = str(exc)
                rec["done"] = True
                _event("error", job_id=job_id, error=f"save failed: {exc}")
            finally:
                try:
                    cine.close()
                except Exception:
                    pass

        threading.Thread(target=_run, name=f"save-{job_id}", daemon=True).start()
        return {"job_id": job_id}

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
