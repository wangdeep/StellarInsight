# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run in development:**
```
python main.py
```

**Build for production (Windows):**
```
pyinstaller xylon_eve.spec
```

**Full release (version bump + GitHub release):**
```
RELEASE.bat        # triggers build.ps1 -Release
```

## Architecture

StellarInsight is a Windows desktop app built from three layers:

1. **`main.py`** — Entry point. Starts uvicorn on port 7742 in a daemon thread, waits for it to be ready, then opens a pywebview window (Edge WebView2 backend on Windows) pointing at `http://127.0.0.1:7742/`. A pystray tray icon runs in a second daemon thread. The window X button hides to tray; actual quit is through the tray menu (`os._exit(0)`).

2. **`app.py`** — Monolithic FastAPI application (~300 KB). All routes live here under `/app/api/`. Key subsystems:
   - **ESI caching**: `_resp_cache_get(key, ttl)` / `_resp_cache_set(key, payload, ttl)` read/write a `esi_cache` SQLite table. Always check cache TTL before making ESI calls.
   - **Name resolution**: `_resolve_entity_names(ids)`, `_resolve_facility_names(ids, token)`, `_resolve_planet_names(ids)` batch-resolve ESI IDs to names. `get_skill_names(ids)` hits SDE instead of ESI.
   - **EVE SSO / PKCE**: OAuth2 callback handled on port 7742 (`/callback`). Refresh tokens are Fernet-encrypted in SQLite. No client secret.
   - **Updater**: `update_start` endpoint downloads installer, runs it with `subprocess.Popen`, then calls `os._exit(0)` after a 2 s sleep (so the Inno Setup installer can replace the binary). Clears the `__update_check__` ESI cache row before exiting to avoid stale version display after re-launch.
   - **Relay server**: `relay_server.py` is a standalone FastAPI WebSocket app (port 777) also mounted as a sub-app inside the main app. Corp data sharing relay — user-startable from tray.

3. **`templates/`** — Jinja2 templates. All pages extend `base.html`.
   - **`base.html`**: Fixed ticker bar (top), fixed topbar, fixed sidebar nav with `transform: translateX` slide. Sidebar state is `body.sidebar-open` class, toggled by `xylonSidebarToggle()`, persisted in `localStorage`. The content area is `main.wrap`; its `padding-left` adjusts via `body.sidebar-open main.wrap` rule (`--sidebar-w: 162px`).
   - **`eve_dashboard.html`**: Home page. Configurable flex-wrap grid of widgets. Widget order/config stored in `localStorage` as `stellar_dash2_{alias}` JSON. Widgets are draggable (mouse-event drag, not HTML5 drag API — WebView2 does not reliably support HTML5 drag). Each widget has a resize handle that snaps to named sizes `sm/md/lg/xl` (25/33/50/100% grid width). `SIZE_CYCLE` controls the size-button cycling order.

## Key conventions

**CSS layout — sidebar + pages**: `base.html`'s sidebar-open padding rule uses `!important`. Any page that overrides `padding` on `main.wrap` must use individual padding properties (not the `padding:` shorthand) to avoid clobbering `padding-left`.

**Drag in WebView2**: HTML5 `dragstart`/`dragover`/`drop` is unreliable inside pywebview's WebView2. Use `mousedown`/`mousemove`/`mouseup` pointer events for any drag interaction.

**ESI data in the dashboard**: `api_eve_dashboard()` must resolve all IDs to human-readable names before returning. Industry jobs → `blueprint_type_name`, `product_type_name`, `facility_name`. PI planets → `planet_name`, `system_name`. Skill queue items → `skill_name` (from SDE, not ESI).

**SDE availability**: Always guard SDE calls with `sde_available()` from `eve/sde_local.py`. The `sde.sqlite` file lives in `XYLON_EVE_DATA` (set by `main.py` at startup).

**WebView2 detection**: Pre-check the Windows registry for GUID `{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` under `EdgeUpdate\Clients` before calling `webview.start(gui="edgechromium")`. This avoids pywebview's bootstrapper download loop.

**Data/log directory**: `_data_dir()` in `main.py` — `%APPDATA%\StellarInsight` on Windows. Passed to `app.py` via `XYLON_EVE_DATA` env var. Never hardcode paths.

## Dependencies

Core runtime: `fastapi`, `uvicorn`, `pywebview`, `pystray`, `pillow`, `cryptography` (Fernet), `aiohttp` or `httpx` for ESI calls.  
Build: `pyinstaller`.  
The `eve/` subpackage contains `eve_esi.py` (ESI HTTP client with retry/backoff) and `sde_local.py` (SDE SQLite queries).
