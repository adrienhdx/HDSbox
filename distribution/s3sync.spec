# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Windows build.

Usage:
    python -m PyInstaller s3sync.spec

The resulting application will be in dist/S3Sync/
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Project root is the parent directory
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPECPATH)))
os.chdir(PROJECT_ROOT)  # Change to project root for PyInstaller

block_cipher = None

# Collect customtkinter data files (themes, assets)
ctk_datas = collect_data_files("customtkinter")

# Application metadata
APP_NAME = "S3Sync"
VERSION = "1.0.0"

# Check for icon file
ICON_FILE = "icon.ico"
if not os.path.exists(ICON_FILE):
    ICON_FILE = None
    print("Warning: icon.ico not found. App will use default icon.")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=ctk_datas + [
        # Add any additional data files here
        # ("source_path", "dest_path"),
    ],
    hiddenimports=[
        # UI modules
        "ui",
        "ui.settings",
        # Keyring backends for Windows
        "keyring.backends.Windows",
        "win32ctypes.pywin32",
        "win32ctypes.pywin32.pywintypes",
        "win32ctypes.core",
        # boto3/botocore internals
        "botocore.vendored.requests.packages.urllib3",
        # pystray backend for Windows
        "pystray._win32",
        # PIL plugins
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter.test",
        "unittest",
        "pydoc",
        "doctest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window - GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE,
    version_info=None,  # Can add version info via version.txt file
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)
