# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

WINDOWS_DIR = Path(SPECPATH).parent
datas = []
binaries = []
hiddenimports = []
for package in ("cryptography", "segno"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

datas += [
    (str(WINDOWS_DIR / "index.html"), "."),
    (str(WINDOWS_DIR / "desktop.css"), "."),
    (str(WINDOWS_DIR / "desktop.js"), "."),
]

import os
if os.environ.get('SENDTOCOPYQ_BUILD_TEST') == '1':
    datas.append((str(WINDOWS_DIR / 'packaging' / 'test-data-profile.json'), '.'))

a = Analysis(
    [str(WINDOWS_DIR / "receiver.py")],
    pathex=[str(WINDOWS_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SendToCopyQ-Receiver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SendToCopyQ-Receiver",
)
