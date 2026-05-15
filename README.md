# S3-Drive Sync Application

A cross-platform desktop application that syncs a local folder to S3-compatible storage, similar to Google Drive or OneDrive.

## Features

- **Real-time Sync**: Monitors a local folder and automatically uploads changes to S3
- **System Tray**: Runs in the background with status indicator and quick actions
- **Modern UI**: Clean configuration dashboard built with CustomTkinter
- **S3-Compatible**: Works with AWS S3, MinIO, Wasabi, Backblaze B2, and more
- **Large File Support**: Automatic multipart uploads for files >100MB
- **Resilient**: Retry with exponential backoff on network failures
- **Secure**: AWS credentials stored in system keyring (Keychain/Credential Locker)
- **Flexible Exclusions**: Glob pattern support for ignoring files (.git, node_modules, etc.)

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   File Watcher  │────▶│   Event Queue    │────▶│   Sync Engine   │
│   (watchdog)    │     │  (thread-safe)   │     │ (ThreadPool)    │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
┌─────────────────┐     ┌──────────────────┐              │
│   System Tray   │◀────│   App State      │◀─────────────┘
│   (pystray)     │     │  (shared state)  │
└────────┬────────┘     └──────────────────┘
         │
┌────────▼────────┐
│  Settings UI    │
│ (customtkinter) │
└─────────────────┘
```

### Component Communication

1. **FileWatcher** (Producer): Monitors the local folder using `watchdog`, debounces rapid file events, and pushes `SyncEvent` objects to a thread-safe `Queue`.

2. **SyncEngine** (Consumer): Pulls events from the queue, uploads files using a `ThreadPoolExecutor` for parallelism, handles retries with exponential backoff, and updates the shared `AppState`.

3. **TrayManager**: Displays current status in the system tray, provides quick access to settings and recent files, updates dynamically when state changes.

4. **SettingsWindow**: CustomTkinter GUI for configuration, validates settings, stores credentials securely via `keyring`.

## Project Structure

```
S3Sync/
├── main.py              # Entry point, orchestration
├── watcher.py           # Watchdog file monitoring with debouncing
├── sync_engine.py       # ThreadPoolExecutor uploads, retry logic
├── s3_client.py         # Boto3 client wrapper, multipart config
├── tray.py              # Pystray system tray integration
├── config.py            # Config management (platformdirs + keyring)
├── models.py            # Dataclasses (SyncEvent, AppState, etc.)
├── ui/
│   ├── __init__.py
│   └── settings.py      # CustomTkinter settings dashboard
├── requirements.txt
└── README.md
```

## Installation

### Prerequisites

- Python 3.10 or higher
- pip (Python package manager)

### Setup

1. Clone or download this repository:
   ```bash
   cd /path/to/S3Sync
   ```

2. Create a virtual environment (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Platform-Specific Notes

**macOS**:
- System tray requires Python to be allowed in Security & Privacy > Accessibility
- Keychain is used for credential storage

**Windows**:
- Windows Credential Locker is used for credential storage
- May need to install Visual C++ Build Tools for some dependencies

**Linux**:
- Requires a system tray implementation (GNOME, KDE, etc.)
- Install `gnome-keyring` or `kwallet` for credential storage
- May need to increase inotify watch limit: `echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf`

## Usage

### Start the Application

```bash
# Normal start (minimized to system tray)
python main.py

# Open settings on startup
python main.py --settings

# Debug mode (verbose logging)
python main.py --debug
```

### First Run

On first launch, the Settings window will open. Configure:

1. **AWS Credentials**: Access Key ID and Secret Access Key
2. **Region**: AWS region (e.g., `us-east-1`)
3. **Bucket**: Your S3 bucket name
4. **Local Folder**: The folder to sync

Click "Test Connection" to verify your settings, then "Save Settings" to start syncing.

### System Tray Menu

- **Status**: Shows "Idle" or "Syncing..." with current file
- **Recent Files**: Last 10 successfully synced files
- **Pause Sync**: Temporarily stop syncing
- **Settings**: Open configuration dashboard
- **Quit**: Gracefully stop and exit

## Configuration

### AWS Credentials

Credentials are stored securely in your system's credential manager:
- **macOS**: Keychain
- **Windows**: Credential Locker  
- **Linux**: Secret Service (GNOME Keyring, KWallet)

### S3-Compatible Storage

To use with MinIO, Wasabi, or other S3-compatible storage, enter the Endpoint URL in settings:

```
https://play.min.io  # MinIO
https://s3.wasabisys.com  # Wasabi
https://s3.us-west-001.backblazeb2.com  # Backblaze B2
```

### Exclusion Patterns

Default exclusions include:
- `.DS_Store`, `Thumbs.db` (OS files)
- `.git/`, `.svn/` (version control)
- `node_modules/`, `__pycache__/` (dependencies/cache)
- `*.log`, `*.tmp`, `*.bak` (temporary files)

Add custom patterns in Settings using glob syntax:
- `*.mp4` - Exclude all MP4 files
- `secret/` - Exclude the "secret" folder
- `*.local.*` - Exclude files with ".local." in the name

### Config File Location

Non-sensitive settings are stored in:
- **macOS**: `~/Library/Application Support/S3Sync/config.json`
- **Windows**: `%APPDATA%\S3Sync\config.json`
- **Linux**: `~/.config/s3sync/config.json`

## Edge Cases Handled

### Network Interruption
- Automatic retry with exponential backoff (1s → 2s → 4s → ... up to 60s)
- Configurable max retries (default: 5)
- Failed uploads are logged with error details

### Duplicate Events
- File events are debounced with a configurable delay (default: 1 second)
- Rapid saves of the same file result in a single upload
- Pending uploads for the same file are cancelled when a new change is detected

### Large Files
- Files larger than 100MB (configurable) use S3 multipart upload
- 8MB chunks with up to 10 concurrent parts
- Progress tracking for large uploads

### Move/Rename Events
- Detected as a single event (not delete + create)
- File is re-uploaded to S3 with new key
- Old S3 key is preserved (one-way sync doesn't delete)

## Development

### Running Tests

```bash
pip install pytest pytest-cov
pytest tests/ --cov=.
```

### Type Checking

```bash
pip install mypy types-Pillow boto3-stubs[s3]
mypy .
```

### Code Formatting

```bash
pip install black isort
black .
isort .
```

## Building Standalone Applications

### macOS (.app bundle)

Build a native macOS application that runs in the menu bar (no Dock icon):

```bash
# Make the build script executable
chmod +x distribution/build_macos.sh distribution/create_icns.sh

# Optional: Create icon from PNG (1024x1024 recommended)
./distribution/create_icns.sh icon.png

# Build the application
./distribution/build_macos.sh
```

The resulting `S3Sync.app` will be in the `dist/` folder. Drag it to Applications to install.

**Note:** The app uses `LSUIElement: True` to hide from the Dock and appear only in the menu bar.

**Prerequisites:**
- Install tkinter for Homebrew Python: `brew install python-tk@3.14` (or your Python version)

### Windows (.exe)

Build a Windows executable:

```cmd
REM Build the application
distribution\build_windows.bat

REM Clean build directories
distribution\build_windows.bat --clean
```

The resulting executable will be in `dist\S3Sync\S3Sync.exe`.

### Build Requirements

| Platform | Package | Install |
|----------|---------|---------|
| macOS | PyInstaller | `pip install pyinstaller` |
| Windows | PyInstaller | `pip install pyinstaller` |

### Icons

- **macOS**: Place `icon.icns` in the project root, or use `distribution/create_icns.sh` to convert from PNG
- **Windows**: Place `icon.ico` in the project root (use online converters for PNG→ICO)
