#!/usr/bin/env python3
"""
S3-Drive Sync Application
=========================

A cross-platform desktop application that syncs a local folder to S3-compatible storage.
Similar to Google Drive or OneDrive, but for any S3-compatible bucket.

Architecture:
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

Usage:
    python main.py              # Start the application
    python main.py --settings   # Open settings directly
    python main.py --help       # Show help

Author: S3Sync Team
License: MIT
"""

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path
from queue import Queue
from typing import Optional

# Configure logging before other imports
def setup_logging(log_file: Optional[Path] = None, debug: bool = False) -> None:
    """
    Configure application logging with console and optional file output.
    
    Uses RotatingFileHandler to prevent log files from growing too large.
    Sets third-party loggers to WARNING to reduce noise.
    """
    level = logging.DEBUG if debug else logging.INFO
    
    # More detailed format for better debugging
    log_format = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # Use RotatingFileHandler to prevent log files from growing too large
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5MB max per file
            backupCount=3,  # Keep 3 backup files
            encoding='utf-8'
        )
        file_handler.setFormatter(logging.Formatter(log_format, date_format))
        handlers.append(file_handler)
    
    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt=date_format,
        handlers=handlers,
    )
    
    # Reduce noise from third-party loggers
    logging.getLogger('boto3').setLevel(logging.WARNING)
    logging.getLogger('botocore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('PIL').setLevel(logging.WARNING)
    logging.getLogger('watchdog').setLevel(logging.INFO)


# Now import application modules
from config import get_config_manager, AppConfig
from models import AppState
from watcher import FileWatcher
from sync_engine import SyncEngine
from tray import TrayManager
from ui.settings import SettingsWindow


class S3SyncApp:
    """
    Main application class that orchestrates all components.
    
    Manages the lifecycle of:
    - File watcher (monitors local folder)
    - Sync engine (uploads to S3)
    - System tray (background operation)
    - Settings UI (configuration)
    """
    
    def __init__(self):
        self.config_manager = get_config_manager()
        self.config = self.config_manager.config
        
        # Shared state
        self.state = AppState()
        self.event_queue: Queue = Queue(maxsize=10000)
        
        # UI command queue for thread-safe tray -> tkinter communication
        self._ui_command_queue: Queue = Queue()
        
        # Components (initialized lazily)
        self._watcher: Optional[FileWatcher] = None
        self._sync_engine: Optional[SyncEngine] = None
        self._tray: Optional[TrayManager] = None
        self._settings_window: Optional[SettingsWindow] = None
        
        # Thread safety
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        
        # Setup logging
        log_file = self.config_manager.get_log_file()
        setup_logging(log_file)
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("=" * 60)
        self.logger.info("S3Sync application initialized")
        self.logger.debug(f"Python version: {sys.version}")
        self.logger.debug(f"Config directory: {self.config_manager.config_dir}")
        self.logger.debug(f"Log file: {log_file}")
    
    def _on_status_update(self, state: AppState) -> None:
        """Callback when sync status changes."""
        self.logger.debug(f"Status update: syncing={state.is_syncing}, pending={state.pending_count}, paused={state.is_paused}")
        if self._tray:
            self._tray.update()
        
        # Show notification for completed syncs
        if (self.config.show_notifications and 
            state.recent_files and 
            not state.is_syncing):
            recent = state.recent_files[0]
            if self._tray:
                self._tray.show_notification(
                    "File Synced",
                    f"✓ {recent.filename}"
                )
    
    def _on_settings_save(self, config: AppConfig) -> None:
        """Callback when settings are saved."""
        self.logger.info("Configuration updated from settings UI")
        self.logger.debug(f"New config: bucket={config.sync.bucket_name}, folder={config.sync.local_folder}")
        self.config = config
        
        # Update components with new config
        if self._watcher:
            self.logger.debug("Updating watcher with new config")
            self._watcher.update_config(config)
        
        if self._sync_engine:
            self.logger.debug("Updating sync engine with new config")
            self._sync_engine.update_config(config)
        
        # Restart services if they weren't running
        if config.is_configured() and not self.state.is_running:
            self.logger.info("Config now complete, starting services")
            self._start_services()
    
    def _on_settings_close(self) -> None:
        """Callback when settings window is closed."""
        self.logger.debug("Settings window closed")
        # If not configured and user closes settings, quit
        if not self.config.is_configured():
            self.logger.warning("Application not configured, exiting")
            self.quit()
    
    def _on_pause_resume(self) -> None:
        """Toggle pause/resume sync (thread-safe, can be called from tray thread)."""
        # Put command in queue - will be processed by main thread
        self._ui_command_queue.put(("pause_resume", None))
    
    def _do_pause_resume(self) -> None:
        """Actually toggle pause/resume (must be called from main thread)."""
        if self.state.is_paused:
            self.logger.info("Resuming sync operations")
            if self._sync_engine:
                self._sync_engine.resume()
            self.state.is_paused = False
            self.logger.debug("Sync resumed")
        else:
            self.logger.info("Pausing sync operations")
            if self._sync_engine:
                self._sync_engine.pause()
            self.state.is_paused = True
            self.logger.debug("Sync paused")
    
    def _open_settings(self) -> None:
        """Open the settings window (thread-safe, can be called from tray thread)."""
        # Put command in queue - will be processed by main thread
        self._ui_command_queue.put(("open_settings", None))
    
    def _do_open_settings(self) -> None:
        """Actually open settings (must be called from main thread)."""
        self.logger.debug("Opening settings window")
        if self._settings_window:
            self._settings_window.deiconify()
            self._settings_window.lift()
            self._settings_window.focus_force()
    
    def _process_ui_commands(self) -> None:
        """Process pending UI commands from tray thread (called from main thread)."""
        try:
            while True:
                cmd, data = self._ui_command_queue.get_nowait()
                if cmd == "open_settings":
                    self._do_open_settings()
                elif cmd == "pause_resume":
                    self._do_pause_resume()
                elif cmd == "quit":
                    self.quit()
                    return
        except:
            pass  # Queue is empty
        
        # Schedule next check
        if self._settings_window and not self._shutdown_event.is_set():
            self._settings_window.after(100, self._process_ui_commands)
    
    def _start_services(self) -> None:
        """Start the watcher and sync engine."""
        if not self.config.is_configured():
            self.logger.warning("Cannot start services: not configured")
            return
        
        self.logger.info("Starting sync services...")
        self.logger.debug(f"Watch folder: {self.config.sync.local_folder}")
        self.logger.debug(f"S3 bucket: {self.config.sync.bucket_name}")
        self.logger.debug(f"S3 prefix: {self.config.sync.s3_prefix or '(none)'}")
        self.logger.debug(f"Max workers: {self.config.sync.max_workers}")
        
        # Start sync engine
        self.logger.debug("Creating SyncEngine")
        self._sync_engine = SyncEngine(
            config=self.config,
            event_queue=self.event_queue,
            state=self.state,
            status_callback=self._on_status_update,
        )
        self._sync_engine.start()
        self.logger.debug("SyncEngine started")
        
        # Start file watcher
        self.logger.debug("Creating FileWatcher")
        self._watcher = FileWatcher(
            config=self.config,
            event_queue=self.event_queue,
        )
        if self._watcher.start():
            self.state.is_running = True
            self.logger.info("Sync services started successfully")
        else:
            self.logger.error("Failed to start file watcher")
    
    def _stop_services(self) -> None:
        """Stop the watcher and sync engine."""
        self.logger.info("Stopping sync services...")
        
        self.state.is_running = False
        
        if self._watcher:
            self.logger.debug("Stopping FileWatcher")
            self._watcher.stop()
            self._watcher = None
            self.logger.debug("FileWatcher stopped")
        
        if self._sync_engine:
            self.logger.debug("Stopping SyncEngine")
            self._sync_engine.stop()
            self._sync_engine = None
            self.logger.debug("SyncEngine stopped")
        
        self.logger.info("Sync services stopped")
    
    def quit(self) -> None:
        """Gracefully shutdown the application."""
        self.logger.info("=" * 60)
        self.logger.info("Shutting down S3Sync...")
        
        self.logger.debug("Setting shutdown event")
        self._shutdown_event.set()
        
        self._stop_services()
        
        if self._tray:
            self.logger.debug("Stopping system tray")
            self._tray.stop()
        
        if self._settings_window:
            self.logger.debug("Destroying settings window")
            self._settings_window.destroy()
        
        self.logger.info("S3Sync shutdown complete")
        self.logger.info("=" * 60)
        sys.exit(0)
    
    def run(self, start_minimized: bool = True, show_settings: bool = False) -> None:
        """
        Run the application.
        
        Args:
            start_minimized: Start minimized to system tray
            show_settings: Show settings window on startup
        """
        self.logger.info(f"Starting S3Sync application (minimized={start_minimized}, show_settings={show_settings})")
        
        # Setup signal handlers for graceful shutdown
        self.logger.debug("Setting up signal handlers")
        signal.signal(signal.SIGINT, lambda s, f: self.quit())
        signal.signal(signal.SIGTERM, lambda s, f: self.quit())
        
        # Create settings window (hidden initially)
        self.logger.debug("Creating settings window")
        self._settings_window = SettingsWindow(
            config_manager=self.config_manager,
            on_save=self._on_settings_save,
            on_close=self._on_settings_close,
        )
        
        # Create system tray
        self.logger.debug("Creating system tray manager")
        self._tray = TrayManager(
            state=self.state,
            on_settings=self._open_settings,
            on_pause_resume=self._on_pause_resume,
            on_quit=self.quit,
        )
        
        # Start tray in detached mode
        self.logger.debug("Starting system tray (detached mode)")
        self._tray.start(blocking=False)
        
        # Check if configured
        if not self.config.is_configured():
            self.logger.info("First run detected - configuration required")
            show_settings = True
        else:
            self.logger.debug(f"Configuration found: bucket={self.config.sync.bucket_name}, folder={self.config.sync.local_folder}")
            # Start sync services
            self._start_services()
        
        # Show settings if requested or if not configured
        if show_settings or not start_minimized:
            self.logger.debug("Showing settings window")
            self._settings_window.deiconify()
            self._settings_window.lift()
        else:
            self.logger.debug("Starting minimized to tray")
            self._settings_window.withdraw()
        
        # Start UI command processor (polls for tray commands)
        self.logger.debug("Starting UI command processor")
        self._settings_window.after(100, self._process_ui_commands)
        
        # Run the tkinter main loop
        self.logger.debug("Entering main event loop")
        try:
            self._settings_window.mainloop()
        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
            self.quit()


def main():
    """Application entry point."""
    parser = argparse.ArgumentParser(
        description="S3-Drive Sync - Sync local folders to S3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py              Start normally (minimized to tray)
  python main.py --settings   Open settings on startup
  python main.py --debug      Enable debug logging
        """
    )
    parser.add_argument(
        '--settings', '-s',
        action='store_true',
        help='Open settings window on startup'
    )
    parser.add_argument(
        '--debug', '-d',
        action='store_true',
        help='Enable debug logging'
    )
    parser.add_argument(
        '--no-tray',
        action='store_true',
        help='Run without system tray (settings only)'
    )
    
    args = parser.parse_args()
    
    # Enable debug logging if requested
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.debug("Debug logging enabled via command line")
    
    # Create and run application
    logging.info("Creating S3SyncApp instance")
    app = S3SyncApp()
    
    try:
        app.run(
            start_minimized=not args.settings,
            show_settings=args.settings,
        )
    except Exception as e:
        logging.error(f"Application error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logging.info("S3Sync application terminated")


if __name__ == "__main__":
    main()
