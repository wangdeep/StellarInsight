# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Stellar Insight -- Windows single-file executable.
#
# Build:
#   pip install pyinstaller pywebview pystray aiohttp websockets httpx
#   pyinstaller xylon_eve.spec
#
# Output: dist/StellarInsight.exe

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

block_cipher = None

# ── Bundled data files ────────────────────────────────────────────────────────
added_files = [
    ("templates",       "templates"),
    ("static",          "static"),
    ("eve",             "eve"),
    ("data",            "data"),
    ("relay_server.py", "."),
]

# pywebview resource files (JS bridge etc.)
try:
    added_files += collect_data_files("webview")
except Exception:
    pass

# ── Use collect_all for packages that have C extensions + dynamic imports ─────
# collect_all returns (datas, binaries, hiddenimports) -- covers everything.
extra_datas    = []
extra_binaries = []
extra_imports  = []

for pkg in ("aiohttp", "multidict", "yarl", "aiosignal", "frozenlist", "aiohappyeyeballs"):
    try:
        d, b, h = collect_all(pkg)
        extra_datas    += d
        extra_binaries += b
        extra_imports  += h
    except Exception:
        pass

added_files += extra_datas

# ── Hidden imports ────────────────────────────────────────────────────────────
hidden_imports = [
    # FastAPI / Starlette / Uvicorn
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "starlette.middleware.sessions",
    "starlette.staticfiles",
    "starlette.templating",
    "starlette.routing",
    "starlette.responses",
    "multipart",
    # Crypto
    "cryptography",
    "cryptography.fernet",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.hazmat.primitives.asymmetric.ec",
    "cryptography.hazmat.backends",
    "cryptography.hazmat.backends.openssl",
    # bcrypt — constant-time password hashing (C-1 dev bypass)
    "bcrypt",
    # PyJWT — local JWKS JWT validation for EVE SSO (L-4)
    "jwt",
    "jwt.algorithms",
    "jwt.exceptions",
    "jwt.jwks_client",
    "jwt.jwk_set_cache",
    # HTTP / WebSocket
    "requests",
    "requests.adapters",
    "requests.auth",
    "urllib3",
    "certifi",
    "httpx",
    "httpx._transports.default",
    "websockets",
    "websockets.legacy",
    "websockets.legacy.client",
    "websockets.legacy.server",
    # Desktop window (pywebview)
    "webview",
    "webview.platforms",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    # Tray
    "pystray",
    "pystray._base",
    "PIL",
    "PIL.Image",
    # App
    "app",
    "eve",
    "eve.eve_sso",
    "eve.eve_esi",
    "eve.eve_scopes",
    "eve.eve_scope_guard",
    "eve.esi_cache",
    "eve.memory_sqlite",
    "eve.game_eve",
    "eve.sde_local",
    "eve.relay_client",
    "eve.chain_mapper",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=extra_binaries,
    datas=added_files,
    hiddenimports=hidden_imports + extra_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "discord", "yt_dlp", "matplotlib", "numpy", "pandas",
        "sklearn", "torch", "tensorflow", "openai", "tiktoken",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="StellarInsight",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="static/icon.ico",
)
