"""
S3 Client wrapper for S3-Drive Sync Application.
Handles boto3 client initialization, multipart uploads, and retry configuration.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

from config import AppConfig

logger = logging.getLogger(__name__)


class UploadProgress:
    """
    Thread-safe upload progress tracker.
    Can be used as a callback for boto3 uploads.
    """
    
    def __init__(
        self,
        filename: str,
        file_size: int,
        callback: Optional[Callable[[str, int, int], None]] = None
    ):
        self.filename = filename
        self.file_size = file_size
        self.callback = callback
        self._seen_bytes = 0
        self._lock = threading.Lock()
    
    def __call__(self, bytes_amount: int) -> None:
        """Called by boto3 during upload."""
        with self._lock:
            self._seen_bytes += bytes_amount
            if self.callback:
                self.callback(self.filename, self._seen_bytes, self.file_size)
    
    @property
    def progress_percent(self) -> float:
        """Get current progress as percentage."""
        with self._lock:
            if self.file_size == 0:
                return 100.0
            return (self._seen_bytes / self.file_size) * 100


class S3ClientWrapper:
    """
    Thread-safe wrapper around boto3 S3 client.
    
    Features:
    - Automatic multipart upload for large files
    - Configurable retry with exponential backoff
    - Progress tracking for uploads
    - Support for S3-compatible endpoints (MinIO, etc.)
    """
    
    def __init__(self, config: AppConfig):
        self.config = config
        self._client = None
        self._transfer_config = None
        self._lock = threading.Lock()
    
    def _create_client(self):
        """Create and configure the boto3 S3 client."""
        aws = self.config.aws
        sync = self.config.sync
        
        logger.debug(f"Creating S3 client - Region: {aws.region}, Endpoint: {aws.endpoint_url or 'AWS default'}")
        logger.debug(f"Retry config: max_attempts={sync.max_retries}, mode=standard")
        logger.debug(f"Multipart config: threshold={sync.multipart_threshold_mb}MB, chunksize={sync.multipart_chunksize_mb}MB")
        
        # Configure retry behavior
        boto_config = BotoConfig(
            retries={
                'total_max_attempts': sync.max_retries,
                'mode': 'standard'  # Exponential backoff
            },
            connect_timeout=10,
            read_timeout=30,
        )
        
        # Build client kwargs
        client_kwargs = {
            'service_name': 's3',
            'aws_access_key_id': aws.access_key,
            'aws_secret_access_key': aws.secret_key,
            'region_name': aws.region,
            'config': boto_config,
        }
        
        # Add custom endpoint for S3-compatible storage
        if aws.endpoint_url:
            client_kwargs['endpoint_url'] = aws.endpoint_url
        
        self._client = boto3.client(**client_kwargs)
        
        # Configure multipart upload settings
        self._transfer_config = TransferConfig(
            multipart_threshold=sync.multipart_threshold_mb * 1024 * 1024,
            max_concurrency=10,
            multipart_chunksize=sync.multipart_chunksize_mb * 1024 * 1024,
            use_threads=True,
        )
        
        logger.info(f"S3 client initialized for region {aws.region}")
        if aws.endpoint_url:
            logger.info(f"Using custom endpoint: {aws.endpoint_url}")
    
    @property
    def client(self):
        """Get the boto3 client, creating it if necessary."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._create_client()
        return self._client
    
    @property
    def transfer_config(self) -> TransferConfig:
        """Get the transfer configuration."""
        if self._transfer_config is None:
            with self._lock:
                if self._transfer_config is None:
                    self._create_client()
        return self._transfer_config
    
    def reinitialize(self, config: AppConfig) -> None:
        """Reinitialize the client with new configuration."""
        with self._lock:
            self.config = config
            self._client = None
            self._transfer_config = None
            logger.info("S3 client marked for reinitialization")
    
    def test_connection(self) -> tuple[bool, str]:
        """
        Test the S3 connection and bucket access.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            bucket = self.config.sync.bucket_name
            if not bucket:
                logger.warning("Connection test failed: Bucket name not configured")
                return False, "Bucket name not configured"
            
            logger.debug(f"Testing connection to bucket: {bucket}")
            # Try to head the bucket (checks if it exists and we have access)
            self.client.head_bucket(Bucket=bucket)
            logger.info(f"Connection test successful for bucket: {bucket}")
            return True, f"Successfully connected to bucket '{bucket}'"
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            logger.error(f"Connection test failed - AWS error code: {error_code}", exc_info=True)
            if error_code == '404':
                return False, f"Bucket '{self.config.sync.bucket_name}' not found"
            elif error_code == '403':
                return False, "Access denied - check your credentials and bucket permissions"
            else:
                return False, f"AWS error: {e.response['Error'].get('Message', str(e))}"
        except EndpointConnectionError as e:
            logger.error(f"Connection test failed - Endpoint error: {e}", exc_info=True)
            return False, f"Could not connect to endpoint: {e}"
        except Exception as e:
            logger.error(f"Connection test failed - Unexpected error: {e}", exc_info=True)
            return False, f"Connection failed: {str(e)}"
    
    def upload_file(
        self,
        local_path: Path,
        s3_key: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Upload a file to S3.
        
        Args:
            local_path: Path to the local file
            s3_key: S3 object key
            progress_callback: Optional callback(filename, bytes_sent, total_bytes)
        
        Returns:
            Tuple of (success, message, etag)
        """
        bucket = self.config.sync.bucket_name
        logger.debug(f"upload_file called: {local_path} -> s3://{bucket}/{s3_key}")
        
        if not local_path.exists():
            logger.warning(f"Upload aborted - file not found: {local_path}")
            return False, f"File not found: {local_path}", None
        
        try:
            file_size = local_path.stat().st_size
            is_multipart = file_size > (self.config.sync.multipart_threshold_mb * 1024 * 1024)
            logger.debug(f"File size: {file_size} bytes, Will use multipart: {is_multipart}")
            
            # Create progress tracker if callback provided
            callback = None
            if progress_callback:
                progress = UploadProgress(local_path.name, file_size, progress_callback)
                callback = progress
            
            # Add S3 prefix if configured
            if self.config.sync.s3_prefix:
                s3_key = f"{self.config.sync.s3_prefix.rstrip('/')}/{s3_key}"
                logger.debug(f"Applied S3 prefix, final key: {s3_key}")
            
            logger.info(f"Uploading {local_path} -> s3://{bucket}/{s3_key}")
            
            # Perform upload with automatic multipart for large files
            self.client.upload_file(
                Filename=str(local_path),
                Bucket=bucket,
                Key=s3_key,
                Config=self.transfer_config,
                Callback=callback,
            )
            
            # Get ETag from uploaded object
            etag = None
            try:
                response = self.client.head_object(Bucket=bucket, Key=s3_key)
                etag = response.get('ETag', '').strip('"')
            except Exception as e:
                logger.warning(f"Could not retrieve ETag after upload: {e}")
            
            logger.info(f"Successfully uploaded {local_path.name} ({file_size} bytes)")
            return True, f"Uploaded to s3://{bucket}/{s3_key}", etag
            
        except ClientError as e:
            error_msg = e.response['Error'].get('Message', str(e))
            logger.error(f"Upload failed for {local_path}: {error_msg}")
            return False, f"Upload failed: {error_msg}", None
        except Exception as e:
            logger.error(f"Upload failed for {local_path}: {e}")
            return False, f"Upload failed: {str(e)}", None
    
    def rename_object(self, old_key: str, new_key: str) -> tuple[bool, str]:
        """
        Rename/move an object in S3 by copying to new key and deleting old.
        
        Args:
            old_key: Current S3 object key (relative path)
            new_key: New S3 object key (relative path)
        
        Returns:
            Tuple of (success, message)
        """
        bucket = self.config.sync.bucket_name
        
        # Add S3 prefix if configured
        if self.config.sync.s3_prefix:
            prefix = self.config.sync.s3_prefix.rstrip('/')
            old_key_full = f"{prefix}/{old_key}"
            new_key_full = f"{prefix}/{new_key}"
        else:
            old_key_full = old_key
            new_key_full = new_key
        
        try:
            logger.debug(f"Renaming S3 object: {old_key_full} -> {new_key_full}")
            
            # Copy to new location
            copy_source = {'Bucket': bucket, 'Key': old_key_full}
            self.client.copy_object(
                CopySource=copy_source,
                Bucket=bucket,
                Key=new_key_full
            )
            logger.debug(f"Copied to new key: {new_key_full}")
            
            # Delete old object
            self.client.delete_object(Bucket=bucket, Key=old_key_full)
            logger.info(f"Renamed s3://{bucket}/{old_key_full} -> {new_key_full}")
            
            return True, f"Renamed to {new_key_full}"
            
        except ClientError as e:
            error_code = e.response['Error'].get('Code', '')
            error_msg = e.response['Error'].get('Message', str(e))
            
            if error_code == '404' or error_code == 'NoSuchKey':
                # Old file doesn't exist in S3 - just upload the new one
                logger.warning(f"Original file not found in S3, cannot rename: {old_key_full}")
                return False, "Original file not found in S3"
            
            logger.error(f"Rename failed: {error_msg}")
            return False, f"Rename failed: {error_msg}"
        except Exception as e:
            logger.error(f"Rename failed: {e}", exc_info=True)
            return False, f"Rename failed: {str(e)}"
    
    def delete_object(self, s3_key: str) -> tuple[bool, str]:
        """
        Delete an object from S3.
        
        Note: This is implemented for future bidirectional sync support.
        Currently not used in one-way sync mode.
        
        Args:
            s3_key: S3 object key to delete
        
        Returns:
            Tuple of (success, message)
        """
        bucket = self.config.sync.bucket_name
        
        try:
            # Add S3 prefix if configured
            if self.config.sync.s3_prefix:
                s3_key = f"{self.config.sync.s3_prefix.rstrip('/')}/{s3_key}"
            
            self.client.delete_object(Bucket=bucket, Key=s3_key)
            logger.info(f"Deleted s3://{bucket}/{s3_key}")
            return True, f"Deleted s3://{bucket}/{s3_key}"
            
        except ClientError as e:
            error_msg = e.response['Error'].get('Message', str(e))
            logger.error(f"Delete failed for {s3_key}: {error_msg}")
            return False, f"Delete failed: {error_msg}"
        except Exception as e:
            logger.error(f"Delete failed for {s3_key}: {e}")
            return False, f"Delete failed: {str(e)}"
    
    def empty_trash(self) -> tuple[bool, int, str]:
        """
        Delete all objects in the .corbeille folder.
        
        Returns:
            Tuple of (success, deleted_count, message)
        """
        bucket = self.config.sync.bucket_name
        
        # Build trash prefix
        if self.config.sync.s3_prefix:
            prefix = self.config.sync.s3_prefix.rstrip('/')
            trash_prefix = f"{prefix}/.corbeille/"
        else:
            trash_prefix = ".corbeille/"
        
        try:
            logger.info(f"Emptying trash: s3://{bucket}/{trash_prefix}")
            
            # List all objects in trash
            paginator = self.client.get_paginator('list_objects_v2')
            deleted_count = 0
            
            for page in paginator.paginate(Bucket=bucket, Prefix=trash_prefix):
                objects = page.get('Contents', [])
                if not objects:
                    continue
                
                # Delete in batches of 1000 (S3 limit)
                delete_objects = [{'Key': obj['Key']} for obj in objects]
                
                if delete_objects:
                    response = self.client.delete_objects(
                        Bucket=bucket,
                        Delete={'Objects': delete_objects, 'Quiet': True}
                    )
                    
                    # Check for errors
                    errors = response.get('Errors', [])
                    if errors:
                        logger.warning(f"Some objects failed to delete: {errors}")
                    
                    deleted_count += len(delete_objects) - len(errors)
            
            if deleted_count > 0:
                logger.info(f"Emptied trash: deleted {deleted_count} objects")
                return True, deleted_count, f"Deleted {deleted_count} objects from trash"
            else:
                logger.info("Trash was already empty")
                return True, 0, "Trash was already empty"
                
        except ClientError as e:
            error_msg = e.response['Error'].get('Message', str(e))
            logger.error(f"Empty trash failed: {error_msg}")
            return False, 0, f"Empty trash failed: {error_msg}"
        except Exception as e:
            logger.error(f"Empty trash failed: {e}", exc_info=True)
            return False, 0, f"Empty trash failed: {str(e)}"

    def move_to_trash(self, s3_key: str) -> tuple[bool, str]:
        """
        Move an S3 object to the Corbeille (trash) folder instead of deleting.
        
        The object is copied to .corbeille/{original_key} and then deleted
        from its original location.
        
        Args:
            s3_key: S3 object key to move to trash (relative path)
        
        Returns:
            Tuple of (success, message)
        """
        bucket = self.config.sync.bucket_name
        
        # Build full keys with prefix
        if self.config.sync.s3_prefix:
            prefix = self.config.sync.s3_prefix.rstrip('/')
            original_key = f"{prefix}/{s3_key}"
            trash_key = f"{prefix}/.corbeille/{s3_key}"
        else:
            original_key = s3_key
            trash_key = f".corbeille/{s3_key}"
        
        try:
            logger.debug(f"Moving to trash: {original_key} -> {trash_key}")
            
            # Copy to trash location
            copy_source = {'Bucket': bucket, 'Key': original_key}
            self.client.copy_object(
                CopySource=copy_source,
                Bucket=bucket,
                Key=trash_key
            )
            logger.debug(f"Copied to trash: {trash_key}")
            
            # Delete original
            self.client.delete_object(Bucket=bucket, Key=original_key)
            logger.info(f"Moved to trash: s3://{bucket}/{original_key} -> {trash_key}")
            
            return True, f"Moved to trash: {trash_key}"
            
        except ClientError as e:
            error_code = e.response['Error'].get('Code', '')
            error_msg = e.response['Error'].get('Message', str(e))
            
            if error_code == '404' or error_code == 'NoSuchKey':
                logger.warning(f"File not found in S3, nothing to trash: {original_key}")
                return True, "File not in S3"  # Not an error - file may not have been synced yet
            
            logger.error(f"Move to trash failed: {error_msg}")
            return False, f"Move to trash failed: {error_msg}"
        except Exception as e:
            logger.error(f"Move to trash failed: {e}", exc_info=True)
            return False, f"Move to trash failed: {str(e)}"
    
    def download_file(
        self,
        s3_key: str,
        local_path: Path,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> tuple[bool, str]:
        """
        Download a file from S3 to local storage.
        
        Uses atomic write: downloads to temp file then renames to final path.
        
        Args:
            s3_key: S3 object key (relative path)
            local_path: Local file path to write to
            progress_callback: Optional callback(filename, bytes_received, total_bytes)
        
        Returns:
            Tuple of (success, message)
        """
        bucket = self.config.sync.bucket_name
        
        # Build full S3 key with prefix
        if self.config.sync.s3_prefix:
            s3_key_full = f"{self.config.sync.s3_prefix.rstrip('/')}/{s3_key}"
        else:
            s3_key_full = s3_key
        
        logger.debug(f"download_file called: s3://{bucket}/{s3_key_full} -> {local_path}")
        
        try:
            # Get file size for progress tracking
            head = self.client.head_object(Bucket=bucket, Key=s3_key_full)
            file_size = head['ContentLength']
            last_modified = head['LastModified']
            logger.debug(f"S3 object size: {file_size} bytes, last modified: {last_modified}")
            
            # Create progress tracker if callback provided
            callback = None
            if progress_callback:
                progress = UploadProgress(local_path.name, file_size, progress_callback)
                callback = progress
            
            # Ensure parent directory exists
            local_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Download to temp file first (atomic write)
            temp_path = local_path.with_suffix(local_path.suffix + '.tmp')
            
            logger.info(f"Downloading s3://{bucket}/{s3_key_full} -> {local_path}")
            
            self.client.download_file(
                Bucket=bucket,
                Key=s3_key_full,
                Filename=str(temp_path),
                Config=self.transfer_config,
                Callback=callback,
            )
            
            # Atomic rename to final path
            temp_path.replace(local_path)
            
            # Try to set mtime to match S3 LastModified
            try:
                import time
                mtime = last_modified.timestamp()
                os.utime(local_path, (mtime, mtime))
            except Exception as e:
                logger.debug(f"Could not set mtime: {e}")
            
            logger.info(f"Successfully downloaded {local_path.name} ({file_size} bytes)")
            return True, f"Downloaded from s3://{bucket}/{s3_key_full}"
            
        except ClientError as e:
            error_code = e.response['Error'].get('Code', '')
            error_msg = e.response['Error'].get('Message', str(e))
            
            if error_code == '404' or error_code == 'NoSuchKey':
                logger.warning(f"Download failed - file not found: {s3_key_full}")
                return False, "File not found in S3"
            
            logger.error(f"Download failed for {s3_key_full}: {error_msg}")
            return False, f"Download failed: {error_msg}"
        except Exception as e:
            logger.error(f"Download failed for {s3_key_full}: {e}", exc_info=True)
            # Clean up temp file if it exists
            temp_path = local_path.with_suffix(local_path.suffix + '.tmp')
            if temp_path.exists():
                temp_path.unlink()
            return False, f"Download failed: {str(e)}"
    
    def list_objects(self, prefix: str = "") -> list[dict]:
        """
        List objects in the bucket.
        
        Note: This is implemented for future bidirectional sync support.
        
        Args:
            prefix: Optional prefix to filter objects
        
        Returns:
            List of object metadata dicts
        """
        bucket = self.config.sync.bucket_name
        
        # Add S3 prefix if configured
        if self.config.sync.s3_prefix:
            prefix = f"{self.config.sync.s3_prefix.rstrip('/')}/{prefix}"
        
        try:
            objects = []
            paginator = self.client.get_paginator('list_objects_v2')
            
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    objects.append({
                        'key': obj['Key'],
                        'size': obj['Size'],
                        'last_modified': obj['LastModified'],
                        'etag': obj['ETag'],
                    })
            
            return objects
            
        except ClientError as e:
            logger.error(f"List objects failed: {e}")
            return []


# Module-level client instance (lazy initialized)
_s3_client: Optional[S3ClientWrapper] = None


def get_s3_client(config: Optional[AppConfig] = None) -> S3ClientWrapper:
    """
    Get the global S3 client instance.
    
    Args:
        config: Optional config to use (required on first call)
    
    Returns:
        S3ClientWrapper instance
    """
    global _s3_client
    
    if _s3_client is None:
        if config is None:
            from config import get_config
            config = get_config()
        _s3_client = S3ClientWrapper(config)
    elif config is not None:
        _s3_client.reinitialize(config)
    
    return _s3_client
