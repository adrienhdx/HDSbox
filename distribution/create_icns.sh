#!/bin/bash
# =============================================================================
# Create macOS .icns icon from PNG
# =============================================================================
# Usage: ./create_icns.sh icon.png [output.icns]
#
# Requirements:
#   - Input PNG should be at least 1024x1024 pixels
#   - Requires macOS (uses iconutil)
# =============================================================================

set -e

# Check arguments
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <input.png> [output.icns]"
    echo ""
    echo "Creates a macOS .icns file from a PNG image."
    echo "Input PNG should be at least 1024x1024 pixels for best quality."
    exit 1
fi

INPUT_PNG="$1"
OUTPUT_ICNS="${2:-icon.icns}"

# Check if input file exists
if [[ ! -f "$INPUT_PNG" ]]; then
    echo "Error: Input file '$INPUT_PNG' not found."
    exit 1
fi

# Check if running on macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "Error: This script requires macOS (uses iconutil)."
    echo ""
    echo "On other platforms, you can use online converters or tools like:"
    echo "  - png2icns (Linux)"
    echo "  - https://cloudconvert.com/png-to-icns"
    exit 1
fi

# Create temporary iconset directory
ICONSET_DIR="icon.iconset"
rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"

echo "Creating icon sizes..."

# Generate all required sizes using sips
# macOS iconset requires specific sizes and naming

sips -z 16 16     "$INPUT_PNG" --out "$ICONSET_DIR/icon_16x16.png" > /dev/null
sips -z 32 32     "$INPUT_PNG" --out "$ICONSET_DIR/icon_16x16@2x.png" > /dev/null
sips -z 32 32     "$INPUT_PNG" --out "$ICONSET_DIR/icon_32x32.png" > /dev/null
sips -z 64 64     "$INPUT_PNG" --out "$ICONSET_DIR/icon_32x32@2x.png" > /dev/null
sips -z 128 128   "$INPUT_PNG" --out "$ICONSET_DIR/icon_128x128.png" > /dev/null
sips -z 256 256   "$INPUT_PNG" --out "$ICONSET_DIR/icon_128x128@2x.png" > /dev/null
sips -z 256 256   "$INPUT_PNG" --out "$ICONSET_DIR/icon_256x256.png" > /dev/null
sips -z 512 512   "$INPUT_PNG" --out "$ICONSET_DIR/icon_256x256@2x.png" > /dev/null
sips -z 512 512   "$INPUT_PNG" --out "$ICONSET_DIR/icon_512x512.png" > /dev/null
sips -z 1024 1024 "$INPUT_PNG" --out "$ICONSET_DIR/icon_512x512@2x.png" > /dev/null

echo "Converting to .icns..."

# Convert iconset to icns
iconutil -c icns "$ICONSET_DIR" -o "$OUTPUT_ICNS"

# Clean up
rm -rf "$ICONSET_DIR"

echo "Successfully created: $OUTPUT_ICNS"

# Show file info
ls -lh "$OUTPUT_ICNS"
