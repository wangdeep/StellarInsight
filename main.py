"""
Stellar Insight -- desktop entry point.

Starts the embedded FastAPI server on a background thread, opens the UI in a
dedicated pywebview window (no browser required), and creates a system tray
icon so the app lives in the taskbar when minimised.

Supports: Windows, Linux, macOS.

Build:
    pip install pyinstaller pywebview pystray pillow
    pyinstaller xylon_eve.spec          # Windows
    bash build.sh                       # Linux / macOS
"""
from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

# ── Platform ──────────────────────────────────────────────────────────────────
_PLATFORM = platform.system()   # 'Windows', 'Linux', 'Darwin'

# ── Null out stdout/stderr if missing (console=False PyInstaller build) ───────
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


# ── Data / log directory (XDG on Linux, AppData on Windows, ~/Library on Mac) ─
def _data_dir() -> Path:
    if _PLATFORM == "Windows":
        base = os.environ.get("APPDATA") or Path.home()
    elif _PLATFORM == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    d = Path(base) / "StellarInsight"
    d.mkdir(parents=True, exist_ok=True)
    return d


_log_dir  = _data_dir()
_log_file = _log_dir / "stellarinsight.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("stellar_insight")
logger.info("Platform: %s  |  Log: %s", _PLATFORM, _log_file)


# ── Cross-platform error dialog ───────────────────────────────────────────────
def _show_dialog(title: str, message: str) -> None:
    """Show a native-ish error dialog on any platform."""
    if _PLATFORM == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # type: ignore
            return
        except Exception:
            pass
    # Linux / macOS — try tkinter, fall back to zenity/osascript, then stderr
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
        return
    except Exception:
        pass
    if _PLATFORM == "Linux":
        try:
            import subprocess
            subprocess.run(["zenity", "--error", f"--title={title}", f"--text={message}"],
                           timeout=30)
            return
        except Exception:
            pass
    if _PLATFORM == "Darwin":
        try:
            import subprocess
            # Escape backslashes and double-quotes so user-controlled strings
            # can't break out of the AppleScript string literal.
            safe_title   = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                            f'display alert "{safe_title}" message "{safe_message}"'], timeout=30)
            return
        except Exception:
            pass
    # Last resort: just print
    print(f"\n[{title}]\n{message}\n", file=sys.stderr)


def _fatal(title: str, message: str) -> None:
    logger.error("%s: %s", title, message)
    _show_dialog(title, message)
    sys.exit(1)


# ── Port helpers ──────────────────────────────────────────────────────────────
EVE_CALLBACK_PORT = 7742


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _wait_for_server(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


# ── Resource path (dev + PyInstaller bundle) ──────────────────────────────────
def _resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base.joinpath(*parts)


# ── Server thread ─────────────────────────────────────────────────────────────
_server_error: Optional[str] = None


def _run_server(port: int) -> None:
    global _server_error
    try:
        import uvicorn

        os.environ["XYLON_EVE_PORT"] = str(port)
        app_root = str(_resource_path())
        if app_root not in sys.path:
            sys.path.insert(0, app_root)
        os.chdir(app_root)

        logger.info("Importing app from: %s", app_root)
        from app import app as fastapi_app  # noqa: F401
        logger.info("App imported OK -- starting uvicorn on port %d", port)

        # log_config=None prevents uvicorn calling isatty() on a None stdout
        uvicorn.run(
            fastapi_app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
        )
    except Exception:
        _server_error = traceback.format_exc()
        logger.error("Server thread crashed:\n%s", _server_error)


# ── Tray icon ─────────────────────────────────────────────────────────────────
# NOTE: The corp sharing relay server is embedded in app.py and starts
# automatically with the main FastAPI server — there is no separate relay
# process to manage from the tray.
def _load_tray_icon():
    try:
        from PIL import Image
        p = _resource_path("static", "icon.png")
        if p.exists():
            return Image.open(str(p)).resize((64, 64))
    except Exception:
        pass
    try:
        from PIL import Image as _I
        return _I.new("RGB", (64, 64), color=(15, 20, 40))
    except ImportError:
        pass
    return None


def _run_tray(window_ref: list) -> None:
    try:
        import pystray

        def _win():
            return window_ref[0] if window_ref else None

        def open_app(icon, item):
            w = _win()
            if w:
                try: w.show()
                except Exception: pass

        def quit_app(icon, item):
            icon.stop()
            w = _win()
            if w:
                try: w.destroy()
                except Exception: pass
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("Open Stellar Insight", open_app, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        )

        icon = pystray.Icon("Stellar Insight", _load_tray_icon(), "Stellar Insight", menu)
        logger.info("Tray icon running")
        icon.run()

    except ImportError:
        logger.warning("pystray not available -- no tray icon")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            pass
    except Exception as exc:
        logger.error("Tray error: %s", exc)


# ── Webview start (platform-aware backend selection) ─────────────────────────
def _start_webview(window) -> None:
    """
    Start pywebview on the main thread.

    Windows : Edge WebView2 (built into Win10/11) via edgechromium backend
    Linux   : WebKit2GTK  (needs: apt install python3-gi gir1.2-webkit2-4.0)
    macOS   : WKWebView   (built-in, no extra install)
    """
    import webview  # already imported before this is called

    if _PLATFORM == "Windows":
        try:
            webview.start(gui="edgechromium", debug=False)
            return
        except Exception as e:
            logger.warning("edgechromium backend failed (%s), trying default", e)

    # Linux / macOS / fallback
    try:
        webview.start(debug=False)
    except Exception as e:
        _fatal("Stellar Insight - Window Error",
               f"pywebview could not open a window: {e}\n\n"
               + ("Install WebKit2GTK:\n  sudo apt install python3-gi gir1.2-webkit2-4.0\n"
                  if _PLATFORM == "Linux" else "")
               + f"\nLog: {_log_file}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    port = EVE_CALLBACK_PORT

    if not _port_free(port):
        if _wait_for_server(port, timeout=1.0):
            # Another instance is already running -- tell the user and exit cleanly
            _show_dialog(
                "Stellar Insight",
                "Stellar Insight is already running.\n\nCheck your system tray.",
            )
            return
        _fatal(
            "Stellar Insight - Port Conflict",
            f"Port {port} is already in use by another application.\n\n"
            "Stellar Insight requires this port for EVE SSO login.\n"
            "Close the conflicting application and try again.\n\n"
            f"Log: {_log_file}",
        )

    data = _data_dir()   # ensure data dir exists

    # Tell app.py and sde_local.py where all persistent data lives.
    # MUST be set before the server thread imports app.py / sde_local.py.
    os.environ["XYLON_EVE_DATA"]   = str(data)
    os.environ["SDE_SQLITE_PATH"]  = str(data / "sde.sqlite")
    # Also create the nebulae sub-directory now so the static mount never
    # fails on first launch (FastAPI raises on missing StaticFiles dir).
    (data / "nebulae").mkdir(parents=True, exist_ok=True)
    logger.info("Data dir: %s", data)

    app_url = f"http://127.0.0.1:{port}/"
    logger.info("Starting on port %d", port)

    # Start FastAPI server
    server_thread = threading.Thread(target=_run_server, args=(port,), daemon=True, name="uvicorn")
    server_thread.start()

    if not _wait_for_server(port, timeout=25.0):
        _fatal(
            "Stellar Insight - Server Failed to Start",
            f"The internal server did not respond within 25 s.\n\n"
            f"{_server_error or '(no traceback captured)'}\n\nLog: {_log_file}",
        )

    logger.info("Server ready -- opening window")

    # Load pywebview
    try:
        import webview
    except Exception as e:
        _fatal(
            "Stellar Insight - Missing Component",
            f"pywebview could not be loaded:\n\n{e}\n\n"
            "Run:  pip install pywebview\n\n"
            + ("Linux also needs:  sudo apt install python3-gi gir1.2-webkit2-4.0\n"
               if _PLATFORM == "Linux" else "")
            + f"\nLog: {_log_file}",
        )

    window = webview.create_window(
        title="Stellar Insight",
        url=app_url,
        width=1360,
        height=860,
        min_size=(900, 620),
        frameless=False,
        easy_drag=False,
        text_select=True,
        zoomable=True,
    )

    window_ref: list = [window]

    # X button hides to tray; use tray > Quit to fully exit
    def _on_closing():
        window.hide()
        return False

    window.events.closing += _on_closing

    # Tray icon in background thread
    tray_thread = threading.Thread(target=_run_tray, args=(window_ref,), daemon=True, name="tray")
    tray_thread.start()

    # Webview blocks main thread
    _start_webview(window)

    logger.info("Stellar Insight exiting.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        _fatal("Stellar Insight - Startup Error",
               f"Stellar Insight failed to start.\n\n{tb}\n\nLog: {_log_file}")
