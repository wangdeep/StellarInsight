#!/usr/bin/env bash
# =============================================================================
# Stellar Insight -- Linux / macOS build script
#
# Usage:
#   chmod +x build.sh
#   ./build.sh
#
# Output:
#   dist/StellarInsight          (standalone binary, no Python needed)
#   dist/StellarInsight.tar.gz   (compressed archive for distribution)
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# ── Colours ------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; MAGENTA='\033[0;35m'; NC='\033[0m'

info()  { echo -e "${CYAN}  $*${NC}"; }
ok()    { echo -e "${GREEN}  [OK]  $*${NC}"; }
warn()  { echo -e "${YELLOW}  [!!]  $*${NC}"; }
fail()  { echo -e "${RED}\n  [FAIL]  $*\n${NC}"; exit 1; }
head()  { echo -e "${MAGENTA}\n===  $*  ===${NC}"; }

PLATFORM="$(uname -s)"   # Linux or Darwin

# =============================================================================
head "Step 1 / 4 -- Checking Python"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        VER=$("$candidate" --version 2>&1)
        MINOR=$(echo "$VER" | grep -oP '3\.\K\d+' || echo "0")
        if [ "$MINOR" -ge 10 ] 2>/dev/null; then
            PY="$candidate"
            ok "$VER  ($candidate)"
            break
        else
            warn "$VER found -- 3.10+ recommended"
        fi
    fi
done
[ -z "$PY" ] && fail "Python 3.10+ not found. Install it and try again."

# =============================================================================
head "Step 2 / 4 -- System dependencies"

if [ "$PLATFORM" = "Linux" ]; then
    info "Checking WebKit2GTK (required by pywebview on Linux)..."
    MISSING_PKGS=()

    if ! python3 -c "import gi; gi.require_version('WebKit2', '4.0'); from gi.repository import WebKit2" 2>/dev/null; then
        MISSING_PKGS+=("python3-gi" "python3-gi-cairo" "gir1.2-gtk-3.0")
        # Try webkit2gtk 4.1 first (Ubuntu 22.04+), fall back to 4.0
        if apt-cache show gir1.2-webkit2-4.1 &>/dev/null 2>&1; then
            MISSING_PKGS+=("gir1.2-webkit2-4.1")
        else
            MISSING_PKGS+=("gir1.2-webkit2-4.0")
        fi
    fi

    if ! python3 -c "import gi; gi.require_version('AppIndicator3', '0.1'); from gi.repository import AppIndicator3" 2>/dev/null; then
        MISSING_PKGS+=("gir1.2-appindicator3-0.1" "libappindicator3-1")
    fi

    if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
        warn "Missing system packages: ${MISSING_PKGS[*]}"
        info "Installing via apt (may prompt for sudo password)..."
        sudo apt-get install -y "${MISSING_PKGS[@]}" || \
            warn "apt install failed -- you may need to install these manually"
    else
        ok "WebKit2GTK and AppIndicator already present"
    fi
elif [ "$PLATFORM" = "Darwin" ]; then
    ok "macOS -- WKWebView is built-in, no extra system packages needed"
fi

# =============================================================================
head "Step 3 / 4 -- Installing / upgrading Python packages"

DEPS=(
    "pyinstaller>=6.0"
    "pywebview>=5.0.0"
    "pystray>=0.19.0"
    "pillow>=10.0.0"
    "fastapi>=0.110.0"
    "uvicorn[standard]>=0.29.0"
    "jinja2>=3.1.0"
    "python-multipart>=0.0.9"
    "itsdangerous>=2.1.0"
    "starlette>=0.36.0"
    "requests>=2.31.0"
    "aiohttp>=3.9.0"
    "httpx>=0.27.0"
    "websockets>=12.0"
    "cryptography>=42.0.0"
    "bcrypt>=4.0.0"
    "PyJWT[crypto]>=2.8.0"
)

info "Upgrading pip..."
"$PY" -m pip install --upgrade --quiet pip

info "Installing packages..."
"$PY" -m pip install --upgrade --quiet "${DEPS[@]}"
ok "All Python packages installed"

# =============================================================================
head "Step 4 / 4 -- Building with PyInstaller"

[ ! -f "xylon_eve.spec" ] && fail "xylon_eve.spec not found. Run this script from inside the xylon_eve/ folder."

# Clean old output
for dir in build dist; do
    [ -d "$dir" ] && { info "Removing old $dir/"; rm -rf "$dir"; }
done

info "Running PyInstaller (this takes a minute)..."
"$PY" -m PyInstaller xylon_eve.spec --clean --noconfirm

EXE="dist/StellarInsight"
[ ! -f "$EXE" ] && fail "Expected dist/StellarInsight was not produced."

chmod +x "$EXE"
SIZE=$(du -sh "$EXE" | cut -f1)
ok "dist/StellarInsight  ($SIZE)"

# -- Create a .desktop launcher (Linux only) ----------------------------------
if [ "$PLATFORM" = "Linux" ]; then
    DESKTOP_FILE="dist/StellarInsight.desktop"
    ABS_EXE="$(pwd)/dist/StellarInsight"
    ABS_ICON="$(pwd)/static/splash.png"

    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Stellar Insight
Comment=EVE Online companion app
Exec=$ABS_EXE
Icon=$ABS_ICON
Terminal=false
Categories=Game;
StartupWMClass=StellarInsight
EOF
    chmod +x "$DESKTOP_FILE"
    ok "dist/StellarInsight.desktop"

    info "To add to your applications menu:"
    info "  cp dist/StellarInsight.desktop ~/.local/share/applications/"
fi

# -- Package into a tarball ---------------------------------------------------
info "Creating distribution archive..."
ARCHIVE="dist/StellarInsight_$(uname -s | tr '[:upper:]' '[:lower:]').tar.gz"
tar -czf "$ARCHIVE" -C dist StellarInsight $([ "$PLATFORM" = "Linux" ] && echo "StellarInsight.desktop" || true)
ASIZE=$(du -sh "$ARCHIVE" | cut -f1)
ok "$ARCHIVE  ($ASIZE)"

# -- Generate SHA-256 checksums (for installer integrity verification) --------
info "Generating SHA-256 checksums..."
BINARY_HASH="dist/StellarInsight.sha256"
ARCHIVE_HASH="${ARCHIVE}.sha256"

if command -v sha256sum &>/dev/null; then
    sha256sum "dist/StellarInsight"          | awk '{print $1}' > "$BINARY_HASH"
    sha256sum "$ARCHIVE"                      | awk '{print $1}' > "$ARCHIVE_HASH"
elif command -v shasum &>/dev/null; then
    shasum -a 256 "dist/StellarInsight"       | awk '{print $1}' > "$BINARY_HASH"
    shasum -a 256 "$ARCHIVE"                  | awk '{print $1}' > "$ARCHIVE_HASH"
else
    warn "sha256sum / shasum not found — skipping checksum generation"
fi

if [ -f "$BINARY_HASH" ]; then
    ok "$BINARY_HASH"
    ok "$ARCHIVE_HASH"
fi

# =============================================================================
echo ""
echo -e "${GREEN}========================================================${NC}"
echo -e "${GREEN}  Build complete!${NC}"
echo ""
echo -e "  Binary   ->  dist/StellarInsight"
echo -e "  Archive  ->  $ARCHIVE"
if [ "$PLATFORM" = "Linux" ]; then
echo -e "  Launcher ->  dist/StellarInsight.desktop"
fi
echo -e "  Checksums -> $BINARY_HASH / $ARCHIVE_HASH"
echo -e "${GREEN}========================================================${NC}"
echo ""
