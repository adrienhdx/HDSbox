"""
S3 Scanner for bidirectional sync.
Scans S3 and local folder at startup to detect differences and queue sync events.
Maintains sync state in SQLite database.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from config import AppConfig, get_config_manager
from models import SyncEvent, EventType
from s3_client import S3ClientWrapper, get_s3_client

logger = logging.getLogger(__name__)


class SyncStateDB:
    """
    SQLite database for tracking sync state.
    Stores last-synced ETag and mtime for each file.
    """
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize the database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                relative_path TEXT PRIMARY KEY,
                s3_etag TEXT,
                s3_size INTEGER,
                s3_last_modified TEXT,
                local_mtime REAL,
                local_size INTEGER,
                last_synced TEXT
            )
        """)
        self._conn.commit()
        logger.debug(f"Sync state database initialized at {self.db_path}")
    
    def get_state(self, relative_path: str) -> Optional[dict]:
        """Get sync state for a file."""
        cursor = self._conn.execute(
            "SELECT * FROM sync_state WHERE relative_path = ?",
            (relative_path,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def update_state(
        self,
        relative_path: str,
        s3_etag: Optional[str] = None,
        s3_size: Optional[int] = None,
        s3_last_modified: Optional[str] = None,
        local_mtime: Optional[float] = None,
        local_size: Optional[int] = None,
    ) -> None:
        """Update or insert sync state for a file."""
        now = datetime.now().isoformat()
        
        self._conn.execute("""
            INSERT INTO sync_state 
                (relative_path, s3_etag, s3_size, s3_last_modified, local_mtime, local_size, last_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                s3_etag = COALESCE(excluded.s3_etag, s3_etag),
                s3_size = COALESCE(excluded.s3_size, s3_size),
                s3_last_modified = COALESCE(excluded.s3_last_modified, s3_last_modified),
                local_mtime = COALESCE(excluded.local_mtime, local_mtime),
                local_size = COALESCE(excluded.local_size, local_size),
                last_synced = excluded.last_synced
        """, (relative_path, s3_etag, s3_size, s3_last_modified, local_mtime, local_size, now))
        self._conn.commit()
    
    def delete_state(self, relative_path: str) -> None:
        """Remove sync state for a file (when deleted from both sides)."""
        self._conn.execute(
            "DELETE FROM sync_state WHERE relative_path = ?",
            (relative_path,)
        )
        self._conn.commit()
    
    def get_files_in_directory(self, dir_prefix: str) -> list[str]:
        """
        Get all files with a given directory prefix.
        Used when a directory is deleted to find all files to trash.
        """
        # Ensure prefix ends with /
        if not dir_prefix.endswith('/'):
            dir_prefix = dir_prefix + '/'
        
        cursor = self._conn.execute(
            "SELECT relative_path FROM sync_state WHERE relative_path LIKE ?",
            (dir_prefix + '%',)
        )
        return [row[0] for row in cursor.fetchall()]
    
    def delete_states_in_directory(self, dir_prefix: str) -> int:
        """
        Delete all sync states for files in a directory.
        Returns the number of deleted records.
        """
        if not dir_prefix.endswith('/'):
            dir_prefix = dir_prefix + '/'
        
        cursor = self._conn.execute(
            "DELETE FROM sync_state WHERE relative_path LIKE ?",
            (dir_prefix + '%',)
        )
        self._conn.commit()
        return cursor.rowcount
    
    def get_all_states(self) -> list[dict]:
        """Get all sync states."""
        cursor = self._conn.execute("SELECT * FROM sync_state")
        return [dict(row) for row in cursor.fetchall()]
    
    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None


class S3Scanner:
    """
    Scans S3 and local folder to detect sync differences.
    
    Used at startup for bidirectional sync:
    - Files in S3 but not local -> queue download
    - Files local but not in S3 -> queue upload
    - Files differ -> compare timestamps/size to decide direction
    """
    
    def __init__(
        self,
        config: AppConfig,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ):
        self.config = config
        self.progress_callback = progress_callback
        
        # Initialize sync state database
        config_manager = get_config_manager()
        db_path = config_manager.config_dir / "sync_state.db"
        self.state_db = SyncStateDB(db_path)
        
        self._s3_client: Optional[S3ClientWrapper] = None
    
    @property
    def s3_client(self) -> S3ClientWrapper:
        """Get or create S3 client."""
        if self._s3_client is None:
            self._s3_client = get_s3_client(self.config)
        return self._s3_client
    
    def scan(self) -> tuple[list[SyncEvent], list[SyncEvent]]:
        """
        Scan S3 and local folder, return events to sync.
        
        Returns:
            Tuple of (download_events, upload_events)
        """
        logger.info("Starting S3/local scan for bidirectional sync...")
        
        local_folder = Path(self.config.sync.local_folder)
        if not local_folder.exists():
            logger.warning(f"Local folder does not exist: {local_folder}")
            local_folder.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created local folder: {local_folder}")
        
        # Get S3 objects (excluding .corbeille/)
        s3_objects = self._get_s3_objects()
        logger.info(f"Found {len(s3_objects)} objects in S3")
        
        # Get local files
        local_files = self._get_local_files()
        logger.info(f"Found {len(local_files)} local files")
        
        # Compare and generate events
        download_events = []
        upload_events = []
        
        s3_keys = set(s3_objects.keys())
        local_paths = set(local_files.keys())
        
        total_files = len(s3_keys | local_paths)
        processed = 0
        
        # Files only in S3 -> download
        for key in s3_keys - local_paths:
            obj = s3_objects[key]
            event = SyncEvent(
                event_type=EventType.DOWNLOAD,
                src_path=Path(key),
                s3_etag=obj['etag'],
                s3_size=obj['size'],
            )
            download_events.append(event)
            logger.debug(f"Queue download (S3 only): {key}")
            
            processed += 1
            self._report_progress("Scanning", processed, total_files)
        
        # Files only local -> upload
        for path in local_paths - s3_keys:
            local_info = local_files[path]
            event = SyncEvent(
                event_type=EventType.CREATED,
                src_path=Path(path),
            )
            upload_events.append(event)
            logger.debug(f"Queue upload (local only): {path}")
            
            processed += 1
            self._report_progress("Scanning", processed, total_files)
        
        # Files in both -> check if sync needed
        for key in s3_keys & local_paths:
            s3_obj = s3_objects[key]
            local_info = local_files[key]
            
            # Get stored sync state
            state = self.state_db.get_state(key)
            
            if state is None:
                # First time seeing this file - compare sizes
                # If sizes match, assume in sync; otherwise prefer S3 (authoritative)
                if s3_obj['size'] != local_info['size']:
                    event = SyncEvent(
                        event_type=EventType.DOWNLOAD,
                        src_path=Path(key),
                        s3_etag=s3_obj['etag'],
                        s3_size=s3_obj['size'],
                    )
                    download_events.append(event)
                    logger.debug(f"Queue download (size mismatch, no state): {key}")
                else:
                    # Sizes match, record state and skip
                    self.state_db.update_state(
                        key,
                        s3_etag=s3_obj['etag'],
                        s3_size=s3_obj['size'],
                        s3_last_modified=s3_obj['last_modified'].isoformat(),
                        local_mtime=local_info['mtime'],
                        local_size=local_info['size'],
                    )
                    logger.debug(f"In sync (sizes match): {key}")
            else:
                # Have previous state - check for changes
                s3_changed = s3_obj['etag'] != state['s3_etag']
                local_changed = abs(local_info['mtime'] - (state['local_mtime'] or 0)) > 1
                
                if s3_changed and not local_changed:
                    # S3 updated, local unchanged -> download
                    event = SyncEvent(
                        event_type=EventType.DOWNLOAD,
                        src_path=Path(key),
                        s3_etag=s3_obj['etag'],
                        s3_size=s3_obj['size'],
                    )
                    download_events.append(event)
                    logger.debug(f"Queue download (S3 changed): {key}")
                elif local_changed and not s3_changed:
                    # Local updated, S3 unchanged -> upload
                    event = SyncEvent(
                        event_type=EventType.MODIFIED,
                        src_path=Path(key),
                    )
                    upload_events.append(event)
                    logger.debug(f"Queue upload (local changed): {key}")
                elif s3_changed and local_changed:
                    # Both changed - conflict! Prefer S3 (more recent wins)
                    # TODO: Could implement smarter conflict resolution
                    event = SyncEvent(
                        event_type=EventType.DOWNLOAD,
                        src_path=Path(key),
                        s3_etag=s3_obj['etag'],
                        s3_size=s3_obj['size'],
                    )
                    download_events.append(event)
                    logger.warning(f"Conflict (both changed), preferring S3: {key}")
                # else: neither changed, skip
            
            processed += 1
            self._report_progress("Scanning", processed, total_files)
        
        logger.info(f"Scan complete: {len(download_events)} downloads, {len(upload_events)} uploads")
        return download_events, upload_events
    
    def _get_s3_objects(self) -> dict[str, dict]:
        """
        Get all S3 objects, excluding .corbeille/ folder.
        
        Returns:
            Dict mapping relative path -> object metadata
        """
        objects = self.s3_client.list_objects()
        result = {}
        
        prefix = self.config.sync.s3_prefix.rstrip('/') + '/' if self.config.sync.s3_prefix else ''
        prefix_len = len(prefix)
        
        for obj in objects:
            key = obj['key']
            
            # Remove prefix if present
            if prefix and key.startswith(prefix):
                relative_key = key[prefix_len:]
            else:
                relative_key = key
            
            # Skip .corbeille/ folder
            if relative_key.startswith('.corbeille/'):
                continue
            
            # Skip empty keys (folder markers)
            if not relative_key or relative_key.endswith('/'):
                continue
            
            # Check exclusions
            if self.config.exclusions.is_excluded(relative_key):
                continue
            
            result[relative_key] = {
                'key': relative_key,
                'size': obj['size'],
                'last_modified': obj['last_modified'],
                'etag': obj['etag'].strip('"'),  # Remove quotes from ETag
            }
        
        return result
    
    def _get_local_files(self) -> dict[str, dict]:
        """
        Get all local files with metadata.
        
        Returns:
            Dict mapping relative path -> file metadata
        """
        local_folder = Path(self.config.sync.local_folder)
        result = {}
        
        for root, dirs, files in os.walk(local_folder):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if not self.config.exclusions.is_excluded(d)]
            
            for filename in files:
                file_path = Path(root) / filename
                relative_path = str(file_path.relative_to(local_folder))
                
                # Use forward slashes for consistency
                relative_path = relative_path.replace('\\', '/')
                
                # Check exclusions
                if self.config.exclusions.is_excluded(relative_path):
                    continue
                
                try:
                    stat = file_path.stat()
                    result[relative_path] = {
                        'path': relative_path,
                        'size': stat.st_size,
                        'mtime': stat.st_mtime,
                    }
                except OSError as e:
                    logger.warning(f"Could not stat file {file_path}: {e}")
        
        return result
    
    def _report_progress(self, stage: str, current: int, total: int) -> None:
        """Report scan progress."""
        if self.progress_callback and total > 0:
            self.progress_callback(stage, current, total)
    
    def update_sync_state_after_upload(
        self,
        relative_path: str,
        local_path: Path,
        s3_etag: Optional[str] = None,
    ) -> None:
        """Update sync state after successful upload."""
        try:
            stat = local_path.stat()
            self.state_db.update_state(
                relative_path,
                s3_etag=s3_etag,
                s3_size=stat.st_size,
                local_mtime=stat.st_mtime,
                local_size=stat.st_size,
            )
        except OSError as e:
            logger.warning(f"Could not update sync state for {relative_path}: {e}")
    
    def update_sync_state_after_download(
        self,
        relative_path: str,
        local_path: Path,
        s3_etag: str,
        s3_size: int,
    ) -> None:
        """Update sync state after successful download."""
        try:
            stat = local_path.stat()
            self.state_db.update_state(
                relative_path,
                s3_etag=s3_etag,
                s3_size=s3_size,
                local_mtime=stat.st_mtime,
                local_size=stat.st_size,
            )
        except OSError as e:
            logger.warning(f"Could not update sync state for {relative_path}: {e}")
    
    def remove_sync_state(self, relative_path: str) -> None:
        """Remove sync state when file is deleted from both sides."""
        self.state_db.delete_state(relative_path)
    
    def get_files_in_directory(self, dir_prefix: str) -> list[str]:
        """
        Get all tracked files within a directory.
        Used when a directory is deleted to find all files to move to trash.
        """
        return self.state_db.get_files_in_directory(dir_prefix)
    
    def delete_states_in_directory(self, dir_prefix: str) -> int:
        """
        Delete all sync states for files in a directory.
        Called after directory deletion events are queued.
        """
        return self.state_db.delete_states_in_directory(dir_prefix)
    
    def close(self) -> None:
        """Close resources."""
        self.state_db.close()
