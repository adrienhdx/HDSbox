"""
Data models for S3-Drive Sync Application.
Defines dataclasses for events, status tracking, and application state.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from pathlib import Path


class EventType(Enum):
    """Types of file system events that trigger sync."""
    CREATED = "created"
    MODIFIED = "modified"
    MOVED = "moved"
    DELETED = "deleted"
    # Bidirectional sync events
    DOWNLOAD = "download"       # S3 -> local
    LOCAL_DELETE = "local_delete"  # Local file deleted -> move to S3 Corbeille


class SyncDirection(Enum):
    """Direction of sync operation."""
    LOCAL_TO_S3 = "upload"
    S3_TO_LOCAL = "download"


class SyncStatus(Enum):
    """Status of a sync operation."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class SyncEvent:
    """Represents a file system event that needs to be synced."""
    event_type: EventType
    src_path: Path
    dest_path: Optional[Path] = None  # For move events
    timestamp: datetime = field(default_factory=datetime.now)
    retry_count: int = 0
    max_retries: int = 5
    # For download events: store S3 metadata
    s3_etag: Optional[str] = None
    s3_size: Optional[int] = None
    
    @property
    def s3_key(self) -> str:
        """Generate S3 key from the source path relative to watch folder."""
        return str(self.src_path)
    
    def can_retry(self) -> bool:
        """Check if the event can be retried."""
        return self.retry_count < self.max_retries


@dataclass
class SyncResult:
    """Result of a sync operation."""
    event: SyncEvent
    status: SyncStatus
    s3_key: str
    file_size: int = 0
    upload_time: float = 0.0
    error_message: Optional[str] = None
    completed_at: datetime = field(default_factory=datetime.now)


@dataclass
class RecentFile:
    """Represents a recently synced file for display in tray menu."""
    filename: str
    s3_key: str
    synced_at: datetime
    file_size: int
    local_path: str = ""  # Full path to open/reveal the file
    
    def display_name(self) -> str:
        """Format for display in tray menu."""
        size_str = format_file_size(self.file_size)
        return f"{self.filename} ({size_str})"
    
    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage."""
        return {
            'filename': self.filename,
            's3_key': self.s3_key,
            'synced_at': self.synced_at.isoformat(),
            'file_size': self.file_size,
            'local_path': self.local_path,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'RecentFile':
        """Deserialize from dictionary."""
        return cls(
            filename=data['filename'],
            s3_key=data['s3_key'],
            synced_at=datetime.fromisoformat(data['synced_at']),
            file_size=data['file_size'],
            local_path=data.get('local_path', ''),
        )


@dataclass 
class AppState:
    """
    Global application state shared between components.
    Thread-safe access should be managed by the caller.
    """
    is_syncing: bool = False
    is_paused: bool = False
    is_running: bool = True
    pending_count: int = 0
    current_file: Optional[str] = None
    recent_files: list[RecentFile] = field(default_factory=list)
    max_recent_files: int = 10
    last_error: Optional[str] = None
    
    def add_recent_file(self, recent: RecentFile, old_filename: Optional[str] = None) -> None:
        """
        Add a file to the recent files list, maintaining max size.
        
        Args:
            recent: The new recent file entry
            old_filename: If this is a rename, the old filename to remove
        """
        # If this is a rename, remove the old filename entry
        if old_filename:
            self.recent_files = [
                rf for rf in self.recent_files 
                if rf.filename != old_filename
            ]
        
        # Also remove any existing entry with the same filename (update case)
        self.recent_files = [
            rf for rf in self.recent_files 
            if rf.filename != recent.filename
        ]
        
        self.recent_files.insert(0, recent)
        if len(self.recent_files) > self.max_recent_files:
            self.recent_files = self.recent_files[:self.max_recent_files]
    
    @property
    def status_text(self) -> str:
        """Get human-readable status text for tray display."""
        if self.is_paused:
            return "⏸ Paused"
        if self.is_syncing:
            if self.current_file:
                return f"⬆ Syncing: {self.current_file}"
            return f"⬆ Syncing ({self.pending_count} pending)"
        return "✓ Idle"


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    size = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
