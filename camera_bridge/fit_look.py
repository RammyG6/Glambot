"""Fit an ffmpeg LUT that reproduces the Phantom SDK's own colour.

Why this exists
---------------
Glambot renders `.cine` footage with ffmpeg, which knows nothing about Phantom's
image pipeline. Rebuilding that pipeline by hand out of the header's black
level, colour matrix and tone curve was measured at ~10% mean error against what
the SDK actually produces - visibly wrong, not a rounding difference.

So instead of reimplementing the pipeline, this asks the SDK to render frames
itself (`PhGetCineImage` under `UC_VIEW`, which is the path PCC's viewer uses)
and fits a transform from ffmpeg's raw decode to that output. The fit is
structural rather than a black box: the cine's own calibration matrix is applied
first - it is known exactly, and a 3x3 is the one part a per-channel curve
cannot express - and only the remaining per-channel tone response is fitted.
Measured 0.5% mean error against the SDK, including on held-out clips.

`LogMode` is a *cine header field*, so `--logmode 1` renders Vision Research's
Log1 even though this camera body cannot record log
(`gsSupportsLogMode` = 0, while the file cine's `GCI_SUPPORTSLOGMODE` = 1).

Run with the bridge's interpreter, which is the only one that can import
pyphantom::

    camera_bridge\\runtime\\Scripts\\python.exe camera_bridge\\fit_look.py \\
        --out looks\\log1.cube --logmode 1 D:\\GlambotImport\\Footage\\*.cine

Reads only. Clips are never modified: `PhSetCineInfo` acts on the open handle.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

GCI_LOGMODE = 246
GCI_SUPPORTSLOGMODE = 245
GCI_MAXIMGSIZE = 400
UC_VIEW = 1
LUT_SIZE = 256
LOOK_SUFFIX = ".look.json"     # must match effects.LOOK_SUFFIX


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
    """PhFile.Dll, bound directly.

    Deliberately not pyphantom's `cine.get_images`: it reports every failure as
    a bare "Cannot read file" with no HRESULT, which makes a frame-range or
    use-case mistake impossible to tell apart from a genuinely unreadable file.
    """
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


def sdk_frames(lib, clip: Path, logmode: int, offsets) -> list[np.ndarray]:
    """The SDK's own render of this clip, as float RGB, at `offsets` frames
    from the start of the take.

    Sampled by offset-from-first rather than by wall position, because ffmpeg
    can only be asked for "the first N frames" reliably - seeking a raw cine by
    time lands near a frame, not on one, and a pairing that is off by a single
    frame poisons the fit with motion instead of colour.
    """
    h = ctypes.c_void_p()
    if lib.PhNewCineFromFile(str(clip).encode(), ctypes.byref(h)) != 0:
        print(f"    ! could not open {clip.name}")
        return []
    out: list[np.ndarray] = []
    try:
        if logmode and not _getu(lib, h, GCI_SUPPORTSLOGMODE):
            print(f"    ! {clip.name}: this cine reports no log-mode support")
            return []
        size = _getu(lib, h, GCI_MAXIMGSIZE)
        lib.PhSetUseCase(h, UC_VIEW)          # the processed path; UC_SAVE is raw
        v = ctypes.c_uint(logmode)
        lib.PhSetCineInfo(h, GCI_LOGMODE, ctypes.byref(v))
        if _getu(lib, h, GCI_LOGMODE) != logmode:
            print(f"    ! {clip.name}: the SDK refused LogMode={logmode}")
            return []
        # Phantom numbers pre-trigger frames negative, so the first image is
        # not frame 0. Frame 0 is the last one.
        first = _first_image(clip)
        for off in offsets:
            frame = first + off
            buf = ctypes.create_string_buffer(size)
            ih = IH()
            rng = IMRANGE(First=frame, Cnt=1)
            if lib.PhGetCineImage(h, ctypes.byref(rng), buf, size, ctypes.byref(ih)) != 0:
                continue
            n = ih.biWidth * abs(ih.biHeight) * (ih.biBitCount // 8)
            a = np.frombuffer(buf.raw[:n], dtype=np.uint16).reshape(
                abs(ih.biHeight), ih.biWidth, 3)
            # Samples are BGR; row order already matches ffmpeg's.
            out.append(a[:, :, ::-1].astype(np.float64) / float(ih.biClrImportant or 4096))
    finally:
        lib.PhDestroyCine(h)
    return out


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


def ffmpeg_frames(ffmpeg: str, clip: Path, shape, offsets) -> list[np.ndarray]:
    """ffmpeg's raw linear decode at the same offsets from the start.

    Decodes `max(offsets)+1` frames from the beginning and keeps the ones asked
    for. Counting forward is the only way to be certain which frame came back;
    it costs a sequential read, which is fine for a one-off fit.
    """
    h, w = shape
    frame_bytes = h * w * 6
    need = max(offsets) + 1
    cmd = [ffmpeg, "-v", "error", "-i", str(clip), "-frames:v", str(need),
           "-vf", "format=rgb48le", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    wanted, out = set(offsets), {}
    try:
        for i in range(need):
            raw = proc.stdout.read(frame_bytes)
            if len(raw) != frame_bytes:
                break
            if i in wanted:
                out[i] = np.frombuffer(raw, dtype="<u2").reshape(
                    h, w, 3).astype(np.float64) / 65535.0
    finally:
        proc.stdout.close()
        proc.wait()
    return [out[o] for o in offsets if o in out]


def aligned(x: np.ndarray, y: np.ndarray) -> float:
    """Correlation of the two frames' luma. A pair that is off by a frame, or
    is not the same frame at all, shows up here before it can skew the fit."""
    a = x.mean(axis=2).ravel()
    b = y.mean(axis=2).ravel()
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / denom) if denom else 0.0


def calib_matrix(clip: Path) -> np.ndarray | None:
    """The cine's colour matrix, from the sidecar the importer already writes."""
    side = Path(str(clip.with_suffix("")) + LOOK_SUFFIX)
    if not side.exists():
        return None
    try:
        look = json.loads(side.read_text(encoding="utf-8")).get("look") or {}
        m = look.get("calib_matrix")
        return np.array(m, dtype=np.float64).reshape(3, 3) if m and len(m) >= 9 else None
    except (OSError, ValueError):
        return None


def pool(a: np.ndarray, k: int = 4) -> np.ndarray:
    """Average k*k blocks. ffmpeg and the SDK demosaic differently, so matching
    edge pixels is neither possible nor the point - this fits the colour
    transform, not the debayer."""
    h, w = a.shape[0] // k * k, a.shape[1] // k * k
    return a[:h, :w].reshape(h // k, k, w // k, k, 3).mean(axis=(1, 3))


def fit_channel(x: np.ndarray, y: np.ndarray, nbins: int = LUT_SIZE) -> np.ndarray:
    """Median output per input bin, gaps interpolated, forced monotonic.

    Median rather than mean so the handful of pixels where the two demosaics
    disagree badly cannot drag a bin. Monotonic because a tone response that
    dips would show as banding on a gradient.
    """
    idx = np.clip((x * nbins).astype(int), 0, nbins - 1)
    lut = np.full(nbins, np.nan)
    for b in range(nbins):
        m = idx == b
        if m.sum() >= 8:
            lut[b] = np.median(y[m])
    ok = ~np.isnan(lut)
    if ok.sum() < 2:
        raise SystemExit("not enough distinct input levels to fit a curve")
    lut = np.interp(np.arange(nbins), np.arange(nbins)[ok], lut[ok])
    return np.clip(np.maximum.accumulate(lut), 0.0, 1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clips", nargs="+", help=".cine files to fit from")
    ap.add_argument("--out", required=True, help="destination .cube")
    ap.add_argument("--logmode", type=int, default=0,
                    help="0 = the camera's own look, 1/2 = Vision Research Log1/Log2")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--offsets", default="0",
                    help="comma-separated frame offsets from the start of each take "
                         "to sample (e.g. 0,200). Every extra offset costs a "
                         "sequential decode up to that frame.")
    ap.add_argument("--min-corr", type=float, default=0.90,
                    help="reject a frame pair whose luma correlation is below this")
    args = ap.parse_args()
    offsets = sorted({max(0, int(o)) for o in args.offsets.split(",") if o.strip()})

    from pyphantom import Phantom
    Phantom()                                  # generates pyphantom's key table
    lib = _phfile()

    xs, ys = [], []
    for name in args.clips:
        clip = Path(name)
        if not clip.exists():
            print(f"  skip {clip.name}: not found")
            continue
        M = calib_matrix(clip)
        if M is None:
            print(f"  skip {clip.name}: no {LOOK_SUFFIX} sidecar - run the colour backfill")
            continue
        sdk = sdk_frames(lib, clip, args.logmode, offsets)
        if not sdk:
            continue
        ff = ffmpeg_frames(args.ffmpeg, clip, sdk[0].shape[:2], offsets)
        used = 0
        for a, b in zip(ff, sdk):
            r = aligned(pool(a), pool(b))
            if r < args.min_corr:
                print(f"    ! dropping a frame pair from {clip.name}: correlation "
                      f"{r:.3f} - ffmpeg and the SDK are not on the same frame")
                continue
            used += 1
            x = pool(a).reshape(-1, 3)
            y = pool(b).reshape(-1, 3)
            # The matrix is applied first and NOT fitted: it is known exactly
            # from the header, and channel mixing is the one thing a
            # per-channel curve cannot represent.
            xs.append(np.clip(x @ M.T, 0.0, 1.0))
            ys.append(y)
        print(f"  {clip.name}: {used} frame pair(s)")

    if not xs:
        raise SystemExit("no usable clips - nothing to fit")
    X, Y = np.concatenate(xs), np.concatenate(ys)
    print(f"\nfitting from {len(X):,} pooled samples")
    luts = np.stack([fit_channel(X[:, c], Y[:, c]) for c in range(3)])

    pred = np.stack([np.interp(X[:, c] * (LUT_SIZE - 1), np.arange(LUT_SIZE), luts[c])
                     for c in range(3)], axis=1)
    err = np.abs(pred - Y)
    print(f"residual vs the SDK: mean {err.mean():.4f}  p95 {np.percentile(err, 95):.4f}"
          f"  p99 {np.percentile(err, 99):.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# Phantom look, LogMode={args.logmode}\n")
        f.write("# Fitted from PhGetCineImage(UC_VIEW) by camera_bridge/fit_look.py.\n")
        f.write("# Input domain: ffmpeg's raw linear RGB AFTER the cine's calib matrix.\n")
        f.write(f"# Clips: {', '.join(Path(c).name for c in args.clips)}\n")
        f.write(f"# Residual vs SDK: mean {err.mean():.4f}, p95 {np.percentile(err, 95):.4f}\n")
        f.write(f"LUT_1D_SIZE {LUT_SIZE}\n")
        for i in range(LUT_SIZE):
            f.write(f"{luts[0, i]:.6f} {luts[1, i]:.6f} {luts[2, i]:.6f}\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
