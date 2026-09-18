# PyInstaller spec for the standalone macOS app. Build with:
#   pyinstaller mac_app/glambot.spec
# (see build_app.sh for the full build - this alone produces dist/Glambot.app
# but skips the icon/version bookkeeping build_app.sh does first.)
#
# Sibling to windows_app/glambot.spec - same datas/hiddenimports collection
# logic, entry point swapped for mac_app/glambot_launcher.py, and a BUNDLE()
# step at the end to produce a real .app instead of a raw onedir folder.
# Both specs' app code (glambot/, templates/, static/, looks/) is 100%
# shared - only the packaging wrapper differs. See launcher_core.py and the
# repo-root VERSION file, which both platforms build from.
#
# onedir (not onefile): starts faster, and mirrors windows_app's choice.
# console=False: no Terminal window - errors/logs go to glambot.log in the
# data folder instead (see launcher_core.py).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller into the spec's globals at exec time -
# it's already the directory containing this spec file (not the file path).
MAC_APP_DIR = Path(SPECPATH).resolve()
REPO_ROOT = MAC_APP_DIR.parent

VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()

block_cipher = None

# These packages are known PyInstaller pain points (they load data files /
# native DLLs dynamically at runtime rather than via plain imports) -
# collect_all pulls in their submodules, data, and binaries so nothing's
# silently missing from the frozen build. Verify by actually running the
# build (see mac_app build/verification steps) - gaps here only surface
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
    "watchdog.observers.fsevents",
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
    # Fitted Phantom colour LUTs. Without these every .cine render silently
    # falls back to the reconstructed chain, which measures ~10% off the SDK.
    # (Kept even though the Phantom camera bridge itself isn't bundled on
    # Mac - see below - so a .cine already downloaded some other way, or a
    # future Mac-compatible bridge, still renders with the right colour.)
    (str(REPO_ROOT / "looks"), "looks"),
]

_vendor_ffprobe = MAC_APP_DIR / "vendor" / "ffprobe"
if _vendor_ffprobe.exists():
    binaries += [(str(_vendor_ffprobe), "vendor")]

# The Phantom camera bridge (camera_bridge/) is a hard dependency on Vision
# Research's Windows-only SDK (PhPy.pyd/PhFile.Dll via ctypes.WinDLL) - see
# the plan this build was written from. There's nothing to bundle here on
# Mac; glambot/phantom_bridge_client.py already degrades gracefully
# (available: False) when camera_bridge/ is absent, so the rest of the app
# works normally with that one feature unavailable.

a = Analysis(
    [str(MAC_APP_DIR / "glambot_launcher.py")],
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
    icon=str(MAC_APP_DIR / "glambotlogo.icns") if (MAC_APP_DIR / "glambotlogo.icns").exists() else None,
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

app = BUNDLE(
    coll,
    name="Glambot.app",
    icon=str(MAC_APP_DIR / "glambotlogo.icns") if (MAC_APP_DIR / "glambotlogo.icns").exists() else None,
    bundle_identifier="com.g6moco.glambot",
    version=VERSION,
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        # Runs a local Flask server (loopback, and LAN if BIND_HOST=0.0.0.0
        # is set) plus the FTP-import server - both need normal outbound/
        # inbound networking, which needs no special Info.plist entitlement
        # for an unsandboxed app, but LSApplicationCategoryType keeps this
        # out of the (irrelevant) sandboxed-Mac-App-Store review path.
        "LSApplicationCategoryType": "public.app-category.photography",
    },
)
