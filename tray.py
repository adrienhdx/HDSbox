"""
System tray integration for S3-Drive Sync Application.
Provides background operation with status display and quick actions.
"""

import logging
import threading
from io import BytesIO
from typing import Callable, Optional

from PIL import Image, ImageDraw

try:
    import pystray
    from pystray import Icon, Menu, MenuItem
except ImportError:
    pystray = None

from models import AppState, RecentFile

logger = logging.getLogger(__name__)


def create_tray_icon(color: str = "#4CAF50", size: int = 64) -> Image.Image:
    """
    Create a simple tray icon programmatically.
    
    Args:
        color: Hex color for the icon
        size: Icon size in pixels
    
    Returns:
        PIL Image for use as tray icon
    """
    # Create a simple cloud-with-arrow icon
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    # Draw a rounded rectangle (cloud shape)
    margin = size // 8
    draw.rounded_rectangle(
        [margin, margin + size // 4, size - margin, size - margin],
        radius=size // 6,
        fill=color,
    )
    
    # Draw upload arrow
    arrow_color = "white"
    center_x = size // 2
    arrow_top = size // 4
    arrow_bottom = size - size // 4
    arrow_width = size // 6
    
    # Arrow body
    draw.rectangle(
        [center_x - arrow_width // 2, arrow_top + size // 6,
         center_x + arrow_width // 2, arrow_bottom],
        fill=arrow_color,
    )
    
    # Arrow head (triangle)
    draw.polygon(
        [
            (center_x, arrow_top),  # Top point
            (center_x - arrow_width, arrow_top + size // 4),  # Bottom left
            (center_x + arrow_width, arrow_top + size // 4),  # Bottom right
        ],
        fill=arrow_color,
    )
    
    return image


def create_syncing_icon(size: int = 64) -> Image.Image:
    """Create an icon indicating active sync (blue color)."""
    return create_tray_icon(color="#2196F3", size=size)


def create_error_icon(size: int = 64) -> Image.Image:
    """Create an icon indicating error state (red color)."""
    return create_tray_icon(color="#F44336", size=size)


def create_paused_icon(size: int = 64) -> Image.Image:
    """Create an icon indicating paused state (orange color)."""
    return create_tray_icon(color="#FF9800", size=size)


class TrayManager:
    """
    Manages the system tray icon and menu.
    
    The tray provides:
    - Visual status indicator (icon color changes based on state)
    - Status text showing current operation
    - List of recently synced files
    - Quick access to settings and quit
    """
    
    def __init__(
        self,
        state: AppState,
        on_settings: Optional[Callable[[], None]] = None,
        on_pause_resume: Optional[Callable[[], None]] = None,
        on_empty_trash: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ):
        if pystray is None:
            raise ImportError("pystray is required for system tray support")
        
        self.state = state
        self.on_settings = on_settings
        self.on_pause_resume = on_pause_resume
        self.on_empty_trash = on_empty_trash
        self.on_quit = on_quit
        
        self._icon: Optional[Icon] = None
        self._icons = {
            'idle': create_tray_icon(),
            'syncing': create_syncing_icon(),
            'error': create_error_icon(),
            'paused': create_paused_icon(),
        }
        
        self._lock = threading.Lock()
    
    def _get_current_icon(self) -> Image.Image:
        """Get the appropriate icon based on current state."""
        if self.state.is_paused:
            return self._icons['paused']
        elif self.state.last_error:
            return self._icons['error']
        elif self.state.is_syncing:
            return self._icons['syncing']
        return self._icons['idle']
    
    def _get_status_text(self, item=None) -> str:
        """Get the current status text for the menu."""
        return self.state.status_text
    
    def _reveal_file(self, path: str):
        """Reveal a file in Finder/Explorer."""
        import subprocess
        import platform
        import os
        
        logger.debug(f"Attempting to reveal file: {path}")
        
        if not path:
            logger.warning("Cannot reveal file, path is empty")
            return
            
        if not os.path.exists(path):
            logger.warning(f"Cannot reveal file, path does not exist: {path}")
            return
            
        system = platform.system()
        try:
            if system == "Darwin":  # macOS
                subprocess.run(["open", "-R", path], check=True)
            elif system == "Windows":
                subprocess.run(["explorer", "/select,", path], check=True)
            else:  # Linux
                # Open the containing folder
                folder = os.path.dirname(path)
                subprocess.run(["xdg-open", folder], check=True)
            logger.debug(f"Revealed file in file manager: {path}")
        except Exception as e:
            logger.error(f"Failed to reveal file: {e}")
    
    def _create_reveal_handler(self, path: str):
        """Create a handler function for revealing a specific file."""
        def handler(icon, item):
            self._reveal_file(path)
        return handler
    
    def _get_recent_files_items(self) -> list:
        """Generate menu items for recent files."""
        items = []
        
        if not self.state.recent_files:
            items.append(MenuItem("  (no recent files)", None, enabled=False))
        else:
            for recent in self.state.recent_files[:5]:
                display = f"  {recent.display_name()}"
                # Create a dedicated handler for each file
                handler = self._create_reveal_handler(recent.local_path)
                items.append(MenuItem(display, handler))
        
        return items
    
    def _is_recent_visible(self, item) -> bool:
        """Check if recent files section should show items."""
        return len(self.state.recent_files) > 0
    
    def _is_paused(self, item=None) -> bool:
        """Check if sync is paused (for checkbox state)."""
        return self.state.is_paused
    
    def _handle_pause_resume(self, icon, item):
        """Handle pause/resume toggle."""
        logger.debug(f"Pause/resume toggled - current paused state: {self.state.is_paused}")
        if self.on_pause_resume:
            self.on_pause_resume()
        self.update()
    
    def _handle_settings(self, icon, item):
        """Handle settings menu click."""
        logger.debug("Settings menu item clicked")
        if self.on_settings:
            self.on_settings()
    
    def _handle_quit(self, icon, item):
        """Handle quit menu click."""
        logger.info("Quit requested from system tray menu")
        if self.on_quit:
            self.on_quit()
        self.stop()
    
    def _handle_empty_trash(self, icon, item):
        """Handle empty trash menu click."""
        logger.info("Empty trash requested from system tray menu")
        if self.on_empty_trash:
            self.on_empty_trash()
    
    def _create_menu(self) -> Menu:
        """Create the tray menu."""
        # Build menu items list dynamically
        items = [
            MenuItem(
                self._get_status_text,
                None,
                enabled=False,
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Recent Files:",
                None,
                enabled=False,
            ),
        ]
        
        # Add recent files directly to main menu
        items.extend(self._get_recent_files_items())
        
        items.extend([
            Menu.SEPARATOR,
            MenuItem(
                "Pause Sync",
                self._handle_pause_resume,
                checked=self._is_paused,
            ),
            MenuItem(
                "Settings...",
                self._handle_settings,
            ),
            MenuItem(
                "Empty Trash",
                self._handle_empty_trash,
            ),
            Menu.SEPARATOR,
            MenuItem(
                "Quit S3Sync",
                self._handle_quit,
            ),
        ])
        
        return Menu(*items)
    
    def start(self, blocking: bool = False) -> None:
        """
        Start the system tray icon.
        
        Args:
            blocking: If True, run in blocking mode (blocks the current thread)
                     If False, run detached (returns immediately)
        """
        logger.info("Starting system tray...")
        
        self._icon = Icon(
            name="S3Sync",
            icon=self._get_current_icon(),
            title="S3Sync",
            menu=self._create_menu(),
        )
        
        if blocking:
            self._icon.run()
        else:
            self._icon.run_detached()
        
        logger.info("System tray started")
    
    def stop(self) -> None:
        """Stop the system tray icon."""
        logger.info("Stopping system tray...")
        
        if self._icon:
            self._icon.stop()
            self._icon = None
        
        logger.info("System tray stopped")
    
    def update(self) -> None:
        """Update the tray icon and menu to reflect current state."""
        if not self._icon:
            return
        
        with self._lock:
            # Determine icon state for logging
            icon_state = 'paused' if self.state.is_paused else (
                'error' if self.state.last_error else (
                    'syncing' if self.state.is_syncing else 'idle'
                )
            )
            logger.debug(f"Updating tray - icon_state: {icon_state}, status: {self.state.status_text}")
            
            # Update icon based on state
            self._icon.icon = self._get_current_icon()
            
            # Update title/tooltip
            self._icon.title = f"S3Sync - {self.state.status_text}"
            
            # Recreate menu with updated recent files
            self._icon.menu = self._create_menu()
            
            # Refresh the menu (for dynamic items like recent files)
            self._icon.update_menu()
    
    def show_notification(self, title: str, message: str) -> None:
        """
        Show a system notification.
        
        Note: Notification support varies by platform.
        """
        if not self._icon:
            logger.debug("Cannot show notification - tray icon not initialized")
            return
        
        logger.debug(f"Showing notification: {title} - {message}")
        try:
            self._icon.notify(message, title)
        except Exception as e:
            logger.debug(f"Notification not supported or failed: {e}")
