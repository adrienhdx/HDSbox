# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for macOS build.

Usage:
    cd /path/to/S3Sync
    python -m PyInstaller distribution/s3sync_macos.spec

The resulting application will be in dist/S3Sync.app/
"""

import os
import sys
import plistlib
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH is the directory containing the spec file
# Project root is the parent directory (S3Sync folder)
SPEC_DIR = SPECPATH  # This is already the directory, not the file path
PROJECT_ROOT = os.path.dirname(SPEC_DIR)

block_cipher = None

# Collect customtkinter data files (themes, assets)
ctk_datas = collect_data_files("customtkinter")

# Application metadata
APP_NAME = "S3Sync"
BUNDLE_ID = "com.s3sync.app"
VERSION = "1.0.0"

# Check for icon file
ICON_FILE = os.path.join(PROJECT_ROOT, "icon.icns")
if not os.path.exists(ICON_FILE):
    ICON_FILE = None
    print("Warning: icon.icns not found. App will use default icon.")

# Main script path
MAIN_SCRIPT = os.path.join(PROJECT_ROOT, "main.py")

a = Analysis(
    [MAIN_SCRIPT],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=ctk_datas + [
        # Add any additional data files here
        # ("source_path", "dest_path"),
    ],
    hiddenimports=[
        # UI modules
        "ui",
        "ui.settings",
        # Keyring backends for macOS
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        # boto3/botocore internals
        "botocore.vendored.requests.packages.urllib3",
        # pystray backend for macOS
        "pystray._darwin",
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
        # Exclude Windows-specific modules
        "win32ctypes",
        "pystray._win32",
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
    upx=False,  # UPX not typically used on macOS
    console=False,  # No console window - GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# Create macOS .app bundle
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=ICON_FILE,
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "LSMinimumSystemVersion": "10.13.0",
        # This is the key setting - hides app from Dock (string "1" like OneDrive uses)
        "LSUIElement": "1",
        # Other useful settings
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # Support dark mode
    },
)
