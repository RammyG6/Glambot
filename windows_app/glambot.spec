# PyInstaller spec for the standalone Windows app. Build with:
#   pyinstaller windows_app/glambot.spec
# (see build_installer.bat for the full build - this alone only produces
# dist/Glambot/, not the installer.)
#
# onedir (not onefile): starts faster and makes bundling the vendored
# ffprobe.exe straightforward. console=False: no cmd window - errors/logs go
# to glambot.log in the data folder instead (see glambot_launcher.py).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller into the spec's globals at exec time -
# it's already the directory containing this spec file (not the file path).
WINDOWS_APP_DIR = Path(SPECPATH).resolve()
REPO_ROOT = WINDOWS_APP_DIR.parent

block_cipher = None

# These packages are known PyInstaller pain points (they load data files /
# native DLLs dynamically at runtime rather than via plain imports) -
# collect_all pulls in their submodules, data, and binaries so nothing's
# silently missing from the frozen build. Verify by actually running the
# build (see windows_app build/verification steps) - gaps here only surface
# at runtime, not at build time.
datas = []
binaries = []
hiddenimports = []
for pkg in ("webview", "pystray", "googleapiclient", "google_auth_oauthlib", "google_auth_httplib2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "watchdog.observers.read_directory_changes",
    "PIL._tkinter_finder",
    "pyftpdlib",
    "pyftpdlib.handlers",
    "pyftpdlib.authorizers",
    "pyftpdlib.servers",
]

datas += [
    (str(REPO_ROOT / "templates"), "templates"),
    (str(REPO_ROOT / "static"), "static"),
    (str(REPO_ROOT / "logo"), "logo"),
    (str(WINDOWS_APP_DIR / "vendor" / "ffprobe.exe"), "vendor"),
]

# The Phantom camera bridge runs under its own embedded Python 3.11 (the
# pyphantom wheel's PhPy.pyd links python311.dll). Ship the whole folder -
# bridge.py plus camera_bridge/runtime/ (build that venv before PyInstaller).
# glambot/phantom_bridge_client.py resolves it relative to the repo root, i.e.
# next to the frozen exe. Skipped gracefully if runtime/ is absent.
_camera_bridge = REPO_ROOT / "camera_bridge"
if _camera_bridge.is_dir():
    datas += [(str(_camera_bridge), "camera_bridge")]

a = Analysis(
    [str(WINDOWS_APP_DIR / "glambot_launcher.py")],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Glambot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(WINDOWS_APP_DIR / "glambotlogo.ico") if (WINDOWS_APP_DIR / "glambotlogo.ico").exists() else None,
    # PyInstaller 6's default nests everything under an _internal/
    # subfolder; "." restores the flat "everything next to the .exe" layout
    # that glambot_launcher.py, app.py, and processor.py all assume
    # (datadir.txt, vendor/ffprobe.exe, logo/ resolved via sys.executable's
    # own parent directory). This has to be set here (EXE), not on COLLECT -
    # COLLECT just reads it back off the EXE object it's given.
    contents_directory=".",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Glambot",
)
