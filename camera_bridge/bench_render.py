"""Benchmark: how fast can the Phantom SDK render a whole clip?

`camera_bridge/README.md`'s 4.3 Gbps figure is a raw byte *copy*
(`SVV_RAWCINE`), not a colour-processed *render*, so it says nothing about
this. `fit_look.py` calls `PhGetCineImage(UC_VIEW)` - the SDK's own
debayer+colour pipeline, the path PCC/Resolve use - but only for a handful of
sample frames to fit a LUT. This script times that same call across every
frame of a real clip, to answer: is rendering every frame through the SDK
directly (skipping ffmpeg's decode + the fitted LUT entirely) fast enough to
be worth doing for real?

Read-only: no camera connection needed (`PhNewCineFromFile` opens a clip
already on disk, same as `fit_look.py`/`spike.py`), no files written, no
pipeline changes.

Run under the bundled Python 3.11 runtime - the only interpreter that can
import pyphantom:

    camera_bridge\\runtime\\Scripts\\python.exe camera_bridge\\bench_render.py \\
        --clip D:\\GlambotImport\\Footage\\25628_p1_20260910-190001.cine \\
        --logmode 0
"""
from __future__ import annotations

import argparse
import ctypes
import os
import time
from pathlib import Path

GCI_LOGMODE = 246
GCI_SUPPORTSLOGMODE = 245
GCI_MAXIMGSIZE = 400
UC_VIEW = 1


class IMRANGE(ctypes.Structure):
    _fields_ = [("First", ctypes.c_int), ("Cnt", ctypes.c_uint)]


class IH(ctypes.Structure):
    """PhInt.h: BITMAPINFOHEADER plus the black/white levels."""
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
        ("BlackLevel", ctypes.c_int32), ("WhiteLevel", ctypes.c_int32),
    ]


def _phfile():
    """PhFile.Dll, bound directly - same helper as fit_look.py."""
    import pyphantom
    d = os.path.join(os.path.dirname(pyphantom.__file__), "data")
    try:
        os.add_dll_directory(d)
    except (AttributeError, OSError):
        pass
    lib = ctypes.WinDLL(os.path.join(d, "PhFile.Dll"))
    lib.PhNewCineFromFile.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.PhNewCineFromFile.restype = ctypes.c_int
    lib.PhDestroyCine.argtypes = [ctypes.c_void_p]
    lib.PhGetCineImage.argtypes = [ctypes.c_void_p, ctypes.POINTER(IMRANGE),
                                   ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(IH)]
    lib.PhGetCineImage.restype = ctypes.c_int
    for name in ("PhGetCineInfo", "PhSetCineInfo"):
        fn = getattr(lib, name)
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        fn.restype = ctypes.c_int
    lib.PhSetUseCase.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return lib


def _getu(lib, h, sel):
    v = ctypes.c_uint(0)
    lib.PhGetCineInfo(h, sel, ctypes.byref(v))
    return v.value


def _first_image(clip: Path) -> int:
    from pyphantom import Cine
    cine = Cine.from_filepath(str(clip))
    try:
        return int(cine.recorded_range.first_image)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        try:
            cine.close()
        except Exception:
            pass


def _frame_count(clip: Path) -> int:
    from pyphantom import Cine
    cine = Cine.from_filepath(str(clip))
    try:
        rng = cine.recorded_range
        return int(rng.last_image) - int(rng.first_image) + 1
    except Exception:  # noqa: BLE001
        return 0
    finally:
        try:
            cine.close()
        except Exception:
            pass


def bench(lib, clip: Path, logmode: int, max_frames: int | None, every: int) -> None:
    total = _frame_count(clip)
    print(f"\n== {clip.name}  LogMode={logmode}  total frames={total} ==")

    h = ctypes.c_void_p()
    if lib.PhNewCineFromFile(str(clip).encode(), ctypes.byref(h)) != 0:
        print("  ! could not open clip")
        return
    try:
        if logmode and not _getu(lib, h, GCI_SUPPORTSLOGMODE):
            print("  ! this cine reports no log-mode support")
            return
        size = _getu(lib, h, GCI_MAXIMGSIZE)
        lib.PhSetUseCase(h, UC_VIEW)
        v = ctypes.c_uint(logmode)
        lib.PhSetCineInfo(h, GCI_LOGMODE, ctypes.byref(v))
        if _getu(lib, h, GCI_LOGMODE) != logmode:
            print(f"  ! the SDK refused LogMode={logmode}")
            return

        first = _first_image(clip)
        n = total if max_frames is None else min(total, max_frames)
        offsets = list(range(0, n, every))
        buf = ctypes.create_string_buffer(size)

        bytes_done = 0
        frames_done = 0
        width = height = 0
        t0 = time.perf_counter()
        for off in offsets:
            frame = first + off
            ih = IH()
            rng = IMRANGE(First=frame, Cnt=1)
            rc = lib.PhGetCineImage(h, ctypes.byref(rng), buf, size, ctypes.byref(ih))
            if rc != 0:
                continue
            width, height = ih.biWidth, abs(ih.biHeight)
            bytes_done += width * height * (ih.biBitCount // 8)
            frames_done += 1
        elapsed = time.perf_counter() - t0

        if frames_done == 0 or elapsed == 0:
            print("  ! no frames rendered")
            return

        fps = frames_done / elapsed
        mbps = (bytes_done / (1024 * 1024)) / elapsed
        sampled_span = offsets[-1] - offsets[0] if len(offsets) > 1 else 0
        extrapolated = (total / fps) if fps else float("inf")

        print(f"  resolution      : {width}x{height}")
        print(f"  frames rendered : {frames_done} (every {every}, span {sampled_span})")
        print(f"  elapsed         : {elapsed:.2f}s")
        print(f"  throughput      : {fps:.2f} fps, {mbps:.1f} MB/s")
        print(f"  extrapolated    : {extrapolated:.1f}s for the full {total}-frame clip")
    finally:
        lib.PhDestroyCine(h)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clip", required=True, help=".cine file to render")
    ap.add_argument("--logmode", default="0,1,2",
                    help="comma-separated LogModes to test (default 0,1,2)")
    ap.add_argument("--max-frames", type=int, default=None,
                    help="cap the number of frames walked (default: whole clip)")
    ap.add_argument("--every", type=int, default=1,
                    help="render every Nth frame instead of all of them, for a "
                         "quick first look before committing to a full walk")
    args = ap.parse_args()

    clip = Path(args.clip)
    if not clip.exists():
        raise SystemExit(f"not found: {clip}")

    from pyphantom import Phantom
    Phantom()  # generates pyphantom's key table
    lib = _phfile()

    for lm in (int(x) for x in args.logmode.split(",") if x.strip()):
        bench(lib, clip, lm, args.max_frames, args.every)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
