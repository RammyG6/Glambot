"""Camera-side validation and transfer benchmark - run under Python 3.11 with
pyphantom installed (the bundled ``camera_bridge/runtime`` has it).

    camera_bridge\\runtime\\Scripts\\python.exe camera_bridge\\spike.py --serial 25628

Two jobs:

*Validation* (default): discover -> connect -> read state, and report the link
the SDK is actually on. Read-only.

*Benchmark* (``--save-partition N --out PATH``): time a real ``.cine`` pull and
report MB/s. With ``--matrix`` it runs the same partition once per variable so
the numbers can be compared directly - this is how to settle why Glambot's
transfers trail PCC's. The suspects it separates:

  use_case  UC_VIEW (pyphantom's default, an interactive-playback read pipeline)
            vs UC_SAVE (the bulk camera->disk path PCC uses). pyphantom has no
            wrapper for PhSetUseCase, so the bridge pokes PhFile.Dll directly.
  ext       writing to ``.part`` (what the importer does, so the watcher never
            sees a half file) vs a plain ``.cine``. Antivirus exclusions are
            usually written by extension, and would not cover ``.part``.
  progress  pyphantom's Python progress callback, which the SDK's save thread
            invokes and which takes the GIL each time, vs no callback at all.

Each run also reports GCI_WRITEERR - the SDK's own reason a save died, which is
far more useful than the percentage it happened to stop at.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import time

UC_VIEW, UC_SAVE = 1, 2
GCI_WRITEERR = 109

_LIB = None


def _phfile():
    """Load the PhFile.Dll that pyphantom already bundles."""
    global _LIB
    if _LIB is None:
        import pyphantom
        dll_dir = os.path.join(os.path.dirname(pyphantom.__file__), "data")
        try:
            os.add_dll_directory(dll_dir)
        except (AttributeError, OSError):
            pass
        lib = ctypes.WinDLL(os.path.join(dll_dir, "PhFile.Dll"))
        lib.PhSetUseCase.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.PhSetUseCase.restype = ctypes.c_int
        lib.PhGetUseCase.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.PhGetUseCase.restype = ctypes.c_int
        _LIB = lib
    return _LIB


def _apply_use_case(cine, use_case: int) -> bool:
    """Set the cine's use case and *verify the readback*.

    The handle is passed as a raw pointer; if pyphantom ever hands back a
    wrapper object instead of an address, the call silently targets nothing and
    the transfer quietly runs at UC_VIEW speed. Only the readback proves it took.
    """
    lib = _phfile()
    h = ctypes.c_void_p(int(cine._cine_handle))
    hres = lib.PhSetUseCase(h, use_case)
    cur = ctypes.c_int(-1)
    lib.PhGetUseCase(h, ctypes.byref(cur))
    ok = hres == 0 and cur.value == use_case
    print(f"    PhSetUseCase({use_case}) hres={hres} readback={cur.value} "
          f"{'OK' if ok else '<-- NOT APPLIED'}")
    return ok


def _write_err(cine) -> int | None:
    try:
        return cine.get_selector_int(GCI_WRITEERR)
    except Exception:  # noqa: BLE001
        return None


def _defender_note(path: str) -> None:
    """Real-time scanning of a multi-GB file mid-write throttles a 10G transfer
    to a few hundred Mbps. Reading the exclusion list needs admin, so just say
    whether scanning is on at all."""
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-MpComputerStatus).RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=20)
        if out.stdout.strip().lower() == "true":
            print(f"  NOTE: Defender real-time protection is ON. If {os.path.dirname(path)} "
                  f"is not excluded, on-access scanning alone can explain a large gap.")
            print(f"        Check (elevated): Get-MpPreference | Select -Expand ExclusionPath")
    except Exception:  # noqa: BLE001
        pass


def _run_one(cam, partition: int, out: str, file_type: str,
             use_case: int, callback: bool) -> dict:
    """One timed pull. Returns a row for the summary table."""
    from pyphantom import Cine, utils

    if os.path.exists(out):
        os.remove(out)
    cine = Cine.from_camera(cam, partition)
    row = {"use_case": "save" if use_case == UC_SAVE else "view",
           "ext": os.path.splitext(out)[1], "progress": "callback" if callback else "none",
           "mb_s": None, "gbps": None, "size_mb": None, "err": None}
    try:
        applied = _apply_use_case(cine, use_case)
        row["applied"] = applied
        cine.save_name = out
        cine.save_type = utils.FileTypeEnum[file_type]
        rng = None
        try:
            rng = cine.recorded_range
            cine.save_range = rng
        except Exception as exc:  # noqa: BLE001
            print(f"    (save_range=recorded_range unavailable: {exc})")

        t0 = time.time()
        if callback:
            cine.save_non_blocking()          # registers pyphantom's Python callback
            while True:
                pct = cine.save_percentage
                if pct >= 100:
                    break
                time.sleep(0.5)
            elapsed = time.time() - t0
        else:
            # Same SDK entry point, but with no Python callback registered.
            # Progress is read off the growing file instead, which costs the
            # save thread nothing.
            from pyphantom.cine import phDoCine
            phDoCine(utils._phantom_keys._SaveNonBlocking, cine._cine_handle)
            stable = 0
            last = -1
            grew_at = t0
            while True:
                time.sleep(0.5)
                size = os.path.getsize(out) if os.path.exists(out) else 0
                if size == last:
                    stable += 1
                    if stable >= 6 and size > 0:
                        break          # 3s without growth = done (or dead)
                    if stable >= 60:
                        raise RuntimeError("no progress for 30s")
                else:
                    stable = 0
                    last = size
                    grew_at = time.time()
            # Stop the clock when the file stopped growing, not when this loop
            # noticed - otherwise the detection delay is charged to the transfer
            # and makes the callback-free path look slower than it is.
            elapsed = grew_at - t0
        elapsed = max(elapsed, 1e-6)
        size = os.path.getsize(out)
        row["size_mb"] = round(size / 1e6, 1)
        row["mb_s"] = round(size / 1e6 / elapsed, 1)
        row["gbps"] = round(size / 1e6 / elapsed * 8 / 1000, 2)
        print(f"    {row['size_mb']:.0f} MB in {elapsed:.1f}s = "
              f"{row['mb_s']:.0f} MB/s ({row['gbps']:.2f} Gbps)")
    except Exception as exc:  # noqa: BLE001
        row["err"] = str(exc)
        print(f"    FAILED: {exc!r}")
    finally:
        we = _write_err(cine)
        if we:
            row["err"] = f"{row['err'] or ''} (GCI_WRITEERR={we})".strip()
            print(f"    SDK write error: {we}")
        try:
            cine.close()
        except Exception:
            pass
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", type=int)
    ap.add_argument("--ip")
    ap.add_argument("--partitions", type=int,
                    help="DANGER: writing this re-partitions camera RAM and ERASES every "
                         "stored take. Omitted by default; refused while a take is stored.")
    ap.add_argument("--save-partition", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--file-type", default="SVV_RAWCINE")
    ap.add_argument("--use-case", choices=["view", "save"], default="save")
    ap.add_argument("--no-callback", action="store_true",
                    help="skip pyphantom's Python progress callback")
    ap.add_argument("--matrix", action="store_true",
                    help="run the full A/B set and print a comparison table")
    args = ap.parse_args()

    from pyphantom import Phantom

    ph = Phantom()
    cams = ph.discover(print_list=True)
    if not cams:
        raise SystemExit("no cameras discovered")

    entry = None
    for c in cams:
        if args.serial and int(c.serial) == args.serial:
            entry = c
    entry = entry or cams[0]
    cn = getattr(entry, "camera_number", getattr(entry, "cn", 0))
    cam = ph.Camera(cn)
    print(f"connected cn={cn} serial={entry.serial} ip={cam.ip_address} model={cam.model}")
    try:
        print(f"  has_10g={bool(cam.has_10g)} adapter={cam.get_selector_string(1097)!r} "
              f"10g_ip={cam.get_selector_string(1093)!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (link info unavailable: {exc})")

    states = cam.get_partition_state(-1)
    print("partition_count:", cam.partition_count)
    print("partition states:", states)

    # Re-partitioning wipes every stored take, even when writing the same value,
    # so it is opt-in and refused while anything is stored.
    if args.partitions is not None and int(cam.partition_count) != args.partitions:
        stored = [n for n, st in states
                  if getattr(st, "name", str(st)).lower() == "stored"]
        if stored:
            raise SystemExit(
                f"refusing --partitions {args.partitions}: partitions {stored} still hold "
                f"un-downloaded takes and re-partitioning would erase them.")
        cam.partition_count = args.partitions
        print("partition_count now:", cam.partition_count)

    if not (args.save_partition and args.out):
        print("\n(no --save-partition/--out: validation only, nothing transferred)")
        return

    _defender_note(args.out)
    base, ext = os.path.splitext(args.out)
    rows = []

    if args.matrix:
        # One variable at a time, against the same stored take. Baseline first.
        plan = [
            ("baseline: UC_VIEW, .cine, callback",      base + ".cine", UC_VIEW, True),
            ("UC_SAVE (the suspected fix)",             base + ".cine", UC_SAVE, True),
            ("UC_SAVE + .part name (what Glambot does)", base + ".part", UC_SAVE, True),
            ("UC_SAVE + no Python callback",           base + ".cine", UC_SAVE, False),
        ]
        for label, out, uc, cb in plan:
            print(f"\n=== {label} ===")
            row = _run_one(cam, args.save_partition, out, args.file_type, uc, cb)
            row["label"] = label
            rows.append(row)
            try:
                os.remove(out)
            except OSError:
                pass
    else:
        uc = UC_SAVE if args.use_case == "save" else UC_VIEW
        print(f"\n=== single run (use_case={args.use_case}, "
              f"callback={not args.no_callback}) ===")
        row = _run_one(cam, args.save_partition, args.out, args.file_type,
                       uc, not args.no_callback)
        row["label"] = f"use_case={args.use_case}"
        rows.append(row)

    print("\n" + "=" * 78)
    print(f"{'variant':<44}{'MB/s':>9}{'Gbps':>8}{'note':>17}")
    print("-" * 78)
    for r in rows:
        note = r["err"][:16] if r["err"] else ("uc ok" if r.get("applied") else "uc NOT set")
        print(f"{r['label']:<44}{(r['mb_s'] or 0):>9.0f}{(r['gbps'] or 0):>8.2f}{note:>17}")
    print("=" * 78)
    print("PCC reaches ~590 MB/s (4.7 Gbps) on this machine for the same clip.")
    print("Whichever row closes the gap is the fix to land in camera_bridge/bridge.py.")


if __name__ == "__main__":
    main()
