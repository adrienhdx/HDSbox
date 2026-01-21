#!/bin/bash
# =============================================================================
# S3Sync - macOS Build Script (PyInstaller)
# =============================================================================
# Builds the S3Sync application as a native macOS .app bundle
# The resulting app runs as a menu bar application (no Dock icon)
#
# Usage:
#   ./build_macos.sh          # Production build
#   ./build_macos.sh --clean  # Clean build directories only
# =============================================================================

set -e  # Exit on error

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Project root is the parent directory
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Configuration
APP_NAME="S3Sync"
VERSION="1.0.0"
BUILD_DIR="$PROJECT_ROOT/build"
DIST_DIR="$PROJECT_ROOT/dist"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored message
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if running on macOS
if [[ "$(uname)" != "Darwin" ]]; then
    log_error "This script is for macOS only."
    log_info "For Windows, use: python -m PyInstaller distribution/s3sync.spec"
    exit 1
fi

# Parse arguments
CLEAN_ONLY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --clean|-c)
            CLEAN_ONLY=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --clean, -c   Clean build directories only"
            echo "  --help, -h    Show this help message"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Clean build directories
clean_build() {
    log_info "Cleaning build directories..."
    rm -rf "$BUILD_DIR" "$DIST_DIR"
    log_success "Clean complete!"
}

# Handle clean only
if $CLEAN_ONLY; then
    clean_build
    exit 0
fi

# ====== BUILD PROCESS ======

echo ""
echo "=========================================="
echo "  $APP_NAME macOS Build (PyInstaller)"
echo "=========================================="
echo ""

log_info "Script directory: $SCRIPT_DIR"
log_info "Project root: $PROJECT_ROOT"
echo ""

# Change to project root
cd "$PROJECT_ROOT"

# Check if we're in the right directory
if [ ! -f "main.py" ]; then
    log_error "main.py not found. Make sure you're in the project root."
    exit 1
fi

# Clean previous builds
clean_build

# Check for icon file
if [ -f "icon.icns" ]; then
    log_success "Found icon.icns"
else
    log_warn "icon.icns not found. App will use default icon."
    log_info "To create an icon, put a PNG file and run: ./distribution/create_icns.sh icon.png"
fi

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    log_info "Activating virtual environment..."
    source venv/bin/activate
fi

# Install PyInstaller if needed
if ! python -c "import PyInstaller" 2>/dev/null; then
    log_info "Installing PyInstaller..."
    pip install pyinstaller
fi

# Build the application
echo ""
log_info "Building application with PyInstaller..."
echo ""

python -m PyInstaller "$SCRIPT_DIR/s3sync_macos.spec" --noconfirm

# Verify the build
if [ -d "$DIST_DIR/$APP_NAME.app" ]; then
    echo ""
    echo "=========================================="
    log_success "Build successful!"
    echo "=========================================="
    echo ""
    log_info "Application: $DIST_DIR/$APP_NAME.app"
    log_info "Size: $(du -sh "$DIST_DIR/$APP_NAME.app" | cut -f1)"
    echo ""
    
    # Verify LSUIElement is set
    if plutil -p "$DIST_DIR/$APP_NAME.app/Contents/Info.plist" | grep -q "LSUIElement.*true\|LSUIElement.*1"; then
        log_success "LSUIElement is set - app will not appear in Dock"
    else
        log_warn "LSUIElement not set correctly"
    fi
    
    echo ""
    log_info "To run: open $DIST_DIR/$APP_NAME.app"
    log_info "To install: cp -r $DIST_DIR/$APP_NAME.app /Applications/"
    echo ""
else
    echo ""
    log_error "Build failed - $DIST_DIR/$APP_NAME.app not created"
    exit 1
fi
