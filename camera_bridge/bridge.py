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
import struct
import sys
import threading
import time
import traceback
from typing import Any

# The SDK and pyphantom both like to print. Keep stdout pristine for JSON.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

try:  # pyphantom is only importable under the bundled 3.11 runtime
    import pyphantom
    from pyphantom import Phantom, Camera, Cine, utils
    # Used directly so a save can run with our own progress callback rather than
    # pyphantom's, which always returns 1 and therefore cannot be cancelled.
    from pyphantom.phantom import phDoCine
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

# Cine info selectors (GCI_* in PhFile.h, mirrored in pyphantom.utils).
GCI_TRIGTIMESEC = 10        # trigger time, whole seconds
GCI_TRIGTIMEFR = 11         # trigger time, fractions
GCI_TOTALIMAGECOUNT = 30    # frames actually recorded
GCI_WRITEERR = 109          # last error from a save on this cine

# Colour profile ("log mode"). pyphantom ports neither the selectors nor an
# enum for these, so they come straight from the SDK headers:
#   PhFile.h:404-405  GCI_SUPPORTSLOGMODE / GCI_LOGMODE
#   Phint.h:640-644   "0 - log mode disabled. 1, 2, etc - log mode enabled.
#                      If log mode enabled, gain, gamma, the pedestals,
#                      r/g/b gains, offset and flare are inactive."
# It is a *cine* parameter - there is no camera-side setter - so the live value
# lives on the live cine handle, which pyphantom addresses as cine number -1
# (see pyphantom/camera.py: `self._live_cine = Cine.from_camera(self, -1)`).
GCI_SUPPORTSLOGMODE = 245
GCI_LOGMODE = 246
GS_SUPPORTS_LOG_MODE = 9005   # camera-side read-only capability (PhCon.cs)
LIVE_CINE = -1

# The rest of the camera's colour description. ffprobe exposes almost none of
# this - its `gamma` tag is the deprecated int32 Gamma (Phint.h:374), not
# fGamma - so the renderer has to be told, which is what `read_look` is for.
GCI_BRIGHT = 202        # fOffset      neutral 0.0
GCI_CONTRAST = 203      # fGain        neutral 1.0
GCI_GAMMA = 204         # fGamma       neutral 1.0
GCI_SATURATION = 205    # fSaturation  neutral 1.0
GCI_FLARE = 225
GCI_TONE = 227          # TONEDESC struct - the tone curve, i.e. the LUT
GCI_ENABLEMATRICES = 228
GCI_USERMATRIX = 229    # CMDESC struct
GCI_CALIBMATRIX = 231   # CMDESC struct - the factory colour correction
GCI_SUPPORTSTOE = 243
GCI_TOE = 244           # fToe         neutral 1.0
GCI_REALBPP = 4

COLOR_PROFILES = {"Rec709": 0, "Log1": 1, "Log2": 2}
_PROFILE_BY_VALUE = {v: k for k, v in COLOR_PROFILES.items()}

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
        # Struct-valued cine info (tone curve, colour matrices). pyphantom's
        # generic get_selector_* can only return scalars and hands back the
        # struct's first field, so these have to go through the C API.
        lib.PhGetCineInfo.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        lib.PhGetCineInfo.restype = ctypes.c_int
        lib.PhSetCineInfo.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        lib.PhSetCineInfo.restype = ctypes.c_int
        # NB: PhFile.Dll also exports PhStopWriteCineFileAsync, which looks like
        # the obvious way to cancel a transfer. It is deliberately NOT used:
        # there is no Phantom header in this repo to check its signature
        # against, and calling it on a live save is the prime suspect for
        # killing this process mid-transfer on 2026-09-08. Cancellation goes
        # through the save progress callback instead (see save_cine).
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


def _cine_struct(cine_handle: Any, selector: int, floats: int) -> list[float] | None:
    """Read a struct-valued cine selector as its leading floats.

    Deliberately oversized buffer: sizing it to the struct we expect crashed
    the process outright on GCI_CALIBMATRIX, and a hard crash here would take
    a download with it.
    """
    lib = _phfile()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(8192)
        if lib.PhGetCineInfo(ctypes.c_void_p(int(cine_handle)), selector, buf) != 0:
            return None
        return list(struct.unpack_from(f"<{floats}f", buf.raw, 0))
    except Exception as exc:  # noqa: BLE001
        _log(f"PhGetCineInfo({selector}) failed: {exc}", "warning")
        return None


def _read_tone(cine_handle: Any) -> dict[str, Any] | None:
    """TONEDESC (PhFile.h:106-112): int count, float[64] points, char[256] label."""
    lib = _phfile()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(8192)
        if lib.PhGetCineInfo(ctypes.c_void_p(int(cine_handle)), GCI_TONE, buf) != 0:
            return None
        raw = buf.raw
        count = struct.unpack_from("<i", raw, 0)[0]
        if not 0 < count <= 32:
            return None
        pts = struct.unpack_from(f"<{count * 2}f", raw, 4)
        label = raw[4 + 64 * 4: 4 + 64 * 4 + 256].split(b"\x00")[0].decode(errors="replace")
        return {"label": label,
                "points": [[round(pts[2 * i], 6), round(pts[2 * i + 1], 6)]
                           for i in range(count)]}
    except Exception as exc:  # noqa: BLE001
        _log(f"tone curve read failed: {exc}", "warning")
        return None


def _read_look(cine: Any) -> dict[str, Any]:
    """Everything the renderer needs to reproduce the camera's colour."""
    h = cine._cine_handle
    wb = _safe(lambda: cine.white_balance)
    levels = _safe(lambda: cine.black_white_levels)
    out: dict[str, Any] = {
        "wb_red": _safe(lambda: float(wb.red_gain)) if wb else None,
        "wb_blue": _safe(lambda: float(wb.blue_gain)) if wb else None,
        "black_level": _safe(lambda: int(levels.black_level)) if levels else None,
        "white_level": _safe(lambda: int(levels.white_level)) if levels else None,
        "tone": _read_tone(h),
        "calib_matrix": _cine_struct(h, GCI_CALIBMATRIX, 9),
        "user_matrix": _cine_struct(h, GCI_USERMATRIX, 9),
    }
    for name, sel in (("gamma", GCI_GAMMA), ("gain", GCI_CONTRAST), ("offset", GCI_BRIGHT),
                      ("saturation", GCI_SATURATION), ("toe", GCI_TOE), ("flare", GCI_FLARE)):
        val = _safe(lambda s=sel: cine.get_selector_float(s))
        out[name] = round(float(val), 6) if val is not None else None
    for name, sel in (("log_mode", GCI_LOGMODE), ("real_bpp", GCI_REALBPP),
                      ("enable_matrices", GCI_ENABLEMATRICES)):
        val = _safe(lambda s=sel: cine.get_selector_int(s))
        out[name] = int(val) if val is not None else None
    return out


def _with_live_cine(cam: Any, fn: Any) -> Any:
    """Run ``fn(cine)`` against the camera's live cine, always closing it.

    Same open/close discipline as ``Bridge._take_id``. Deliberately not
    ``cam._live_cine``: that handle is private to the pyphantom wrapper and
    closed in its own teardown.
    """
    cine = None
    try:
        cine = Cine.from_camera(cam, LIVE_CINE)
        return fn(cine)
    finally:
        if cine is not None:
            try:
                cine.close()
            except Exception:
                pass


def _supports_color_profile(cam: Any) -> bool:
    """Whether this body has log mode at all. Asked before the field is offered,
    so an unsupported camera never shows a control that would quietly do
    nothing."""
    val = _safe(lambda: cam.get_selector_int(GS_SUPPORTS_LOG_MODE))
    if val is None:
        val = _safe(lambda: _with_live_cine(
            cam, lambda c: c.get_selector_uint(GCI_SUPPORTSLOGMODE)))
    return bool(val)


def _get_color_profile(cam: Any) -> str | None:
    raw = _with_live_cine(cam, lambda c: c.get_selector_uint(GCI_LOGMODE))
    if raw is None:
        return None
    n = int(raw)
    # Header says "1, 2, etc", so don't assume 2 is the ceiling - name the ones
    # we know and pass anything else through rather than mislabelling it.
    return _PROFILE_BY_VALUE.get(n, f"Log{n}")


def _set_color_profile(cam: Any, value: Any) -> None:
    # set_selector_* takes a single SetSelector(selector, value) namedtuple -
    # the one-arg signature in pyphantom/camera.py wins over the two-arg form in
    # its README. Same shape as the FrameRange already used in save_cine.
    _with_live_cine(cam, lambda c: c.set_selector_uint(
        utils.SetSelector(GCI_LOGMODE, int(value))))


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
        # getter/setter are attribute names on the pyphantom Camera, or
        # callables taking (cam) / (cam, value) for anything the wrapper has no
        # property for - the colour profile is a cine selector, not a camera one.
        "color_profile": (_get_color_profile, _set_color_profile, "profile"),
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

    @staticmethod
    def _read_field(cam: Any, getter: Any) -> Any:
        val = getter(cam) if callable(getter) else getattr(cam, getter)
        # The page preselects a dropdown with String(choice) === String(value),
        # so an enum must come back as its bare name: a SyncModeEnum serialises
        # as "SyncModeEnum.INTERNAL" and would never match the "INTERNAL"
        # option, leaving the control looking blank/wrong.
        return getattr(val, "name", val)

    def get_settings(self, _args: dict) -> dict:
        cam = self._require_cam()
        choices_for = {
            "sync": [m.name for m in utils.SyncModeEnum],
            "profile": list(COLOR_PROFILES),
        }
        fields: dict[str, Any] = {}
        for name, (getter, setter, kind) in self._LIVE_FIELDS.items():
            # Don't offer a control the body can't honour - it would appear to
            # work and quietly do nothing.
            if kind == "profile" and not _supports_color_profile(cam):
                continue
            val = _safe(lambda g=getter: self._read_field(cam, g))
            entry: dict[str, Any] = {"value": val, "writable": setter is not None, "kind": kind}
            # gsExpIndexPresets (1091) returns a typed ISO table the generic
            # pyphantom accessors can't unpack (comes back as a bare int), so
            # exp_index stays a plain number field.
            if kind in choices_for:
                entry["choices"] = choices_for[kind]
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
            getter, setter, kind = spec
            try:
                coerced = _coerce(value, kind)
                if callable(setter):
                    setter(cam, coerced)
                else:
                    setattr(cam, setter, coerced)
                applied[name] = _safe(lambda g=getter: self._read_field(cam, g))
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

        rec: dict[str, Any] = {"cine": cine, "pct": -1, "path": dest,
                               "done": False, "error": None, "cancel": False,
                               "aborted_in_band": False}
        self._saves[job_id] = rec
        tick = threading.Event()   # reused for the poll sleep; set() wakes it early

        def _progress(_cine_handle: Any, progress: Any) -> int:
            """The SDK's save thread calls this. Returning 0 asks it to abort,
            which is the in-band cancel - far safer than reaching into PhFile.Dll
            to stop a transfer that is already running."""
            try:
                rec["pct"] = int(progress)
            except (TypeError, ValueError):
                pass
            if rec["cancel"]:
                rec["aborted_in_band"] = True
                return 0
            return 1

        # The native side keeps a raw pointer to this callback, so it must stay
        # referenced for the life of the save. (pyphantom's own
        # save_non_blocking() hands its callback over without storing it, which
        # is a crash waiting to happen - another reason not to use it here.)
        rec["progress_cb"] = _progress

        def _run() -> None:
            try:
                # Deliberately not cine.save_non_blocking(): that installs
                # pyphantom's callback, which always returns 1 and so can never
                # be cancelled. Same SDK entry point, our own callback.
                cine.progress_callback = _progress
                phDoCine(utils._phantom_keys._SaveNonBlocking, cine._cine_handle)
                last_pct = -1
                stalled_for = 0.0
                while True:
                    if rec["cancel"] and rec["aborted_in_band"]:
                        raise RuntimeError("cancelled by operator")
                    pct = int(rec["pct"])
                    if pct >= 0:
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

        # Report what is actually being written. The format is an operator
        # setting, and picking the processed cine over the raw one costs several
        # times the bytes and the time - worth stating on every download rather
        # than leaving it to be discovered by benchmarking.
        res = _safe(lambda: cine.resolution)
        rng = _safe(lambda: cine.recorded_range)
        frames = None
        if rng is not None:
            frames = abs(int(rng.last_image) - int(rng.first_image)) + 1
        return {
            "job_id": job_id,
            "file_type": getattr(ftype, "name", str(ftype)),
            "width": _safe(lambda: int(res.x)) if res is not None else None,
            "height": _safe(lambda: int(res.y)) if res is not None else None,
            "frames": frames,
        }

    def cancel_save(self, args: dict) -> dict:
        """Ask an in-flight save to abort. The take stays on the camera; the
        caller discards the partial file.

        The flag is what does the work: the SDK's own save thread reads it
        through our progress callback and stops when that returns 0. This
        returns as soon as the flag is set - whether the SDK actually honoured
        it shows up as ``aborted``, which the caller polls before falling back
        to restarting this process.
        """
        job_id = str(args.get("job_id") or "")
        rec = self._saves.get(job_id)
        if rec is None or rec.get("done"):
            # Already finished - nothing to stop, and the caller's own wait loop
            # is what actually unblocks the operator.
            return {"job_id": job_id, "stopped": False, "reason": "no such active save"}
        rec["cancel"] = True
        # Wake the poll loop so it reports the cancellation without waiting out
        # its 0.5s sleep.
        try:
            rec["tick"].set()
        except Exception:
            pass
        # Give the save thread a moment to reach the callback, then say whether
        # the in-band abort took. Deliberately NOT calling
        # PhStopWriteCineFileAsync first: its signature is a guess (no Phantom
        # header in the repo) and calling it on a live transfer is the prime
        # suspect for killing this process mid-save on 2026-09-08.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if rec.get("aborted_in_band") or rec.get("done"):
                break
            time.sleep(0.1)
        aborted = bool(rec.get("aborted_in_band") or rec.get("done"))
        if not aborted:
            _log(f"cancel_save({job_id}): SDK has not honoured the in-band abort yet; "
                 "the caller will restart the bridge if it doesn't stop", "warning")
        return {"job_id": job_id, "stopped": aborted, "aborted": aborted}

    def save_progress(self, args: dict) -> dict:
        rec = self._saves.get(str(args.get("job_id")))
        if rec is None:
            raise RuntimeError("unknown job_id")
        return {"pct": rec["pct"], "done": rec["done"], "error": rec["error"]}

    def read_look(self, args: dict) -> dict:
        """Read a .cine file's colour description. No camera needed -
        Cine.from_filepath opens the file directly - so this also works for
        clips already on disk and while show control holds the port."""
        self._require_pyphantom()
        path = str(args["path"])
        self._phantom()          # ensure the key table is generated
        cine = Cine.from_filepath(path)
        try:
            look = _read_look(cine)
        finally:
            try:
                cine.close()
            except Exception:
                pass
        return {"path": path, "look": look}

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
    if kind == "profile":
        if isinstance(value, str):
            name = value.strip()
            if name in COLOR_PROFILES:
                return COLOR_PROFILES[name]
            # _get_color_profile labels an unnamed mode "LogN"; accept it back.
            if name.lower().startswith("log") and name[3:].isdigit():
                return int(name[3:])
        try:
            return int(value)
        except (TypeError, ValueError):
            raise RuntimeError(f"unknown colour profile {value!r}")
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
