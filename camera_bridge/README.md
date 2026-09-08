# camera_bridge

The Phantom SDK's `pyphantom` wheel ships a `PhPy.pyd` linked against
`python311.dll`. Glambot runs on Python 3.12 and cannot import it in-process,
so camera control lives in this **separate Python 3.11 subprocess**.

`glambot/phantom_bridge_client.py` spawns `bridge.py` and talks to it with
newline-delimited JSON on stdin/stdout (protocol documented at the top of
`bridge.py`).

## Runtime layout

```
camera_bridge/
  bridge.py          the JSON-RPC server (this repo)
  spike.py           validation + transfer benchmark (this repo)
  runtime/           embedded CPython 3.11 + pyphantom  (NOT in git; built at package time)
    python.exe
    Lib/site-packages/pyphantom/...
```

`phantom_bridge_client.py` looks for an interpreter in this order:

1. `$GLAMBOT_PY311` (explicit override)
2. `camera_bridge/runtime/python.exe` (packaged app)
3. `py -3.11` / `python3.11` on PATH (dev machines)

## Dev setup

```
py -3.11 -m venv camera_bridge/runtime      # or any venv
camera_bridge/runtime/Scripts/pip install \
  "C:/Users/ramza/Documents/Phantom/PhSDK_3.12.77.806/Python/pyphantom-3.12.77.806-py311-none-any.whl"
```

## Transfer speed

PCC pulls a clip off this camera at roughly **590 MB/s (4.7 Gbps)** on the show
PC; Glambot has trailed that. The transfer itself happens entirely inside
`PhFile.Dll` (`PhDoCine(SaveNonBlocking)`), so there is no buffer size or socket
option on our side to tune - only these levers:

| Lever | Where |
|---|---|
| `PhSetUseCase(hC, UC_SAVE)` - the bulk camera→disk pipeline. A cine handle defaults to `UC_VIEW`, tuned for interactive playback. pyphantom has no wrapper and never calls it. | `bridge.py::_set_save_use_case` |
| Not registering pyphantom's **Python** progress callback, which the SDK's save thread invokes and which takes the GIL each time (PCC's is native). | `bridge.py::save_cine` |
| The destination filename. The importer writes `.<name>.part` then renames, so the watcher never sees a half file - but AV exclusions are written by extension and would not cover `.part`. | `phantom_import.py::_download_partition` |
| Antivirus. On-access scanning of a multi-GB file mid-write throttles a 10G transfer on its own. | Defender exclusion for the download folder |

`spike.py --matrix` runs one pull per lever against the same stored take and
prints a comparison table, so the numbers decide rather than guesswork:

```
camera_bridge/runtime/Scripts/python.exe camera_bridge/spike.py \
  --serial 25628 --save-partition 1 --out D:\GlambotAuto_Import\ab.cine --matrix
```

Run it with a take actually stored on the camera (`spike.py --serial N` on its
own is read-only and prints the partition states). `--partitions` is **opt-in
and refused while a take is stored**: writing `PartitionsCount` re-partitions
camera RAM and erases every stored cine, even when writing the same value.

**Confirmed 2026-09-08** on the show PC, so these are no longer suspects:
the SDK reports `has_10g=True`, `adapter='Ethernet 7'` (Marvell AQtion, linked
at 10 Gbps) and `10g_ip='172.16.37.56'` - the same address the importer connects
to, so the data path is genuinely 10G. A whole idle control cycle
(connect + `get_state` + disconnect) measures **under 100 ms**, so camera reads
are not what makes the record badge lag.

## Packaging

`windows_app/glambot.spec` bundles `camera_bridge/` (including `runtime/`) as
data files. Build the `runtime/` venv before running PyInstaller. No inbound
firewall rule is needed - the bridge dials out to the camera on TCP 7115.
