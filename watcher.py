"""
File system watcher for S3-Drive Sync Application.
Monitors a local directory for changes using watchdog with debouncing.
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileDeletedEvent,
    DirCreatedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    DirDeletedEvent,
)

from models import SyncEvent, EventType
from config import AppConfig, ExclusionSettings

logger = logging.getLogger(__name__)


class DebouncedEventHandler(FileSystemEventHandler):
    """
    File system event handler with debouncing to prevent duplicate events.
    
    Many applications (especially editors) generate multiple events when
    saving a file. This handler uses a time-based debounce to consolidate
    rapid successive events on the same file.
    """
    
    def __init__(
        self,
        event_queue: Queue,
        watch_folder: Path,
        exclusions: ExclusionSettings,
        debounce_seconds: float = 1.0,
    ):
        super().__init__()
        self.event_queue = event_queue
        self.watch_folder = watch_folder
        self.exclusions = exclusions
        self.debounce_seconds = debounce_seconds
        
        # Track last event time per file for debouncing
        self._last_events: dict[str, datetime] = defaultdict(lambda: datetime.min)
        self._pending_events: dict[str, SyncEvent] = {}
        self._lock = threading.Lock()
        
        # Start debounce processor thread
        self._shutdown = threading.Event()
        self._processor_thread = threading.Thread(
            target=self._process_pending_events,
            daemon=True,
            name="DebounceProcessor"
        )
        self._processor_thread.start()
        logger.debug(f"DebouncedEventHandler initialized: watch_folder={watch_folder}, debounce={debounce_seconds}s")
    
    def stop(self):
        """Stop the debounce processor thread."""
        self._shutdown.set()
        self._processor_thread.join(timeout=2.0)
    
    def _get_relative_path(self, abs_path: str) -> str:
        """Convert absolute path to path relative to watch folder."""
        try:
            return str(Path(abs_path).relative_to(self.watch_folder))
        except ValueError:
            return abs_path
    
    def _should_ignore(self, path: str) -> bool:
        """Check if the path should be ignored."""
        rel_path = self._get_relative_path(path)
        
        # Check exclusion patterns
        if self.exclusions.is_excluded(rel_path):
            logger.debug(f"Ignoring excluded path: {rel_path}")
            return True
        
        # Ignore hidden files (starting with .)
        if Path(path).name.startswith('.'):
            logger.debug(f"Ignoring hidden file: {rel_path}")
            return True
        
        return False
    
    def _should_debounce(self, path: str) -> bool:
        """Check if the event should be debounced (skipped)."""
        now = datetime.now()
        with self._lock:
            last_time = self._last_events[path]
            if (now - last_time).total_seconds() < self.debounce_seconds:
                return True
            self._last_events[path] = now
        return False
    
    def _schedule_event(self, event: SyncEvent) -> None:
        """Schedule an event for processing after debounce period."""
        path_key = str(event.src_path)
        with self._lock:
            # Store/replace pending event for this path
            if path_key in self._pending_events:
                logger.debug(f"Replacing pending event for: {path_key}")
            self._pending_events[path_key] = event
            self._last_events[path_key] = datetime.now()
            logger.debug(f"Scheduled {event.event_type.value} event: {path_key} (pending: {len(self._pending_events)})")
    
    def _process_pending_events(self) -> None:
        """Background thread that processes debounced events."""
        while not self._shutdown.is_set():
            time.sleep(0.1)  # Check every 100ms
            
            now = datetime.now()
            threshold = timedelta(seconds=self.debounce_seconds)
            events_to_process = []
            
            with self._lock:
                paths_to_remove = []
                for path, event in self._pending_events.items():
                    last_time = self._last_events.get(path, datetime.min)
                    if now - last_time >= threshold:
                        events_to_process.append(event)
                        paths_to_remove.append(path)
                
                for path in paths_to_remove:
                    del self._pending_events[path]
            
            # Queue events outside the lock
            for event in events_to_process:
                logger.info(f"Queueing {event.event_type.value} event: {event.src_path}")
                self.event_queue.put(event)
    
    def on_created(self, event):
        """Handle file creation events."""
        if isinstance(event, DirCreatedEvent):
            logger.debug(f"Ignoring directory creation: {event.src_path}")
            return  # Ignore directory creation
        
        logger.debug(f"File created: {event.src_path}")
        if self._should_ignore(event.src_path):
            return
        
        rel_path = self._get_relative_path(event.src_path)
        sync_event = SyncEvent(
            event_type=EventType.CREATED,
            src_path=Path(rel_path),
        )
        self._schedule_event(sync_event)
    
    def on_modified(self, event):
        """Handle file modification events."""
        if isinstance(event, DirModifiedEvent):
            return  # Ignore directory modification
        
        logger.debug(f"File modified: {event.src_path}")
        if self._should_ignore(event.src_path):
            return
        
        # Skip if file doesn't exist (might be a transient state)
        if not Path(event.src_path).exists():
            logger.debug(f"File no longer exists, skipping: {event.src_path}")
            return
        
        rel_path = self._get_relative_path(event.src_path)
        sync_event = SyncEvent(
            event_type=EventType.MODIFIED,
            src_path=Path(rel_path),
        )
        self._schedule_event(sync_event)
    
    def on_moved(self, event):
        """Handle file move/rename events."""
        if isinstance(event, DirMovedEvent):
            logger.debug(f"Ignoring directory move: {event.src_path} -> {event.dest_path}")
            return  # Ignore directory moves for now
        
        logger.debug(f"File moved: {event.src_path} -> {event.dest_path}")
        src_ignored = self._should_ignore(event.src_path)
        dest_ignored = self._should_ignore(event.dest_path)
        
        if src_ignored and dest_ignored:
            return
        
        if src_ignored:
            # Moved from ignored to watched - treat as creation
            rel_path = self._get_relative_path(event.dest_path)
            sync_event = SyncEvent(
                event_type=EventType.CREATED,
                src_path=Path(rel_path),
            )
        elif dest_ignored:
            # Moved from watched to ignored - treat as deletion
            # (Not syncing deletes in one-way mode, but log it)
            logger.info(f"File moved to excluded location: {event.src_path}")
            return
        else:
            # Normal move within watched folder
            src_rel = self._get_relative_path(event.src_path)
            dest_rel = self._get_relative_path(event.dest_path)
            sync_event = SyncEvent(
                event_type=EventType.MOVED,
                src_path=Path(dest_rel),  # Use destination as the file to sync
                dest_path=Path(src_rel),  # Store original location
            )
        
        self._schedule_event(sync_event)
    
    def on_deleted(self, event):
        """Handle file deletion events."""
        if isinstance(event, DirDeletedEvent):
            logger.debug(f"Ignoring directory deletion: {event.src_path}")
            return  # Ignore directory deletion
        
        logger.debug(f"File deleted: {event.src_path}")
        if self._should_ignore(event.src_path):
            return
        
        # In one-way sync mode, we don't propagate deletes to S3
        # Just log and skip (design allows for future bidirectional sync)
        rel_path = self._get_relative_path(event.src_path)
        logger.info(f"File deleted (not syncing in one-way mode): {rel_path}")
        
        # NOTE: We intentionally do NOT clear pending events here.
        # Applications like Excel/Word use "safe save" which:
        # 1. Write to temp file
        # 2. Delete original  <-- we're here
        # 3. Rename temp to original (triggers create/modify)
        # If we cleared pending events, the final create would be lost.


class FileWatcher:
    """
    Manages file system watching for a configured folder.
    
    Uses watchdog's Observer with the DebouncedEventHandler to monitor
    for file changes and queue them for syncing.
    """
    
    def __init__(self, config: AppConfig, event_queue: Queue):
        self.config = config
        self.event_queue = event_queue
        self._observer: Optional[Observer] = None
        self._handler: Optional[DebouncedEventHandler] = None
        self._is_running = False
    
    @property
    def is_running(self) -> bool:
        """Check if the watcher is currently running."""
        return self._is_running
    
    @property
    def watch_folder(self) -> Path:
        """Get the configured watch folder path."""
        return Path(self.config.sync.local_folder)
    
    def start(self) -> bool:
        """
        Start watching the configured folder.
        
        Returns:
            True if started successfully, False otherwise
        """
        if self._is_running:
            logger.warning("Watcher is already running")
            return True
        
        watch_path = self.watch_folder
        logger.debug(f"Attempting to start file watcher for: {watch_path}")
        
        if not watch_path.exists():
            logger.error(f"Watch folder does not exist: {watch_path}")
            return False
        
        if not watch_path.is_dir():
            logger.error(f"Watch path is not a directory: {watch_path}")
            return False
        
        try:
            logger.debug(f"Creating DebouncedEventHandler with {self.config.sync.debounce_seconds}s debounce")
            # Create event handler with debouncing
            self._handler = DebouncedEventHandler(
                event_queue=self.event_queue,
                watch_folder=watch_path,
                exclusions=self.config.exclusions,
                debounce_seconds=self.config.sync.debounce_seconds,
            )
            
            # Create and configure observer
            logger.debug("Creating watchdog Observer")
            self._observer = Observer()
            self._observer.schedule(
                self._handler,
                str(watch_path),
                recursive=True,
            )
            
            # Start watching
            self._observer.start()
            self._is_running = True
            
            logger.info(f"Started watching folder: {watch_path} (recursive=True)")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start file watcher: {e}", exc_info=True)
            self._cleanup()
            return False
    
    def stop(self) -> None:
        """Stop watching the folder."""
        if not self._is_running:
            return
        
        logger.info("Stopping file watcher...")
        self._cleanup()
        logger.info("File watcher stopped")
    
    def _cleanup(self) -> None:
        """Clean up observer and handler."""
        if self._handler:
            self._handler.stop()
            self._handler = None
        
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
        
        self._is_running = False
    
    def restart(self) -> bool:
        """Restart the watcher (e.g., after config change)."""
        self.stop()
        return self.start()
    
    def update_config(self, config: AppConfig) -> None:
        """Update configuration and restart if needed."""
        old_folder = self.config.sync.local_folder
        self.config = config
        
        if self._is_running and old_folder != config.sync.local_folder:
            logger.info("Watch folder changed, restarting watcher...")
            self.restart()
