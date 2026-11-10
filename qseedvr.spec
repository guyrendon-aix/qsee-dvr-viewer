# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Q-See DVR dashboard.
#
#   pyinstaller qseedvr.spec --noconfirm --clean
#
# Linux / Windows -> dist/qseedvr[.exe]                 single-file console binary
# macOS           -> dist/QSee DVR Viewer.app           app bundle (CI wraps it in a .dmg)
#
# Never bundled: dvr_config.json. The app creates it on first run in the per-user data dir
# (see user_data_dir() in app/main.py), so no credentials ship inside a build.
import os
import re
import sys

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "QSee DVR Viewer"
VERSION = os.environ.get("APP_VERSION", "0.0.0").lstrip("v")
# macOS wants a purely numeric bundle version ("1.2.0"), so drop suffixes like "-dev.abc1234".
_m = re.match(r"\d+(\.\d+){0,2}", VERSION)
PLIST_VERSION = _m.group(0) if _m else "0.0.0"

# uvicorn picks its event loop / HTTP protocol / logging classes by import string at runtime,
# so static analysis can't see them.
hiddenimports = collect_submodules("uvicorn") + ["app.auth", "app.routers.playback"]

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=[("static", "static")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # GUI toolkits and dev tooling the server never uses; keeps the binary lean.
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    # onedir inside a .app (onefile .app bundles are deprecated in PyInstaller 6).
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="QSeeDVR",
        console=False,          # no Terminal window; logs go to ~/Library/Application Support/QSeeDVR/qseedvr.log
        argv_emulation=False,
        target_arch=None,       # native arch of the runner (arm64 or x86_64)
        codesign_identity=None,  # ad-hoc signed; CI re-seals the bundle
        entitlements_file=None,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="QSeeDVR")
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier="com.guyrendon.qseedvr",
        version=PLIST_VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": PLIST_VERSION,
            "CFBundleVersion": PLIST_VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # The app talks to the DVR on the local network.
            "NSLocalNetworkUsageDescription": "Connects to your DVR on the local network to show camera streams.",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="qseedvr",
        console=True,
        strip=False,  # stripping can corrupt bundled numpy/OpenCV shared libraries
        upx=False,
        runtime_tmpdir=None,
    )
