"""Step 0 validation - run under Python 3.11 with pyphantom installed.

    py -3.11 -m venv .venv311
    .venv311\\Scripts\\activate
    pip install "C:\\Users\\ramza\\Documents\\Phantom\\PhSDK_3.12.77.806\\Python\\pyphantom-3.12.77.806-py311-none-any.whl"
    python camera_bridge\\spike.py --serial 25628 --partitions 4 --save-partition 1 --out D:\\GlambotAuto_Import\\spike.cine

Proves: discover -> connect -> set partitions -> read state -> save a stored
partition to a .cine, and reports whether Cine.save() works or throws (the
wheel marks it "# todo BUG this throws exception"). If it throws, the bridge
needs the ctypes/PhFile fallback.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import time


def _apply_save_use_case(cine) -> None:
    """PhSetUseCase(hC, UC_SAVE=2) via PhFile.Dll - pyphantom has no wrapper for it.
    A cine handle defaults to UC_VIEW (playback pipeline); UC_SAVE is the bulk
    camera->disk transfer path PCC uses."""
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
    h = ctypes.c_void_p(int(cine._cine_handle))
    hres = lib.PhSetUseCase(h, 2)
    cur = ctypes.c_int(-1)
    lib.PhGetUseCase(h, ctypes.byref(cur))
    print(f"  PhSetUseCase(UC_SAVE) hres={hres} use_case_now={cur.value}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", type=int)
    ap.add_argument("--ip")
    ap.add_argument("--partitions", type=int, default=4)
    ap.add_argument("--save-partition", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--file-type", default="SVV_RAWCINE")
    ap.add_argument("--use-case", choices=["view", "save"], default="save",
                    help="view = pyphantom default (UC_VIEW); save = PhSetUseCase(UC_SAVE) first")
    args = ap.parse_args()

    from pyphantom import Phantom, Cine, utils

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
              f"link_speed_mbps={cam.get_selector_int(1098)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (link info unavailable: {exc})")

    print("partition_count before:", cam.partition_count)
    if int(cam.partition_count) != args.partitions:
        # NB: writing this erases all stored cines - only when it truly changes.
        cam.partition_count = args.partitions
    print("partition_count after :", cam.partition_count)

    print("partition states:", cam.get_partition_state(-1))
    print("exp_index:", cam.exp_index, "frame_rate:", cam.frame_rate, "exposure:", cam.exposure)

    if args.save_partition and args.out:
        cine = Cine.from_camera(cam, args.save_partition)
        if args.use_case == "save":
            _apply_save_use_case(cine)
        cine.save_name = args.out
        cine.save_type = utils.FileTypeEnum[args.file_type]
        print(f"saving partition {args.save_partition} -> {args.out} "
              f"({args.file_type}, use_case={args.use_case})")
        t0 = time.time()
        try:
            cine.save_non_blocking()
            while True:
                pct = cine.save_percentage
                print(f"  {pct}%")
                if pct >= 100:
                    break
                time.sleep(0.5)
            elapsed = time.time() - t0
            try:
                import os
                size = os.path.getsize(args.out)
                mbps = size / 1e6 / elapsed if elapsed else 0
                print(f"OK {size/1e6:.0f} MB in {elapsed:.1f}s = {mbps:.0f} MB/s "
                      f"({mbps*8/1000:.2f} Gbps)")
            except OSError:
                print(f"OK save finished in {elapsed:.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED pyphantom save threw: {exc!r}")
            print(" -> bridge must use the ctypes/PhFile.Dll fallback")
        finally:
            try:
                cine.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
