"""
Settings dashboard for S3-Drive Sync Application.
Modern UI built with CustomTkinter for configuration management.
"""

import logging
import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Optional

import customtkinter as ctk

from config import AppConfig, ConfigManager, get_config_manager
from s3_client import get_s3_client

logger = logging.getLogger(__name__)


class SettingsWindow(ctk.CTk):
    """
    Main settings/configuration window.
    
    Provides forms for:
    - AWS credentials and region configuration
    - S3 bucket and local folder selection
    - Sync settings (workers, retry, etc.)
    - Exclusion pattern management
    """
    
    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        on_save: Optional[Callable[[AppConfig], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        
        # Hide window immediately to prevent flash on startup
        self.withdraw()
        
        self.config_manager = config_manager or get_config_manager()
        self.config = self.config_manager.config
        self.on_save = on_save
        self.on_close = on_close
        
        # Configure window
        self.title("S3Sync Settings")
        self.geometry("600x700")
        self.minsize(500, 600)
        
        # Set theme
        ctk.set_appearance_mode("dark" if self.config.dark_mode else "light")
        ctk.set_default_color_theme("blue")
        
        # Configure grid
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # Create scrollable frame for content
        self.main_frame = ctk.CTkScrollableFrame(self)
        self.main_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        self.main_frame.grid_columnconfigure(0, weight=1)
        
        # Build UI sections
        self._create_aws_section()
        self._create_sync_section()
        self._create_advanced_section()
        self._create_exclusions_section()
        self._create_buttons_section()
        
        # Load current values
        self._load_values()
        
        # Bind close event
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
    
    def _create_section_header(self, parent, text: str, row: int) -> ctk.CTkLabel:
        """Create a section header label."""
        label = ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        label.grid(row=row, column=0, columnspan=2, pady=(15, 10), sticky="w")
        return label
    
    def _create_labeled_entry(
        self,
        parent,
        label_text: str,
        row: int,
        show: str = "",
        placeholder: str = "",
    ) -> ctk.CTkEntry:
        """Create a label + entry pair."""
        label = ctk.CTkLabel(parent, text=label_text)
        label.grid(row=row, column=0, padx=5, pady=5, sticky="w")
        
        entry = ctk.CTkEntry(
            parent,
            width=350,
            show=show,
            placeholder_text=placeholder,
        )
        entry.grid(row=row, column=1, padx=5, pady=5, sticky="ew")
        
        return entry
    
    def _create_aws_section(self) -> None:
        """Create AWS credentials section."""
        self._create_section_header(self.main_frame, "☁️ AWS Configuration", 0)
        
        # AWS Frame
        aws_frame = ctk.CTkFrame(self.main_frame)
        aws_frame.grid(row=1, column=0, padx=5, pady=5, sticky="ew")
        aws_frame.grid_columnconfigure(1, weight=1)
        
        # Access Key
        self.access_key_entry = self._create_labeled_entry(
            aws_frame, "Access Key ID:", 0,
            placeholder="AKIA..."
        )
        
        # Secret Key
        self.secret_key_entry = self._create_labeled_entry(
            aws_frame, "Secret Access Key:", 1,
            show="•",
            placeholder="Your secret key"
        )
        
        # Show/hide secret key toggle
        self.show_secret_var = ctk.BooleanVar(value=False)
        show_btn = ctk.CTkCheckBox(
            aws_frame,
            text="Show",
            variable=self.show_secret_var,
            command=self._toggle_secret_visibility,
            width=60,
        )
        show_btn.grid(row=1, column=2, padx=5)
        
        # Region
        self.region_entry = self._create_labeled_entry(
            aws_frame, "Region:", 2,
            placeholder="us-east-1"
        )
        
        # Endpoint URL (optional, for S3-compatible storage)
        self.endpoint_entry = self._create_labeled_entry(
            aws_frame, "Endpoint URL:", 3,
            placeholder="(Optional) For MinIO, Wasabi, etc."
        )
        
        # Test Connection button
        test_btn = ctk.CTkButton(
            aws_frame,
            text="Test Connection",
            command=self._test_connection,
            width=120,
        )
        test_btn.grid(row=4, column=0, columnspan=2, pady=10, sticky="e")
        
        # Connection status label (on its own row to take full width)
        self.connection_status = ctk.CTkLabel(
            aws_frame,
            text="",
            text_color="gray",
            anchor="e",
        )
        self.connection_status.grid(row=5, column=0, columnspan=2, pady=(0, 10), sticky="e")
    
    def _create_sync_section(self) -> None:
        """Create sync settings section."""
        self._create_section_header(self.main_frame, "📁 Sync Settings", 2)
        
        sync_frame = ctk.CTkFrame(self.main_frame)
        sync_frame.grid(row=3, column=0, padx=5, pady=5, sticky="ew")
        sync_frame.grid_columnconfigure(1, weight=1)
        
        # Bucket Name
        self.bucket_entry = self._create_labeled_entry(
            sync_frame, "S3 Bucket:", 0,
            placeholder="my-bucket-name"
        )
        
        # S3 Prefix (optional)
        self.prefix_entry = self._create_labeled_entry(
            sync_frame, "S3 Prefix:", 1,
            placeholder="(Optional) folder/path/"
        )
        
        # Local Folder
        folder_label = ctk.CTkLabel(sync_frame, text="Local Folder:")
        folder_label.grid(row=2, column=0, padx=5, pady=5, sticky="w")
        
        folder_frame = ctk.CTkFrame(sync_frame, fg_color="transparent")
        folder_frame.grid(row=2, column=1, padx=5, pady=5, sticky="ew")
        folder_frame.grid_columnconfigure(0, weight=1)
        
        self.folder_entry = ctk.CTkEntry(
            folder_frame,
            placeholder_text="Select a folder to sync...",
        )
        self.folder_entry.grid(row=0, column=0, sticky="ew")
        
        browse_btn = ctk.CTkButton(
            folder_frame,
            text="Browse",
            command=self._browse_folder,
            width=80,
        )
        browse_btn.grid(row=0, column=1, padx=(5, 0))
    
    def _create_advanced_section(self) -> None:
        """Create advanced settings section."""
        self._create_section_header(self.main_frame, "⚙️ Advanced Settings", 4)
        
        adv_frame = ctk.CTkFrame(self.main_frame)
        adv_frame.grid(row=5, column=0, padx=5, pady=5, sticky="ew")
        adv_frame.grid_columnconfigure(1, weight=1)
        
        # Max Workers
        workers_label = ctk.CTkLabel(adv_frame, text="Upload Workers:")
        workers_label.grid(row=0, column=0, padx=5, pady=5, sticky="w")
        
        self.workers_slider = ctk.CTkSlider(
            adv_frame,
            from_=1,
            to=10,
            number_of_steps=9,
            command=self._on_workers_change,
        )
        self.workers_slider.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        
        self.workers_value = ctk.CTkLabel(adv_frame, text="4")
        self.workers_value.grid(row=0, column=2, padx=5, pady=5)
        
        # Debounce Seconds
        debounce_label = ctk.CTkLabel(adv_frame, text="Debounce (sec):")
        debounce_label.grid(row=1, column=0, padx=5, pady=5, sticky="w")
        
        self.debounce_slider = ctk.CTkSlider(
            adv_frame,
            from_=0.5,
            to=5.0,
            command=self._on_debounce_change,
        )
        self.debounce_slider.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        
        self.debounce_value = ctk.CTkLabel(adv_frame, text="1.0")
        self.debounce_value.grid(row=1, column=2, padx=5, pady=5)
        
        # Max Retries
        retries_label = ctk.CTkLabel(adv_frame, text="Max Retries:")
        retries_label.grid(row=2, column=0, padx=5, pady=5, sticky="w")
        
        self.retries_slider = ctk.CTkSlider(
            adv_frame,
            from_=1,
            to=10,
            number_of_steps=9,
            command=self._on_retries_change,
        )
        self.retries_slider.grid(row=2, column=1, padx=5, pady=5, sticky="ew")
        
        self.retries_value = ctk.CTkLabel(adv_frame, text="5")
        self.retries_value.grid(row=2, column=2, padx=5, pady=5)
        
        # Multipart Threshold
        multipart_label = ctk.CTkLabel(adv_frame, text="Multipart Threshold (MB):")
        multipart_label.grid(row=3, column=0, padx=5, pady=5, sticky="w")
        
        self.multipart_entry = ctk.CTkEntry(adv_frame, width=80)
        self.multipart_entry.grid(row=3, column=1, padx=5, pady=5, sticky="w")
        
        # UI Preferences
        prefs_frame = ctk.CTkFrame(adv_frame, fg_color="transparent")
        prefs_frame.grid(row=4, column=0, columnspan=3, pady=10)
        
        self.start_minimized_var = ctk.BooleanVar()
        start_min_check = ctk.CTkCheckBox(
            prefs_frame,
            text="Start minimized to tray",
            variable=self.start_minimized_var,
        )
        start_min_check.grid(row=0, column=0, padx=10)
        
        self.notifications_var = ctk.BooleanVar()
        notif_check = ctk.CTkCheckBox(
            prefs_frame,
            text="Show notifications",
            variable=self.notifications_var,
        )
        notif_check.grid(row=0, column=1, padx=10)
        
        self.dark_mode_var = ctk.BooleanVar()
        dark_check = ctk.CTkCheckBox(
            prefs_frame,
            text="Dark mode",
            variable=self.dark_mode_var,
            command=self._toggle_dark_mode,
        )
        dark_check.grid(row=0, column=2, padx=10)
    
    def _create_exclusions_section(self) -> None:
        """Create file exclusions section."""
        self._create_section_header(self.main_frame, "🚫 Exclusion Patterns", 6)
        
        excl_frame = ctk.CTkFrame(self.main_frame)
        excl_frame.grid(row=7, column=0, padx=5, pady=5, sticky="ew")
        excl_frame.grid_columnconfigure(0, weight=1)
        
        # Info label
        info_label = ctk.CTkLabel(
            excl_frame,
            text="Files matching these patterns will not be synced (glob syntax):",
            text_color="gray",
        )
        info_label.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="w")
        
        # Patterns text box
        self.exclusions_text = ctk.CTkTextbox(excl_frame, height=150)
        self.exclusions_text.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="ew")
        
        # Reset to defaults button
        reset_btn = ctk.CTkButton(
            excl_frame,
            text="Reset to Defaults",
            command=self._reset_exclusions,
            width=120,
            fg_color="gray",
        )
        reset_btn.grid(row=2, column=1, pady=5, sticky="e")
    
    def _create_buttons_section(self) -> None:
        """Create bottom buttons section."""
        btn_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        btn_frame.grid(row=8, column=0, pady=20, sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        
        # Save button
        save_btn = ctk.CTkButton(
            btn_frame,
            text="Save Settings",
            command=self._save_settings,
            width=150,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        save_btn.grid(row=0, column=1, padx=5)
        
        # Cancel button
        cancel_btn = ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=self._on_window_close,
            width=100,
            height=40,
            fg_color="gray",
        )
        cancel_btn.grid(row=0, column=0, padx=5, sticky="e")
    
    def _load_values(self) -> None:
        """Load current configuration values into the form."""
        config = self.config
        
        # AWS
        self.access_key_entry.insert(0, config.aws.access_key or "")
        self.secret_key_entry.insert(0, config.aws.secret_key or "")
        self.region_entry.insert(0, config.aws.region or "us-east-1")
        if config.aws.endpoint_url:
            self.endpoint_entry.insert(0, config.aws.endpoint_url)
        
        # Sync
        self.bucket_entry.insert(0, config.sync.bucket_name or "")
        self.prefix_entry.insert(0, config.sync.s3_prefix or "")
        self.folder_entry.insert(0, config.sync.local_folder or "")
        
        # Advanced
        self.workers_slider.set(config.sync.max_workers)
        self.workers_value.configure(text=str(config.sync.max_workers))
        
        self.debounce_slider.set(config.sync.debounce_seconds)
        self.debounce_value.configure(text=f"{config.sync.debounce_seconds:.1f}")
        
        self.retries_slider.set(config.sync.max_retries)
        self.retries_value.configure(text=str(config.sync.max_retries))
        
        self.multipart_entry.insert(0, str(config.sync.multipart_threshold_mb))
        
        # Preferences
        self.start_minimized_var.set(config.start_minimized)
        self.notifications_var.set(config.show_notifications)
        self.dark_mode_var.set(config.dark_mode)
        
        # Exclusions
        self.exclusions_text.delete("1.0", "end")
        self.exclusions_text.insert("1.0", "\n".join(config.exclusions.patterns))
    
    def _collect_values(self) -> AppConfig:
        """Collect form values into a config object."""
        config = AppConfig()
        
        # AWS
        config.aws.access_key = self.access_key_entry.get().strip()
        config.aws.secret_key = self.secret_key_entry.get().strip()
        config.aws.region = self.region_entry.get().strip() or "us-east-1"
        endpoint = self.endpoint_entry.get().strip()
        config.aws.endpoint_url = endpoint if endpoint else None
        
        # Sync
        config.sync.bucket_name = self.bucket_entry.get().strip()
        config.sync.s3_prefix = self.prefix_entry.get().strip()
        config.sync.local_folder = self.folder_entry.get().strip()
        config.sync.max_workers = int(self.workers_slider.get())
        config.sync.debounce_seconds = round(self.debounce_slider.get(), 1)
        config.sync.max_retries = int(self.retries_slider.get())
        
        try:
            config.sync.multipart_threshold_mb = int(self.multipart_entry.get())
        except ValueError:
            config.sync.multipart_threshold_mb = 100
        
        # Preferences
        config.start_minimized = self.start_minimized_var.get()
        config.show_notifications = self.notifications_var.get()
        config.dark_mode = self.dark_mode_var.get()
        
        # Exclusions
        exclusions_text = self.exclusions_text.get("1.0", "end").strip()
        patterns = [p.strip() for p in exclusions_text.split("\n") if p.strip()]
        config.exclusions.patterns = patterns
        
        return config
    
    def _validate_config(self, config: AppConfig) -> tuple[bool, str]:
        """Validate the configuration."""
        if not config.aws.access_key:
            return False, "AWS Access Key is required"
        
        if not config.aws.secret_key:
            return False, "AWS Secret Key is required"
        
        if not config.sync.bucket_name:
            return False, "S3 Bucket name is required"
        
        if not config.sync.local_folder:
            return False, "Local folder path is required"
        
        local_path = Path(config.sync.local_folder)
        if not local_path.exists():
            return False, f"Local folder does not exist: {config.sync.local_folder}"
        
        if not local_path.is_dir():
            return False, f"Path is not a directory: {config.sync.local_folder}"
        
        return True, "Configuration is valid"
    
    def _toggle_secret_visibility(self) -> None:
        """Toggle visibility of secret key."""
        show = "" if self.show_secret_var.get() else "•"
        self.secret_key_entry.configure(show=show)
    
    def _toggle_dark_mode(self) -> None:
        """Toggle dark/light mode."""
        mode = "dark" if self.dark_mode_var.get() else "light"
        ctk.set_appearance_mode(mode)
    
    def _on_workers_change(self, value) -> None:
        """Handle workers slider change."""
        self.workers_value.configure(text=str(int(value)))
    
    def _on_debounce_change(self, value) -> None:
        """Handle debounce slider change."""
        self.debounce_value.configure(text=f"{value:.1f}")
    
    def _on_retries_change(self, value) -> None:
        """Handle retries slider change."""
        self.retries_value.configure(text=str(int(value)))
    
    def _browse_folder(self) -> None:
        """Open folder browser dialog."""
        logger.debug("Opening folder browser dialog")
        folder = filedialog.askdirectory(
            title="Select Folder to Sync",
            initialdir=self.folder_entry.get() or Path.home(),
        )
        if folder:
            logger.debug(f"Folder selected: {folder}")
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, folder)
        else:
            logger.debug("Folder selection cancelled")
    
    def _reset_exclusions(self) -> None:
        """Reset exclusions to default patterns."""
        from config import ExclusionSettings
        defaults = ExclusionSettings()
        self.exclusions_text.delete("1.0", "end")
        self.exclusions_text.insert("1.0", "\n".join(defaults.patterns))
    
    def _test_connection(self) -> None:
        """Test the S3 connection with current settings."""
        logger.debug("Connection test initiated from UI")
        self.connection_status.configure(text="Testing...", text_color="gray")
        self.update()
        
        # Run test in background thread
        def do_test():
            logger.debug("Running connection test in background thread")
            config = self._collect_values()
            s3_client = get_s3_client(config)
            success, message = s3_client.test_connection()
            logger.debug(f"Connection test result: success={success}, message={message}")
            
            # Update UI in main thread
            self.after(0, lambda: self._show_connection_result(success, message))
        
        threading.Thread(target=do_test, daemon=True).start()
    
    def _show_connection_result(self, success: bool, message: str) -> None:
        """Show connection test result."""
        color = "#4CAF50" if success else "#F44336"
        self.connection_status.configure(text=message, text_color=color)
    
    def _save_settings(self) -> None:
        """Validate and save settings."""
        logger.debug("Save settings initiated")
        config = self._collect_values()
        
        valid, message = self._validate_config(config)
        logger.debug(f"Config validation result: valid={valid}, message={message}")
        
        if not valid:
            logger.warning(f"Configuration validation failed: {message}")
            messagebox.showerror("Validation Error", message)
            return
        
        try:
            logger.info(f"Saving configuration - bucket: {config.sync.bucket_name}, folder: {config.sync.local_folder}")
            self.config_manager.save(config)
            self.config = config
            logger.info("Configuration saved successfully")
            
            if self.on_save:
                self.on_save(config)
            
            messagebox.showinfo("Success", "Settings saved successfully!")
            
        except Exception as e:
            logger.error(f"Failed to save settings: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to save settings: {e}")
    
    def _on_window_close(self) -> None:
        """Handle window close."""
        logger.debug("Settings window close requested")
        if self.on_close:
            self.on_close()
        self.withdraw()  # Hide instead of destroy to allow reopening
    
    def show(self) -> None:
        """Show the settings window (thread-safe)."""
        logger.debug("Showing settings window")
        # Use after() to schedule on main thread - avoids GIL issues with pystray
        self.after(0, self._do_show)
    
    def _do_show(self) -> None:
        """Actually show the window (must be called from main thread)."""
        self.deiconify()
        self.lift()
        self.focus_force()
