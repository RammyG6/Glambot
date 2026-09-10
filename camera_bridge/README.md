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

**Measured 2026-09-08** on the show PC, camera 25628 (VEO 4K 990S), one stored
take of 956 frames = 10573 MB, via `spike.py --matrix`:

| Variant | MB/s | Gbps |
|---|---:|---:|
| UC_VIEW, `.cine`, progress callback (pyphantom's default) | 528 | 4.23 |
| UC_SAVE | **542** | **4.33** |
| UC_SAVE + `.part` filename (what the importer writes) | 542 | 4.33 |
| UC_SAVE, no Python progress callback | 511 | 4.09 |

### The variable that actually mattered: `file_type`

**Read this before re-investigating transfer speed.** Every row above used
`SVV_RAWCINE`, because that is `spike.py`'s default. Meanwhile the app was
running with `"file_type": "SVV_CINE"` saved in
`<inbox>/.glambot/phantom_import.json`, and pulled at roughly **544 Mbps** -
about 8x slower, minutes instead of seconds, on an idle machine. The table
above measured the default rather than the configuration in use, and briefly
led to the wrong conclusion that no gap existed.

- `SVV_RAWCINE` - raw packed cine, a near-straight copy of the camera's 10-bit
  packed Bayer data. Sustains ~4.3 Gbps. **Use this.**
- `SVV_CINE` / `SVV_TIFCINE` - the SDK demosaics and processes every frame on
  this PC. Far more bytes and far more CPU, so the link idles waiting on
  processing and the Ethernet graph reads a fraction of line rate.

The non-raw formats are also wrong for the render pipeline:
`effects.build_cine_source_filter` reproduces the camera's own processing - black
reference, colour matrix, tone curve - on the assumption the data is unprocessed
raw, so processed footage gets all of that double-applied. Without a `.look.json`
sidecar it falls back to the legacy `wbgain` + 2.2 gamma approximation, and if the
`wbgain` tags are missing too it silently loses both the colour fix and the BT.709
output tagging.

So: **when comparing against PCC, match the format on both sides**, and pass
`spike.py --file-type` to measure whatever the app is actually configured for.
Every download now logs its format, resolution, frame count and bits/pixel
(raw packed lands on exactly 10.0), which is the quickest way to spot this.

Secondary conclusions from the table, still valid:

- `PhSetUseCase(UC_SAVE)` is worth about **+3%**. Kept
  (`bridge.py::_set_save_use_case`, readback verified `=2`) because it is free
  and it is the documented call, but it was never the big lever it was assumed
  to be.
- The **`.part` filename costs nothing** - byte-identical throughput. The
  antivirus-by-extension theory is dead; no exclusion is needed for it.
- **Dropping the Python progress callback does not help** (511 vs 542). Do not
  bother bypassing `save_non_blocking()` for a native progress poll; the
  callback is not on the critical path, and it is what drives the UI.
- `D:` sequential write measured 1026 MB/s over 2 GB, so raw pulls are not
  disk-bound either.

`spike.py --serial N` on its own is read-only and prints the partition states.
`--partitions` is **opt-in and refused while a take is stored**: writing
`PartitionsCount` re-partitions camera RAM and erases every stored cine, even
when writing the same value.

Also confirmed on this machine, so no longer suspects: the SDK reports
`has_10g=True`, `adapter='Ethernet 7'` (Marvell AQtion, linked at 10 Gbps) and
`10g_ip='172.16.37.56'` - the same address the importer connects to, so the data
path is genuinely 10G. A whole idle control cycle (connect + `get_state` +
disconnect) measures **under 100 ms**, so camera reads are not what makes the
record badge lag.

## Cancelling a download

Not via `PhStopWriteCineFileAsync`. PhFile.Dll exports it and it looks like the
obvious call, but there is no Phantom header here to check its signature
against, and calling it on a live save coincided with the bridge process dying
mid-transfer (which then stranded a multi-GB `.part`, because the cleanup ran
while the dying process still held the handle).

The supported route is **in-band**: the SDK's save progress callback aborts the
transfer when it returns 0. pyphantom's `_default_progress_callback` always
returns 1, so `bridge.py::save_cine` installs its own and starts the save with
`phDoCine(utils._phantom_keys._SaveNonBlocking, handle)` rather than
`cine.save_non_blocking()`. Two things to know if you touch this:

- **Keep a reference to the callback** (it lives on the job record). The native
  side holds a raw pointer to it. pyphantom passes its own without storing it.
- `cine.save_percentage` is only updated by pyphantom's callback, so once you
  replace it, progress has to come from your own.

Measured on camera 25628: cancelling 4s into an 18.7 GB pull returned in
**0.1s**, the file stopped growing, and the bridge process was unaffected.
`phantom_import.cancel_active_save` still keeps a backstop - if the SDK hasn't
taken the abort within 3s it deliberately stops the bridge, which is guaranteed
to end the write and release the handle.

## Packaging

`windows_app/glambot.spec` bundles `camera_bridge/` (including `runtime/`) as
data files. Build the `runtime/` venv before running PyInstaller. No inbound
firewall rule is needed - the bridge dials out to the camera on TCP 7115.
