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

info()    { echo -e "${CYAN}  $*${NC}"; }
ok()      { echo -e "${GREEN}  [OK]  $*${NC}"; }
warn()    { echo -e "${YELLOW}  [!!]  $*${NC}"; }
fail()    { echo -e "${RED}\n  [FAIL]  $*\n${NC}"; exit 1; }
section() { echo -e "${MAGENTA}\n===  $*  ===${NC}"; }

PLATFORM="$(uname -s)"   # Linux or Darwin

# =============================================================================
section "Step 1 / 5 -- Checking Python"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        VER=$("$candidate" --version 2>&1)
        # Portable minor-version extraction: works on GNU grep and BSD/macOS grep
        MINOR=$(echo "$VER" | grep -o '3\.[0-9]*' | head -1 | cut -d. -f2 || echo "0")
        if [ "${MINOR:-0}" -ge 10 ] 2>/dev/null; then
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
section "Step 2 / 5 -- System dependencies"

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
section "Step 3 / 5 -- Setting up build virtual environment"

VENV_DIR=".build-venv"

# Reuse an existing venv if it looks healthy, otherwise recreate it
if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ]; then
    info "Reusing existing venv at $VENV_DIR"
else
    info "Creating isolated build venv at $VENV_DIR ..."
    "$PY" -m venv "$VENV_DIR"
    ok "Venv created"
fi

VPYTHON="$VENV_DIR/bin/python"
VPIP="$VENV_DIR/bin/pip"

info "Upgrading pip inside venv..."
"$VPYTHON" -m pip install --upgrade --quiet pip

# =============================================================================
section "Step 4 / 5 -- Installing Python packages (into venv)"

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

info "Installing packages..."
"$VPIP" install --quiet "${DEPS[@]}"
ok "All Python packages installed"

# =============================================================================
section "Step 5 / 5 -- Building with PyInstaller"

[ ! -f "xylon_eve.spec" ] && fail "xylon_eve.spec not found. Run this script from inside the StellarInsight/ folder."

# Clean old output
for dir in build dist; do
    [ -d "$dir" ] && { info "Removing old $dir/"; rm -rf "$dir"; }
done

info "Running PyInstaller (this takes a minute)..."
"$VPYTHON" -m PyInstaller xylon_eve.spec --clean --noconfirm

EXE="dist/StellarInsight"
[ ! -f "$EXE" ] && fail "Expected dist/StellarInsight was not produced."

chmod +x "$EXE"
SIZE=$(du -sh "$EXE" | cut -f1)
ok "dist/StellarInsight  ($SIZE)"

# -- Create a .desktop launcher and install helper (Linux only) ---------------
if [ "$PLATFORM" = "Linux" ]; then
    DESKTOP_FILE="dist/StellarInsight.desktop"

    # Use placeholder paths — the install.sh script fills them in at install time
    # so the tarball works regardless of where it's extracted on the target machine.
    cat > "$DESKTOP_FILE" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Stellar Insight
Comment=EVE Online companion app
Exec=INSTALL_PATH_PLACEHOLDER
Icon=ICON_PATH_PLACEHOLDER
Terminal=false
Categories=Game;
StartupWMClass=StellarInsight
DESKTOP

    # Generate a small install script that writes correct absolute paths
    INSTALL_SH="dist/install.sh"
    cat > "$INSTALL_SH" <<'INSTALL'
#!/usr/bin/env bash
# Installs Stellar Insight for the current user (no root needed).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_SRC="$SCRIPT_DIR/StellarInsight"
ICON_SRC="$SCRIPT_DIR/../static/splash.png"

BIN_DEST="$HOME/.local/bin/StellarInsight"
ICON_DEST="$HOME/.local/share/icons/StellarInsight.png"
APP_DEST="$HOME/.local/share/applications/StellarInsight.desktop"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/icons" "$HOME/.local/share/applications"

cp "$BIN_SRC" "$BIN_DEST"
chmod +x "$BIN_DEST"

[ -f "$ICON_SRC" ] && cp "$ICON_SRC" "$ICON_DEST" || ICON_DEST="application-x-executable"

sed "s|INSTALL_PATH_PLACEHOLDER|$BIN_DEST|g; s|ICON_PATH_PLACEHOLDER|$ICON_DEST|g" \
    "$SCRIPT_DIR/StellarInsight.desktop" > "$APP_DEST"

echo "Installed to $BIN_DEST"
echo "Desktop entry written to $APP_DEST"
echo "You may need to log out and back in for the app to appear in your launcher."
INSTALL

    chmod +x "$INSTALL_SH"
    ok "dist/StellarInsight.desktop"
    ok "dist/install.sh"
    info "Users should run ./install.sh after extracting the archive."
fi

# -- Package into a tarball ---------------------------------------------------
info "Creating distribution archive..."
ARCHIVE="dist/StellarInsight_$(uname -s | tr '[:upper:]' '[:lower:]').tar.gz"

if [ "$PLATFORM" = "Linux" ]; then
    tar -czf "$ARCHIVE" -C dist StellarInsight StellarInsight.desktop install.sh
else
    tar -czf "$ARCHIVE" -C dist StellarInsight
fi

ASIZE=$(du -sh "$ARCHIVE" | cut -f1)
ok "$ARCHIVE  ($ASIZE)"

# -- Generate SHA-256 checksums (for installer integrity verification) --------
info "Generating SHA-256 checksums..."
BINARY_HASH="dist/StellarInsight.sha256"
ARCHIVE_HASH="${ARCHIVE}.sha256"

if command -v sha256sum &>/dev/null; then
    sha256sum "dist/StellarInsight" | awk '{print $1}' > "$BINARY_HASH"
    sha256sum "$ARCHIVE"            | awk '{print $1}' > "$ARCHIVE_HASH"
elif command -v shasum &>/dev/null; then
    shasum -a 256 "dist/StellarInsight" | awk '{print $1}' > "$BINARY_HASH"
    shasum -a 256 "$ARCHIVE"            | awk '{print $1}' > "$ARCHIVE_HASH"
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
echo -e "  Installer->  dist/install.sh"
fi
echo -e "  Checksums -> $BINARY_HASH / $ARCHIVE_HASH"
echo -e "${GREEN}========================================================${NC}"
echo ""
