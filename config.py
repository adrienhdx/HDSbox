"""
Configuration management for S3-Drive Sync Application.
Handles secure credential storage via keyring and settings persistence.
"""

import json
import os
import stat
import fnmatch
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import keyring
from platformdirs import user_config_dir, user_log_dir

logger = logging.getLogger(__name__)

# Application identifiers
APP_NAME = "S3Sync"
APP_AUTHOR = "S3Sync"
KEYRING_SERVICE = "S3SyncApp"


@dataclass
class AWSCredentials:
    """AWS credentials - stored securely in system keyring."""
    access_key: str = ""
    secret_key: str = ""
    region: str = "us-east-1"
    endpoint_url: Optional[str] = None  # For S3-compatible storage (MinIO, etc.)
    
    def is_valid(self) -> bool:
        """Check if credentials appear to be set."""
        return bool(self.access_key and self.secret_key)


@dataclass
class SyncSettings:
    """Sync configuration settings - stored in config file."""
    bucket_name: str = ""
    local_folder: str = ""
    s3_prefix: str = ""  # Optional prefix for S3 keys
    debounce_seconds: float = 1.0
    max_workers: int = 10
    multipart_threshold_mb: int = 100
    multipart_chunksize_mb: int = 8
    max_retries: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 30.0


@dataclass
class ExclusionSettings:
    """File exclusion patterns - stored in config file."""
    patterns: list[str] = field(default_factory=lambda: [
        # Common OS files
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        "*.swp",
        "*.swo",
        "*~",
        # Version control
        ".git",
        ".git/**",
        ".svn",
        ".svn/**",
        ".hg",
        ".hg/**",
        # IDE/Editor
        ".idea",
        ".idea/**",
        ".vscode",
        ".vscode/**",
        "*.pyc",
        "__pycache__",
        "__pycache__/**",
        # Build artifacts
        "node_modules",
        "node_modules/**",
        ".env",
        "*.log",
        # Temporary files
        "*.tmp",
        "*.temp",
        "*.bak",
        # S3Sync trash folder (soft delete)
        ".corbeille",
        ".corbeille/**",
    ])
    
    def is_excluded(self, relative_path: str) -> bool:
        """Check if a path matches any exclusion pattern."""
        path_parts = Path(relative_path).parts
        
        for pattern in self.patterns:
            # Check the full path
            if fnmatch.fnmatch(relative_path, pattern):
                return True
            # Check filename only
            if fnmatch.fnmatch(Path(relative_path).name, pattern):
                return True
            # Check if any parent directory matches
            for part in path_parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
        return False
    
    def add_pattern(self, pattern: str) -> None:
        """Add an exclusion pattern."""
        if pattern not in self.patterns:
            self.patterns.append(pattern)
    
    def remove_pattern(self, pattern: str) -> None:
        """Remove an exclusion pattern."""
        if pattern in self.patterns:
            self.patterns.remove(pattern)


@dataclass
class AppConfig:
    """Complete application configuration."""
    aws: AWSCredentials = field(default_factory=AWSCredentials)
    sync: SyncSettings = field(default_factory=SyncSettings)
    exclusions: ExclusionSettings = field(default_factory=ExclusionSettings)
    
    # UI preferences
    start_minimized: bool = True
    show_notifications: bool = True
    dark_mode: bool = True
    
    def is_configured(self) -> bool:
        """Check if the app has minimum required configuration."""
        return (
            self.aws.is_valid() and 
            bool(self.sync.bucket_name) and 
            bool(self.sync.local_folder) and
            Path(self.sync.local_folder).is_dir()
        )


class ConfigManager:
    """
    Manages application configuration with secure credential storage.
    
    - AWS credentials are stored in the system keyring (Keychain on macOS,
      Credential Locker on Windows, Secret Service on Linux)
    - Other settings are stored in a JSON config file with restricted permissions
    """
    
    def __init__(self):
        self.config_dir = Path(user_config_dir(APP_NAME, APP_AUTHOR))
        self.config_file = self.config_dir / "config.json"
        self.log_dir = Path(user_log_dir(APP_NAME, APP_AUTHOR))
        self._config: Optional[AppConfig] = None
    
    @property
    def config(self) -> AppConfig:
        """Get the current configuration, loading if necessary."""
        if self._config is None:
            self._config = self.load()
        return self._config
    
    def _ensure_directories(self) -> None:
        """Create config and log directories if they don't exist."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Set restrictive permissions on config directory (Unix only)
        if os.name != 'nt':
            os.chmod(self.config_dir, stat.S_IRWXU)  # 700
    
    def load(self) -> AppConfig:
        """Load configuration from file and keyring."""
        config = AppConfig()
        logger.debug(f"Loading configuration from {self.config_file}")
        
        # Load settings from JSON file
        if self.config_file.exists():
            try:
                logger.debug("Config file exists, reading...")
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                
                logger.debug(f"Config sections found: {list(data.keys())}")
                
                # Load sync settings
                if 'sync' in data:
                    for key, value in data['sync'].items():
                        if hasattr(config.sync, key):
                            setattr(config.sync, key, value)
                    logger.debug(f"Loaded sync settings: bucket={config.sync.bucket_name}, folder={config.sync.local_folder}")
                
                # Load exclusion settings
                if 'exclusions' in data:
                    if 'patterns' in data['exclusions']:
                        config.exclusions.patterns = data['exclusions']['patterns']
                
                # Load AWS non-sensitive settings
                if 'aws' in data:
                    config.aws.region = data['aws'].get('region', 'us-east-1')
                    config.aws.endpoint_url = data['aws'].get('endpoint_url')
                
                # Load UI preferences
                config.start_minimized = data.get('start_minimized', True)
                config.show_notifications = data.get('show_notifications', True)
                config.dark_mode = data.get('dark_mode', True)
                
                logger.info(f"Configuration loaded from {self.config_file}")
                
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load config file: {e}")
        
        # Load credentials from keyring
        logger.debug("Attempting to load credentials from system keyring")
        try:
            access_key = keyring.get_password(KEYRING_SERVICE, "aws_access_key")
            secret_key = keyring.get_password(KEYRING_SERVICE, "aws_secret_key")
            if access_key:
                config.aws.access_key = access_key
                logger.debug("AWS access key loaded from keyring")
            else:
                logger.debug("No AWS access key found in keyring")
            if secret_key:
                config.aws.secret_key = secret_key
                logger.debug("AWS secret key loaded from keyring")
            else:
                logger.debug("No AWS secret key found in keyring")
        except Exception as e:
            logger.warning(f"Failed to load credentials from keyring: {e}", exc_info=True)
        
        self._config = config
        return config
    
    def save(self, config: Optional[AppConfig] = None) -> None:
        """Save configuration to file and keyring."""
        if config is None:
            config = self._config
        if config is None:
            raise ValueError("No configuration to save")
        
        logger.debug(f"Saving configuration to {self.config_file}")
        self._ensure_directories()
        
        # Prepare data for JSON (exclude sensitive credentials)
        data = {
            'sync': asdict(config.sync),
            'exclusions': {
                'patterns': config.exclusions.patterns
            },
            'aws': {
                'region': config.aws.region,
                'endpoint_url': config.aws.endpoint_url,
            },
            'start_minimized': config.start_minimized,
            'show_notifications': config.show_notifications,
            'dark_mode': config.dark_mode,
        }
        
        # Write config file
        try:
            with open(self.config_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug("Config file written successfully")
            
            # Set restrictive permissions (Unix only)
            if os.name != 'nt':
                os.chmod(self.config_file, stat.S_IRUSR | stat.S_IWUSR)  # 600
                logger.debug("Set restrictive permissions (600) on config file")
            
            logger.info(f"Configuration saved to {self.config_file}")
        except IOError as e:
            logger.error(f"Failed to save config file: {e}", exc_info=True)
            raise
        
        # Save credentials to keyring
        try:
            if config.aws.access_key:
                keyring.set_password(KEYRING_SERVICE, "aws_access_key", config.aws.access_key)
            if config.aws.secret_key:
                keyring.set_password(KEYRING_SERVICE, "aws_secret_key", config.aws.secret_key)
            logger.debug("Credentials saved to keyring")
        except Exception as e:
            logger.error(f"Failed to save credentials to keyring: {e}")
            raise
        
        self._config = config
    
    def delete_credentials(self) -> None:
        """Remove credentials from keyring."""
        try:
            keyring.delete_password(KEYRING_SERVICE, "aws_access_key")
            keyring.delete_password(KEYRING_SERVICE, "aws_secret_key")
            logger.info("Credentials deleted from keyring")
        except keyring.errors.PasswordDeleteError:
            pass  # Already deleted
    
    def get_log_file(self) -> Path:
        """Get the path to the log file."""
        self._ensure_directories()
        return self.log_dir / "s3sync.log"
    
    def get_recent_files_path(self) -> Path:
        """Get the path to the recent files cache."""
        return self.config_dir / "recent_files.json"
    
    def save_recent_files(self, recent_files: list) -> None:
        """Save recent files list to disk."""
        from models import RecentFile
        
        self._ensure_directories()
        recent_path = self.get_recent_files_path()
        
        try:
            data = [rf.to_dict() for rf in recent_files]
            with open(recent_path, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Saved {len(recent_files)} recent files to {recent_path}")
        except Exception as e:
            logger.error(f"Failed to save recent files: {e}")
    
    def load_recent_files(self) -> list:
        """Load recent files list from disk."""
        from models import RecentFile
        
        recent_path = self.get_recent_files_path()
        
        if not recent_path.exists():
            logger.debug("No recent files cache found")
            return []
        
        try:
            with open(recent_path, 'r') as f:
                data = json.load(f)
            
            recent_files = [RecentFile.from_dict(item) for item in data]
            logger.debug(f"Loaded {len(recent_files)} recent files from {recent_path}")
            return recent_files
        except Exception as e:
            logger.error(f"Failed to load recent files: {e}")
            return []
    
    def reset_to_defaults(self) -> AppConfig:
        """Reset configuration to defaults."""
        self.delete_credentials()
        if self.config_file.exists():
            self.config_file.unlink()
        self._config = AppConfig()
        return self._config


# Global config manager instance
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """Get the global config manager instance."""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def get_config() -> AppConfig:
    """Convenience function to get the current configuration."""
    return get_config_manager().config
