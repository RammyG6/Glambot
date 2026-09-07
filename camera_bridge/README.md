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
  spike.py           Step 0 validation script (this repo)
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

## Packaging

`windows_app/glambot.spec` bundles `camera_bridge/` (including `runtime/`) as
data files. Build the `runtime/` venv before running PyInstaller. No inbound
firewall rule is needed - the bridge dials out to the camera on TCP 7115.
