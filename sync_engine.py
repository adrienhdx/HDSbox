"""
Sync engine for S3-Drive Sync Application.
Handles queued upload operations with ThreadPoolExecutor.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Callable, Optional

from models import (
    SyncEvent,
    SyncResult,
    SyncStatus,
    EventType,
    RecentFile,
    AppState,
    format_file_size,
)
from config import AppConfig
from s3_client import S3ClientWrapper, get_s3_client

logger = logging.getLogger(__name__)


class SyncEngine:
    """
    Manages the sync queue and coordinates uploads to S3.
    
    Architecture:
    - Consumes SyncEvents from a thread-safe queue (populated by FileWatcher)
    - Uses ThreadPoolExecutor for parallel uploads
    - Reports status updates via callback to UI layer
    - Implements retry with exponential backoff for failed uploads
    """
    
    def __init__(
        self,
        config: AppConfig,
        event_queue: Queue,
        state: AppState,
        status_callback: Optional[Callable[[AppState], None]] = None,
    ):
        self.config = config
        self.event_queue = event_queue
        self.state = state
        self.status_callback = status_callback
        
        self._s3_client: Optional[S3ClientWrapper] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        self._active_futures: dict[str, Future] = {}
        self._futures_lock = threading.Lock()
        self._is_running = False
    
    @property
    def is_running(self) -> bool:
        """Check if the engine is currently running."""
        return self._is_running
    
    def _get_s3_client(self) -> S3ClientWrapper:
        """Get or create the S3 client."""
        if self._s3_client is None:
            self._s3_client = get_s3_client(self.config)
        return self._s3_client
    
    def _notify_status(self) -> None:
        """Notify the UI of a status change."""
        if self.status_callback:
            try:
                self.status_callback(self.state)
            except Exception as e:
                logger.error(f"Status callback error: {e}")
    
    def start(self) -> None:
        """Start the sync engine."""
        if self._is_running:
            logger.warning("Sync engine is already running")
            return
        
        logger.info("Starting sync engine...")
        
        # Create thread pool for uploads
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.sync.max_workers,
            thread_name_prefix="S3Upload"
        )
        
        # Start consumer thread
        self._shutdown.clear()
        self._consumer_thread = threading.Thread(
            target=self._consume_events,
            daemon=True,
            name="SyncConsumer"
        )
        self._consumer_thread.start()
        
        self._is_running = True
        logger.info(f"Sync engine started with {self.config.sync.max_workers} workers")
    
    def stop(self) -> None:
        """Stop the sync engine gracefully."""
        if not self._is_running:
            return
        
        logger.info("Stopping sync engine...")
        
        # Signal shutdown
        self._shutdown.set()
        
        # Wait for consumer thread
        if self._consumer_thread:
            self._consumer_thread.join(timeout=5.0)
        
        # Shutdown executor (wait for pending uploads)
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None
        
        self._is_running = False
        logger.info("Sync engine stopped")
    
    def _consume_events(self) -> None:
        """Consumer loop that processes events from the queue."""
        logger.debug("Consumer thread started, waiting for events...")
        while not self._shutdown.is_set():
            try:
                # Wait while paused (check every 0.5s)
                while self.state.is_paused and not self._shutdown.is_set():
                    time.sleep(0.5)
                    continue
                
                if self._shutdown.is_set():
                    break
                
                # Get event with timeout to allow shutdown/pause check
                event = self.event_queue.get(timeout=0.5)
                
                # Double-check pause state after getting event
                if self.state.is_paused:
                    # Put event back in queue for later
                    self.event_queue.put(event)
                    logger.debug(f"Sync paused, re-queued event: {event.src_path}")
                    continue
                
                logger.debug(f"Dequeued event: {event.event_type.value} for {event.src_path}")
                self._handle_event(event)
                self.event_queue.task_done()
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing event: {e}", exc_info=True)
    
    def _handle_event(self, event: SyncEvent) -> None:
        """Handle a sync event by submitting it to the thread pool."""
        logger.debug(f"Handling sync event: {event.event_type.value} for {event.src_path}")
        
        # Update state
        self.state.is_syncing = True
        self.state.pending_count += 1
        self._notify_status()
        
        # Submit upload task
        path_key = str(event.src_path)
        
        # Cancel any pending upload for the same file
        with self._futures_lock:
            if path_key in self._active_futures:
                logger.debug(f"Cancelling pending upload for: {path_key}")
                self._active_futures[path_key].cancel()
        
        # Choose the right handler based on event type
        if event.event_type == EventType.MOVED and event.dest_path:
            # For rename/move: try to rename in S3, fall back to upload if needed
            logger.debug(f"Submitting rename task to thread pool: {event.dest_path} -> {path_key}")
            future = self._executor.submit(self._rename_file, event)
        else:
            # For create/modify: upload the file
            logger.debug(f"Submitting upload task to thread pool: {path_key}")
            future = self._executor.submit(self._upload_file, event)
        
        with self._futures_lock:
            self._active_futures[path_key] = future
            logger.debug(f"Active uploads count: {len(self._active_futures)}")
        
        # Add completion callback
        future.add_done_callback(lambda f: self._on_upload_complete(path_key, f))
    
    def _rename_file(self, event: SyncEvent) -> SyncResult:
        """
        Rename a file in S3 (copy + delete), or upload if rename fails.
        
        This runs in a thread pool worker.
        """
        s3_client = self._get_s3_client()
        local_path = Path(self.config.sync.local_folder) / event.src_path
        new_s3_key = str(event.src_path).replace("\\", "/")
        old_s3_key = str(event.dest_path).replace("\\", "/") if event.dest_path else None
        
        # Update current file in state
        self.state.current_file = event.src_path.name
        self._notify_status()
        
        start_time = time.time()
        
        # Check if the new file exists locally
        if not local_path.exists():
            logger.warning(f"File no longer exists, skipping: {local_path}")
            return SyncResult(
                event=event,
                status=SyncStatus.FAILED,
                s3_key=new_s3_key,
                error_message="File no longer exists"
            )
        
        file_size = local_path.stat().st_size
        
        # Try to rename in S3
        if old_s3_key:
            logger.info(f"Renaming in S3: {old_s3_key} -> {new_s3_key}")
            success, message = s3_client.rename_object(old_s3_key, new_s3_key)
            
            if success:
                upload_time = time.time() - start_time
                logger.info(f"Renamed: {old_s3_key} -> {new_s3_key} in {upload_time:.2f}s")
                return SyncResult(
                    event=event,
                    status=SyncStatus.COMPLETED,
                    s3_key=new_s3_key,
                    file_size=file_size,
                    upload_time=upload_time,
                )
            else:
                # Rename failed (probably old file doesn't exist in S3)
                # Fall back to uploading the file
                logger.info(f"Rename failed ({message}), uploading file instead: {new_s3_key}")
        
        # Fall back to regular upload
        return self._upload_file(event)
    
    def _upload_file(self, event: SyncEvent) -> SyncResult:
        """
        Upload a file to S3 with retry logic.
        
        This runs in a thread pool worker.
        """
        s3_client = self._get_s3_client()
        local_path = Path(self.config.sync.local_folder) / event.src_path
        s3_key = str(event.src_path).replace("\\", "/")  # Normalize for S3
        
        # Update current file in state
        self.state.current_file = event.src_path.name
        self._notify_status()
        
        start_time = time.time()
        
        # Check if file still exists
        if not local_path.exists():
            logger.warning(f"File no longer exists, skipping: {local_path}")
            return SyncResult(
                event=event,
                status=SyncStatus.FAILED,
                s3_key=s3_key,
                error_message="File no longer exists"
            )
        
        file_size = local_path.stat().st_size
        logger.debug(f"Starting upload: {local_path.name} ({format_file_size(file_size)}) -> s3://{s3_key}")
        
        # Retry loop with exponential backoff
        while event.can_retry():
            try:
                success, message = s3_client.upload_file(
                    local_path=local_path,
                    s3_key=s3_key,
                    progress_callback=self._on_upload_progress,
                )
                
                if success:
                    upload_time = time.time() - start_time
                    return SyncResult(
                        event=event,
                        status=SyncStatus.COMPLETED,
                        s3_key=s3_key,
                        file_size=file_size,
                        upload_time=upload_time,
                    )
                else:
                    raise Exception(message)
                    
            except Exception as e:
                event.retry_count += 1
                
                if event.can_retry():
                    # Exponential backoff with jitter
                    delay = min(
                        self.config.sync.retry_base_delay * (2 ** event.retry_count),
                        self.config.sync.retry_max_delay
                    )
                    logger.warning(
                        f"Upload failed for {event.src_path}, "
                        f"retrying in {delay:.1f}s (attempt {event.retry_count}/{event.max_retries}): {e}"
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"Upload failed permanently for {event.src_path}: {e}")
                    return SyncResult(
                        event=event,
                        status=SyncStatus.FAILED,
                        s3_key=s3_key,
                        file_size=file_size,
                        error_message=str(e),
                    )
        
        # Should not reach here, but handle it
        return SyncResult(
            event=event,
            status=SyncStatus.FAILED,
            s3_key=s3_key,
            error_message="Max retries exceeded"
        )
    
    def _on_upload_progress(self, filename: str, bytes_sent: int, total_bytes: int) -> None:
        """Callback for upload progress updates."""
        # Could be used to update a progress bar in the UI
        percent = (bytes_sent / total_bytes * 100) if total_bytes > 0 else 100
        logger.debug(f"Upload progress: {filename} - {percent:.1f}%")
    
    def _on_upload_complete(self, path_key: str, future: Future) -> None:
        """Callback when an upload completes (success or failure)."""
        logger.debug(f"Upload complete callback for: {path_key}")
        
        # Remove from active futures
        with self._futures_lock:
            self._active_futures.pop(path_key, None)
            logger.debug(f"Remaining active uploads: {len(self._active_futures)}")
        
        # Update pending count
        self.state.pending_count = max(0, self.state.pending_count - 1)
        
        try:
            result = future.result()
            
            if result.status == SyncStatus.COMPLETED:
                # Add to recent files
                recent = RecentFile(
                    filename=result.event.src_path.name,
                    s3_key=result.s3_key,
                    synced_at=result.completed_at,
                    file_size=result.file_size,
                    local_path=str(result.event.src_path),
                )
                self.state.add_recent_file(recent)
                self.state.last_error = None
                
                logger.info(
                    f"Synced: {result.event.src_path} "
                    f"({format_file_size(result.file_size)}) "
                    f"in {result.upload_time:.2f}s"
                )
            else:
                self.state.last_error = result.error_message
                logger.error(f"Sync failed: {result.event.src_path} - {result.error_message}")
                
        except Exception as e:
            self.state.last_error = str(e)
            logger.error(f"Upload error: {e}")
        
        # Update syncing state
        with self._futures_lock:
            has_active = len(self._active_futures) > 0
        
        self.state.is_syncing = has_active or self.state.pending_count > 0
        self.state.current_file = None
        self._notify_status()
    
    def update_config(self, config: AppConfig) -> None:
        """Update configuration."""
        self.config = config
        if self._s3_client:
            self._s3_client.reinitialize(config)
    
    def pause(self) -> None:
        """Pause syncing (finish current uploads, don't start new ones)."""
        self.state.is_paused = True
        self._notify_status()
        logger.info("Sync paused")
    
    def resume(self) -> None:
        """Resume syncing."""
        self.state.is_paused = False
        self._notify_status()
        logger.info("Sync resumed")
    
    def get_queue_size(self) -> int:
        """Get the current number of items in the queue."""
        return self.event_queue.qsize()
    
    def clear_queue(self) -> int:
        """Clear all pending items from the queue. Returns count of cleared items."""
        cleared = 0
        while True:
            try:
                self.event_queue.get_nowait()
                cleared += 1
            except Empty:
                break
        
        self.state.pending_count = 0
        self._notify_status()
        logger.info(f"Cleared {cleared} items from sync queue")
        return cleared
