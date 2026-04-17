"""
Xylon EVE – standalone FastAPI app (no Discord, no login).

Single local user (LOCAL_USER_ID = 1). EVE characters are linked via
EVE SSO / PKCE.  All data stored in a local SQLite database.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import time
import datetime
import json
import base64
import asyncio
from typing import Any, Dict, List, Optional

import aiohttp
import requests
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from eve.eve_esi import esi_get_json, esi_get_public_json, universe_names
from eve.eve_sso import (
    build_pkce_authorize_url,
    exchange_code_pkce,
    refresh_access_token,
    verify_access_token,
    encrypt_refresh_token,
    decrypt_refresh_token,
    get_sso_scopes,
)
from eve.memory_sqlite import MemoryStore
from eve.relay_client import relay as _relay

logger = logging.getLogger("xylon_eve")

# ── Configuration ─────────────────────────────────────────────────────────────

# Developer: fill in your EVE developer application CLIENT_ID here before
# building. Register at https://developers.eveonline.com as a "Native" app.
# No client secret is needed; PKCE handles authentication securely.
EVE_CLIENT_ID: str = os.environ.get("EVE_SSO_CLIENT_ID", "07e9baf3137b463e9b35994bb30071ac")
# Make the client ID available to eve_sso.py which reads it via os.getenv()
os.environ["EVE_SSO_CLIENT_ID"] = EVE_CLIENT_ID
os.environ["EVE_CLIENT_ID"] = EVE_CLIENT_ID

# Port the embedded server listens on. Override with ENV if you need a
# different port (e.g. if 7742 is taken).
APP_PORT: int = int(os.environ.get("XYLON_EVE_PORT", "7742"))

# Redirect URI registered in your EVE developer app.
REDIRECT_URI: str = f"http://localhost:{APP_PORT}/eve/callback"

# Single local user — no login required for a desktop app.
LOCAL_USER_ID: int = 1
LOCAL_USER: Dict[str, Any] = {"id": LOCAL_USER_ID, "username": "capsuleer", "global_name": "Capsuleer"}

APP_PREFIX = "/app"
API_PREFIX = "/app/api"

ESI_BASE = "https://esi.evetech.net/latest"


def _db_path() -> str:
    # 1. Explicit override via env (set by main.py to %APPDATA%\StellarInsight)
    base = os.environ.get("XYLON_EVE_DATA", "")
    if base:
        return os.path.join(base, "xylon_eve.db")
    # 2. Running in dev (no PyInstaller) — store in project data/ folder
    here = os.path.dirname(os.path.abspath(__file__))
    # Guard against running inside a PyInstaller _MEIPASS temp dir
    if "_MEI" not in here:
        return os.path.join(here, "data", "xylon_eve.db")
    # 3. Fallback: use the OS user data directory directly
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        base = os.path.join(appdata, "StellarInsight")
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        base = os.path.join(xdg, "StellarInsight")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "xylon_eve.db")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    )
    return cur.fetchone() is not None


# ── ESI response cache ────────────────────────────────────────────────────────
# TTL constants (seconds) — tuned to data change frequency
_TTL_STATIC      = 3600 * 6   # 6 h  — corp info, char attributes
_TTL_SLOW        = 3600       # 1 h  — skills (finish on timers)
_TTL_MEDIUM      = 60 * 10    # 10 m — assets, PI, structures
_TTL_FAST        = 60 * 3     # 3 m  — industry jobs, orders
_TTL_WALLET      = 60 * 2     # 2 m  — wallet balance, journal
_TTL_SKILLQUEUE  = 60 * 5     # 5 m  — skill queue
_TTL_ZKILL       = 60 * 15    # 15 m — zkillboard per-system intel

def _resp_cache_get(key: str, ttl: int) -> Optional[dict]:
    """Return cached ESI response dict if still fresh, else None."""
    try:
        con = _connect()
        _ensure_esi_cache(con)
        row = con.execute(
            "SELECT data, fetched_at FROM esi_cache WHERE cache_key=?", (key,)
        ).fetchone()
        con.close()
        if not row:
            return None
        if time.time() - float(row["fetched_at"]) > ttl:
            return None
        return json.loads(row["data"])
    except Exception:
        return None

def _resp_cache_set(key: str, payload: dict, ttl: int) -> None:
    """Store ESI response dict with a write-through to the esi_cache table."""
    _esi_cache_set(key, payload, ttl_s=ttl)

# ── ESI name/icon SQLite cache ─────────────────────────────────────────────────

def _ensure_esi_cache(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS esi_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )"""
    )
    con.commit()


def _esi_cache_get(k: str) -> Optional[dict]:
    try:
        con = _connect()
        _ensure_esi_cache(con)
        row = con.execute(
            "SELECT data, fetched_at FROM esi_cache WHERE cache_key=?", (k,)
        ).fetchone()
        con.close()
        if not row:
            return None
        ttl = 86400 * 30
        if time.time() - float(row["fetched_at"]) > ttl:
            return None
        return json.loads(row["data"])
    except Exception:
        return None


def _esi_cache_set(k: str, payload: dict, ttl_s: int = 86400 * 30) -> None:
    try:
        con = _connect()
        _ensure_esi_cache(con)
        con.execute(
            "INSERT OR REPLACE INTO esi_cache (cache_key, data, fetched_at) VALUES (?,?,?)",
            (k, json.dumps(payload), time.time()),
        )
        con.commit()
        con.close()
    except Exception:
        pass


# ── EVE entity resolution helpers ─────────────────────────────────────────────

async def _resolve_entity_names(ids: List[int], *, ttl_s: int = 86400 * 30) -> Dict[int, str]:
    out: Dict[int, str] = {}
    uniq = list(dict.fromkeys([int(i) for i in (ids or []) if i]))
    if not uniq:
        return out
    missing: List[int] = []
    for eid in uniq:
        cached = _esi_cache_get(f"id:{eid}")
        if cached and cached.get("name"):
            out[eid] = str(cached["name"])
        else:
            missing.append(eid)
    try:
        for i in range(0, len(missing), 500):
            names = await universe_names(missing[i : i + 500])
            for eid, name in (names or {}).items():
                out[int(eid)] = str(name)
                _esi_cache_set(f"id:{int(eid)}", {"name": str(name)}, ttl_s)
    except Exception:
        pass
    # SDE fallback: ESI universe_names silently omits inventory type IDs
    if sde_available():
        still_missing = [eid for eid in uniq if eid not in out]
        if still_missing:
            sde_names = get_type_names(still_missing)
            for eid, name in sde_names.items():
                out[eid] = name
                _esi_cache_set(f"id:{eid}", {"name": name}, ttl_s)
    return out


async def _resolve_facility_names(
    ids: List[int], access_token: Optional[str] = None
) -> Dict[int, str]:
    out: Dict[int, str] = {}
    uniq = list(dict.fromkeys([int(i) for i in (ids or []) if i]))
    if not uniq:
        return out
    TTL = 86400 * 7
    missing: List[int] = []
    for fid in uniq:
        cached = _esi_cache_get(f"fac:{fid}")
        if cached and cached.get("name"):
            out[fid] = str(cached["name"])
        else:
            missing.append(fid)
    if not missing:
        return out
    still_missing: List[int] = []
    try:
        for i in range(0, len(missing), 500):
            names = await universe_names(missing[i : i + 500])
            for fid, name in (names or {}).items():
                out[int(fid)] = str(name)
                _esi_cache_set(f"fac:{int(fid)}", {"name": str(name)}, TTL)
        still_missing = [f for f in missing if f not in out]
    except Exception:
        still_missing = [f for f in missing if f not in out]
    if access_token and still_missing:
        for fid in still_missing[:50]:
            if fid <= 1_000_000_000_000:
                continue
            try:
                data = await esi_get_json(
                    f"/universe/structures/{fid}/", access_token=access_token
                )
                name = (data or {}).get("name")
                if name:
                    out[fid] = str(name)
                    _esi_cache_set(f"fac:{fid}", {"name": str(name)}, TTL)
            except Exception:
                continue
    return out


async def _resolve_planet_names(planet_ids: List[int]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    uniq = list(dict.fromkeys([int(i) for i in (planet_ids or []) if i]))
    missing = [
        p
        for p in uniq
        if not (
            _esi_cache_get(f"planet:{p}")
            and _esi_cache_get(f"planet:{p}").get("name")
        )
    ]
    for p in missing:
        cached = _esi_cache_get(f"planet:{p}")
        if cached and cached.get("name"):
            out[p] = str(cached["name"])
    still = [p for p in uniq if p not in out]
    for pid in still[:200]:
        try:
            j = await esi_get_public_json(f"/universe/planets/{pid}/")
            name = (j or {}).get("name") if isinstance(j, dict) else None
            if name:
                out[pid] = str(name)
                _esi_cache_set(f"planet:{pid}", {"name": str(name)}, 86400 * 365)
        except Exception:
            continue
    return out


# ── EVE character DB helpers ───────────────────────────────────────────────────

def _eve_characters_user_col(con: sqlite3.Connection) -> str:
    cols = [r[1] for r in con.execute("PRAGMA table_info(eve_characters)").fetchall()]
    if "user_id" in cols:
        return "user_id"
    if "discord_user_id" in cols:
        return "discord_user_id"
    return "user_id"


def eve_list_characters(user_id: int = LOCAL_USER_ID) -> List[Dict[str, Any]]:
    with _connect() as con:
        if _table_exists(con, "eve_characters"):
            uc = _eve_characters_user_col(con)
            rows = con.execute(
                f"SELECT character_id, character_name, alias, is_default FROM eve_characters "
                f"WHERE {uc}=? ORDER BY is_default DESC, character_name ASC",
                (int(user_id),),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                cid = int(d["character_id"])
                d["portrait_url"] = (
                    f"https://images.evetech.net/characters/{cid}/portrait?size=64"
                )
                out.append(d)
            return out
        return []


def eve_get_character_by_alias(
    user_id: int, alias_or_name: str
) -> Optional[Dict[str, Any]]:
    a = (alias_or_name or "").strip()
    if not a:
        return None
    with _connect() as con:
        if not _table_exists(con, "eve_characters"):
            return None
        uc = _eve_characters_user_col(con)
        cid = int(a) if a.isdigit() else -1
        row = con.execute(
            f"""SELECT {uc} as user_id, character_id, character_name, refresh_token,
                       scopes, alias, is_default, updated_ts
                FROM eve_characters
                WHERE {uc}=? AND (
                    lower(coalesce(alias,''))=lower(?) OR
                    lower(character_name)=lower(?) OR
                    character_id=?
                )
                ORDER BY (lower(coalesce(alias,''))=lower(?)) DESC,
                         is_default DESC, updated_ts DESC
                LIMIT 1""",
            (int(user_id), a, a, cid, a),
        ).fetchone()
        return dict(row) if row else None


def eve_get_watch(user_id: int = LOCAL_USER_ID) -> Dict[str, Any]:
    with _connect() as con:
        if not _table_exists(con, "eve_watch"):
            return {}
        row = con.execute(
            "SELECT * FROM eve_watch WHERE user_id=? LIMIT 1", (int(user_id),)
        ).fetchone()
        return dict(row) if row else {}


async def eve_access_token_for(user_id: int, alias_or_name: str) -> Dict[str, Any]:
    ch = eve_get_character_by_alias(int(user_id), alias_or_name)
    if not ch:
        raise HTTPException(
            status_code=404,
            detail="Character not found — link a character first",
        )
    raw_refresh = decrypt_refresh_token(str(ch.get("refresh_token") or "").strip())
    if not raw_refresh:
        raise HTTPException(status_code=400, detail="Missing refresh token for character")
    try:
        ts = await refresh_access_token(refresh_token=raw_refresh)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Token refresh failed: {e}")
    return {
        "character": {
            "character_id": int(ch["character_id"]),
            "character_name": str(ch.get("character_name") or ""),
            "alias": str(ch.get("alias") or ""),
            "is_default": bool(ch.get("is_default")),
            "scopes": str(ch.get("scopes") or ""),
        },
        "access_token": ts.access_token,
        "scopes": (
            getattr(ts, "scopes", None)
            or getattr(ts, "scope", None)
            or str(ch.get("scopes") or "")
        ),
    }


async def eve_get_corp_id(character_id: int) -> int:
    cache_key = f"corp_id:{character_id}"
    cached = _esi_cache_get(cache_key)
    if cached and cached.get("corp_id"):
        return int(cached["corp_id"])
    try:
        data = await esi_get_public_json(f"/characters/{int(character_id)}/")
    except Exception as e:
        raise RuntimeError(f"ESI unreachable while fetching character info: {e}") from e
    corp_id = int((data or {}).get("corporation_id") or 0)
    if corp_id <= 0:
        raise RuntimeError("Could not resolve corporation ID — ESI may be down or the character is invalid.")
    _esi_cache_set(cache_key, {"corp_id": corp_id}, ttl_s=3600)
    return corp_id


def _fmt_isk(v: Any) -> str:
    try:
        x = float(v)
    except Exception:
        return str(v)
    sign = "-" if x < 0 else ""
    x = abs(x)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if x >= div:
            return f"{sign}{x/div:.2f}{unit} ISK"
    return f"{sign}{x:.0f} ISK"


# ── Require local user (always succeeds — desktop app, no login needed) ───────

def require_user(request: Request) -> Dict[str, Any]:
    """Desktop app: always returns the single local user — no login required."""
    return LOCAL_USER


# ── Premium access constants ───────────────────────────────────────────────────

PREMIUM_PAYMENT_CHAR_ID: int = 2123356839        # ISK receiver character ID
PREMIUM_PAYMENT_MEMO:    str = "SNEK"            # Required substring in wallet memo
PREMIUM_PRICE_ISK:       float = 5_000_000_000   # 5 billion ISK
PREMIUM_DEV_CODE:        str   = "teacup"        # Rotating secret dev code


def _get_instance_id() -> str:
    """Return (or create) the permanent installation UUID."""
    memory = MemoryStore(_db_path())
    return memory.get_or_create_instance_id()


def require_premium(request: Request) -> bool:
    """
    FastAPI dependency — raises 402 if no premium access is found for any
    linked character.  Desktop app: checks the first linked character.
    Returns True on success so callers can use it as a guard.
    """
    chars = eve_list_characters(LOCAL_USER_ID)
    if not chars:
        from fastapi import HTTPException
        raise HTTPException(status_code=402, detail="no_character_linked")
    memory = MemoryStore(_db_path())
    for ch in chars:
        cid = ch.get("character_id") or ch.get("id")
        if cid and memory.premium_is_granted(int(cid)):
            return True
    from fastapi import HTTPException
    raise HTTPException(status_code=402, detail="premium_required")


def get_theme_vars() -> Dict[str, str]:
    return {
        "bg_color": "#0b0e14",
        "accent": "#3ca8e8",
        "text": "#c8d0dc",
        "panel": "rgba(14,18,26,0.92)",
    }


# ── App factory ────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(title="Xylon EVE", version="1.0.0")

    # Load (or generate-and-persist) the session secret so it survives restarts.
    # A fresh random key on every startup invalidates all cookies and forces re-login.
    _mem = MemoryStore(_db_path())
    session_secret = _mem.kv_get("session_secret")
    if not session_secret:
        session_secret = os.environ.get("XYLON_EVE_SESSION_SECRET") or secrets.token_urlsafe(32)
        _mem.kv_set("session_secret", session_secret)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=False,
    )

    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates = Jinja2Templates(directory=os.path.join(base_dir, "templates"))
    app.mount(
        f"{APP_PREFIX}/static",
        StaticFiles(directory=os.path.join(base_dir, "static")),
        name="static",
    )

    # ── Health ──────────────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        return {"ok": True, "ts": time.time()}

    # ── Root redirects ──────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    @app.get(f"{APP_PREFIX}", include_in_schema=False)
    @app.get(f"{APP_PREFIX}/", include_in_schema=False)
    def root_redirect():
        # First-run: if SDE is missing, send user to the download page first.
        if not sde_available():
            return RedirectResponse(url="/app/setup", status_code=302)
        return RedirectResponse(url=f"{APP_PREFIX}/eve_online", status_code=302)

    # ── First-run SDE download page ─────────────────────────────────────────────

    @app.get("/app/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_page(request: Request):
        """First-run page shown when sde.sqlite is not yet present."""
        if sde_available():
            return RedirectResponse(url="/app/eve_online", status_code=302)
        return HTMLResponse(content=_SETUP_HTML)

    @app.post("/app/api/setup/download_sde")
    async def setup_download_sde():
        """Kick off background SDE download."""
        if _sde_dl_state["status"] == "downloading":
            return {"status": "downloading", "progress": _sde_dl_state["progress"]}
        _sde_dl_state["status"] = "downloading"
        _sde_dl_state["progress"] = 0
        _sde_dl_state["error"] = ""
        asyncio.create_task(_download_sde_task())
        return {"status": "started"}

    @app.get("/app/api/setup/sde_status")
    def setup_sde_status():
        """Poll endpoint: returns download progress."""
        ready = sde_available()
        return {
            "ready": ready,
            "status": "ready" if ready else _sde_dl_state["status"],
            "progress": 100 if ready else _sde_dl_state["progress"],
            "error": _sde_dl_state["error"],
        }

    @app.post("/app/api/setup/download_nebulae")
    async def setup_download_nebulae():
        """Kick off background nebula image download (phase 2 of first-run setup)."""
        if _nebulae_dl_state["status"] == "downloading":
            return {"status": "downloading", "progress": _nebulae_dl_state["progress"]}
        _nebulae_dl_state["status"] = "downloading"
        _nebulae_dl_state["progress"] = 0
        _nebulae_dl_state["done"] = 0
        _nebulae_dl_state["total"] = 0
        _nebulae_dl_state["error"] = ""
        asyncio.create_task(_download_nebulae_task())
        return {"status": "started"}

    @app.get("/app/api/setup/nebulae_status")
    def setup_nebulae_status():
        """Poll endpoint for nebula download progress."""
        return {
            "status":   _nebulae_dl_state["status"],
            "progress": _nebulae_dl_state["progress"],
            "done":     _nebulae_dl_state["done"],
            "total":    _nebulae_dl_state["total"],
            "error":    _nebulae_dl_state["error"],
        }

    # ── Settings: asset status + force re-download ──────────────────────────

    @app.get("/app/api/settings/asset_status")
    def settings_asset_status():
        """Return current install status for SDE and nebula images."""
        neb_dir = _nebula_out_dir()
        neb_total = len(_NEBULA_SOURCES)
        neb_have = len([f for f in os.listdir(neb_dir) if f.endswith(".jpg")]) if os.path.isdir(neb_dir) else 0
        sde_path = os.path.join(os.path.dirname(_db_path()), "sde.sqlite")
        sde_size_mb = round(os.path.getsize(sde_path) / 1_048_576, 1) if os.path.exists(sde_path) else 0
        return {
            "sde": {
                "installed": sde_available(),
                "size_mb": sde_size_mb,
                "dl_status":   _sde_dl_state["status"],
                "dl_progress": _sde_dl_state["progress"],
                "dl_error":    _sde_dl_state["error"],
            },
            "nebulae": {
                "have":    neb_have,
                "total":   neb_total,
                "dl_status":   _nebulae_dl_state["status"],
                "dl_progress": _nebulae_dl_state["progress"],
                "dl_done":     _nebulae_dl_state["done"],
                "dl_error":    _nebulae_dl_state["error"],
            },
        }

    @app.post("/app/api/settings/redownload_sde")
    async def settings_redownload_sde():
        """Force re-download of the SDE by closing all connections then deleting the file."""
        import time as _time
        if _sde_dl_state["status"] == "downloading":
            return {"ok": False, "error": "SDE download already in progress"}

        sde_path = os.path.join(os.path.dirname(_db_path()), "sde.sqlite")

        # ── Step 1: close the sde_local module connection ──────────────────
        try:
            import eve.sde_local as _sde_mod
            conn = _sde_mod._sde_conn
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                _sde_mod._sde_conn = None
        except Exception:
            pass

        # ── Step 2: short async yield so any in-flight queries can finish ──
        await asyncio.sleep(0.3)

        # ── Step 3: delete with retries (Windows holds file locks briefly) ─
        for p in [sde_path + ".bz2", sde_path]:
            if not os.path.exists(p):
                continue
            last_err = None
            for attempt in range(5):
                try:
                    os.remove(p)
                    last_err = None
                    break
                except PermissionError as exc:
                    last_err = exc
                    _time.sleep(0.4 * (attempt + 1))  # back-off: 0.4s, 0.8s, 1.2s…
                except Exception as exc:
                    last_err = exc
                    break
            if last_err:
                return {"ok": False, "error": f"Could not remove {os.path.basename(p)}: {last_err}"}

        _sde_dl_state["status"]   = "downloading"
        _sde_dl_state["progress"] = 0
        _sde_dl_state["error"]    = ""
        asyncio.create_task(_download_sde_task())
        return {"ok": True}

    @app.post("/app/api/settings/redownload_nebulae")
    async def settings_redownload_nebulae():
        """Force re-download of all nebula images by clearing the nebulae directory first."""
        if _nebulae_dl_state["status"] == "downloading":
            return {"ok": False, "error": "Nebula download already in progress"}
        neb_dir = _nebula_out_dir()
        if os.path.isdir(neb_dir):
            removed = 0
            for f in os.listdir(neb_dir):
                if f.endswith(".jpg"):
                    try:
                        os.remove(os.path.join(neb_dir, f))
                        removed += 1
                    except Exception:
                        pass
            logger.info(f"[Nebulae] Cleared {removed} cached images for re-download")
        _nebulae_dl_state["status"]   = "downloading"
        _nebulae_dl_state["progress"] = 0
        _nebulae_dl_state["done"]     = 0
        _nebulae_dl_state["total"]    = len(_NEBULA_SOURCES)
        _nebulae_dl_state["error"]    = ""
        asyncio.create_task(_download_nebulae_task())
        return {"ok": True}

    # ── EVE page routes ─────────────────────────────────────────────────────────

    @app.get(f"{APP_PREFIX}/eve_online", response_class=HTMLResponse)
    def eve_online_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        watch = eve_get_watch(LOCAL_USER_ID) or {}
        return templates.TemplateResponse(
            request,
            "eve.html",
            {"user": user,
                "theme": get_theme_vars(),
                "chars": chars,
                "watch": watch},
        )

    @app.get(f"{APP_PREFIX}/market", response_class=HTMLResponse)
    def market_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        return templates.TemplateResponse(request, "market.html", {"user": user, "theme": get_theme_vars(), "chars": chars})

    @app.get(f"{APP_PREFIX}/industry", response_class=HTMLResponse)
    def industry_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        return templates.TemplateResponse(request, "industry.html", {"user": user, "theme": get_theme_vars(), "chars": chars})

    @app.get(f"{APP_PREFIX}/fitting", response_class=HTMLResponse)
    def fitting_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        return templates.TemplateResponse(request, "fitting.html", {"user": user, "theme": get_theme_vars(), "chars": chars})

    @app.get(f"{APP_PREFIX}/structures", response_class=HTMLResponse)
    def structures_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        return templates.TemplateResponse(request, "structures.html", {"user": user, "theme": get_theme_vars(), "chars": chars})

    @app.get(f"{APP_PREFIX}/skills", response_class=HTMLResponse)
    def skills_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        return templates.TemplateResponse(request, "skills.html", {"user": user, "theme": get_theme_vars(), "chars": chars})

    @app.get(f"{APP_PREFIX}/navigator", response_class=HTMLResponse)
    def navigator_page(request: Request, user=Depends(require_user)):
        return templates.TemplateResponse(request, "navigator.html", {"user": user, "theme": get_theme_vars()})

    @app.get(f"{APP_PREFIX}/wormholes", response_class=HTMLResponse)
    def wormholes_page(request: Request, user=Depends(require_user)):
        return templates.TemplateResponse(request, "wormholes.html", {"user": user, "theme": get_theme_vars()})

    @app.get(f"{APP_PREFIX}/eve_console", response_class=HTMLResponse)
    def eve_console_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        return templates.TemplateResponse(request, "eve_console.html", {"user": user, "theme": get_theme_vars(), "chars": chars})

    @app.get(f"{APP_PREFIX}/settings", response_class=HTMLResponse)
    def settings_page(request: Request, user=Depends(require_user)):
        chars = eve_list_characters(LOCAL_USER_ID)
        mem = MemoryStore(_db_path())
        instance_id = mem.get_or_create_instance_id()
        # Short instance reference shown to user for memo field (first 10 chars)
        instance_ref = instance_id.replace("-", "")[:10].upper()
        # Check if any linked character has premium; grab reinstall key for first premium char
        is_premium = False
        reinstall_key = None
        for c in chars:
            cid = c.get("character_id")
            if cid and mem.premium_is_granted(int(cid)):
                is_premium = True
                if reinstall_key is None:
                    reinstall_key = mem.get_reinstall_key(int(cid))
                break
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"user": user,
                "theme": get_theme_vars(),
                "chars": chars,
                "client_id": EVE_CLIENT_ID,
                "port": APP_PORT,
                "is_premium": is_premium,
                "reinstall_key": reinstall_key,
                "instance_ref": instance_ref,
                "payment_char_id": PREMIUM_PAYMENT_CHAR_ID,
                "payment_price_b": int(PREMIUM_PRICE_ISK // 1_000_000_000),
                "payment_memo": PREMIUM_PAYMENT_MEMO},
        )

    # ── Premium access API ──────────────────────────────────────────────────────

    @app.get(f"{API_PREFIX}/premium/status")
    def api_premium_status(user=Depends(require_user)):
        """Return current premium status for the local user."""
        chars = eve_list_characters(LOCAL_USER_ID)
        mem = MemoryStore(_db_path())
        instance_id = mem.get_or_create_instance_id()
        instance_ref = instance_id.replace("-", "")[:10].upper()
        granted_chars = []
        for c in chars:
            cid = c.get("character_id")
            if cid and mem.premium_is_granted(int(cid)):
                rk = mem.get_reinstall_key(int(cid))
                granted_chars.append({
                    "character_id": int(cid),
                    "name": c.get("character_name", ""),
                    "reinstall_key": rk,
                })
        return {
            "ok": True,
            "is_premium": len(granted_chars) > 0,
            "granted_chars": granted_chars,
            "instance_ref": instance_ref,
            "payment_char_id": PREMIUM_PAYMENT_CHAR_ID,
            "payment_price_isk": PREMIUM_PRICE_ISK,
            "payment_memo": PREMIUM_PAYMENT_MEMO,
        }

    @app.post(f"{API_PREFIX}/premium/redeem_key")
    async def api_premium_redeem_key(request: Request, user=Depends(require_user)):
        """Redeem a one-time access key or the rotating dev code."""
        body = await request.json()
        key_input: str = str(body.get("key", "")).strip()
        if not key_input:
            return {"ok": False, "error": "No key provided"}
        chars = eve_list_characters(LOCAL_USER_ID)
        if not chars:
            return {"ok": False, "error": "No EVE character linked"}
        # Use the first/default character
        char = next((c for c in chars if c.get("is_default")), chars[0])
        cid = int(char["character_id"])
        char_name = char.get("character_name", "")
        mem = MemoryStore(_db_path())
        # Check dev code first
        if mem.key_check_dev_code(key_input, cid, current_code=PREMIUM_DEV_CODE):
            reinstall_key = mem.generate_reinstall_key(cid, "dev_code")
            return {"ok": True, "method": "dev_code", "character": char_name, "reinstall_key": reinstall_key}
        # Try one-time key
        if mem.key_redeem(key_input, cid):
            reinstall_key = mem.generate_reinstall_key(cid, "key")
            return {"ok": True, "method": "key", "character": char_name, "reinstall_key": reinstall_key}
        return {"ok": False, "error": "Invalid or already-used key"}

    @app.post(f"{API_PREFIX}/premium/verify_payment")
    async def api_premium_verify_payment(request: Request, user=Depends(require_user)):
        """
        Check the linked character's wallet journal for a payment to the
        premium character ID with the correct memo substring.
        """
        chars = eve_list_characters(LOCAL_USER_ID)
        if not chars:
            return {"ok": False, "error": "No EVE character linked"}
        char = next((c for c in chars if c.get("is_default")), chars[0])
        alias = char.get("alias") or char.get("character_name")
        cid = int(char["character_id"])
        mem = MemoryStore(_db_path())
        # Already granted?
        if mem.premium_is_granted(cid):
            return {"ok": True, "already_granted": True, "character": char.get("character_name")}
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
        except Exception as e:
            return {"ok": False, "error": f"Could not refresh token: {e}"}
        access_token = token.get("access_token")
        if not access_token:
            return {"ok": False, "error": "No access token — re-link your character"}
        from eve.eve_esi import get_wallet_journal
        try:
            # Check last 3 pages of journal to find the payment
            found = False
            for page in range(1, 4):
                entries = await get_wallet_journal(cid, access_token=access_token, page=page)
                if not entries:
                    break
                for entry in entries:
                    # ref_type "player_donation" or "corporate_reward", amount > threshold
                    # second_party_id is the recipient
                    if (
                        entry.get("second_party_id") == PREMIUM_PAYMENT_CHAR_ID
                        and float(entry.get("amount", 0)) >= PREMIUM_PRICE_ISK
                        and PREMIUM_PAYMENT_MEMO.upper() in str(entry.get("reason", "")).upper()
                    ):
                        found = True
                        break
                if found:
                    break
        except Exception as e:
            return {"ok": False, "error": f"ESI wallet lookup failed: {e}"}
        if found:
            mem.premium_grant(cid, granted_by="isk_payment", note="wallet_journal_verified")
            reinstall_key = mem.generate_reinstall_key(cid, "isk_payment")
            return {"ok": True, "verified": True, "character": char.get("character_name"), "reinstall_key": reinstall_key}
        return {
            "ok": False,
            "verified": False,
            "error": (
                f"No matching payment found. Make sure you sent at least "
                f"{int(PREMIUM_PRICE_ISK // 1_000_000_000)}B ISK to character ID "
                f"{PREMIUM_PAYMENT_CHAR_ID} with '{PREMIUM_PAYMENT_MEMO}' in the memo."
            ),
        }

    @app.post(f"{API_PREFIX}/premium/redeem_reinstall_key")
    async def api_premium_redeem_reinstall_key(request: Request, user=Depends(require_user)):
        """
        Validate a character-bound reinstall key.
        The key must match the calling character (last-4 suffix check + DB lookup).
        On success, premium is restored for that character.
        """
        body = await request.json()
        key_input: str = str(body.get("key", "")).strip().upper()
        if not key_input:
            return {"ok": False, "error": "No key provided"}
        chars = eve_list_characters(LOCAL_USER_ID)
        if not chars:
            return {"ok": False, "error": "No EVE character linked"}
        char = next((c for c in chars if c.get("is_default")), chars[0])
        cid = int(char["character_id"])
        char_name = char.get("character_name", "")
        mem = MemoryStore(_db_path())
        result = mem.redeem_reinstall_key(key_input, cid)
        if result == "ok":
            # Key matched — premium already stored in DB; generate a fresh key for this install
            new_key = mem.generate_reinstall_key(cid, "reinstall")
            return {"ok": True, "character": char_name, "reinstall_key": new_key}
        if result == "wrong_character":
            return {"ok": False, "error": "This key belongs to a different character. Link the correct character first."}
        return {"ok": False, "error": "Key not found or already used"}

    # ── EVE character API ───────────────────────────────────────────────────────

    @app.get(f"{API_PREFIX}/eve/chars")
    @app.get(f"{API_PREFIX}/eo/chars")
    def api_eve_chars(user=Depends(require_user)):
        return {"ok": True, "chars": eve_list_characters(LOCAL_USER_ID)}

    @app.get(f"{API_PREFIX}/eve/character/summary")
    @app.get(f"{API_PREFIX}/eo/character/summary")
    async def api_char_summary(alias: str, user=Depends(require_user)):
        token = await eve_access_token_for(LOCAL_USER_ID, alias)
        ch = token["character"]
        cid = int(ch["character_id"])
        pub = await esi_get_public_json(f"/characters/{cid}/")
        corp_id = (pub or {}).get("corporation_id")
        alliance_id = (pub or {}).get("alliance_id")
        names: Dict[int, str] = {}
        ids_to_resolve = [i for i in [corp_id, alliance_id] if i]
        if ids_to_resolve:
            names = await _resolve_entity_names(ids_to_resolve)
        portrait = f"https://images.evetech.net/characters/{cid}/portrait?size=128"
        return {
            "ok": True,
            "character": {
                **ch,
                "portrait_url": portrait,
                "corporation_id": corp_id,
                "corporation_name": names.get(corp_id, str(corp_id)) if corp_id else None,
                "alliance_id": alliance_id,
                "alliance_name": names.get(alliance_id) if alliance_id else None,
                "security_status": (pub or {}).get("security_status"),
            },
        }

    @app.get(f"{API_PREFIX}/eve/character/pilot_panel")
    @app.get(f"{API_PREFIX}/eo/character/pilot_panel")
    async def api_pilot_panel(alias: str, user=Depends(require_user)):
        token = await eve_access_token_for(LOCAL_USER_ID, alias)
        ch = token["character"]
        cid = int(ch["character_id"])
        at = token["access_token"]

        pub, loc, ship, online = await asyncio.gather(
            esi_get_public_json(f"/characters/{cid}/"),
            esi_get_json(f"/characters/{cid}/location/", access_token=at),
            esi_get_json(f"/characters/{cid}/ship/", access_token=at),
            esi_get_json(f"/characters/{cid}/online/", access_token=at),
            return_exceptions=True,
        )

        corp_id = (pub or {}).get("corporation_id") if isinstance(pub, dict) else None
        alliance_id = (pub or {}).get("alliance_id") if isinstance(pub, dict) else None
        ids_to_resolve = [i for i in [corp_id, alliance_id] if i]
        names = await _resolve_entity_names(ids_to_resolve) if ids_to_resolve else {}

        # Ship type name
        ship_type_id = (ship or {}).get("ship_type_id") if isinstance(ship, dict) else None
        ship_names: Dict[int, str] = {}
        if ship_type_id:
            ship_names = await _resolve_entity_names([int(ship_type_id)])

        # Location name
        solar_system_id = (loc or {}).get("solar_system_id") if isinstance(loc, dict) else None
        loc_names: Dict[int, str] = {}
        if solar_system_id:
            loc_names = await _resolve_entity_names([int(solar_system_id)])

        portrait = f"https://images.evetech.net/characters/{cid}/portrait?size=128"
        corp_logo = (
            f"https://images.evetech.net/corporations/{corp_id}/logo?size=64"
            if corp_id else None
        )

        return {
            "ok": True,
            "portrait_url": portrait,
            "corp_logo_url": corp_logo,
            "character_id": cid,
            "character_name": ch.get("character_name"),
            "security_status": (pub or {}).get("security_status") if isinstance(pub, dict) else None,
            "corporation_id": corp_id,
            "corporation_name": names.get(corp_id) if corp_id else None,
            "alliance_id": alliance_id,
            "alliance_name": names.get(alliance_id) if alliance_id else None,
            "online": (online or {}).get("online") if isinstance(online, dict) else None,
            "last_login": (online or {}).get("last_login") if isinstance(online, dict) else None,
            "location_system_id": solar_system_id,
            "location_name": loc_names.get(solar_system_id) if solar_system_id else None,
            "ship_type_id": ship_type_id,
            "ship_name": (ship or {}).get("ship_name") if isinstance(ship, dict) else None,
            "ship_type_name": ship_names.get(int(ship_type_id)) if ship_type_id else None,
            "scopes": ch.get("scopes", ""),
        }

    @app.get(f"{API_PREFIX}/eve/character/skills")
    @app.get(f"{API_PREFIX}/eo/character/skills")
    async def api_char_skills(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = token['character']['character_id']
            _ck = f"resp:char_skills:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_SLOW)): return cached
            data = await esi_get_json(f"/characters/{cid}/skills/", access_token=token["access_token"])
            result = {"ok": True, "skills": data}
            _resp_cache_set(_ck, result, _TTL_SLOW)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "skills": []}

    @app.get(f"{API_PREFIX}/eve/character/attributes")
    @app.get(f"{API_PREFIX}/eo/character/attributes")
    async def api_char_attributes(alias: str, user=Depends(require_user)):
        """Return character sheet attributes (including implant bonuses) from ESI."""
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_attrs:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_STATIC)): return cached
            data = await esi_get_json(f"/characters/{cid}/attributes/", access_token=token["access_token"])
            attrs = {
                "intelligence": int(data.get("intelligence") or 17),
                "memory":       int(data.get("memory")       or 17),
                "perception":   int(data.get("perception")   or 17),
                "willpower":    int(data.get("willpower")    or 17),
                "charisma":     int(data.get("charisma")     or 17),
                "bonus_remaps":              data.get("bonus_remaps"),
                "last_remap_date":           data.get("last_remap_date"),
                "accrued_remap_cooldown_date": data.get("accrued_remap_cooldown_date"),
            }
            result = {"ok": True, "attributes": attrs}
            _resp_cache_set(_ck, result, _TTL_STATIC)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "attributes": {}}

    @app.get(f"{API_PREFIX}/eve/character/skillqueue")
    @app.get(f"{API_PREFIX}/eo/character/skillqueue")
    async def api_char_skillqueue(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = token['character']['character_id']
            _ck = f"resp:char_skillqueue:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_SKILLQUEUE)): return cached
            data = await esi_get_json(f"/characters/{cid}/skillqueue/", access_token=token["access_token"])
            result = {"ok": True, "skillqueue": data}
            _resp_cache_set(_ck, result, _TTL_SKILLQUEUE)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "skillqueue": []}

    @app.get(f"{API_PREFIX}/eve/character/wallet")
    @app.get(f"{API_PREFIX}/eo/character/wallet")
    async def api_char_wallet(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_wallet:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_WALLET)): return cached
            bal = await esi_get_json(f"/characters/{cid}/wallet/", access_token=token["access_token"])
            result = {"ok": True, "balance": bal, "formatted": _fmt_isk(bal)}
            _resp_cache_set(_ck, result, _TTL_WALLET)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "balance": None}

    @app.get(f"{API_PREFIX}/eve/character/journal")
    @app.get(f"{API_PREFIX}/eo/character/journal")
    async def api_char_journal(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_journal:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_WALLET)): return cached
            data = await esi_get_json(f"/characters/{cid}/wallet/journal/", access_token=token["access_token"]) or []
            party_ids = list({int(e[k]) for e in data for k in ("first_party_id", "second_party_id") if e.get(k)})
            id_names = await _resolve_entity_names(party_ids) if party_ids else {}
            for e in data:
                for k in ("first_party_id", "second_party_id"):
                    pid = e.get(k)
                    if pid:
                        e[k.replace("_id", "_name")] = id_names.get(int(pid), "")
            result = {"ok": True, "journal": data, "id_names": {str(k): v for k, v in id_names.items()}}
            _resp_cache_set(_ck, result, _TTL_WALLET)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "journal": []}

    @app.get(f"{API_PREFIX}/eve/character/orders")
    @app.get(f"{API_PREFIX}/eo/character/orders")
    async def api_char_orders(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_orders:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_FAST)): return cached
            data = await esi_get_json(f"/characters/{cid}/orders/", access_token=token["access_token"])
            type_ids = list({int(o["type_id"]) for o in (data or []) if o.get("type_id")})
            loc_ids = list({int(o["location_id"]) for o in (data or []) if o.get("location_id")})
            names = await _resolve_entity_names(type_ids) if type_ids else {}
            locs = await _resolve_facility_names(loc_ids, token["access_token"]) if loc_ids else {}
            for o in data or []:
                o["type_name"] = names.get(int(o.get("type_id", 0)), "")
                o["location_name"] = locs.get(int(o.get("location_id", 0)), "")
            result = {"ok": True, "orders": data, "type_names": {str(k): v for k, v in names.items()}, "location_names": {str(k): v for k, v in locs.items()}}
            _resp_cache_set(_ck, result, _TTL_FAST)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "orders": []}

    @app.get(f"{API_PREFIX}/eve/character/assets")
    @app.get(f"{API_PREFIX}/eo/character/assets")
    async def api_char_assets(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_assets:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_MEDIUM)): return cached
            assets = await esi_get_json(f"/characters/{cid}/assets/", access_token=token["access_token"])
            type_ids = list({int(a["type_id"]) for a in (assets or []) if a.get("type_id")})
            loc_ids = list({int(a["location_id"]) for a in (assets or []) if a.get("location_id")})
            names = await _resolve_entity_names(type_ids) if type_ids else {}
            locs = await _resolve_facility_names(loc_ids, token["access_token"]) if loc_ids else {}
            for a in assets or []:
                a["type_name"] = names.get(int(a.get("type_id", 0)), "")
                a["location_name"] = locs.get(int(a.get("location_id", 0)), "")
            result = {"ok": True, "assets": assets, "type_names": {str(k): v for k, v in names.items()}, "location_names": {str(k): v for k, v in locs.items()}}
            _resp_cache_set(_ck, result, _TTL_MEDIUM)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "assets": []}

    @app.get(f"{API_PREFIX}/eve/character/blueprints")
    @app.get(f"{API_PREFIX}/eo/character/blueprints")
    async def api_char_blueprints(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_blueprints:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_MEDIUM)): return cached
            data = await esi_get_json(f"/characters/{cid}/blueprints/", access_token=token["access_token"])
            type_ids = list({int(b["type_id"]) for b in (data or []) if b.get("type_id")})
            names = await _resolve_entity_names(type_ids) if type_ids else {}
            for b in data or []:
                b["type_name"] = names.get(int(b.get("type_id", 0)), "")
            result = {"ok": True, "blueprints": data}
            _resp_cache_set(_ck, result, _TTL_MEDIUM)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "blueprints": []}

    @app.get(f"{API_PREFIX}/eve/character/industry")
    @app.get(f"{API_PREFIX}/eo/character/industry")
    async def api_char_industry(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_industry:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_FAST)): return cached
            data = await esi_get_json(f"/characters/{cid}/industry/jobs/", access_token=token["access_token"])
            type_ids = list({int(j["blueprint_type_id"]) for j in (data or []) if j.get("blueprint_type_id")})
            prod_ids = list({int(j["product_type_id"]) for j in (data or []) if j.get("product_type_id")})
            fac_ids = list({int(j["facility_id"]) for j in (data or []) if j.get("facility_id")})
            names = await _resolve_entity_names(list(set(type_ids + prod_ids))) if type_ids or prod_ids else {}
            locs = await _resolve_facility_names(fac_ids, token["access_token"]) if fac_ids else {}
            for j in data or []:
                j["blueprint_type_name"] = names.get(int(j.get("blueprint_type_id", 0)), "")
                j["product_type_name"] = names.get(int(j.get("product_type_id", 0)), "")
                j["facility_name"] = locs.get(int(j.get("facility_id", 0)), "")
            result = {"ok": True, "jobs": data, "type_names": {str(k): v for k, v in names.items()}, "location_names": {str(k): v for k, v in locs.items()}}
            _resp_cache_set(_ck, result, _TTL_FAST)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "jobs": []}

    @app.get(f"{API_PREFIX}/eve/character/pi")
    @app.get(f"{API_PREFIX}/eo/character/pi")
    async def api_char_pi(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            _ck = f"resp:char_pi:{cid}"
            if (cached := _resp_cache_get(_ck, _TTL_MEDIUM)): return cached
            planets = await esi_get_json(f"/characters/{cid}/planets/", access_token=token["access_token"]) or []
            planet_ids = [int(p["planet_id"]) for p in planets if p.get("planet_id")]
            system_ids = list({int(p["solar_system_id"]) for p in planets if p.get("solar_system_id")})
            planet_names = await _resolve_planet_names(planet_ids) if planet_ids else {}
            sys_names = await _resolve_entity_names(system_ids) if system_ids else {}
            loc_names = {**{str(k): v for k, v in planet_names.items()}, **{str(k): v for k, v in sys_names.items()}}
            for p in planets:
                p["planet_name"] = planet_names.get(int(p.get("planet_id", 0)), "")
                p["system_name"] = sys_names.get(int(p.get("solar_system_id", 0)), "")
            result = {"ok": True, "planets": planets, "location_names": loc_names}
            _resp_cache_set(_ck, result, _TTL_MEDIUM)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "planets": []}

    @app.get(f"{API_PREFIX}/eve/dashboard")
    @app.get(f"{API_PREFIX}/eo/dashboard")
    async def api_eve_dashboard(
        alias: str,
        include_assets: bool = False,
        include_corp: bool = False,
        user=Depends(require_user),
    ):
        token = await eve_access_token_for(LOCAL_USER_ID, alias)
        ch = token["character"]
        cid = int(ch["character_id"])
        at = token["access_token"]

        tasks = {
            "pub": esi_get_public_json(f"/characters/{cid}/"),
            "wallet": esi_get_json(f"/characters/{cid}/wallet/", access_token=at),
            "skillqueue": esi_get_json(f"/characters/{cid}/skillqueue/", access_token=at),
            "industry": esi_get_json(f"/characters/{cid}/industry/jobs/", access_token=at),
            "pi": esi_get_json(f"/characters/{cid}/planets/", access_token=at),
            "location": esi_get_json(f"/characters/{cid}/location/", access_token=at),
            "ship": esi_get_json(f"/characters/{cid}/ship/", access_token=at),
            "online": esi_get_json(f"/characters/{cid}/online/", access_token=at),
        }

        results = {}
        for k, coro in tasks.items():
            try:
                results[k] = await coro
            except Exception:
                results[k] = None

        pub = results["pub"] or {}
        balance = results["wallet"]
        skillqueue = results["skillqueue"] or []
        industry_jobs = results["industry"] or []
        planets = results["pi"] or []
        location = results["location"] or {}
        ship = results["ship"] or {}
        online_data = results["online"] or {}

        active_sq = [s for s in skillqueue if s.get("finish_date")]
        next_skill = active_sq[0] if active_sq else None

        active_jobs = [j for j in industry_jobs if j.get("status") == "active"]
        soonest_job = min(active_jobs, key=lambda j: j.get("end_date", ""), default=None)

        sys_id = location.get("solar_system_id")
        ship_type_id = ship.get("ship_type_id")
        corp_id = pub.get("corporation_id")
        ids_to_resolve = [i for i in [sys_id, ship_type_id, corp_id] if i]
        names = await _resolve_entity_names(ids_to_resolve) if ids_to_resolve else {}

        portrait = f"https://images.evetech.net/characters/{cid}/portrait?size=128"

        corp_name = names.get(corp_id) if corp_id else None
        sys_name = names.get(sys_id) if sys_id else None
        ship_type_name = names.get(ship_type_id) if ship_type_id else None
        security_status = pub.get("security_status")

        # Optionally fetch full corp ESI data (ticker, member count, etc.)
        corp_esi_data: dict = {}
        if include_corp and corp_id:
            try:
                corp_esi_data = await esi_get_public_json(f"/corporations/{corp_id}/") or {}
            except Exception:
                pass
        if corp_name and "name" not in corp_esi_data:
            corp_esi_data["name"] = corp_name
        if corp_id and "corporation_id" not in corp_esi_data:
            corp_esi_data["corporation_id"] = corp_id

        return {
            "ok": True,
            "character": {
                **ch,
                "portrait_url": portrait,
                "security_status": security_status,
                "corporation_id": corp_id,
                "corporation_name": corp_name,
            },
            # Top-level aliases for dashboard template compatibility
            "security": security_status,
            # eve.html reads j.corp?.corp?.name (corp_summary format has {corp: {name, ticker,...}})
            "corp": {
                "corporation_id": corp_id,
                "corporation_name": corp_name,
                "corp": corp_esi_data,  # nested for eve.html j.corp.corp.name
                "corp_id": corp_id,
                "logo_url": f"https://images.evetech.net/corporations/{corp_id}/logo?size=64" if corp_id else None,
            },
            "corp_name": corp_name,
            "wallet": {
                "balance": balance,
                "formatted": _fmt_isk(balance),
                "balance_pretty": _fmt_isk(balance),
            },
            "wallet_balance": balance,
            "skillqueue": {
                "count": len(active_sq),
                "next": next_skill,
            },
            # eve.html template aliases
            "skills": {
                "queue_len": len(active_sq),
                "next_finish": (next_skill or {}).get("finish_date") if next_skill else None,
            },
            # Template expects d.skill_queue as array
            "skill_queue": active_sq,
            "industry": {
                "active_count": len(active_jobs),
                "active_jobs": len(active_jobs),
                "soonest": soonest_job,
                "soonest_end": (soonest_job or {}).get("end_date") if soonest_job else None,
            },
            # Template expects d.industry_jobs as array
            "industry_jobs": active_jobs,
            "pi": {
                "planet_count": len(planets),
                "planets": len(planets),
            },
            # Template expects d.pi_planets as array
            "pi_planets": planets,
            # eve.html reads j.identity.security_status
            "identity": {
                "character_name": ch.get("character_name"),
                "security_status": security_status,
                "corporation_id": corp_id,
                "corporation_name": corp_name,
            },
            "location": {
                "solar_system_id": sys_id,
                "solar_system_name": sys_name,
                "system_name": sys_name,
                "system": sys_name,
                "location_type": ("Station" if location.get("station_id") else ("Space" if sys_id else None)),
            },
            "ship": {
                "ship_type_id": ship_type_id,
                "ship_name": ship.get("ship_name"),
                "ship_type_name": ship_type_name,
                "ship_type": ship_type_name,
            },
            "is_online": online_data.get("online"),
            "online": online_data,
        }

    # ── Corp API ─────────────────────────────────────────────────────────────────

    @app.get(f"{API_PREFIX}/eve/corp/summary")
    @app.get(f"{API_PREFIX}/eo/corp/summary")
    async def api_corp_summary(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            corp_id = await eve_get_corp_id(cid)
            _ck = f"resp:corp_summary:{corp_id}"
            if (cached := _resp_cache_get(_ck, _TTL_STATIC)): return cached
            data = await esi_get_public_json(f"/corporations/{corp_id}/") or {}
            logo = f"https://images.evetech.net/corporations/{corp_id}/logo?size=128"
            entity_ids = [i for i in [data.get("ceo_id"), data.get("alliance_id")] if i]
            loc_ids = [i for i in [data.get("home_station_id")] if i]
            id_names_map = await _resolve_entity_names([int(i) for i in entity_ids]) if entity_ids else {}
            loc_names_map = await _resolve_facility_names([int(i) for i in loc_ids], token["access_token"]) if loc_ids else {}
            if data.get("ceo_id"):
                data["ceo_name"] = id_names_map.get(int(data["ceo_id"]), "")
            if data.get("alliance_id"):
                data["alliance_name"] = id_names_map.get(int(data["alliance_id"]), "")
            if data.get("home_station_id"):
                data["home_station_name"] = loc_names_map.get(int(data["home_station_id"]), "")
            result = {
                "ok": True, "corp": data, "corp_id": corp_id, "logo_url": logo,
                "id_names": {str(k): v for k, v in id_names_map.items()},
                "location_names": {str(k): v for k, v in loc_names_map.items()},
            }
            _resp_cache_set(_ck, result, _TTL_STATIC)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "corp": {}}

    @app.get(f"{API_PREFIX}/eve/corp/wallets")
    @app.get(f"{API_PREFIX}/eo/corp/wallets")
    async def api_corp_wallets(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            corp_id = await eve_get_corp_id(cid)
            _ck = f"resp:corp_wallets:{corp_id}"
            if (cached := _resp_cache_get(_ck, _TTL_WALLET)): return cached
            data = await esi_get_json(f"/corporations/{corp_id}/wallets/", access_token=token["access_token"])
            result = {"ok": True, "wallets": data}
            _resp_cache_set(_ck, result, _TTL_WALLET)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "wallets": []}

    @app.get(f"{API_PREFIX}/eve/corp/journal")
    @app.get(f"{API_PREFIX}/eo/corp/journal")
    async def api_corp_journal(
        alias: str, division: int = 1, user=Depends(require_user)
    ):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            corp_id = await eve_get_corp_id(cid)
            _ck = f"resp:corp_journal:{corp_id}:{division}"
            if (cached := _resp_cache_get(_ck, _TTL_WALLET)): return cached
            data = await esi_get_json(f"/corporations/{corp_id}/wallets/{division}/journal/", access_token=token["access_token"]) or []
            party_ids = list({int(e[k]) for e in data for k in ("first_party_id", "second_party_id") if e.get(k)})
            id_names = await _resolve_entity_names(party_ids) if party_ids else {}
            for e in data:
                for k in ("first_party_id", "second_party_id"):
                    pid = e.get(k)
                    if pid:
                        e[k.replace("_id", "_name")] = id_names.get(int(pid), "")
            result = {"ok": True, "entries": data, "id_names": {str(k): v for k, v in id_names.items()}}
            _resp_cache_set(_ck, result, _TTL_WALLET)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "entries": []}

    @app.get(f"{API_PREFIX}/eve/corp/orders")
    @app.get(f"{API_PREFIX}/eo/corp/orders")
    async def api_corp_orders(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            corp_id = await eve_get_corp_id(cid)
            _ck = f"resp:corp_orders:{corp_id}"
            if (cached := _resp_cache_get(_ck, _TTL_FAST)): return cached
            data = await esi_get_json(f"/corporations/{corp_id}/orders/", access_token=token["access_token"]) or []
            type_ids = list({int(o["type_id"]) for o in data if o.get("type_id")})
            loc_ids = list({int(o["location_id"]) for o in data if o.get("location_id")})
            type_names = await _resolve_entity_names(type_ids) if type_ids else {}
            loc_names = await _resolve_facility_names(loc_ids, token["access_token"]) if loc_ids else {}
            for o in data:
                o["type_name"] = type_names.get(int(o.get("type_id") or 0)) or ""
                o["location_name"] = loc_names.get(int(o.get("location_id") or 0)) or ""
            result = {"ok": True, "orders": data, "type_names": {str(k): v for k, v in type_names.items()}, "location_names": {str(k): v for k, v in loc_names.items()}}
            _resp_cache_set(_ck, result, _TTL_FAST)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "orders": []}

    @app.get(f"{API_PREFIX}/eve/corp/assets")
    @app.get(f"{API_PREFIX}/eo/corp/assets")
    async def api_corp_assets(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            corp_id = await eve_get_corp_id(cid)
            _ck = f"resp:corp_assets:{corp_id}"
            if (cached := _resp_cache_get(_ck, _TTL_MEDIUM)): return cached
            data = await esi_get_json(f"/corporations/{corp_id}/assets/", access_token=token["access_token"]) or []
            type_ids = list({int(a["type_id"]) for a in data if a.get("type_id")})
            loc_ids = list({int(a["location_id"]) for a in data if a.get("location_id")})
            type_names = await _resolve_entity_names(type_ids) if type_ids else {}
            loc_names = await _resolve_facility_names(loc_ids, token["access_token"]) if loc_ids else {}
            for a in data:
                tid = int(a.get("type_id") or 0)
                lid = int(a.get("location_id") or 0)
                a["type_name"] = type_names.get(tid) or a.get("type_name") or ""
                a["location_name"] = loc_names.get(lid) or a.get("location_name") or ""
            total_stacks = len(data)
            loc_counts: dict = {}
            for a in data:
                lid = a.get("location_id")
                if lid:
                    loc_counts[lid] = loc_counts.get(lid, 0) + 1
            top_loc_id = max(loc_counts, key=loc_counts.get) if loc_counts else None
            top_location = loc_names.get(top_loc_id) if top_loc_id else "—"
            sample_items = sorted(data, key=lambda x: x.get("quantity", 0), reverse=True)[:15]
            result = {
                "ok": True,
                "assets": data,
                "total_stacks": total_stacks,
                "top_location": top_location or "—",
                "sample_items": sample_items,
                "type_names": {str(k): v for k, v in type_names.items()},
                "location_names": {str(k): v for k, v in loc_names.items()},
            }
            _resp_cache_set(_ck, result, _TTL_MEDIUM)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "assets": []}

    @app.get(f"{API_PREFIX}/eve/corp/industry")
    @app.get(f"{API_PREFIX}/eo/corp/industry")
    async def api_corp_industry(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            corp_id = await eve_get_corp_id(cid)
            _ck = f"resp:corp_industry:{corp_id}"
            if (cached := _resp_cache_get(_ck, _TTL_FAST)): return cached
            data = await esi_get_json(f"/corporations/{corp_id}/industry/jobs/", access_token=token["access_token"]) or []
            type_ids = list({int(j["blueprint_type_id"]) for j in data if j.get("blueprint_type_id")})
            prod_ids = list({int(j["product_type_id"]) for j in data if j.get("product_type_id")})
            fac_ids = list({int(j["facility_id"]) for j in data if j.get("facility_id")})
            all_type_ids = list(set(type_ids + prod_ids))
            installer_ids = list({int(j["installer_id"]) for j in data if j.get("installer_id")})
            names = await _resolve_entity_names(all_type_ids) if all_type_ids else {}
            locs = await _resolve_facility_names(fac_ids, token["access_token"]) if fac_ids else {}
            char_names = await _resolve_entity_names(installer_ids) if installer_ids else {}
            for j in data:
                j["blueprint_type_name"] = names.get(int(j.get("blueprint_type_id", 0)), "")
                j["product_type_name"] = names.get(int(j.get("product_type_id", 0)), "")
                j["facility_name"] = locs.get(int(j.get("facility_id", 0)), "")
                j["installer_name"] = char_names.get(int(j.get("installer_id", 0)), "")
            result = {
                "ok": True,
                "jobs": data,
                "type_names": {str(k): v for k, v in names.items()},
                "location_names": {str(k): v for k, v in locs.items()},
                "id_names": {str(k): v for k, v in char_names.items()},
            }
            _resp_cache_set(_ck, result, _TTL_FAST)
            return result
        except Exception as e:
            return {"ok": False, "detail": str(e), "jobs": []}

    @app.get(f"{API_PREFIX}/eve/corp/structures")
    @app.get(f"{API_PREFIX}/eo/corp/structures")
    @app.get(f"{API_PREFIX}/structures/list")
    async def api_structures_list(alias: str, user=Depends(require_user)):
        try:
            token = await eve_access_token_for(LOCAL_USER_ID, alias)
            cid = int(token["character"]["character_id"])
            corp_id = await eve_get_corp_id(cid)
            _ck = f"resp:corp_structures:{corp_id}"
            if (cached := _resp_cache_get(_ck, _TTL_MEDIUM)): return cached
            structures = await esi_get_json(
                f"/corporations/{corp_id}/structures/", access_token=token["access_token"]
            ) or []
            type_ids = list({int(s["type_id"]) for s in structures if s.get("type_id")})
            sys_ids = list({int(s["system_id"]) for s in structures if s.get("system_id")})
            id_names = await _resolve_entity_names(list(set(type_ids + sys_ids)))
            fuel_alert_days = float(os.environ.get("FUEL_ALERT_DAYS", "5"))
            fuel_max_days = float(os.environ.get("FUEL_MAX_DAYS", "30"))
            now = datetime.datetime.now(datetime.timezone.utc)
            out = []
            for s in structures:
                sid = int(s.get("structure_id") or 0)
                tid = int(s.get("type_id") or 0)
                sysid = int(s.get("system_id") or 0)
                type_name = id_names.get(tid, str(tid))
                sys_name = id_names.get(sysid, str(sysid))
                days_remaining = None
                fuel_pct = None
                fuel_expires = s.get("fuel_expires")
                if fuel_expires:
                    try:
                        exp = datetime.datetime.fromisoformat(fuel_expires.replace("Z", "+00:00"))
                        days_remaining = max(0.0, (exp - now).total_seconds() / 86400)
                        fuel_pct = min(100, round(days_remaining / fuel_max_days * 100))
                    except Exception:
                        pass
                services = [
                    {"name": sv.get("name", ""), "state": sv.get("state", "")}
                    for sv in (s.get("services") or [])
                ]
                out.append({
                    "structure_id": sid,
                    "type_id": tid,
                    "name": s.get("name") or f"Structure {sid}",
                    "type_name": type_name,
                    "system_name": sys_name,
                    "system_id": sysid,
                    "state": s.get("state"),
                    "fuel_expires": fuel_expires,
                    "days_remaining": round(days_remaining, 1) if days_remaining is not None else None,
                    "fuel_pct": fuel_pct,
                    "low_fuel": days_remaining is not None and days_remaining < fuel_alert_days,
                    "services": services,
                    "reinforce_hour": s.get("reinforce_hour"),
                })
            out.sort(key=lambda x: (0 if x["low_fuel"] else 1, x["name"]))
            corp_name = ""
            try:
                cp = await esi_get_public_json(f"/corporations/{corp_id}/")
                corp_name = (cp or {}).get("name", "")
            except Exception:
                pass
            result = {"ok": True, "structures": out, "corp_id": corp_id, "corp_name": corp_name}
            _resp_cache_set(_ck, result, _TTL_MEDIUM)
            return result
        except Exception as e:
            logger.exception("[structures/list] Unhandled error")
            return {"ok": False, "detail": str(e), "structures": []}

    # ── Fittings API ─────────────────────────────────────────────────────────────

    @app.get("/app/api/fittings")
    def api_fittings_list(user=Depends(require_user)):
        memory = MemoryStore(_db_path())
        return {"fittings": memory.fitting_list() if hasattr(memory, "fitting_list") else []}

    @app.post("/app/api/fittings")
    async def api_fittings_save(request: Request, user=Depends(require_user)):
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            return {"success": False, "error": "Name is required"}
        memory = MemoryStore(_db_path())
        fit_id = memory.fitting_save(
            name=name,
            ship_type_id=int(body.get("ship_type_id", 0)),
            ship_name=str(body.get("ship_name", "")),
            fit_data=str(body.get("fit_data", "{}")),
            eft_string=str(body.get("eft_string", "")),
            saved_by_name="local",
        ) if hasattr(memory, "fitting_save") else None
        return {"success": True, "id": fit_id}

    @app.get("/app/api/fittings/{fit_id}")
    def api_fittings_get(fit_id: int, user=Depends(require_user)):
        memory = MemoryStore(_db_path())
        fit = memory.fitting_get(fit_id) if hasattr(memory, "fitting_get") else None
        if not fit:
            return {"success": False, "error": "Not found"}
        return {"success": True, "fitting": fit}

    @app.delete("/app/api/fittings/{fit_id}")
    def api_fittings_delete(fit_id: int, user=Depends(require_user)):
        memory = MemoryStore(_db_path())
        ok = memory.fitting_delete(fit_id) if hasattr(memory, "fitting_delete") else False
        return {"success": ok}

    # ── Set default character ────────────────────────────────────────────────────

    @app.post(f"{API_PREFIX}/eve/default")
    @app.post(f"{API_PREFIX}/eo/default")
    async def api_eve_set_default(request: Request, user=Depends(require_user)):
        body = await request.json()
        target = str(body.get("alias") or body.get("character_id") or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="Missing alias or character_id")
        with _connect() as con:
            if not _table_exists(con, "eve_characters"):
                raise HTTPException(status_code=404, detail="No characters linked")
            uc = _eve_characters_user_col(con)
            row = con.execute(
                f"SELECT character_id FROM eve_characters WHERE {uc}=? AND (alias=? OR character_id=?) LIMIT 1",
                (LOCAL_USER_ID, target, int(target) if target.isdigit() else -1),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Character not found")
            cid = int(row[0])
            con.execute(f"UPDATE eve_characters SET is_default=0 WHERE {uc}=?", (LOCAL_USER_ID,))
            con.execute(
                f"UPDATE eve_characters SET is_default=1 WHERE {uc}=? AND character_id=?",
                (LOCAL_USER_ID, cid),
            )
            con.commit()
        return {"ok": True}

    # ── Remove character ─────────────────────────────────────────────────────────

    @app.delete(f"{API_PREFIX}/eve/character")
    async def api_eve_remove_character(alias: str, user=Depends(require_user)):
        with _connect() as con:
            if not _table_exists(con, "eve_characters"):
                raise HTTPException(status_code=404, detail="No characters linked")
            uc = _eve_characters_user_col(con)
            con.execute(
                f"DELETE FROM eve_characters WHERE {uc}=? AND (alias=? OR character_name=?)",
                (LOCAL_USER_ID, alias, alias),
            )
            con.commit()
        return {"ok": True}

    @app.post(f"{API_PREFIX}/eve/reset_all")
    async def api_eve_reset_all(user=Depends(require_user)):
        """Delete all linked characters and clear the ESI cache."""
        deleted_chars = 0
        cleared_cache = 0
        with _connect() as con:
            if _table_exists(con, "eve_characters"):
                uc = _eve_characters_user_col(con)
                res = con.execute(f"DELETE FROM eve_characters WHERE {uc}=?", (LOCAL_USER_ID,))
                deleted_chars = res.rowcount
            if _table_exists(con, "sde_cache"):
                res2 = con.execute("DELETE FROM sde_cache WHERE 1=1")
                cleared_cache = res2.rowcount
            # Clear any other ESI caches
            for tbl in ["esi_cache", "nav_movements", "nav_sigs", "nav_structs", "map_systems", "map_connections"]:
                try:
                    if _table_exists(con, tbl):
                        con.execute(f"DELETE FROM {tbl} WHERE 1=1")
                except Exception:
                    pass
            con.commit()
        return {"ok": True, "deleted_chars": deleted_chars, "cleared_cache": cleared_cache}

    # ── SDE (Static Data Export) endpoints ──────────────────────────────────────

    _sde_mem: Dict[str, Any] = {}

    def _sde_cache_get(key: str) -> Optional[dict]:
        v = _sde_mem.get(key)
        if not v:
            return None
        ts, data = v
        if time.time() - ts > 86400 * 30:
            return None
        return data

    def _sde_cache_set(key: str, data: Any, ttl_s: int = 86400 * 30) -> None:
        _sde_mem[key] = (time.time(), data)

    @app.get("/app/api/sde/type_names")
    async def api_sde_type_names(type_ids: str = ""):
        ids = [int(x.strip()) for x in type_ids.split(",") if x.strip().isdigit()]
        if not ids:
            return {}
        names = await _resolve_entity_names(ids)
        # Return flat {str(typeId): {"typeID": id, "typeName": name}} for industry template compatibility
        return {str(k): {"typeID": k, "typeName": v} for k, v in names.items()}

    @app.get("/app/api/sde/market_groups")
    async def api_sde_market_groups():
        if not sde_available():
            return JSONResponse(status_code=503, content={"error": "SDE not available"})
        try:
            return get_market_groups()
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.get("/app/api/sde/market_group_items/{group_id}")
    async def api_sde_market_group_items(group_id: int):
        if not sde_available():
            return []
        try:
            return get_market_group_items(group_id)
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.get("/app/api/sde/market_group_items_unused/{group_id}")
    async def api_sde_market_group_items_esi(group_id: int):
        key = f"sde:mg_items:{group_id}"
        cached = _sde_cache_get(key)
        if cached:
            return cached
        try:
            data = await esi_get_public_json(f"/markets/groups/{group_id}/")
            result = {"ok": True, "group": data}
            _sde_cache_set(key, result)
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/app/api/sde/market_prices/{type_id}")
    async def api_sde_market_prices_inner(type_id: int):
        """Delegate to the full multi-hub market prices implementation."""
        return await api_sde_market_prices(type_id)

    @app.get("/app/api/sde/type_search")
    async def api_sde_type_search(q: str = ""):
        if not q or len(q) < 2:
            return {"results": []}
        try:
            from eve.eve import esi_search
            data = esi_search(q, ["inventory_type"])
            ids = (data.get("inventory_type") or [])[:40]
            names = await _resolve_entity_names(ids) if ids else {}
            return {"results": [{"id": i, "name": names.get(i, str(i))} for i in ids]}
        except Exception as e:
            return {"results": [], "error": str(e)}

    # ── EVE SSO (PKCE flow) ──────────────────────────────────────────────────────

    @app.get("/eve/login", include_in_schema=False)
    async def eve_login(request: Request, scope_mode: str = "min"):
        """Start EVE SSO PKCE flow. Opens browser to CCP login page."""
        scopes = get_sso_scopes(mode=scope_mode)
        state = secrets.token_urlsafe(24)
        # PKCE: generate code_verifier and code_challenge
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        # Store verifier in module-level dict (avoids SameSite cookie issues on OAuth return)
        _pkce_verifiers[state] = code_verifier
        # Persist state -> user mapping in DB
        ms = MemoryStore(_db_path())
        ms.eve_create_pending_state(LOCAL_USER_ID, state)
        url = build_pkce_authorize_url(
            client_id=EVE_CLIENT_ID,
            state=state,
            scopes=scopes,
            redirect_uri=REDIRECT_URI,
            code_challenge=code_challenge,
        )
        return RedirectResponse(url, status_code=302)

    @app.get("/eve/callback", include_in_schema=False)
    async def eve_callback(request: Request, code: str = "", state: str = ""):
        """EVE SSO PKCE callback. Exchanges code for tokens and stores character."""
        if not code or not state:
            return HTMLResponse("Missing code or state.", status_code=400)
        code_verifier = _pkce_verifiers.pop(state, None)
        if not code_verifier:
            return HTMLResponse("Session expired or invalid state.", status_code=400)
        ms = MemoryStore(_db_path())
        user_id = ms.eve_consume_pending_state(state)
        if not user_id:
            return HTMLResponse("State expired or already used.", status_code=400)
        try:
            tokens = await exchange_code_pkce(
                code=code,
                code_verifier=code_verifier,
                client_id=EVE_CLIENT_ID,
                redirect_uri=REDIRECT_URI,
            )
            verify = await verify_access_token(tokens.access_token)
            char_id = None
            char_name = None
            scopes = ""
            if isinstance(verify, dict):
                char_id = verify.get("CharacterID") or verify.get("character_id")
                char_name = verify.get("CharacterName") or verify.get("character_name")
                raw = verify.get("Scopes") or verify.get("scp") or ""
                scopes = " ".join(raw) if isinstance(raw, list) else str(raw)
                if not char_id and isinstance(verify.get("sub"), str):
                    parts = verify["sub"].split(":")
                    if parts and parts[-1].isdigit():
                        char_id = int(parts[-1])
            if not char_id:
                return HTMLResponse("Could not read character from token.", status_code=400)
            refresh_enc = encrypt_refresh_token(tokens.refresh_token)
            ms.eve_upsert_auth(
                user_id=int(user_id),
                character_id=int(char_id),
                character_name=str(char_name or char_id),
                refresh_token=refresh_enc,
                scopes=scopes,
                alias=str(char_name or char_id),
                is_default=1,
            )
        except Exception as e:
            logger.exception("EVE callback error")
            return HTMLResponse(f"Authentication failed: {e}", status_code=400)
        return HTMLResponse(
            """<!doctype html><html><head><meta charset=utf-8>
            <meta http-equiv="refresh" content="0;url=/app/eve_online">
            <style>body{background:#0b0e14;color:#c8d0dc;font-family:sans-serif;
            display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}
            .box{text-align:center;} h2{color:#3ca8e8;} a{color:#3ca8e8;}</style>
            </head><body><div class="box">
            <h2>✓ Character linked</h2>
            <p>Redirecting…</p>
            <script>window.location.replace("/app/eve_online");</script>
            </div></body></html>""",
            status_code=200,
        )

    return app


app = create_app()


# ═══════════════════════════════════════════════════════════════════════════════
# EXTENSION: All remaining EVE feature endpoints
# ═══════════════════════════════════════════════════════════════════════════════

# ── SDE first-run download ─────────────────────────────────────────────────────

SDE_DOWNLOAD_URL = "https://www.fuzzwork.co.uk/dump/sqlite-latest.sqlite.bz2"


async def _download_sde_task():
    """Download + decompress the Fuzzwork SDE into data/sde.sqlite.
    Updates _sde_dl progress dict so the setup page can poll it."""
    import bz2
    import aiohttp as _aio
    _dl = _sde_dl_state  # module-level dict, shared with the poll endpoint

    sde_path = os.path.join(os.path.dirname(_db_path()), "sde.sqlite")
    tmp_path = sde_path + ".bz2"
    os.makedirs(os.path.dirname(sde_path), exist_ok=True)

    try:
        # ── Download ──────────────────────────────────────────────────────────
        async with _aio.ClientSession(timeout=_aio.ClientTimeout(total=None)) as session:
            async with session.get(SDE_DOWNLOAD_URL) as resp:
                if resp.status != 200:
                    _dl["status"] = "error"
                    _dl["error"] = f"Download failed: HTTP {resp.status}"
                    return
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            _dl["progress"] = min(90, int(downloaded / total * 90))

        # ── Decompress ────────────────────────────────────────────────────────
        _dl["progress"] = 91
        # Write to a temp output so we never leave a partial/0-byte sde.sqlite
        sde_tmp_out = sde_path + ".tmp"
        try:
            with bz2.open(tmp_path, "rb") as src, open(sde_tmp_out, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        except Exception as decomp_err:
            # Clean up partial output and the bz2 download
            for p in [sde_tmp_out, tmp_path]:
                try:
                    if os.path.exists(p): os.remove(p)
                except Exception:
                    pass
            raise RuntimeError(f"Decompression failed: {decomp_err}") from decomp_err

        # Verify the decompressed file is a plausible SQLite DB (≥ 1 MB)
        out_size = os.path.getsize(sde_tmp_out) if os.path.exists(sde_tmp_out) else 0
        if out_size < 1_000_000:
            for p in [sde_tmp_out, tmp_path]:
                try:
                    if os.path.exists(p): os.remove(p)
                except Exception:
                    pass
            raise RuntimeError(
                f"Decompressed file is only {out_size} bytes — expected > 1 MB. "
                f"The download may be corrupt or the URL may have changed."
            )

        # Atomically replace sde.sqlite with the newly decompressed file
        if os.path.exists(sde_path):
            try: os.remove(sde_path)
            except Exception: pass
        os.rename(sde_tmp_out, sde_path)
        try: os.remove(tmp_path)
        except Exception: pass

        logger.info(f"[SDE] Decompressed OK: {sde_path} ({out_size // 1_048_576} MB)")

        # Reload the SDE module connection so it picks up the new file
        try:
            import eve.sde_local as _sde_mod
            _sde_mod.SDE_PATH = sde_path
            if _sde_mod._sde_conn:
                _sde_mod._sde_conn.close()
                _sde_mod._sde_conn = None
        except Exception:
            pass

        _dl["progress"] = 100
        _dl["status"] = "done"
        logger.info(f"[SDE] Download complete: {sde_path}")

    except Exception as e:
        _dl["status"] = "error"
        _dl["error"] = str(e)
        logger.error(f"[SDE] Download failed: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


# Module-level download state dicts (shared between create_app() closure and tasks)
_sde_dl_state: Dict[str, Any] = {"status": "idle", "progress": 0, "error": ""}

# ── Nebula image download state + task ────────────────────────────────────────
_nebulae_dl_state: Dict[str, Any] = {"status": "idle", "progress": 0, "done": 0, "total": 0, "error": ""}

_NEBULA_SOURCES = [
    # (filename, CDN URL)  — all from res.eveonline.ccpgames.com
    ("genesis",              "http://res.eveonline.ccpgames.com/fb/fbdd62f5fe5b4b38_37f3dbbf6e2a48cd006d3fea764ac7c2"),
    ("kador",                "http://res.eveonline.ccpgames.com/65/65a09a5f64475ef9_89c8a8950be55c6ea546b49d84da5af8"),
    ("domain",               "http://res.eveonline.ccpgames.com/8e/8e2045b05e9aeb5e_2f4793ec99f6966749fa1c862b82cfb6"),
    ("the_bleak_lands",      "http://res.eveonline.ccpgames.com/85/85d2a394d335433f_d5cd6cd448c22ff1f8ee2d19c26e1b3e"),
    ("devoid",               "http://res.eveonline.ccpgames.com/da/daa18f450ef5d304_4df40248edf2e128850bd3260c77603e"),
    ("tash-murkon",          "http://res.eveonline.ccpgames.com/75/7512f0439de0f945_f7aa6452497fac7a728a9db2552e69cd"),
    ("kor-azor",             "http://res.eveonline.ccpgames.com/15/15f2c1bf42cd346a_612f7f9bdac2a973c97cb514210e8e2b"),
    ("aridia",               "http://res.eveonline.ccpgames.com/da/da6f5e3d1b76602b_f3665f521abd7ea7aef2119d9711ff00"),
    ("khanid",               "http://res.eveonline.ccpgames.com/59/59d09b18e14c0240_b52302b8018f39b36667dd667ae17330"),
    ("querious",             "http://res.eveonline.ccpgames.com/fe/fec97d13928c6dba_08f8336a39af5fc3dbaacf377fc1292e"),
    ("delve",                "http://res.eveonline.ccpgames.com/cf/cf6651b9365d6655_1cbc58779d990253411fdf6e38d48878"),
    ("period_basis",         "http://res.eveonline.ccpgames.com/bf/bf1857b85ad63714_0146c77fee83665c64457b6a9bdf64b6"),
    ("derelik",              "http://res.eveonline.ccpgames.com/8c/8c4800862d53ec8f_f4535b9c786fc7ba1f667fd87d663a99"),
    ("providence",           "http://res.eveonline.ccpgames.com/37/373b801a7ace916e_a6a07b53b08d1ca381e46b7a7d7f41f6"),
    ("catch",                "http://res.eveonline.ccpgames.com/e0/e03b439f6247b0c9_a9a1c6a8227e5032f623c4734e1b8921"),
    ("stain",                "http://res.eveonline.ccpgames.com/b6/b6e2632e3dd7a348_da0fd8010304da3b2e4d1a4b9be38559"),
    ("paragon_soul",         "http://res.eveonline.ccpgames.com/29/29b99fe47d9a0c33_0ec5b6347505ddedefaa193142702072"),
    ("esoteria",             "http://res.eveonline.ccpgames.com/2e/2e8c08c61560dac2_11a244119c06864202fa4ac21d269886"),
    ("the_citadel",          "http://res.eveonline.ccpgames.com/a7/a74c90e0df6d352e_b2606d300e06f2aa952b1f325773a548"),
    ("the_forge",            "http://res.eveonline.ccpgames.com/95/95f06e84f4c00ff3_9b88438cf69db5b5225149a266f443d6"),
    ("lonetrek",             "http://res.eveonline.ccpgames.com/2c/2c476b9e6cb9b608_a9a8fb24754044935878c7e009568c08"),
    ("black_rise",           "http://res.eveonline.ccpgames.com/2d/2dab17692ad88d15_af630e8afdc564cd0c61cc6f7f15bc92"),
    ("pure_blind",           "http://res.eveonline.ccpgames.com/b7/b7a3ebf9f4ce1a7a_a8bd67e64dc5cc2dd454f53a51bde589"),
    ("deklein",              "http://res.eveonline.ccpgames.com/98/98123be44dfdec4f_3685b2a579c29e16f2bcaadda084837a"),
    ("branch",               "http://res.eveonline.ccpgames.com/c9/c90e9c216dabcdd4_a5386981c5de524dfdbbdc1c00bf9e4c"),
    ("tenal",                "http://res.eveonline.ccpgames.com/e7/e7d58b1f741c48f1_adec0a03c83f7c68fe9b42b95f72f7a3"),
    ("tribute",              "http://res.eveonline.ccpgames.com/4d/4d25a2142672bab6_32ba3b9551c8f0a7540c10b98078f7a1"),
    ("vale_of_the_silent",   "http://res.eveonline.ccpgames.com/99/99208419e98be558_c80cda89d994ad6b48462144217c38e3"),
    ("geminate",             "http://res.eveonline.ccpgames.com/92/92492da801c6ff03_163ff0a2baffd3d5d59f8bac31baaa4c"),
    ("venal",                "http://res.eveonline.ccpgames.com/ad/adffe24ed22674fe_bcb5e40b2e77d650dbb4e724e6b69387"),
    ("the_kalevala_expanse", "http://res.eveonline.ccpgames.com/7a/7af6b262243d61a4_d26052b4c691c02cf5a3339059322461"),
    ("malpais",              "http://res.eveonline.ccpgames.com/a8/a8c4422c70d7c15f_fea842550fcdb4be0ab6fc64b0474e2d"),
    ("perrigen_falls",       "http://res.eveonline.ccpgames.com/7f/7faef1ba7692900a_e77de2ce29e8e168660a862ec8fae0f0"),
    ("oasa",                 "http://res.eveonline.ccpgames.com/15/15671360b326d4e5_413a3b48eef2a04145d3f3b03f5fa3f9"),
    ("outer_passage",        "http://res.eveonline.ccpgames.com/2f/2f2fe35702f110e0_1f48fcce1a9d95acc937499cedb21342"),
    ("cobalt_edge",          "http://res.eveonline.ccpgames.com/ca/ca2e09b2da79d54b_bf80cf4d0802052eeda629aeb5fc9e83"),
    ("sinq_laison",          "http://res.eveonline.ccpgames.com/9d/9d15140ca81eea3a_eb99a76d700678069715a4a24ac11755"),
    ("everyshore",           "http://res.eveonline.ccpgames.com/2a/2a93977f42e6690f_88a7655012b231404f0eaa9a5b9af9b8"),
    ("essence",              "http://res.eveonline.ccpgames.com/5d/5d63eeb17068b394_1d7eeab15e6cd20d8902d65fac14bc25"),
    ("verge_vendor",         "http://res.eveonline.ccpgames.com/7e/7e86da9877da2d49_2774a3d3d6ebe017f57ac541bd32c65f"),
    ("placid",               "http://res.eveonline.ccpgames.com/d5/d587171390610dee_b3571d309642405c712e47f97dbb63f9"),
    ("syndicate",            "http://res.eveonline.ccpgames.com/c8/c80536dd932c88b3_6651dc4d6044954db3904ad714b1f08c"),
    ("cloud_ring",           "http://res.eveonline.ccpgames.com/55/552dfa27536a1fc8_71d619c9a677a6d4549e6d87577c425e"),
    ("outer_ring",           "http://res.eveonline.ccpgames.com/9b/9bbc27b126083c7d_e2c108f2978d4d00aec0cf9db5ccb131"),
    ("solitude",             "http://res.eveonline.ccpgames.com/cc/ccd79fbf2af35742_b2c528e85b29473358faecce9519a90a"),
    ("fade",                 "http://res.eveonline.ccpgames.com/a3/a3dc40eb0aec1864_efac28d6ab6b28c3118a8359449dd7a8"),
    ("fountain",             "http://res.eveonline.ccpgames.com/36/3681e8f83fa18a1f_2550059bfef1b79b22a0be1587129759"),
    ("heimatar",             "http://res.eveonline.ccpgames.com/7c/7c5fecd06297e9c0_33e7db56df57bf5ece3aa95f00b564db"),
    ("metropolis",           "http://res.eveonline.ccpgames.com/6f/6fb4f3cba7bea236_69eba5cf1a9878d61619d0ef3e265ad5"),
    ("molden_heath",         "http://res.eveonline.ccpgames.com/8d/8d3e773064363c72_1cccd9002f13d84b2ca21aa40b5178e4"),
    ("great_wildlands",      "http://res.eveonline.ccpgames.com/39/39fa2be914ce693d_aa8595cc35a3713fcfd6821794db6270"),
    ("wh_generic",           "http://res.eveonline.ccpgames.com/f1/f1eeb0300c591529_b0d5553979c2c247d83d796c1fcf67ee"),
    ("wh_c5c6",              "http://res.eveonline.ccpgames.com/0d/0d950df736ab8da8_7cd9440d71b6bf50ac2bf01984922807"),
    ("pochven",              "http://res.eveonline.ccpgames.com/aa/aa6c1773b07ab26d_1e0d455d70cccea389c45250d6747677"),
]


def _nebula_out_dir() -> str:
    """Absolute path to static/nebulae inside the app bundle."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "nebulae")


def _dxt1_decompress_block(block: bytes, x: int, y: int, img_w: int, pixels: bytearray) -> None:
    """Decode one 4×4 DXT1 (BC1) block into `pixels` (RGBA flat array)."""
    c0 = int.from_bytes(block[0:2], "little")
    c1 = int.from_bytes(block[2:4], "little")
    lut = int.from_bytes(block[4:8], "little")

    def rgb565(v):
        r = ((v >> 11) & 0x1F) * 255 // 31
        g = ((v >> 5) & 0x3F) * 255 // 63
        b = (v & 0x1F) * 255 // 31
        return r, g, b

    r0, g0, b0 = rgb565(c0)
    r1, g1, b1 = rgb565(c1)
    if c0 > c1:
        palette = [
            (r0, g0, b0, 255),
            (r1, g1, b1, 255),
            ((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3, 255),
            ((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3, 255),
        ]
    else:
        palette = [
            (r0, g0, b0, 255),
            (r1, g1, b1, 255),
            ((r0+r1)//2, (g0+g1)//2, (b0+b1)//2, 255),
            (0, 0, 0, 0),
        ]
    for py in range(4):
        for px in range(4):
            xi, yi = x + px, y + py
            if xi >= img_w: continue
            idx = (lut >> (2*(py*4+px))) & 3
            pos = (yi * img_w + xi) * 4
            pixels[pos:pos+4] = palette[idx]


def _dxt5_decompress_block(block: bytes, x: int, y: int, img_w: int, pixels: bytearray) -> None:
    """Decode one 4×4 DXT5 (BC3) block — alpha channel from first 8 bytes, color from last 8."""
    a0, a1 = block[0], block[1]
    abits = int.from_bytes(block[2:8], "little")
    if a0 > a1:
        # 8-value gradient per BC3 spec: (a0*(7-i) + a1*i) / 7
        alut = [a0, a1] + [(a0*(7-i) + a1*i) // 7 for i in range(1, 7)]
    else:
        # 6-value gradient + 0,255 per BC3 spec
        alut = [a0, a1] + [(a0*(5-i) + a1*i) // 5 for i in range(1, 5)] + [0, 255]

    _dxt1_decompress_block(block[8:], x, y, img_w, pixels)
    for py in range(4):
        for px in range(4):
            xi, yi = x+px, y+py
            if xi >= img_w: continue
            ai = (abits >> (3*(py*4+px))) & 7
            pos = (yi * img_w + xi) * 4 + 3
            pixels[pos] = alut[ai] if ai < len(alut) else 255


def _decode_dxt(raw: bytes, width: int, height: int, fmt: str) -> "Image.Image":
    """Decode a full DXT1 or DXT5 surface to a PIL Image."""
    from PIL import Image
    block_size = 8 if fmt == "DXT1" else 16
    pixels = bytearray(width * height * 4)
    off = 0
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            block = raw[off:off+block_size]
            if len(block) < block_size:
                break
            if fmt == "DXT1":
                _dxt1_decompress_block(block, x, y, width, pixels)
            else:
                _dxt5_decompress_block(block, x, y, width, pixels)
            off += block_size
    return Image.frombytes("RGBA", (width, height), bytes(pixels)).convert("RGB")


def _convert_dds_to_jpg(raw: bytes, out_path: str, target_w: int = 400, quality: int = 82) -> str:
    """Extract a face from a DDS cubemap and save as JPEG.
    Returns empty string on success, or an error description on failure."""
    import struct
    from io import BytesIO
    try:
        from PIL import Image
    except ImportError:
        return "Pillow not installed"

    # ── Diagnose what we received ──────────────────────────────────────────────
    magic = raw[:4] if len(raw) >= 4 else b""
    if magic != b"DDS ":
        snippet = raw[:64]
        try:
            text_hint = snippet.decode("utf-8", errors="replace")[:40]
        except Exception:
            text_hint = repr(snippet[:16])
        return f"Not a DDS file (magic={magic!r}, starts with: {text_hint!r})"

    # ── Parse DDS header (DDSURFACEDESC2) ─────────────────────────────────────
    # Offsets into the 124-byte DDS_HEADER struct (all LE uint32):
    #   4=size, 8=flags, 12=height, 16=width, ...
    #   76=pixelformat_size, 80=pf_flags, 84=fourCC, 88=bitcount,
    #   92=rmask, 96=gmask, 100=bmask, 104=amask
    #   108=caps1, 112=caps2
    try:
        hdr_height = struct.unpack_from("<I", raw, 12)[0]
        hdr_width  = struct.unpack_from("<I", raw, 16)[0]
        pf_flags   = struct.unpack_from("<I", raw, 80)[0]
        four_cc    = raw[84:88]
        bit_count  = struct.unpack_from("<I", raw, 88)[0]
        caps2      = struct.unpack_from("<I", raw, 112)[0]
    except struct.error as e:
        return f"DDS header parse error: {e}"

    is_cubemap  = bool(caps2 & 0x200)
    has_fourcc  = bool(pf_flags & 0x4)
    is_dxt1     = four_cc == b"DXT1"
    is_dxt3     = four_cc == b"DXT3"
    is_dxt5     = four_cc == b"DXT5"
    is_dx10     = four_cc == b"DX10"
    pixel_data  = raw[128:]

    # DX10 extended header is 20 bytes before pixel data
    if is_dx10:
        pixel_data = raw[148:]

    face_w, face_h = hdr_width, hdr_height

    face = None

    # ── Path 1: Pillow's built-in DDS decoder (handles many formats) ──────────
    try:
        img = Image.open(BytesIO(raw)).convert("RGB")
        w, h = img.size
        if h >= w * 5:
            face = img.crop((0, 0, w, w))
        elif w >= h * 5:
            face = img.crop((0, 0, h, h))
        else:
            face = img
    except Exception:
        pass

    # ── Path 2: Manual DXT1 / DXT5 decompression ─────────────────────────────
    if face is None and has_fourcc:
        try:
            if is_dxt1:
                face = _decode_dxt(pixel_data, face_w, face_h, "DXT1")
            elif is_dxt3 or is_dxt5:
                face = _decode_dxt(pixel_data, face_w, face_h, "DXT5")
        except Exception as e:
            return f"DXT decode error ({four_cc!r}): {e}"

    # ── Path 3: Raw uncompressed BGRA / BGR ───────────────────────────────────
    if face is None and not has_fourcc:
        try:
            face_size = face_w * face_h * 4
            if len(pixel_data) >= face_size:
                face = Image.frombytes("RGBA", (face_w, face_h),
                                       pixel_data[:face_size], "raw", "BGRA").convert("RGB")
            else:
                face_size3 = face_w * face_h * 3
                if len(pixel_data) >= face_size3:
                    face = Image.frombytes("RGB", (face_w, face_h),
                                           pixel_data[:face_size3], "raw", "BGR").convert("RGB")
        except Exception as e:
            return f"Raw pixel decode error: {e}"

    if face is None:
        return (f"Unsupported DDS format: fourCC={four_cc!r}, pf_flags={pf_flags:#x}, "
                f"bits={bit_count}, {face_w}x{face_h}, cubemap={is_cubemap}, "
                f"dx10={is_dx10}, pixel_data={len(pixel_data)}B")

    try:
        ratio = target_w / face.width
        new_h = max(1, int(face.height * ratio))
        face = face.resize((target_w, new_h), Image.LANCZOS)
        face.save(out_path, "JPEG", quality=quality, optimize=True)
        return ""  # success
    except Exception as e:
        return f"Save error: {e}"


async def _download_nebulae_task():
    """Download CCP nebula DDS textures, convert to JPEG, save to static/nebulae/.
    Skips files that already exist. Updates _nebulae_dl_state for progress polling."""
    import aiohttp as _aio
    _st = _nebulae_dl_state
    out_dir = _nebula_out_dir()
    os.makedirs(out_dir, exist_ok=True)

    # Only download files that don't exist yet
    pending = [(name, url) for name, url in _NEBULA_SOURCES
               if not os.path.exists(os.path.join(out_dir, f"{name}.jpg"))]

    _st["total"] = len(pending)
    _st["done"]  = 0

    if not pending:
        _st["status"]   = "done"
        _st["progress"] = 100
        logger.info("[Nebulae] All images already present — skipping download")
        return

    logger.info(f"[Nebulae] Downloading {len(pending)} nebula images…")
    loop = asyncio.get_event_loop()

    headers = {"User-Agent": "StellarInsight/1.4 (EVE desktop tool)"}
    async with _aio.ClientSession(
        timeout=_aio.ClientTimeout(connect=15, sock_read=30),
        headers=headers,
    ) as session:
        for i, (name, url) in enumerate(pending):
            out_path = os.path.join(out_dir, f"{name}.jpg")
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        if len(raw) >= 1000:
                            # CPU-bound DDS conversion — run in thread pool
                            err = await loop.run_in_executor(
                                None, _convert_dds_to_jpg, raw, out_path
                            )
                            if not err:
                                logger.debug(f"[Nebulae] ✓ {name}.jpg")
                            else:
                                logger.warning(f"[Nebulae] DDS decode failed: {name}: {err}")
                        else:
                            logger.warning(f"[Nebulae] Too small ({len(raw)}B): {name}")
                    else:
                        logger.warning(f"[Nebulae] HTTP {resp.status}: {name}")
            except Exception as exc:
                logger.warning(f"[Nebulae] Error downloading {name}: {exc}")

            _st["done"]     = i + 1
            _st["progress"] = int((i + 1) / len(pending) * 100)
            await asyncio.sleep(0.15)  # gentle pacing — don't hammer CCP CDN

    _st["status"]   = "done"
    _st["progress"] = 100
    total_on_disk = len([f for f in os.listdir(out_dir) if f.endswith(".jpg")])
    logger.info(f"[Nebulae] Download pass complete. {total_on_disk} images on disk.")

# PKCE verifier store: state -> code_verifier
# Using a module-level dict instead of session cookies avoids SameSite cookie
# restrictions when the OAuth callback arrives from EVE's domain.
_pkce_verifiers: Dict[str, str] = {}


_SETUP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Stellar Insight – First Run Setup</title>
  <style>
    :root { --bg:#0b0f14; --panel:#101827; --text:#e8eefc; --muted:#9db1d1;
            --accent:#32b6ff; --accent2:#a855f7; --pop:#ff8a3d; --danger:#ff4d6d; }
    * { box-sizing:border-box; margin:0; padding:0; }
    body { background:var(--bg); color:var(--text); font-family:system-ui,sans-serif;
           display:flex; align-items:center; justify-content:center;
           min-height:100vh; padding:32px; }
    .card { background:var(--panel); border:1px solid rgba(50,182,255,.18);
            border-radius:16px; padding:48px; max-width:560px; width:100%; text-align:center; }
    h1 { font-size:1.6rem; color:var(--accent); margin-bottom:8px; }
    .intro { color:var(--muted); line-height:1.6; margin-bottom:28px; }

    /* ── Phase blocks ─────────────────────────────────── */
    .phase { text-align:left; margin-bottom:20px; opacity:.45;
             transition:opacity .3s; }
    .phase.active { opacity:1; }
    .phase.done    { opacity:.7; }
    .phase-label { display:flex; align-items:center; gap:8px;
                   font-size:.9rem; font-weight:600; margin-bottom:6px; }
    .phase-num { width:22px; height:22px; border-radius:50%;
                 background:rgba(50,182,255,.15); border:1px solid rgba(50,182,255,.3);
                 display:flex; align-items:center; justify-content:center;
                 font-size:.75rem; font-weight:700; color:var(--accent); flex-shrink:0; }
    .phase.p2 .phase-num { background:rgba(168,85,247,.15);
                           border-color:rgba(168,85,247,.3); color:var(--accent2); }
    .phase-name { flex:1; }
    .phase-check { font-size:1rem; display:none; }
    .phase.done .phase-check { display:inline; }
    .bar-wrap { background:rgba(255,255,255,.07); border-radius:6px;
                height:14px; overflow:hidden; margin-bottom:6px; }
    .bar { height:100%; border-radius:6px; width:0%; transition:width .4s ease; }
    .p1 .bar { background:var(--accent); }
    .p2 .bar { background:var(--accent2); }
    .phase-status { font-size:.78rem; color:var(--muted); min-height:1.3em; }
    .size-note { font-size:.75rem; color:rgba(157,177,209,.5); margin-top:3px; }

    /* ── Button ───────────────────────────────────────── */
    .btn { display:inline-block; margin-top:20px; padding:12px 36px;
           background:var(--accent); color:#0b0f14; border:none; border-radius:8px;
           font-size:1rem; font-weight:700; cursor:pointer; }
    .btn:hover { opacity:.88; }
    .btn:disabled { opacity:.4; cursor:not-allowed; }

    /* ── Skip nebulae link ────────────────────────────── */
    .skip { display:none; font-size:.8rem; color:var(--muted); margin-top:10px;
            cursor:pointer; text-decoration:underline; text-underline-offset:2px; }
  </style>
</head>
<body>
<div class="card">
  <h1>⚙️ First-Run Setup</h1>
  <p class="intro">
    Stellar Insight needs to download two asset packages before it can start.
    Both are stored locally — no re-download on future launches.
  </p>

  <!-- Phase 1: SDE -->
  <div class="phase p1 active" id="ph1">
    <div class="phase-label">
      <div class="phase-num">1</div>
      <span class="phase-name">EVE Static Data Export</span>
      <span class="phase-check">✅</span>
    </div>
    <div class="bar-wrap"><div class="bar" id="bar1"></div></div>
    <div class="phase-status" id="st1">Ready to download.</div>
    <div class="size-note">~85 MB compressed · fuzzwork.co.uk</div>
  </div>

  <!-- Phase 2: Nebula images -->
  <div class="phase p2" id="ph2">
    <div class="phase-label">
      <div class="phase-num">2</div>
      <span class="phase-name">Nebula Images <span style="font-weight:400;color:var(--muted);font-size:.8rem;">(Navigator backgrounds)</span></span>
      <span class="phase-check">✅</span>
    </div>
    <div class="bar-wrap"><div class="bar" id="bar2"></div></div>
    <div class="phase-status" id="st2">Waiting for Phase 1…</div>
    <div class="size-note">54 images · ~15 MB total · res.eveonline.ccpgames.com</div>
  </div>

  <button class="btn" id="btn" onclick="startSetup()">Download &amp; Continue</button>
  <div class="skip" id="skip-neb" onclick="skipNebulae()">Skip nebula images for now</div>
</div>

<script>
  let _phase = 0; // 0=idle, 1=sde, 2=nebulae, 3=done

  function setPhaseActive(n) {
    document.getElementById('ph1').className = 'phase p1' + (n===1?' active':n>1?' done':'');
    document.getElementById('ph2').className = 'phase p2' + (n===2?' active':n>2?' done':'');
  }

  async function startSetup() {
    document.getElementById('btn').disabled = true;
    _phase = 1;
    setPhaseActive(1);
    document.getElementById('st1').textContent = 'Starting download…';
    try {
      await fetch('/app/api/setup/download_sde', {method:'POST'});
      pollSde();
    } catch(e) {
      document.getElementById('st1').textContent = 'Error: ' + e.message;
      document.getElementById('btn').disabled = false;
    }
  }

  async function pollSde() {
    try {
      const r = await fetch('/app/api/setup/sde_status');
      const d = await r.json();
      const pct = d.progress || 0;
      document.getElementById('bar1').style.width = pct + '%';
      if (d.ready) {
        document.getElementById('bar1').style.width = '100%';
        document.getElementById('st1').textContent = 'Complete ✓';
        document.getElementById('ph1').className = 'phase p1 done';
        startNebulae();
        return;
      }
      if (d.status === 'error') {
        document.getElementById('st1').textContent = 'Error: ' + (d.error||'unknown');
        document.getElementById('btn').disabled = false;
        return;
      }
      if (d.status === 'done' && !d.ready) {
        document.getElementById('st1').textContent = 'Decompression failed — click Download to retry';
        document.getElementById('btn').disabled = false;
        return;
      }
      document.getElementById('st1').textContent =
        pct < 91 ? 'Downloading… ' + pct + '%' : 'Decompressing…';
    } catch(e) {}
    setTimeout(pollSde, 800);
  }

  async function startNebulae() {
    _phase = 2;
    setPhaseActive(2);
    document.getElementById('st2').textContent = 'Starting…';
    document.getElementById('skip-neb').style.display = 'block';
    try {
      await fetch('/app/api/setup/download_nebulae', {method:'POST'});
      pollNebulae();
    } catch(e) {
      document.getElementById('st2').textContent = 'Error: ' + e.message;
      finishSetup();
    }
  }

  async function pollNebulae() {
    try {
      const r = await fetch('/app/api/setup/nebulae_status');
      const d = await r.json();
      const pct = d.progress || 0;
      document.getElementById('bar2').style.width = pct + '%';
      const tot = d.total || 54;
      const done = d.done || 0;
      if (d.status === 'done') {
        document.getElementById('bar2').style.width = '100%';
        document.getElementById('st2').textContent = 'Complete ✓';
        document.getElementById('ph2').className = 'phase p2 done';
        document.getElementById('skip-neb').style.display = 'none';
        finishSetup();
        return;
      }
      document.getElementById('st2').textContent =
        'Downloading ' + done + ' / ' + tot + ' images…';
    } catch(e) {}
    setTimeout(pollNebulae, 600);
  }

  function skipNebulae() {
    document.getElementById('skip-neb').style.display = 'none';
    document.getElementById('st2').textContent = 'Skipped — run scripts/download_nebulae.py later';
    document.getElementById('ph2').className = 'phase p2';
    finishSetup();
  }

  function finishSetup() {
    _phase = 3;
    document.getElementById('st1').textContent = 'All done! Loading Stellar Insight…';
    setTimeout(() => { window.location.href = '/app/eve_online'; }, 1400);
  }
</script>
</body>
</html>"""

import math
import difflib
from collections import Counter

# ── SDE import ─────────────────────────────────────────────────────────────────
import logging as _sde_logging
_sde_log = _sde_logging.getLogger("xylon.sde")
_SDE_MKT = "https://market.fuzzwork.co.uk"

try:
    from eve.sde_local import (
        sde_available, sde_info, get_type_names, get_type_name,
        get_market_groups, get_market_group_items, get_blueprint_details,
        get_all_ships, get_all_skills, get_skill_names, get_type_dogma,
        type_search, module_search, drone_search,
        get_slot_types, get_default_charge, get_region_name_sde,
        get_system_info_sde, get_system_description, get_constellation_info,
        get_system_celestials, get_npc_stations, get_neighboring_systems,
        get_trade_hub_distances, get_nearest_sec_entry,
    )
    _sde_log.info(f"[SDE] Local SDE module loaded, available={sde_available()}")
except ImportError as _sde_import_err:
    _sde_log.warning(f"[SDE] Could not import sde_local: {_sde_import_err}")
    sde_available = lambda: False
    sde_info = lambda: {"available": False}
    get_type_names = lambda ids: {}
    get_type_name = lambda tid: f"Type {tid}"
    get_market_groups = lambda: []
    get_market_group_items = lambda gid: []
    get_blueprint_details = lambda tid, **kw: {}
    get_all_ships = lambda: []
    get_all_skills = lambda: []
    get_skill_names = lambda ids: {}
    get_type_dogma = lambda tid: {}
    type_search = lambda q: []
    module_search = lambda query="", slot="": []
    drone_search = lambda query="", limit=100: []
    get_slot_types = lambda ids: {}
    get_default_charge = lambda gid: {}
    get_region_name_sde = lambda sid: None
    get_system_info_sde = lambda sid: None
    get_system_description = lambda sid: ""
    get_constellation_info = lambda cid: None
    get_system_celestials = lambda sid: {}
    get_npc_stations = lambda sid: []
    get_neighboring_systems = lambda sid: []
    get_trade_hub_distances = lambda sid: {}
    get_nearest_sec_entry = lambda sid, t: None


# ── SDE market price cache (separate from main ESI cache) ─────────────────────

def _sde_cache_get(key: str) -> Optional[dict]:
    try:
        with _connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS sde_cache (
                k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL, ttl REAL NOT NULL
            )""")
            row = con.execute("SELECT v, ts, ttl FROM sde_cache WHERE k=?", (key,)).fetchone()
            if row and (time.time() - row[1]) < row[2]:
                return json.loads(row[0])
    except Exception:
        pass
    return None


def _sde_cache_set(key: str, data, ttl_s: int = 86400 * 30):
    try:
        with _connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS sde_cache (
                k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL, ttl REAL NOT NULL
            )""")
            con.execute("INSERT OR REPLACE INTO sde_cache (k, v, ts, ttl) VALUES (?,?,?,?)",
                        (key, json.dumps(data), time.time(), float(ttl_s)))
            con.commit()
    except Exception:
        pass


# ── ESI adjusted prices cache (used for industry EIV) ─────────────────────────

_esi_adj_prices: dict = {}
_esi_adj_prices_ts: float = 0.0


async def _get_esi_adjusted_prices() -> dict:
    global _esi_adj_prices, _esi_adj_prices_ts
    if _esi_adj_prices and (time.time() - _esi_adj_prices_ts) < 86400:
        return _esi_adj_prices
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://esi.evetech.net/latest/markets/prices/") as r:
                if r.status == 200:
                    data = await r.json()
                    _esi_adj_prices = {
                        int(row["type_id"]): float(row.get("adjusted_price") or row.get("average_price") or 0)
                        for row in (data or []) if row.get("type_id")
                    }
                    _esi_adj_prices_ts = time.time()
    except Exception:
        pass
    return _esi_adj_prices


# ── ESI general data cache (blueprints, assets, skills for cache mgmt endpoints) ─

_esi_data_cache: dict = {}   # key -> {data, ts, character_id, alias}


def cache_status() -> dict:
    """Return what's cached and approximate staleness."""
    out = []
    for k, v in _esi_data_cache.items():
        age_s = int(time.time() - v.get("ts", 0))
        out.append({"key": k, "age_seconds": age_s, "character_id": v.get("character_id"), "alias": v.get("alias")})
    return {"count": len(out), "entries": out}


def set_cached(key: str, data: dict, *, character_id: int = 0, alias: str = "") -> None:
    _esi_data_cache[key] = {"data": data, "ts": time.time(), "character_id": character_id, "alias": alias}


def get_cached(key: str) -> Optional[dict]:
    entry = _esi_data_cache.get(key)
    if entry:
        return entry.get("data")
    return None


# ── Helper: resolve type IDs to names for cache mgmt ─────────────────────────

async def _resolve_type_names(type_ids: List[int]) -> Dict[int, str]:
    if not type_ids:
        return {}
    if sde_available():
        try:
            return get_type_names(type_ids)
        except Exception:
            pass
    return await _resolve_entity_names(type_ids)


async def _resolve_location_names(loc_pairs: List[tuple], access_token: str = None) -> Dict[int, str]:
    """Resolve (location_type, location_id) pairs to names."""
    ids = [lid for _, lid in loc_pairs if lid]
    return await _resolve_facility_names(ids, access_token=access_token)


# ── Corp ops ticker (minimal — no Discord) ────────────────────────────────────

_ticker_cache: Dict[str, Any] = {"text": "Corp ops: loading…", "ts": 0.0}
_ticker_running: bool = False
_TICKER_TTL = 300  # 5 minutes


@app.get("/app/api/corp_ops_ticker")
def api_corp_ops_ticker():
    return {"ok": True, "text": _ticker_cache["text"]}


# ── J-space static data (loaded once from data/jspace_static.json) ────────────

_jspace_static: Dict[str, Any] = {}
try:
    import pathlib as _pathlib
    _jsd_path = _pathlib.Path(__file__).resolve().parent / "data" / "jspace_static.json"
    if _jsd_path.exists():
        with open(_jsd_path) as _jf:
            _jsd = json.load(_jf)
            _jspace_static = _jsd.get("systems", {})
            logger.info(f"Loaded {len(_jspace_static)} J-space systems from embedded data")
except Exception as _je:
    logger.warning(f"Could not load jspace_static.json: {_je}")


# ── Sovereignty + FW caches ───────────────────────────────────────────────────

_sov_cache: dict = {"data": {}, "ts": 0.0}
_fw_cache: dict = {"data": {}, "ts": 0.0}


async def _refresh_sov_cache():
    now = time.time()
    if now - _sov_cache["ts"] < 3600 and _sov_cache["data"]:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://esi.evetech.net/latest/sovereignty/map/") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lookup = {}
                    for entry in data:
                        sid = entry.get("system_id")
                        if sid:
                            lookup[sid] = {
                                "alliance_id": entry.get("alliance_id"),
                                "corporation_id": entry.get("corporation_id"),
                                "faction_id": entry.get("faction_id"),
                            }
                    _sov_cache["data"] = lookup
                    _sov_cache["ts"] = now
    except Exception as e:
        logger.debug(f"[SOV] Refresh failed: {e}")


async def _refresh_fw_cache():
    now = time.time()
    if now - _fw_cache["ts"] < 1800 and _fw_cache["data"]:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://esi.evetech.net/latest/fw/systems/") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lookup = {}
                    for entry in data:
                        sid = entry.get("solar_system_id")
                        if sid:
                            lookup[sid] = {
                                "contested": entry.get("contested", "uncontested"),
                                "occupier_faction_id": entry.get("occupier_faction_id"),
                                "owner_faction_id": entry.get("owner_faction_id"),
                                "victory_points": entry.get("victory_points", 0),
                                "victory_points_threshold": entry.get("victory_points_threshold", 0),
                            }
                    _fw_cache["data"] = lookup
                    _fw_cache["ts"] = now
    except Exception as e:
        logger.debug(f"[FW] Refresh failed: {e}")


def _get_system_sov(system_id: int) -> Optional[dict]:
    asyncio.ensure_future(_refresh_sov_cache())
    return _sov_cache["data"].get(system_id)


def _get_system_fw(system_id: int) -> Optional[dict]:
    asyncio.ensure_future(_refresh_fw_cache())
    return _fw_cache["data"].get(system_id)


# ── Region map + system stats caches ─────────────────────────────────────────

_region_map_cache: Dict[int, dict] = {}
_stats_cache: Dict[str, Any] = {"data": None, "ts": 0}


# ═══════════════════════════════════════════════════════════════════════════════
# NAV / WORMHOLE / CHAIN MAP ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

def _require_user():
    return LOCAL_USER


@app.get("/app/api/nav/movements")
def api_nav_movements(request: Request):
    memory = MemoryStore(_db_path())
    chars = memory.eve_list_characters(LOCAL_USER_ID)
    if not chars:
        return {"error": "No linked EVE character", "movements": []}
    default = next((c for c in chars if c.get("is_default")), chars[0])
    char_id = default.get("character_id")
    movements = memory.nav_get_movements(char_id, limit=50)
    return {"character_id": char_id, "character_name": default.get("character_name"), "movements": movements}


@app.get("/app/api/nav/fleet")
async def api_nav_fleet(request: Request):
    memory = MemoryStore(_db_path())
    fleet = memory.nav_get_all_recent_movements(limit=50)
    esi_fleet = None
    try:
        chars = memory.eve_list_characters(LOCAL_USER_ID)
        default = next((c for c in chars if c.get("is_default")), chars[0]) if chars else None
        if default:
            import sqlite3 as _sql
            _c = _sql.connect(_db_path())
            _c.row_factory = _sql.Row
            char_row = _c.execute(
                "SELECT refresh_token, scopes FROM eve_characters WHERE character_id=? LIMIT 1",
                (int(default["character_id"]),)
            ).fetchone()
            _c.close()
            if char_row and char_row["refresh_token"] and "esi-fleets.read_fleet.v1" in str(char_row["scopes"] or ""):
                raw_refresh = decrypt_refresh_token(str(char_row["refresh_token"]))
                tok = await refresh_access_token(refresh_token=str(raw_refresh)) if raw_refresh else None
                access_token = getattr(tok, "access_token", None) if tok else None
                if not access_token and isinstance(tok, dict):
                    access_token = tok.get("access_token")
                if access_token:
                    cid = int(default["character_id"])
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                        async with session.get(
                            f"https://esi.evetech.net/latest/characters/{cid}/fleet/",
                            headers={"Authorization": f"Bearer {access_token}"}
                        ) as resp:
                            if resp.status == 200:
                                fleet_data = await resp.json()
                                fleet_id = fleet_data.get("fleet_id")
                                if fleet_id:
                                    async with session.get(
                                        f"https://esi.evetech.net/latest/fleets/{fleet_id}/members/",
                                        headers={"Authorization": f"Bearer {access_token}"}
                                    ) as mresp:
                                        if mresp.status == 200:
                                            members = await mresp.json()
                                            raw_members = members if isinstance(members, list) else []
                                            ship_ids = list({m.get("ship_type_id") for m in raw_members if m.get("ship_type_id")})
                                            sys_ids = list({m.get("solar_system_id") for m in raw_members if m.get("solar_system_id")})
                                            char_ids = list({m.get("character_id") for m in raw_members if m.get("character_id")})
                                            ship_info: dict = {}
                                            if sde_available() and ship_ids:
                                                try:
                                                    ship_info = {tid: {"name": n, "group": ""} for tid, n in get_type_names(ship_ids).items()}
                                                except Exception:
                                                    pass
                                            name_map: dict = {}
                                            all_ids = [i for i in (sys_ids + char_ids) if i]
                                            if all_ids:
                                                try:
                                                    async with session.post(
                                                        "https://esi.evetech.net/latest/universe/names/",
                                                        json=all_ids[:1000],
                                                        headers={"Content-Type": "application/json"}
                                                    ) as nr:
                                                        if nr.status == 200:
                                                            for item in await nr.json():
                                                                name_map[item["id"]] = item["name"]
                                                except Exception:
                                                    pass
                                            enriched = []
                                            for m in raw_members:
                                                sid_info = ship_info.get(m.get("ship_type_id"), {})
                                                enriched.append({
                                                    "character_id": m.get("character_id"),
                                                    "character_name": name_map.get(m.get("character_id"), f"Pilot {m.get('character_id')}"),
                                                    "ship_type_id": m.get("ship_type_id"),
                                                    "ship_name": sid_info.get("name", f"Ship {m.get('ship_type_id')}"),
                                                    "ship_group": sid_info.get("group", "Unknown"),
                                                    "solar_system_id": m.get("solar_system_id"),
                                                    "system_name": name_map.get(m.get("solar_system_id"), f"System {m.get('solar_system_id')}"),
                                                    "role": m.get("role", "squad_member"),
                                                    "squad_id": m.get("squad_id"),
                                                    "wing_id": m.get("wing_id"),
                                                })
                                            esi_fleet = {"fleet_id": fleet_id, "members": enriched, "role": fleet_data.get("role")}
    except Exception:
        pass
    return {"fleet": fleet, "esi_fleet": esi_fleet}


@app.get("/app/api/nav/intel/{system_ref}")
def api_nav_intel(system_ref: str, request: Request):
    memory = MemoryStore(_db_path())
    try:
        sys_id = int(system_ref)
        intel = memory.nav_get_intel(sys_id, limit=30)
        return {"system_id": sys_id, "intel": intel}
    except ValueError:
        pass
    import sqlite3 as _sql
    _c = _sql.connect(_db_path())
    _c.row_factory = _sql.Row
    row = _c.execute("SELECT system_id FROM nav_movements WHERE lower(system_name)=? LIMIT 1", (system_ref.lower(),)).fetchone()
    if not row:
        row = _c.execute("SELECT system_id FROM nav_movements WHERE lower(system_name) LIKE ? LIMIT 1", (f"%{system_ref.lower()}%",)).fetchone()
    _c.close()
    if row:
        intel = memory.nav_get_intel(int(row["system_id"]), limit=30)
        return {"system_id": int(row["system_id"]), "system_name": system_ref, "intel": intel}
    return {"system_id": None, "system_name": system_ref, "intel": [], "error": f"System '{system_ref}' not found"}


@app.post("/app/api/nav/intel")
def api_nav_submit_intel(request: Request, body: dict = Body(...)):
    memory = MemoryStore(_db_path())
    system_id = body.get("system_id")
    system_name = body.get("system_name", "")
    if system_id and isinstance(system_id, str) and not system_id.isdigit():
        system_name = system_id
        system_id = None
    if not system_id and system_name:
        import sqlite3 as _sql
        _c = _sql.connect(_db_path())
        _c.row_factory = _sql.Row
        row = _c.execute("SELECT system_id FROM nav_movements WHERE lower(system_name)=? LIMIT 1", (system_name.lower(),)).fetchone()
        if not row:
            row = _c.execute("SELECT system_id FROM nav_movements WHERE lower(system_name) LIKE ? LIMIT 1", (f"%{system_name.lower()}%",)).fetchone()
        _c.close()
        if row:
            system_id = int(row["system_id"])
    content = body.get("content", "").strip()
    if not system_id or not content:
        return {"error": "system_id and content required"}
    intel_id = memory.nav_add_intel(int(system_id), content, character_id=LOCAL_USER_ID, character_name="Capsuleer")
    return {"success": True, "id": intel_id}


@app.get("/app/api/nav/intel_recent")
def api_nav_recent_intel(request: Request):
    memory = MemoryStore(_db_path())
    intel = memory.nav_get_recent_intel(limit=50)
    return {"intel": intel}


@app.get("/app/api/nav/wh_active")
def api_nav_wh_active(request: Request):
    memory = MemoryStore(_db_path())
    connections = memory.wh_list_active(limit=50)
    return {"connections": connections}


@app.get("/app/api/nav/flat_map")
async def api_nav_flat_map(request: Request):
    """Chain map data — hybrid movement history + live WH connections."""
    import sqlite3 as _sql
    now = time.time()
    J_IDLE_HOURS = 16
    K_IDLE_HOURS = 72
    _c = _sql.connect(_db_path())
    _c.row_factory = _sql.Row
    sys_rows = _c.execute(
        """SELECT m.system_id, m.system_name, m.security_status,
                  m.region_name, m.constellation_name, m.timestamp as last_seen
           FROM nav_movements m
           INNER JOIN (
             SELECT system_id, MAX(timestamp) as max_ts
             FROM nav_movements
             WHERE timestamp > datetime('now', '-48 hours')
             GROUP BY system_id
           ) latest ON m.system_id = latest.system_id AND m.timestamp = latest.max_ts
           ORDER BY m.system_name"""
    ).fetchall()
    visitor_rows = _c.execute(
        """SELECT DISTINCT system_id, character_id, character_name
           FROM nav_movements
           WHERE timestamp > datetime('now', '-48 hours')
           ORDER BY character_name"""
    ).fetchall()
    _c.close()

    sys_visitors: dict = {}
    all_chars: dict = {}
    for vr in visitor_rows:
        sid = vr["system_id"]
        cid = vr["character_id"]
        cname = vr["character_name"] or f"Char {cid}"
        if sid not in sys_visitors:
            sys_visitors[sid] = []
        if not any(v["character_id"] == cid for v in sys_visitors[sid]):
            sys_visitors[sid].append({"character_id": cid, "character_name": cname})
        all_chars[cid] = cname

    systems = []
    sys_ids = set()
    for r in sys_rows:
        sid = r["system_id"]
        if sid in sys_ids:
            continue
        sys_ids.add(sid)
        sname = r["system_name"] or str(sid)
        sec = r["security_status"] or 0
        is_jspace = sname.upper().startswith("J") and any(c.isdigit() for c in sname)
        idle_limit_h = J_IDLE_HOURS if is_jspace else K_IDLE_HOURS
        last_seen_ts = r["last_seen"] or ""
        try:
            ls = datetime.datetime.strptime(last_seen_ts, "%Y-%m-%d %H:%M:%S")
            idle_hours = (datetime.datetime.utcnow() - ls).total_seconds() / 3600
        except Exception:
            idle_hours = 0
        ttl_hours = max(0, idle_limit_h - idle_hours)
        expired = ttl_hours <= 0
        jspace_data = None
        if is_jspace and sname in _jspace_static:
            jd = _jspace_static[sname]
            jspace_data = {"class": jd.get("class"), "effect": jd.get("effect"), "statics": jd.get("statics", [])}
        systems.append({
            "id": sid, "name": sname,
            "security_status": sec,
            "region": r["region_name"] or "?",
            "constellation": r["constellation_name"] or "?",
            "is_jspace": is_jspace,
            "idle_hours": round(idle_hours, 1),
            "ttl_hours": round(ttl_hours, 1),
            "expired": expired,
            "jspace": jspace_data,
            "visitors": sys_visitors.get(sid, []),
        })

    systems = [s for s in systems if not s["expired"]]
    sys_ids = {s["id"] for s in systems}

    _c2 = _sql.connect(_db_path())
    _c2.row_factory = _sql.Row
    move_rows = _c2.execute(
        """SELECT character_id, system_id, system_name, timestamp
           FROM nav_movements
           WHERE timestamp > datetime('now', '-48 hours')
           ORDER BY character_id, timestamp"""
    ).fetchall()
    _c2.close()

    connections = []
    seen_conns = set()
    prev_char = None
    prev_sid = None
    for r in move_rows:
        cid = r["character_id"]
        sid = r["system_id"]
        if cid == prev_char and prev_sid and prev_sid != sid:
            if prev_sid in sys_ids and sid in sys_ids:
                key = tuple(sorted([prev_sid, sid]))
                if key not in seen_conns:
                    seen_conns.add(key)
                    connections.append({"from": prev_sid, "to": sid})
        prev_char = cid
        prev_sid = sid

    memory = MemoryStore(_db_path())
    wh_conns = memory.wh_list_active(limit=100)
    sys_name_to_id = {s["name"].lower(): s["id"] for s in systems}

    async def _inject_wh_system(sys_name: str):
        sname = sys_name.strip()
        is_jspace = sname.upper().startswith("J") and any(c.isdigit() for c in sname)
        resolved_id = None
        sec = -1.0 if is_jspace else 0.0
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(
                    "https://esi.evetech.net/latest/universe/ids/",
                    json=[sname],
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    if resp.status == 200:
                        id_data = await resp.json()
                        sys_list = id_data.get("systems", [])
                        if sys_list:
                            resolved_id = sys_list[0].get("id")
        except Exception:
            pass
        if not resolved_id:
            import hashlib
            resolved_id = -abs(int(hashlib.md5(sname.encode()).hexdigest()[:8], 16))
        jspace_data = None
        if is_jspace and sname in _jspace_static:
            jd = _jspace_static[sname]
            jspace_data = {"class": jd.get("class"), "effect": jd.get("effect"), "statics": jd.get("statics", [])}
        node = {
            "id": resolved_id, "name": sname,
            "security_status": sec, "region": "Unknown", "constellation": "?",
            "is_jspace": is_jspace, "idle_hours": 0, "ttl_hours": 16 if is_jspace else 72,
            "expired": False, "jspace": jspace_data, "wh_only": True,
        }
        systems.append(node)
        sys_ids.add(resolved_id)
        sys_name_to_id[sname.lower()] = resolved_id
        return resolved_id

    for wh in wh_conns:
        a_name = wh["from_system"].strip()
        b_name = wh["to_system"].strip()
        a_id = sys_name_to_id.get(a_name.lower())
        b_id = sys_name_to_id.get(b_name.lower())
        if not a_id:
            a_id = await _inject_wh_system(a_name)
        if not b_id:
            b_id = await _inject_wh_system(b_name)
        if a_id and b_id:
            key = tuple(sorted([a_id, b_id]))
            if key not in seen_conns:
                seen_conns.add(key)
                connections.append({"from": a_id, "to": b_id, "is_wh": True,
                                    "wh_type": wh.get("wh_type"), "wh_id": wh.get("id")})

    return {
        "systems": systems,
        "connections": connections,
        "characters": [{"character_id": cid, "character_name": cname} for cid, cname in sorted(all_chars.items(), key=lambda x: x[1])],
    }


@app.get("/app/api/nav/wh_history")
def api_nav_wh_history(request: Request):
    memory = MemoryStore(_db_path())
    history = memory.wh_history(limit=30)
    return {"history": history}


@app.delete("/app/api/nav/wh/{conn_id}")
def api_nav_wh_delete(conn_id: int, request: Request):
    memory = MemoryStore(_db_path())
    ok = memory.wh_close_connection(conn_id)
    return {"success": ok, "id": conn_id}


@app.get("/app/api/nav/sigs/{system_id}")
def api_nav_sigs(system_id: int, request: Request):
    memory = MemoryStore(_db_path())
    sigs = memory.nav_get_sigs(system_id)
    return {"system_id": system_id, "sigs": sigs}


@app.post("/app/api/nav/sigs")
def api_nav_paste_sigs(request: Request, body: dict = Body(...)):
    memory = MemoryStore(_db_path())
    system_id = body.get("system_id")
    raw_text = body.get("raw_text", "").strip()
    system_name = body.get("system_name", "")
    if not system_id or not raw_text:
        return {"error": "system_id and raw_text required"}
    result = memory.nav_parse_and_upsert_sigs(
        int(system_id), raw_text,
        system_name=system_name,
        scanned_by=LOCAL_USER_ID,
        scanned_by_name="Capsuleer",
    )
    return {"success": True, **result}


@app.delete("/app/api/nav/sig/{sig_id}")
def api_nav_sig_delete(sig_id: int, request: Request):
    memory = MemoryStore(_db_path())
    ok = memory.nav_delete_sig(sig_id)
    return {"success": ok}


@app.delete("/app/api/nav/sigs/{system_id}")
def api_nav_sigs_delete_all(system_id: int, request: Request):
    memory = MemoryStore(_db_path())
    count = memory.nav_delete_all_sigs(system_id)
    return {"success": True, "deleted": count}


@app.get("/app/api/nav/system_detail/{system_id}")
async def api_nav_system_detail(system_id: int, request: Request):
    """Full system detail: ESI stats, intel, WH connections, signatures, J-space, zKill."""
    import time as _t
    memory = MemoryStore(_db_path())
    system_info = {}
    kills = {}
    jumps_count = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f"https://esi.evetech.net/latest/universe/systems/{system_id}/") as resp:
                if resp.status == 200:
                    system_info = await resp.json()
    except Exception:
        pass

    if _stats_cache.get("data"):
        kills_list = _stats_cache["data"].get("kills", [])
        jumps_list = _stats_cache["data"].get("jumps", [])
        kills = next((k for k in kills_list if k.get("system_id") == system_id), {})
        j = next((j for j in jumps_list if j.get("system_id") == system_id), {})
        jumps_count = j.get("ship_jumps", 0)

    intel = memory.nav_get_intel(system_id, limit=10)
    sys_name = system_info.get("name", str(system_id))
    wh_conns = memory.wh_list_active(system=sys_name)
    sigs = memory.nav_get_sigs(system_id)

    visitors = []
    try:
        import sqlite3 as _sql
        _c = _sql.connect(_db_path())
        _c.row_factory = _sql.Row
        rows = _c.execute(
            """SELECT DISTINCT character_name, timestamp FROM nav_movements
               WHERE system_id=? AND timestamp > datetime('now', '-24 hours')
               ORDER BY timestamp DESC LIMIT 10""",
            (system_id,)
        ).fetchall()
        _c.close()
        visitors = [{"name": r["character_name"], "time": r["timestamp"]} for r in rows]
    except Exception:
        pass

    is_jspace = sys_name.upper().startswith("J") and any(c.isdigit() for c in sys_name)
    jspace_info = None
    if is_jspace:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(
                    f"http://anoik.is/api/v2/systems/{sys_name}",
                    headers={"User-Agent": "XylonEVE"}
                ) as resp:
                    if resp.status == 200:
                        adata = await resp.json()
                        jspace_info = {
                            "class": adata.get("class") or adata.get("wormholeClass", {}).get("title"),
                            "statics": adata.get("statics", []),
                            "effect": adata.get("effect") or adata.get("effectName"),
                            "source": "anoik.is",
                        }
        except Exception:
            pass
        if not jspace_info and sys_name in _jspace_static:
            edata = _jspace_static[sys_name]
            jspace_info = {
                "class": edata.get("class", "Unknown"),
                "statics": edata.get("statics", []),
                "effect": edata.get("effect"),
                "shattered": edata.get("shattered", False),
                "source": "embedded",
            }
        if not jspace_info:
            jspace_info = {"class": "Unknown", "statics": [], "effect": None, "source": "none"}

    zkill_info = None
    _zk_ck = f"resp:zkill_system:{system_id}"
    _zk_hit = _resp_cache_get(_zk_ck, _TTL_ZKILL)
    if _zk_hit is not None:
        zkill_info = _zk_hit.get('zkill_info')
    else:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(
                    f"https://zkillboard.com/api/kills/systemID/{system_id}/pastSeconds/86400/",
                    headers={"User-Agent": "XylonEVE", "Accept-Encoding": "gzip"}
                ) as resp:
                    if resp.status == 200:
                        zkills = await resp.json(content_type=None)
                        if isinstance(zkills, list):
                            kill_count = len(zkills)
                            corp_presence = Counter()
                            ship_types = Counter()
                            total_isk = 0.0
                            hour_kills = Counter()
                            for km in zkills[:20]:
                                zkb = km.get("zkb", {})
                                total_isk += zkb.get("totalValue", 0)
                                kill_time = km.get("killmail_time", "")
                                if kill_time and "T" in kill_time:
                                    try:
                                        hour = int(kill_time.split("T")[1][:2])
                                        hour_kills[hour] += 1
                                    except Exception:
                                        pass
                                victim = km.get("victim", {})
                                ship_id = victim.get("ship_type_id")
                                if ship_id:
                                    ship_types[ship_id] += 1
                                vc = victim.get("corporation_id")
                                if vc:
                                    corp_presence[vc] += 1
                            danger = 0
                            if kill_count >= 1: danger = 1
                            if kill_count >= 3: danger = 2
                            if kill_count >= 8: danger = 3
                            if kill_count >= 15: danger = 4
                            if kill_count >= 30: danger = 5
                            zkill_info = {
                                "kills_24h": kill_count,
                                "last_kill": zkills[0].get("killmail_time", "") if zkills else "",
                                "total_isk_destroyed": round(total_isk, 0),
                                "top_corps": [cid for cid, _ in corp_presence.most_common(5)],
                                "top_ship_types": [sid for sid, _ in ship_types.most_common(5)],
                                "peak_hours": [h for h, _ in hour_kills.most_common(3)],
                                "danger_level": danger,
                            }
        except Exception:
            pass
        _resp_cache_set(_zk_ck, {'zkill_info': zkill_info}, _TTL_ZKILL)

    sec = system_info.get("security_status", 0.0)
    celestials = {}
    constellation_info = None
    neighbors = []
    hub_distances = {}
    nearest_hs = None
    nearest_ls = None
    npc_stations = []
    system_desc = ""
    planets_detail = []
    constellation_id = system_info.get("constellation_id")

    if sde_available():
        try:
            celestials = get_system_celestials(system_id) or {}
            planets_detail = celestials.get("planets", [])
        except Exception:
            pass
        try:
            if constellation_id:
                constellation_info = get_constellation_info(constellation_id)
        except Exception:
            pass
        try:
            neighbors = get_neighboring_systems(system_id) or []
        except Exception:
            pass
        if not is_jspace:
            try:
                hub_distances = get_trade_hub_distances(system_id) or {}
            except Exception:
                pass
            try:
                if sec < 0.45:
                    nearest_hs = get_nearest_sec_entry(system_id, "highsec")
                if sec < 0.0:
                    nearest_ls = get_nearest_sec_entry(system_id, "lowsec")
            except Exception:
                pass
        try:
            npc_stations = get_npc_stations(system_id) or []
        except Exception:
            pass
        try:
            system_desc = get_system_description(system_id) or ""
        except Exception:
            pass

    sov_info = None
    if not is_jspace:
        sov_info = _get_system_sov(system_id)
        if sov_info:
            sov_info = dict(sov_info)
            _FACTION_NAMES = {
                500001: ("Caldari State", 1000035), 500002: ("Minmatar Republic", 1000127),
                500003: ("Amarr Empire", 1000084), 500004: ("Gallente Federation", 1000120),
                500005: ("Jove Empire", 1000161), 500006: ("CONCORD Assembly", 1000125),
                500021: ("Triglavian Collective", 1000298), 500022: ("EDENCOM", 1000299),
            }
            fid = sov_info.get("faction_id")
            if fid and fid in _FACTION_NAMES:
                fname, fcorp = _FACTION_NAMES[fid]
                sov_info["faction_name"] = fname
                sov_info["faction_logo"] = f"https://images.evetech.net/corporations/{fcorp}/logo?size=64"
            aid = sov_info.get("alliance_id")
            if aid:
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as _s:
                        async with _s.get(f"https://esi.evetech.net/latest/alliances/{aid}/") as _r:
                            if _r.status == 200:
                                _d = await _r.json()
                                sov_info["alliance_name"] = _d.get("name")
                    sov_info["alliance_logo"] = f"https://images.evetech.net/alliances/{aid}/logo?size=64"
                except Exception:
                    sov_info["alliance_logo"] = f"https://images.evetech.net/alliances/{aid}/logo?size=64"
            cid = sov_info.get("corporation_id")
            if cid:
                sov_info["corp_logo"] = f"https://images.evetech.net/corporations/{cid}/logo?size=64"

    fw_info = None
    if not is_jspace:
        fw_info = _get_system_fw(system_id)

    # Resolve region_id and region_name for nebula imagery
    region_id_out = None
    region_name_out = None
    if constellation_info and constellation_info.get("region_id"):
        region_id_out = int(constellation_info["region_id"])
        region_name_out = constellation_info.get("region_name")
    elif sde_available() and not is_jspace:
        try:
            _sinfo = get_system_info_sde(system_id)
            if _sinfo and _sinfo.get("region_id"):
                region_id_out = int(_sinfo["region_id"])
        except Exception:
            pass
    if not region_name_out and region_id_out and sde_available():
        try:
            region_name_out = get_region_name_sde(region_id_out)
        except Exception:
            pass

    return {
        "system_id": system_id,
        "name": sys_name,
        "security_status": round(sec, 2),
        "security_class": "Highsec" if sec >= 0.5 else "Lowsec" if sec > 0 else "Wormhole" if is_jspace else "Nullsec",
        "constellation_id": constellation_id,
        "constellation": constellation_info,
        "region_id": region_id_out,
        "region_name": region_name_out,
        "planets_count": len(system_info.get("planets", [])),
        "stargates_count": len(system_info.get("stargates", [])),
        "activity": {
            "ship_kills": kills.get("ship_kills", 0),
            "npc_kills": kills.get("npc_kills", 0),
            "pod_kills": kills.get("pod_kills", 0),
            "jumps": jumps_count,
        },
        "jspace": jspace_info,
        "zkill": zkill_info,
        "intel": [{"content": i.get("content"), "by": i.get("character_name"), "time": i.get("timestamp")} for i in intel],
        "wh_connections": [{"id": w.get("id"), "from": w["from_system"], "to": w["to_system"], "type": w.get("wh_type"), "status": w.get("mass_status")} for w in wh_conns],
        "recent_visitors": visitors,
        "signatures": [{"id": s.get("id"), "sig_id": s.get("sig_id"), "group": s.get("sig_group"), "info": s.get("sig_info"), "scanned_by": s.get("scanned_by_name")} for s in sigs],
        "celestials": celestials, "planets_detail": planets_detail,
        "neighbors": neighbors, "hub_distances": hub_distances,
        "nearest_highsec": nearest_hs, "nearest_lowsec": nearest_ls,
        "npc_stations": npc_stations,
        "sovereignty": sov_info, "fw_status": fw_info,
        "description": system_desc,
    }


@app.get("/app/api/nav/region_map/{region_id}")
async def api_nav_region_map(region_id: int, request: Request):
    """Fetch systems and connections for a region (cached permanently)."""
    import sqlite3 as _sql
    cached = _region_map_cache.get(region_id)
    if cached:
        return cached["data"]
    try:
        _c = _sql.connect(_db_path())
        _c.row_factory = _sql.Row
        row = _c.execute("SELECT data FROM nav_region_cache WHERE region_id=?", (region_id,)).fetchone()
        _c.close()
        if row:
            data = json.loads(row["data"])
            _region_map_cache[region_id] = {"data": data}
            return data
    except Exception:
        pass

    systems_data = []
    connections = []
    seen_conns = set()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.get(f"https://esi.evetech.net/latest/universe/regions/{region_id}/") as resp:
                if resp.status != 200:
                    return {"systems": [], "connections": [], "error": "Region not found"}
                region_info = await resp.json()
            constellation_ids = region_info.get("constellations", [])
            for const_id in constellation_ids:
                try:
                    async with session.get(f"https://esi.evetech.net/latest/universe/constellations/{const_id}/") as resp:
                        if resp.status != 200:
                            continue
                        const_data = await resp.json()
                    for sys_id in const_data.get("systems", []):
                        try:
                            async with session.get(f"https://esi.evetech.net/latest/universe/systems/{sys_id}/") as resp:
                                if resp.status != 200:
                                    continue
                                sys_data = await resp.json()
                            pos = sys_data.get("position", {})
                            systems_data.append({
                                "id": sys_id, "name": sys_data.get("name", str(sys_id)),
                                "x": pos.get("x", 0), "y": pos.get("y", 0), "z": pos.get("z", 0),
                                "security_status": sys_data.get("security_status", 0),
                            })
                            for gate_id in sys_data.get("stargates", []):
                                try:
                                    async with session.get(f"https://esi.evetech.net/latest/universe/stargates/{gate_id}/") as resp:
                                        if resp.status == 200:
                                            gate_data = await resp.json()
                                            dest = gate_data.get("destination", {}).get("system_id")
                                            if dest:
                                                key = tuple(sorted([sys_id, dest]))
                                                if key not in seen_conns:
                                                    seen_conns.add(key)
                                                    connections.append({"from": sys_id, "to": dest})
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception as e:
        return {"systems": [], "connections": [], "error": str(e)}

    result = {"systems": systems_data, "connections": connections}
    _region_map_cache[region_id] = {"data": result}
    if systems_data:
        try:
            import sqlite3 as _sql2
            _c2 = _sql2.connect(_db_path())
            _c2.execute(
                "INSERT OR REPLACE INTO nav_region_cache (region_id, region_name, data, cached_ts) VALUES (?,?,?,?)",
                (region_id, "", json.dumps(result), time.time())
            )
            _c2.commit()
            _c2.close()
        except Exception:
            pass
    return result


@app.get("/app/api/nav/system_stats")
async def api_nav_system_stats():
    """System kills and jumps from ESI (cached 60s)."""
    if _stats_cache["data"] and (time.time() - _stats_cache["ts"]) < 60:
        return _stats_cache["data"]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            kills_data, jumps_data = [], []
            async with session.get("https://esi.evetech.net/latest/universe/system_kills/?datasource=tranquility") as resp:
                if resp.status == 200:
                    kills_data = await resp.json()
            async with session.get("https://esi.evetech.net/latest/universe/system_jumps/?datasource=tranquility") as resp:
                if resp.status == 200:
                    jumps_data = await resp.json()
            result = {"kills": kills_data, "jumps": jumps_data}
            _stats_cache["data"] = result
            _stats_cache["ts"] = time.time()
            return result
    except Exception as e:
        if _stats_cache["data"]:
            return _stats_cache["data"]
        return {"error": str(e), "kills": [], "jumps": []}


@app.get("/app/api/nav/ai/intelligence")
async def api_nav_ai_intelligence(request: Request):
    """AI-formatted intelligence for user's current system."""
    memory = MemoryStore(_db_path())
    chars = memory.eve_list_characters(LOCAL_USER_ID)
    if not chars:
        return {"error": "No linked EVE character"}
    default = next((c for c in chars if c.get("is_default")), chars[0])
    char_id = default.get("character_id")
    movements = memory.nav_get_movements(char_id, limit=1)
    if not movements:
        return {"error": "No location data. Move in-game to start tracking."}
    loc = movements[0]
    system_id = loc.get("system_id")
    system_name = loc.get("system_name", "?")
    security = loc.get("security_status", 0.0)
    intel = memory.nav_get_intel(system_id, limit=5)
    sec_class = "Highsec" if security >= 0.5 else "Lowsec" if security > 0 else "Nullsec"
    return {
        "system_name": system_name, "system_id": system_id,
        "security_status": f"{security:.2f}", "security_class": sec_class,
        "region": loc.get("region_name", "?"),
        "constellation": loc.get("constellation_name", "?"),
        "recent_intel_reports": [{"report": i.get("content"), "by": i.get("character_name"), "time": i.get("timestamp")} for i in intel],
        "summary": f"System {system_name} is a {security:.2f} {sec_class} system in {loc.get('region_name', '?')}. {'There are recent intel reports.' if intel else 'No recent intel reports.'}"
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SDE ENDPOINTS (local SQLite — Fuzzwork SDE)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/app/api/sde/blueprint_details/{type_id}")
async def api_sde_blueprint_details(type_id: int):
    if not sde_available():
        return {"error": "SDE not available. Download sde.sqlite to the data/ folder."}
    try:
        return get_blueprint_details(type_id)
    except Exception as e:
        return {"error": str(e)}


@app.get("/app/api/sde/type_names")
async def api_sde_type_names(type_ids: str = ""):
    ids = [int(x.strip()) for x in type_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return {}
    if not sde_available():
        return {"error": "SDE not available"}
    try:
        names = get_type_names(ids)
        return {str(k): {"typeID": k, "typeName": v} for k, v in names.items()}
    except Exception as e:
        return {}


@app.get("/app/api/sde/type_dogma/{type_id}")
async def api_sde_type_dogma(type_id: int):
    if not sde_available():
        return {"error": "SDE not available"}
    try:
        return get_type_dogma(type_id)
    except Exception as e:
        return {"error": str(e)}


@app.get("/app/api/sde/market_groups")
async def api_sde_market_groups():
    if not sde_available():
        return JSONResponse(status_code=503, content={"error": "SDE not available"})
    try:
        groups = get_market_groups()
        return groups
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/app/api/sde/market_group_items/{group_id}")
async def api_sde_market_group_items(group_id: int):
    if not sde_available():
        return []
    try:
        return get_market_group_items(group_id)
    except Exception:
        return []


@app.get("/app/api/sde/ships")
async def api_sde_ships():
    if not sde_available():
        return []
    try:
        return get_all_ships()
    except Exception:
        return []


@app.get("/app/api/sde/skills")
async def api_sde_skills():
    if not sde_available():
        return []
    try:
        return get_all_skills()
    except Exception as e:
        logger.error(f"[SDE] get_all_skills error: {e}")
        return []


@app.get("/app/api/eve/skills/queue_rich")
@app.get("/app/api/eo/skills/queue_rich")
async def api_skills_queue_rich(alias: str, user=Depends(require_user)):
    """Skill queue enriched with resolved skill names from SDE."""
    token = await eve_access_token_for(LOCAL_USER_ID, alias)
    cid = int(token["character"]["character_id"])
    at = token["access_token"]

    queue = await esi_get_json(f"/characters/{cid}/skillqueue/", access_token=at) or []
    char_skills_data = {}
    try:
        cs = await esi_get_json(f"/characters/{cid}/skills/", access_token=at) or {}
        char_skills_data = {s["skill_id"]: s for s in cs.get("skills", [])}
    except Exception:
        pass

    # Resolve skill names from SDE
    skill_ids = list({int(e["skill_id"]) for e in queue if e.get("skill_id")})
    name_map = get_skill_names(skill_ids) if sde_available() else {}

    enriched = []
    for entry in queue:
        sid = int(entry.get("skill_id", 0))
        trained = char_skills_data.get(sid, {})
        enriched.append({
            **entry,
            "skill_name": name_map.get(sid) or f"Skill {sid}",
            "trained_skill_level": trained.get("trained_skill_level"),
            "active_skill_level": trained.get("active_skill_level"),
        })

    return {"ok": True, "queue": enriched, "char_id": cid}


@app.post("/app/api/sync/skill_plan")
async def api_sync_push_skill_plan(req: Request, user=Depends(require_user)):
    """Push local skill plan to corp sync server."""
    body = await req.json()
    plan = body.get("plan", [])
    alias = body.get("alias", "")
    if not plan:
        return {"ok": False, "detail": "No plan provided."}
    sync_url = _sync_cfg_get("sync_url")
    sync_token = _sync_cfg_get("sync_token")
    if not sync_url or not sync_token:
        return {"ok": False, "detail": "Sync server not configured."}
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{sync_url.rstrip('/')}/api/skill_plan",
                json={"plan": plan, "alias": alias},
                headers={"Authorization": f"Bearer {sync_token}"},
            )
            r.raise_for_status()
            return {"ok": True}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.get("/app/api/sync/skill_plans")
async def api_sync_get_skill_plans(user=Depends(require_user)):
    """Fetch all members' skill plans from sync server."""
    sync_url = _sync_cfg_get("sync_url")
    sync_token = _sync_cfg_get("sync_token")
    if not sync_url or not sync_token:
        return {"ok": False, "plans": [], "detail": "Sync server not configured."}
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{sync_url.rstrip('/')}/api/skill_plans",
                headers={"Authorization": f"Bearer {sync_token}"},
            )
            r.raise_for_status()
            return {"ok": True, "plans": r.json().get("plans", [])}
    except Exception as e:
        return {"ok": False, "plans": [], "detail": str(e)}


@app.get("/app/api/sde/type_search")
async def api_sde_type_search(q: str = "", category: str = ""):
    if not sde_available():
        return []
    try:
        return type_search(q)
    except Exception:
        return []


@app.get("/app/api/sde/module_search")
async def api_sde_module_search(q: str = "", slot: str = ""):
    if not sde_available():
        return []
    try:
        return module_search(query=q, slot=slot)
    except Exception:
        return []


@app.get("/app/api/sde/default_charge/{charge_group_id}")
async def api_sde_default_charge(charge_group_id: int):
    if not sde_available():
        return {}
    try:
        return get_default_charge(charge_group_id)
    except Exception:
        return {}


@app.get("/app/api/sde/slot_types")
async def api_sde_slot_types(type_ids: str = ""):
    if not sde_available() or not type_ids:
        return {}
    try:
        ids = [int(x) for x in type_ids.split(',') if x.strip().isdigit()]
        return get_slot_types(ids)
    except Exception:
        return {}


@app.get("/app/api/sde/drone_search")
async def api_sde_drone_search(q: str = "", limit: int = 100):
    if not sde_available():
        return []
    try:
        return drone_search(query=q, limit=limit)
    except Exception:
        return []


@app.get("/app/api/sde/market_prices/{type_id}")
async def api_sde_market_prices(type_id: int):
    """Fetch live prices from trade hubs. Fuzzwork primary, ESI fallback."""
    ck = f"sde:mkt_price:{type_id}"
    cached = _sde_cache_get(ck)
    if cached:
        return cached

    HUBS = {
        "Jita":    (60003760, 10000002),
        "Amarr":   (60008494, 10000043),
        "Dodixie": (60011866, 10000032),
        "Rens":    (60004588, 10000030),
        "Hek":     (60005686, 10000042),
    }

    async def _esi_regional_price(session, region_id: int, tid: int) -> dict:
        try:
            orders = []
            page = 1
            while True:
                url = (f"https://esi.evetech.net/latest/markets/{region_id}/orders/"
                       f"?type_id={tid}&order_type=all&page={page}")
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        break
                    batch = await r.json()
                    if not batch:
                        break
                    orders.extend(batch)
                    if len(batch) < 1000:
                        break
                    page += 1
            if not orders:
                return {}
            sells = [o["price"] for o in orders if not o["is_buy_order"]]
            buys = [o["price"] for o in orders if o["is_buy_order"]]
            return {
                "sell": {"min": min(sells) if sells else None, "volume": sum(o["volume_remain"] for o in orders if not o["is_buy_order"])},
                "buy": {"max": max(buys) if buys else None, "volume": sum(o["volume_remain"] for o in orders if o["is_buy_order"])},
                "_source": "esi_fallback",
            }
        except Exception:
            return {}

    results = {}
    fuzzwork_ok = True
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            for hub_name, (station_id, region_id) in HUBS.items():
                try:
                    url = f"{_SDE_MKT}/aggregates/?station={station_id}&types={type_id}"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            item = data.get(str(type_id)) or data.get(type_id) or {}
                            if item:
                                results[hub_name] = item
                            else:
                                results[hub_name] = await _esi_regional_price(session, region_id, type_id)
                        else:
                            fuzzwork_ok = False
                            results[hub_name] = await _esi_regional_price(session, region_id, type_id)
                except Exception:
                    fuzzwork_ok = False
                    results[hub_name] = None
    except Exception:
        fuzzwork_ok = False

    missing = [h for h, v in results.items() if v is None]
    if missing:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                for hub_name in missing:
                    _, region_id = HUBS[hub_name]
                    results[hub_name] = await _esi_regional_price(session, region_id, type_id)
        except Exception:
            pass

    history = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f"https://esi.evetech.net/latest/markets/10000002/history/?type_id={type_id}") as resp:
                if resp.status == 200:
                    history = await resp.json()
    except Exception:
        pass

    result = {
        "hubs": results,
        "history": history[-365:] if isinstance(history, list) else [],
        "price_source": "fuzzwork" if fuzzwork_ok else "esi_fallback",
    }
    _sde_cache_set(ck, result, 600)
    return result


@app.get("/app/api/sde/diag")
async def api_sde_diagnostic():
    diag = {"sde_local": sde_info() if sde_available() else {"available": False, "error": "SDE not available"}}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f"{_SDE_MKT}/aggregates/?station=60003760&types=34") as resp:
                diag["market_api"] = {"status": resp.status, "ok": resp.status == 200}
    except Exception as e:
        diag["market_api"] = {"ok": False, "error": str(e)}
    return diag


@app.delete("/app/api/sde/cache")
async def api_sde_cache_clear():
    try:
        with _connect() as con:
            con.execute("DELETE FROM sde_cache WHERE 1=1")
            con.commit()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# ESI CACHE MANAGEMENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/app/api/esi/cache/status")
async def api_esi_cache_status():
    return cache_status()


@app.post("/app/api/esi/cache/refresh_all")
async def api_esi_cache_refresh_all():
    results = await _esi_refresh_all_characters()
    return {"ok": True, "results": results}


async def _esi_refresh_all_characters() -> list:
    results = []
    try:
        with _connect() as con:
            if not _table_exists(con, "eve_characters"):
                return [{"error": "eve_characters table not found"}]
            uid_col = _eve_characters_user_col(con)
            rows = con.execute(
                f"SELECT DISTINCT character_id, character_name, alias, refresh_token, {uid_col} as user_id "
                f"FROM eve_characters WHERE refresh_token IS NOT NULL AND refresh_token != ''"
            ).fetchall()
            chars = [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]

    for ch in chars:
        cid = int(ch["character_id"])
        alias = ch.get("alias") or ch.get("character_name") or str(cid)
        uid = int(ch["user_id"])
        char_result = {"character": alias, "character_id": cid, "refreshed": [], "errors": []}
        try:
            token = await eve_access_token_for(uid, alias)
            at = token["access_token"]
            char_info = token["character"]

            try:
                data = await esi_get_json(f"/characters/{cid}/blueprints/", access_token=at)
                bps = data or []
                type_ids = list(set(int(b.get("type_id") or 0) for b in bps if isinstance(b, dict) and b.get("type_id")))
                type_names = await _resolve_type_names(type_ids) if type_ids else {}
                set_cached(f"blueprints:{cid}", {"ok": True, "character": char_info, "blueprints": data, "type_names": type_names}, character_id=cid, alias=alias)
                char_result["refreshed"].append(f"blueprints ({len(bps)})")
            except Exception as e:
                char_result["errors"].append(f"blueprints: {e}")

            try:
                data = await esi_get_json(f"/characters/{cid}/assets/", access_token=at)
                type_ids = [int(a.get("type_id") or 0) for a in (data or [])[:500]]
                loc_pairs = [(str(a.get("location_type") or ""), int(a.get("location_id") or 0)) for a in (data or [])[:500]]
                type_names = await _resolve_type_names(type_ids)
                location_names = await _resolve_location_names(loc_pairs, access_token=at)
                set_cached(f"assets:{cid}", {"ok": True, "character": char_info, "assets": data, "type_names": type_names, "location_names": location_names}, character_id=cid, alias=alias)
                char_result["refreshed"].append(f"assets ({len(data or [])})")
            except Exception as e:
                char_result["errors"].append(f"assets: {e}")

            try:
                data = await esi_get_json(f"/characters/{cid}/skills/", access_token=at)
                set_cached(f"skills:{cid}", {"ok": True, "character": char_info, "skills": data}, character_id=cid, alias=alias)
                skills_list = data.get("skills", []) if isinstance(data, dict) else []
                char_result["refreshed"].append(f"skills ({len(skills_list)})")
            except Exception as e:
                char_result["errors"].append(f"skills: {e}")

            # Track character location for the navigator
            try:
                loc = await esi_get_json(f"/characters/{cid}/location/", access_token=at)
                sys_id = (loc or {}).get("solar_system_id")
                if sys_id:
                    sys_info = None
                    if sde_available():
                        try:
                            sys_info = get_system_info_sde(int(sys_id))
                        except Exception:
                            pass
                    sys_name = (sys_info or {}).get("solarSystemName") or (sys_info or {}).get("name") or str(sys_id)
                    sec_status = (sys_info or {}).get("security", 0.0)
                    region_name = (sys_info or {}).get("regionName") or ""
                    constellation_name = (sys_info or {}).get("constellationName") or ""
                    # Also try ESI for names if SDE didn't return them
                    if sys_name == str(sys_id):
                        try:
                            names = await _resolve_entity_names([int(sys_id)])
                            sys_name = names.get(int(sys_id), str(sys_id))
                        except Exception:
                            pass
                    memory = MemoryStore(_db_path())
                    memory.nav_log_movement(
                        character_id=cid,
                        system_id=int(sys_id),
                        system_name=sys_name,
                        security_status=float(sec_status),
                        region_name=region_name,
                        constellation_name=constellation_name,
                        character_name=char_info.get("character_name", alias),
                    )
                    char_result["refreshed"].append(f"location ({sys_name})")
            except Exception as e:
                char_result["errors"].append(f"location: {e}")

        except Exception as e:
            char_result["errors"].append(f"token: {e}")

        results.append(char_result)
    return results


@app.post("/app/api/esi/cache/refresh")
async def api_esi_cache_refresh(alias: str):
    token = await eve_access_token_for(LOCAL_USER_ID, alias)
    cid = int(token["character"]["character_id"])
    refreshed = []
    try:
        data = await esi_get_json(f"/characters/{cid}/blueprints/", access_token=token["access_token"])
        bps = data or []
        type_ids = list(set(int(b.get("type_id") or 0) for b in bps if isinstance(b, dict) and b.get("type_id")))
        type_names = await _resolve_type_names(type_ids) if type_ids else {}
        set_cached(f"blueprints:{cid}", {"ok": True, "character": token["character"], "blueprints": data, "type_names": type_names}, character_id=cid, alias=alias)
        refreshed.append(f"blueprints ({len(bps)} items)")
    except Exception as e:
        refreshed.append(f"blueprints FAILED: {e}")
    try:
        data = await esi_get_json(f"/characters/{cid}/assets/", access_token=token["access_token"])
        type_ids = [int(a.get("type_id") or 0) for a in (data or [])[:500]]
        loc_pairs = [(str(a.get("location_type") or ""), int(a.get("location_id") or 0)) for a in (data or [])[:500]]
        type_names = await _resolve_type_names(type_ids)
        location_names = await _resolve_location_names(loc_pairs, access_token=token.get("access_token"))
        set_cached(f"assets:{cid}", {"ok": True, "character": token["character"], "assets": data, "type_names": type_names, "location_names": location_names}, character_id=cid, alias=alias)
        refreshed.append(f"assets ({len(data or [])} items)")
    except Exception as e:
        refreshed.append(f"assets FAILED: {e}")
    try:
        data = await esi_get_json(f"/characters/{cid}/skills/", access_token=token["access_token"])
        set_cached(f"skills:{cid}", {"ok": True, "character": token["character"], "skills": data}, character_id=cid, alias=alias)
        skills_list = data.get("skills", []) if isinstance(data, dict) else []
        refreshed.append(f"skills ({len(skills_list)} skills)")
    except Exception as e:
        refreshed.append(f"skills FAILED: {e}")
    return {"ok": True, "refreshed": refreshed}


# ═══════════════════════════════════════════════════════════════════════════════
# INDUSTRY / MANUFACTURING CALCULATOR ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/app/api/industry/cost_index")
async def api_industry_cost_index(system_name: str = ""):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://esi.evetech.net/latest/industry/systems/") as r:
                if r.status != 200:
                    return {"ok": False, "error": f"ESI {r.status}"}
                systems = await r.json()

        q = system_name.strip().lower()
        match = next((s for s in systems if (s.get("solar_system_name", "") or "").lower() == q), None)
        if not match:
            names = [(s.get("solar_system_name", ""), s) for s in systems]
            close = difflib.get_close_matches(q, [n[0].lower() for n in names], n=1, cutoff=0.8)
            if close:
                match = next((s for name, s in names if name.lower() == close[0]), None)

        if not match:
            return {"ok": False, "error": f"System '{system_name}' not found"}

        indices = {c["activity"]: c["cost_index"] for c in (match.get("cost_indices") or [])}
        return {
            "ok": True,
            "system_id": match.get("solar_system_id"),
            "system_name": match.get("solar_system_name", system_name),
            "cost_indices": indices,
            "manufacturing": indices.get(1, 0.0),
            "reactions": indices.get(11, 0.0),
            "invention": indices.get(8, 0.0),
            "copying": indices.get(5, 0.0),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/app/api/industry/char_bonuses")
async def api_industry_char_bonuses(alias: str):
    try:
        token = await eve_access_token_for(LOCAL_USER_ID, alias)
        cid = int(token["character"]["character_id"])
        data = await esi_get_json(f"/characters/{cid}/skills/", access_token=token["access_token"])
        skills_list = (data or {}).get("skills", [])
        skill_map = {s["skill_id"]: s["active_skill_level"] for s in skills_list}
        industry_lvl = skill_map.get(3380, 0)
        adv_industry_lvl = skill_map.get(3388, 0)
        mass_production_lvl = skill_map.get(11395, 0)
        adv_mass_prod_lvl = skill_map.get(24268, 0)
        te_bonus_pct = (industry_lvl * 4) + (adv_industry_lvl * 3)
        parallel_jobs = 1 + mass_production_lvl + adv_mass_prod_lvl
        return {
            "ok": True,
            "te_bonus_pct": te_bonus_pct,
            "parallel_jobs": parallel_jobs,
            "skills": {
                "industry": industry_lvl,
                "advanced_industry": adv_industry_lvl,
                "mass_production": mass_production_lvl,
                "adv_mass_production": adv_mass_prod_lvl,
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "te_bonus_pct": 0, "parallel_jobs": 1, "skills": {}}


@app.post("/app/api/industry/calc")
async def api_industry_calc(request: Request):
    """Full manufacturing calculation with ME/TE, EIV, install cost, profit."""
    body = await request.json()
    bp_type_id = int(body.get("bp_type_id", 0))
    runs = max(1, int(body.get("runs", 1)))
    me = max(0, min(10, int(body.get("me", 0))))
    te = max(0, min(20, int(body.get("te", 0))))
    activity = int(body.get("activity", 1))
    facility_tax = float(body.get("facility_tax", 0.0)) / 100.0
    scc_surcharge = float(body.get("scc_surcharge", 4.0)) / 100.0
    cost_index = float(body.get("cost_index", 0.0)) / 100.0
    mat_hub = body.get("mat_hub", "Jita")
    out_hub = body.get("out_hub", "Jita")
    mat_order = body.get("mat_order_type", "sell")
    out_order = body.get("out_order_type", "sell")
    char_te_bonus = float(body.get("char_te_bonus_pct", 0)) / 100.0
    struct_me_bonus = float(body.get("struct_me_bonus_pct", 0)) / 100.0
    struct_te_bonus = float(body.get("struct_te_bonus_pct", 0)) / 100.0
    buy_broker_fee = float(body.get("buy_broker_fee", 0.0)) / 100.0
    sell_broker_fee = float(body.get("sell_broker_fee", 0.0)) / 100.0
    sales_tax = float(body.get("sales_tax", 0.0)) / 100.0

    if not bp_type_id:
        return {"ok": False, "error": "No blueprint selected"}
    if not sde_available():
        return {"ok": False, "error": "SDE not available. Download sde.sqlite to data/ folder."}

    bp_data = get_blueprint_details(bp_type_id, activity_id=activity)
    bp_info = bp_data.get(str(bp_type_id), {})
    act_key = {1: "manufacturing", 5: "copying", 8: "invention", 11: "reactions"}.get(activity, "manufacturing")
    mfg = bp_info.get(act_key) or {}
    raw_materials = mfg.get("materials", [])
    products = mfg.get("products", [])
    base_time = mfg.get("time", 0)

    if not raw_materials and not products:
        return {"ok": False, "error": "No manufacturing data found for this blueprint in SDE"}

    me_multiplier = (1.0 - me / 100.0) * (1.0 - struct_me_bonus)
    all_type_ids = [m["typeID"] for m in raw_materials]
    product_type_ids = [p["typeID"] for p in products]
    all_ids_to_name = list(set(all_type_ids + product_type_ids + [bp_type_id]))
    type_names = get_type_names(all_ids_to_name)

    materials = []
    for mat in raw_materials:
        tid = mat["typeID"]
        base_qty = mat["quantity"]
        adj_qty = max(1, math.ceil(base_qty * runs * me_multiplier))
        materials.append({
            "type_id": tid,
            "name": type_names.get(tid, f"Type {tid}"),
            "base_qty": base_qty * runs,
            "adj_qty": adj_qty,
            "waste_qty": max(0, (base_qty * runs) - adj_qty),
        })

    product = None
    if products:
        p = products[0]
        tid = p["typeID"]
        product = {"type_id": tid, "name": type_names.get(tid, f"Type {tid}"), "quantity": p["quantity"] * runs}

    te_multiplier = (1.0 - te / 100.0) * (1.0 - char_te_bonus) * (1.0 - struct_te_bonus)
    job_time_secs = max(1, round(base_time * runs * te_multiplier))

    mat_prices: dict = {}
    product_price = {"sell": 0, "buy": 0}
    all_price_ids = list(set([m["type_id"] for m in materials] + ([product["type_id"]] if product else [])))

    async def fetch_price(tid, hub):
        try:
            result = await api_sde_market_prices(tid)
            hubs = result.get("hubs", {})
            hub_data = hubs.get(hub) or hubs.get("Jita") or {}
            sell_raw = hub_data.get("sell") or {}
            buy_raw = hub_data.get("buy") or {}
            sell_price = 0.0
            buy_price = 0.0
            if isinstance(sell_raw, dict):
                sell_price = float(sell_raw.get("min") or sell_raw.get("price") or 0)
            elif sell_raw:
                sell_price = float(sell_raw)
            if isinstance(buy_raw, dict):
                buy_price = float(buy_raw.get("max") or buy_raw.get("price") or 0)
            elif buy_raw:
                buy_price = float(buy_raw)
            return tid, {"sell": sell_price, "buy": buy_price}
        except Exception:
            return tid, {"sell": 0, "buy": 0}

    mat_tasks = [fetch_price(tid, mat_hub) for tid in [m["type_id"] for m in materials]]
    out_tasks = [fetch_price(product["type_id"], out_hub)] if product else []
    mat_results = await asyncio.gather(*mat_tasks)
    out_results = await asyncio.gather(*out_tasks)
    for tid, prices in mat_results:
        mat_prices[tid] = prices
    if out_results:
        _, product_price = out_results[0]

    total_mat_cost_sell = 0.0
    total_mat_cost_buy = 0.0
    total_mat_net_sell = 0.0
    total_mat_net_buy = 0.0
    for mat in materials:
        p = mat_prices.get(mat["type_id"], {"sell": 0, "buy": 0})
        mat["unit_sell"] = p["sell"]
        mat["unit_buy"] = p["buy"]
        mat["gross_sell"] = mat["adj_qty"] * p["sell"]
        mat["gross_buy"] = mat["adj_qty"] * p["buy"]
        mat["net_sell"] = mat["gross_sell"] * (1.0 + buy_broker_fee)
        mat["net_buy"] = mat["gross_buy"]
        total_mat_cost_sell += mat["gross_sell"]
        total_mat_cost_buy += mat["gross_buy"]
        total_mat_net_sell += mat["net_sell"]
        total_mat_net_buy += mat["net_buy"]

    adj_prices = await _get_esi_adjusted_prices()
    eiv = sum(
        (adj_prices.get(mat["type_id"], mat["unit_sell"]) * mat["adj_qty"])
        for mat in materials
    )
    if eiv == 0:
        eiv = total_mat_cost_sell

    raw_install = eiv * cost_index
    install_cost = raw_install * (1.0 + facility_tax + scc_surcharge)

    out_qty = product["quantity"] if product else 0
    out_val_sell = out_qty * product_price["sell"]
    out_val_buy = out_qty * product_price["buy"]
    out_net_sell = out_val_sell * (1.0 - sell_broker_fee - sales_tax)
    out_net_buy = out_val_buy * (1.0 - sales_tax)
    broker_cost_sell = out_val_sell * sell_broker_fee
    sales_tax_cost = out_val_sell * sales_tax

    if product:
        product["unit_sell"] = product_price["sell"]
        product["unit_buy"] = product_price["buy"]
        product["gross_sell"] = out_val_sell
        product["gross_buy"] = out_val_buy
        product["net_sell"] = out_net_sell
        product["net_buy"] = out_net_buy

    mat_cost_chosen = total_mat_net_sell if mat_order == "sell" else total_mat_net_buy
    out_val_chosen = out_net_sell if out_order == "sell" else out_net_buy
    total_cost = mat_cost_chosen + install_cost
    profit = out_val_chosen - total_cost
    margin = (profit / out_val_chosen * 100) if out_val_chosen else 0
    isk_per_hour = (profit / (job_time_secs / 3600)) if job_time_secs > 0 else 0

    return {
        "ok": True,
        "blueprint": {"type_id": bp_type_id, "name": type_names.get(bp_type_id, f"Blueprint {bp_type_id}"), "me": me, "te": te, "runs": runs, "activity": activity},
        "product": product,
        "materials": materials,
        "job": {
            "time_secs": job_time_secs, "eiv": eiv, "cost_index": cost_index * 100,
            "raw_install": raw_install, "facility_tax_cost": raw_install * facility_tax,
            "scc_cost": raw_install * scc_surcharge, "install_cost": install_cost,
            "broker_cost_sell": broker_cost_sell, "sales_tax_cost": sales_tax_cost,
        },
        "totals": {
            "mat_cost_sell": total_mat_cost_sell, "mat_cost_buy": total_mat_cost_buy,
            "mat_net_sell": total_mat_net_sell, "mat_net_buy": total_mat_net_buy,
            "mat_cost_chosen": mat_cost_chosen, "install_cost": install_cost,
            "total_cost": total_cost, "out_val_sell": out_val_sell, "out_val_buy": out_val_buy,
            "out_net_sell": out_net_sell, "out_net_buy": out_net_buy,
            "out_val_chosen": out_val_chosen, "broker_cost_sell": broker_cost_sell,
            "sales_tax_cost": sales_tax_cost, "profit": profit,
            "margin_pct": round(margin, 2), "isk_per_hour": isk_per_hour,
        }
    }


@app.get("/app/api/industry/presets")
async def api_mfg_presets_list():
    memory = MemoryStore(_db_path())
    return {"ok": True, "presets": memory.mfg_preset_list()}


@app.post("/app/api/industry/presets")
async def api_mfg_presets_save(request: Request):
    body = await request.json()
    memory = MemoryStore(_db_path())
    pid = memory.mfg_preset_save(body)
    return {"ok": True, "id": pid}


@app.delete("/app/api/industry/presets/{preset_id}")
async def api_mfg_presets_delete(preset_id: int):
    memory = MemoryStore(_db_path())
    ok = memory.mfg_preset_delete(preset_id)
    return {"ok": ok}


@app.get("/app/api/industry/presets/{preset_id}")
async def api_mfg_presets_get(preset_id: int):
    memory = MemoryStore(_db_path())
    p = memory.mfg_preset_get(preset_id)
    return {"ok": bool(p), "preset": p}


@app.get("/app/api/industry/global_defaults")
async def api_mfg_global_get():
    memory = MemoryStore(_db_path())
    return {"ok": True, "defaults": memory.mfg_global_get()}


@app.post("/app/api/industry/global_defaults")
async def api_mfg_global_set(request: Request):
    body = await request.json()
    memory = MemoryStore(_db_path())
    for k, v in body.items():
        memory.mfg_global_set(str(k), str(v))
    return {"ok": True}


@app.get("/app/api/industry/structure_bonuses")
async def api_industry_structure_bonuses(alias: str):
    """Structure manufacturing ME/TE bonuses (base + fitted rigs from corp assets)."""
    try:
        token = await eve_access_token_for(LOCAL_USER_ID, alias)
        cid = int(token["character"]["character_id"])
        corp_id = await eve_get_corp_id(cid)
        acc_tok = token["access_token"]
    except Exception as e:
        return {"ok": False, "error": str(e), "structures": []}

    STRUCT_BASE = {
        35825: {"name": "Raitaru", "me_pct": 1.0, "te_pct": 15.0},
        35826: {"name": "Azbel", "me_pct": 1.0, "te_pct": 20.0},
        35827: {"name": "Sotiyo", "me_pct": 1.0, "te_pct": 30.0},
        45647: {"name": "Tatara", "me_pct": 1.0, "te_pct": 25.0},
        45646: {"name": "Athanor", "me_pct": 0.0, "te_pct": 0.0},
    }
    RIG_BONUSES = {
        37151: {"name": "Medium ME Rig I — Composite", "me_pct": 2.0, "te_pct": 0.0},
        37152: {"name": "Medium ME Rig I — Components", "me_pct": 2.0, "te_pct": 0.0},
        37153: {"name": "Medium ME Rig I — Ships", "me_pct": 2.0, "te_pct": 0.0},
        37154: {"name": "Medium TE Rig I — Composite", "me_pct": 0.0, "te_pct": 4.0},
        37155: {"name": "Medium TE Rig I — Components", "me_pct": 0.0, "te_pct": 4.0},
        37156: {"name": "Medium TE Rig I — Ships", "me_pct": 0.0, "te_pct": 4.0},
        37157: {"name": "Medium ME Rig II — Composite", "me_pct": 4.0, "te_pct": 0.0},
        37158: {"name": "Medium ME Rig II — Components", "me_pct": 4.0, "te_pct": 0.0},
        37159: {"name": "Medium ME Rig II — Ships", "me_pct": 4.0, "te_pct": 0.0},
        37160: {"name": "Medium TE Rig II — Composite", "me_pct": 0.0, "te_pct": 8.0},
        37161: {"name": "Medium TE Rig II — Components", "me_pct": 0.0, "te_pct": 8.0},
        37162: {"name": "Medium TE Rig II — Ships", "me_pct": 0.0, "te_pct": 8.0},
        37163: {"name": "Large ME Rig I", "me_pct": 2.0, "te_pct": 0.0},
        37164: {"name": "Large TE Rig I", "me_pct": 0.0, "te_pct": 4.0},
        37165: {"name": "Large ME Rig II", "me_pct": 4.0, "te_pct": 0.0},
        37166: {"name": "Large TE Rig II", "me_pct": 0.0, "te_pct": 8.0},
        46494: {"name": "Medium Reaction TE Rig I", "me_pct": 0.0, "te_pct": 4.0},
        46495: {"name": "Medium Reaction ME Rig I", "me_pct": 2.0, "te_pct": 0.0},
        46496: {"name": "Medium Reaction TE Rig II", "me_pct": 0.0, "te_pct": 8.0},
        46497: {"name": "Medium Reaction ME Rig II", "me_pct": 4.0, "te_pct": 0.0},
    }

    try:
        raw_structs = await esi_get_json(f"/corporations/{corp_id}/structures/", access_token=acc_tok) or []
    except Exception:
        raw_structs = []

    struct_map = {}
    for s in raw_structs:
        sid = int(s.get("structure_id") or 0)
        tid = int(s.get("type_id") or 0)
        sysid = int(s.get("system_id") or 0)
        if not sid:
            continue
        base = STRUCT_BASE.get(tid, {"name": "Unknown Structure", "me_pct": 0.0, "te_pct": 0.0})
        struct_map[sid] = {"structure_id": sid, "type_id": tid, "system_id": sysid,
                           "base_name": base["name"], "base_me_pct": base["me_pct"],
                           "base_te_pct": base["te_pct"], "rigs": []}

    if not struct_map:
        return {"ok": True, "structures": []}

    sys_ids = list({v["system_id"] for v in struct_map.values() if v["system_id"]})
    sys_names = {}
    try:
        nm = await universe_names(sys_ids)
        for sid, n in nm.items():
            sys_names[sid] = (n.get("name") if isinstance(n, dict) else n) or str(sid)
    except Exception:
        pass

    try:
        assets = await esi_get_json(f"/corporations/{corp_id}/assets/", access_token=acc_tok) or []
    except Exception:
        assets = []

    rig_type_ids = set()
    for a in assets:
        if str(a.get("location_flag", "")) == "StructureRig":
            loc_id = int(a.get("location_id") or 0)
            type_id = int(a.get("type_id") or 0)
            if loc_id in struct_map and type_id:
                struct_map[loc_id]["rigs"].append(type_id)
                rig_type_ids.add(type_id)

    rig_names = {}
    if rig_type_ids and sde_available():
        try:
            rig_names = get_type_names(list(rig_type_ids))
        except Exception:
            pass

    out = []
    for sid, s in struct_map.items():
        total_me = s["base_me_pct"]
        total_te = s["base_te_pct"]
        rig_details = []
        for rig_tid in s["rigs"]:
            rb = RIG_BONUSES.get(rig_tid, {"me_pct": 0.0, "te_pct": 0.0})
            total_me += rb["me_pct"]
            total_te += rb["te_pct"]
            rig_details.append({
                "type_id": rig_tid,
                "name": rig_names.get(rig_tid) or RIG_BONUSES.get(rig_tid, {}).get("name") or f"Rig {rig_tid}",
                "me_pct": rb["me_pct"], "te_pct": rb["te_pct"],
            })
        out.append({
            "structure_id": sid, "type_id": s["type_id"],
            "name": s["base_name"],
            "system_name": sys_names.get(s["system_id"], str(s["system_id"])),
            "base_me_pct": s["base_me_pct"], "base_te_pct": s["base_te_pct"],
            "rigs": rig_details, "total_me_pct": total_me, "total_te_pct": total_te,
        })

    return {"ok": True, "structures": out}


# ═══════════════════════════════════════════════════════════════════════════════
# CANVAS MAP ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/app/api/map/state")
async def api_map_state(request: Request, map_id: str = "corp"):
    """Full map state: systems, connections, routes, live pilot positions."""
    memory = MemoryStore(_db_path())
    state = memory.map_full_state(map_id)
    try:
        needs_update = [s for s in state["systems"] if not s.get("region_name") or not s.get("region_id")]
        if needs_update and sde_available():
            for s in needs_update:
                sid = int(s["system_id"])
                info = get_system_info_sde(sid)
                if info:
                    updates = {}
                    if not s.get("region_name") and info.get("region_id"):
                        rn = get_region_name_sde(sid)
                        if rn:
                            updates["region_name"] = rn
                            s["region_name"] = rn
                    if not s.get("region_id") and info.get("region_id"):
                        updates["region_id"] = int(info["region_id"])
                        s["region_id"] = int(info["region_id"])
                    if updates:
                        memory.map_update_system(sid, map_id=map_id, **updates)
    except Exception:
        pass

    pilot_by_sys: dict = {}
    try:
        import sqlite3 as _sq3
        _pc = _sq3.connect(_db_path())
        _pc.row_factory = _sq3.Row
        recent_pilots = _pc.execute(
            """SELECT DISTINCT m.character_id, m.character_name, m.system_id
               FROM nav_movements m
               INNER JOIN (
                 SELECT character_id, MAX(timestamp) as max_ts
                 FROM nav_movements GROUP BY character_id
               ) latest ON m.character_id=latest.character_id AND m.timestamp=latest.max_ts
               WHERE m.timestamp > datetime('now','-2 hours')"""
        ).fetchall()
        _pc.close()
        for row in recent_pilots:
            sid = row["system_id"]
            if sid:
                pilot_by_sys.setdefault(int(sid), []).append(row["character_name"] or "?")
    except Exception:
        pass

    sig_counts: dict = {}
    for sys in state["systems"]:
        sigs = memory.nav_get_sigs(int(sys["system_id"]))
        sig_counts[sys["system_id"]] = len(sigs)

    # ── Attach ESI activity stats (kills / NPC kills / jumps) ──────────────
    # _stats_cache is polled by api_nav_system_stats and cached 60s globally.
    # Build lookup dicts keyed by system_id so we can annotate each node.
    kills_by_sys: dict = {}
    npc_by_sys: dict = {}
    pod_by_sys: dict = {}
    jumps_by_sys: dict = {}
    try:
        if _stats_cache.get("data"):
            for k in _stats_cache["data"].get("kills", []):
                sid_k = k.get("system_id")
                if sid_k:
                    kills_by_sys[int(sid_k)] = k.get("ship_kills", 0)
                    npc_by_sys[int(sid_k)]   = k.get("npc_kills", 0)
                    pod_by_sys[int(sid_k)]   = k.get("pod_kills", 0)
            for j in _stats_cache["data"].get("jumps", []):
                sid_j = j.get("system_id")
                if sid_j:
                    jumps_by_sys[int(sid_j)] = j.get("ship_jumps", 0)
    except Exception:
        pass

    # ── Annotate each system with stats + WH class/effect/statics ─────────
    for sys in state["systems"]:
        sid = int(sys["system_id"])
        sys["ship_kills"]  = kills_by_sys.get(sid, 0)
        sys["npc_kills"]   = npc_by_sys.get(sid, 0)
        sys["pod_kills"]   = pod_by_sys.get(sid, 0)
        sys["ship_jumps"]  = jumps_by_sys.get(sid, 0)
        # Danger level 0-5 based on ship kills
        k = sys["ship_kills"]
        sys["danger_level"] = 0 if k == 0 else 1 if k < 3 else 2 if k < 8 else 3 if k < 15 else 4 if k < 30 else 5
        # WH class, effect, statics from embedded static data
        sname = sys.get("system_name", "")
        if sys.get("is_wh") and sname in _jspace_static:
            jd = _jspace_static[sname]
            sys["jspace_class"]   = jd.get("class")
            sys["jspace_effect"]  = jd.get("effect")
            sys["jspace_statics"] = jd.get("statics", [])
        else:
            sys["jspace_class"]   = None
            sys["jspace_effect"]  = None
            sys["jspace_statics"] = []

    return {
        "ok": True, "map_id": map_id,
        "systems": state["systems"],
        "connections": state["connections"],
        "routes": state.get("routes", []),
        "pilots": pilot_by_sys,
        "sig_counts": sig_counts,
    }


@app.post("/app/api/map/system")
async def api_map_add_system(request: Request):
    body = await request.json()
    memory = MemoryStore(_db_path())
    sid = body.get("system_id")
    sname = body.get("system_name", "")
    x, y = float(body.get("x", 200)), float(body.get("y", 200))
    map_id = body.get("map_id", "corp")
    sec = body.get("sec_status")
    region = body.get("region_name")
    region_id = body.get("region_id")
    is_wh = bool(body.get("is_wh", False))
    # Backfill region_id from SDE if not provided
    if not region_id and sid and sde_available():
        try:
            info = get_system_info_sde(int(sid))
            if info and info.get("region_id"):
                region_id = int(info["region_id"])
        except Exception:
            pass
    result = memory.map_add_system(int(sid), sname, x, y, map_id=map_id, added_by="Capsuleer",
                                   sec_status=sec, region_name=region, region_id=region_id,
                                   is_wh=is_wh)
    return result


@app.put("/app/api/map/system/{system_id}")
async def api_map_update_system(system_id: int, request: Request):
    body = await request.json()
    memory = MemoryStore(_db_path())
    map_id = body.pop("map_id", "corp")
    ok = memory.map_update_system(system_id, map_id=map_id, **body)
    return {"ok": ok}


@app.delete("/app/api/map/system/{system_id}")
async def api_map_delete_system(system_id: int, request: Request, map_id: str = "corp"):
    memory = MemoryStore(_db_path())
    ok = memory.map_delete_system(system_id, map_id=map_id)
    return {"ok": ok}


@app.post("/app/api/map/connection")
async def api_map_add_connection(request: Request):
    body = await request.json()
    memory = MemoryStore(_db_path())
    result = memory.map_add_connection(
        int(body["from_sys_id"]), int(body["to_sys_id"]),
        map_id=body.get("map_id", "corp"),
        wh_type=body.get("wh_type"),
        created_by="Capsuleer"
    )
    return result


@app.put("/app/api/map/connection/{conn_id}")
async def api_map_update_connection(conn_id: int, request: Request):
    body = await request.json()
    memory = MemoryStore(_db_path())
    kwargs = {k: v for k, v in body.items() if k != "map_id"}
    if kwargs.get("time_status") == "eol" and "eol_ts" not in kwargs:
        kwargs["eol_ts"] = time.time() + 15300
    elif kwargs.get("time_status") in ("fresh", "reduced"):
        kwargs["eol_ts"] = None
    ok = memory.map_update_connection(conn_id, **kwargs)
    return {"ok": ok}


@app.delete("/app/api/map/connection/{conn_id}")
async def api_map_delete_connection(conn_id: int, request: Request):
    memory = MemoryStore(_db_path())
    ok = memory.map_delete_connection(conn_id)
    return {"ok": ok}


@app.get("/app/api/map/structures/{system_id}")
async def api_map_get_structures(system_id: int, request: Request, map_id: str = "corp"):
    memory = MemoryStore(_db_path())
    return {"structures": memory.map_get_structures(system_id, map_id)}


@app.post("/app/api/map/structures")
async def api_map_add_structures(request: Request):
    """Parse D-Scan paste and add structures for a system."""
    body = await request.json()
    memory = MemoryStore(_db_path())
    system_id = int(body.get("system_id", 0))
    system_name = body.get("system_name", "")
    raw_text = body.get("raw_text", "").strip()
    map_id = body.get("map_id", "corp")
    UPWELL = {"astrahus", "fortizar", "keepstar", "raitaru", "azbel", "sotiyo",
              "athanor", "tatara", "pharolux", "tenebrex", "ansiblex"}
    added = []
    for line in raw_text.split("\n"):
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        name = parts[2].strip() if len(parts) > 2 else ""
        stype = parts[1].strip() if len(parts) > 1 else ""
        if not any(u in stype.lower() for u in UPWELL):
            continue
        owner = parts[3].strip() if len(parts) > 3 else None
        sid = memory.map_add_structure(system_id, system_name, name, struct_type=stype,
                                       owner=owner, map_id=map_id, added_by="Capsuleer")
        added.append(sid)
    return {"ok": True, "added": len(added)}


@app.delete("/app/api/map/structure/{struct_id}")
async def api_map_delete_structure(struct_id: int, request: Request):
    memory = MemoryStore(_db_path())
    return {"ok": memory.map_delete_structure(struct_id)}


@app.get("/app/api/map/routes")
async def api_map_get_routes(request: Request, map_id: str = "corp"):
    memory = MemoryStore(_db_path())
    return {"routes": memory.map_get_routes(map_id)}


@app.post("/app/api/map/routes")
async def api_map_add_route(request: Request):
    body = await request.json()
    memory = MemoryStore(_db_path())
    rid = memory.map_add_route(
        body.get("name", ""), body.get("system_name", ""),
        system_id=body.get("system_id"),
        map_id=body.get("map_id", "corp"),
        added_by="Capsuleer"
    )
    return {"ok": True, "id": rid}


@app.delete("/app/api/map/routes/{route_id}")
async def api_map_delete_route(route_id: int, request: Request):
    memory = MemoryStore(_db_path())
    return {"ok": memory.map_delete_route(route_id)}


@app.get("/app/api/map/system_info/{system_id}")
async def api_map_system_info(system_id: int, request: Request):
    memory = MemoryStore(_db_path())
    sigs = memory.nav_get_sigs(system_id)
    structs = memory.map_get_structures(system_id)
    return {"system_id": system_id, "sigs": sigs, "structures": structs}


# ═══════════════════════════════════════════════════════════════════════════════
# CONSOLE / HISTORY ENDPOINTS (simplified — no bot agent)
# ═══════════════════════════════════════════════════════════════════════════════

_console_history: List[Dict[str, Any]] = []


@app.get("/app/api/console/history")
def api_console_history(limit: int = 60):
    return {"ok": True, "history": _console_history[-int(limit):]}


@app.post("/app/api/console/run")
async def api_console_run(request: Request):
    """Basic console passthrough — records input and returns a data-only response."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")
    _console_history.append({"role": "user", "content": text, "ts": time.time()})
    response = (
        "The standalone EVE app console accepts freeform notes. "
        "For ESI data use the dedicated endpoints or the navigation/dashboard panels. "
        f"You entered: {text}"
    )
    _console_history.append({"role": "assistant", "content": response, "ts": time.time()})
    return {"ok": True, "response": response}


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TICKER REFRESH
# ═══════════════════════════════════════════════════════════════════════════════

async def _ticker_background_refresh():
    """Refresh corp ops ticker with live ESI data for linked characters."""
    import time as _time
    global _ticker_cache
    try:
        memory = MemoryStore(_db_path())
        chars = memory.eve_list_characters(LOCAL_USER_ID)
        if not chars:
            _ticker_cache["text"] = "Corp ops: no characters linked."
            return

        segments = []
        default = next((c for c in chars if c.get("is_default")), chars[0])
        char_name = default.get("character_name", "?")
        char_id = int(default.get("character_id", 0))

        try:
            token = await eve_access_token_for(LOCAL_USER_ID, char_name)
            access_token = token["access_token"]
            scopes = token.get("scopes", "")
            corp_id = await eve_get_corp_id(char_id)
            corp_info = await esi_get_public_json(f"/corporations/{corp_id}/")
            corp_name = (corp_info or {}).get("name", "Corp") if isinstance(corp_info, dict) else "Corp"

            if "esi-wallet.read_character_wallet.v1" in scopes:
                try:
                    bal = await esi_get_json(f"/characters/{char_id}/wallet/", access_token=access_token)
                    if bal is not None:
                        b = float(bal)
                        if b >= 1e12: s2 = f"{b/1e12:.2f}T ISK"
                        elif b >= 1e9: s2 = f"{b/1e9:.2f}B ISK"
                        elif b >= 1e6: s2 = f"{b/1e6:.1f}M ISK"
                        else: s2 = f"{b:,.0f} ISK"
                        segments.append(f"💰 {char_name}: {s2}")
                except Exception:
                    pass

            if "esi-skills.read_skillqueue.v1" in scopes:
                try:
                    q = await esi_get_json(f"/characters/{char_id}/skillqueue/", access_token=access_token)
                    if q:
                        act = [x for x in q if x.get("finish_date")]
                        if act:
                            try:
                                fd = datetime.datetime.fromisoformat(act[0]["finish_date"].replace("Z", "+00:00"))
                                h = (fd - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600
                                ts2 = f"{int(h*60)}m" if h < 1 else (f"{h:.1f}h" if h < 48 else f"{h/24:.1f}d")
                            except Exception:
                                ts2 = "?"
                            segments.append(f"📚 {len(act)} skills · next: {ts2}")
                    elif isinstance(q, list) and len(q) == 0:
                        segments.append("📚 Queue empty!")
                except Exception:
                    pass

            if segments:
                segments.insert(0, f"🏛️ {corp_name}")
        except Exception as e:
            logger.debug(f"Ticker refresh: {e}")

        if not segments:
            text = f"Corp ops: {char_name} standing by."
        else:
            text = "    ★    ".join(segments)
        if len(text) > 500:
            text = text[:497] + "…"
        _ticker_cache["text"] = text
        _ticker_cache["ts"] = _time.time()
    except Exception as e:
        logger.error(f"Ticker refresh error: {e}")


async def _ticker_loop():
    global _ticker_running
    if _ticker_running:
        return
    _ticker_running = True
    await asyncio.sleep(5)
    while True:
        try:
            await _ticker_background_refresh()
        except Exception:
            pass
        await asyncio.sleep(_TICKER_TTL)


async def _esi_auto_refresh_task():
    """Background: refresh all character ESI cache every 30 minutes."""
    await asyncio.sleep(30)
    while True:
        try:
            await _esi_refresh_all_characters()
        except Exception as e:
            logger.error(f"[ESI_REFRESH] Auto-refresh error: {e}")
        await asyncio.sleep(1800)


async def _location_track_task():
    """Background: poll character locations every 2 minutes for navigator."""
    await asyncio.sleep(15)  # brief startup delay
    while True:
        try:
            with _connect() as con:
                if not _table_exists(con, "eve_characters"):
                    await asyncio.sleep(120)
                    continue
                uid_col = _eve_characters_user_col(con)
                rows = con.execute(
                    f"SELECT DISTINCT character_id, character_name, alias, refresh_token "
                    f"FROM eve_characters WHERE refresh_token IS NOT NULL AND refresh_token != ''"
                ).fetchall()
                chars = [dict(r) for r in rows]
            for ch in chars:
                cid = int(ch["character_id"])
                alias = ch.get("alias") or ch.get("character_name") or str(cid)
                try:
                    token = await eve_access_token_for(LOCAL_USER_ID, alias)
                    at = token["access_token"]
                    char_info = token["character"]
                    loc = await esi_get_json(f"/characters/{cid}/location/", access_token=at)
                    sys_id = (loc or {}).get("solar_system_id")
                    if sys_id:
                        sys_info = None
                        if sde_available():
                            try:
                                sys_info = get_system_info_sde(int(sys_id))
                            except Exception:
                                pass
                        sys_name = (sys_info or {}).get("solarSystemName") or (sys_info or {}).get("name") or str(sys_id)
                        sec_status = float((sys_info or {}).get("security", 0.0))
                        region_name = (sys_info or {}).get("regionName") or ""
                        constellation_name = (sys_info or {}).get("constellationName") or ""
                        if sys_name == str(sys_id):
                            try:
                                names = await _resolve_entity_names([int(sys_id)])
                                sys_name = names.get(int(sys_id), str(sys_id))
                            except Exception:
                                pass
                        memory = MemoryStore(_db_path())
                        memory.nav_log_movement(
                            character_id=cid,
                            system_id=int(sys_id),
                            system_name=sys_name,
                            security_status=sec_status,
                            region_name=region_name,
                            constellation_name=constellation_name,
                            character_name=char_info.get("character_name", alias),
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[LOC_TRACK] Error: {e}")
        await asyncio.sleep(120)  # every 2 minutes


@app.on_event("startup")
async def _start_background_tasks():
    asyncio.create_task(_ticker_loop())
    asyncio.create_task(_esi_auto_refresh_task())
    asyncio.create_task(_location_track_task())
    asyncio.create_task(_sync_client_loop())


# ════════════════════════════════════════════════════════════════════════════
# Corp Sharing — Sync Client
# ════════════════════════════════════════════════════════════════════════════

# ── Persistent sync config (stored in app SQLite) ────────────────────────────

_SYNC_CONFIG_KEYS = ("sync_url", "sync_token", "sync_corp_id",
                     "sync_corp_name", "sync_is_admin", "sync_invite_token",
                     "sync_room_code")

# Default relay URL (developer-hosted instance)
DEFAULT_RELAY_URL = "http://insight.stellarforge.nexus/share"


def _sync_cfg_get(key: str) -> Optional[str]:
    try:
        with _connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS app_config "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = con.execute(
                "SELECT value FROM app_config WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None
    except Exception:
        return None


def _sync_cfg_set(key: str, value: Optional[str]) -> None:
    try:
        with _connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS app_config "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if value is None:
                con.execute("DELETE FROM app_config WHERE key=?", (key,))
            else:
                con.execute(
                    "INSERT INTO app_config(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            con.commit()
    except Exception:
        pass


def _sync_cfg_clear() -> None:
    for k in _SYNC_CONFIG_KEYS:
        _sync_cfg_set(k, None)


# ── In-memory SSE fan-out ─────────────────────────────────────────────────────

_sync_sse_queues: List[asyncio.Queue] = []


def _sync_broadcast_sse(event_type: str, data: Any) -> None:
    """Push a message to all active SSE listeners (browser tabs)."""
    msg = json.dumps({"type": event_type, "data": data})
    dead: List[asyncio.Queue] = []
    for q in _sync_sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sync_sse_queues.remove(q)
        except ValueError:
            pass


# ── Relay event handler ───────────────────────────────────────────────────────

async def _relay_on_event(msg: dict) -> None:
    """
    Handle incoming events from the relay WebSocket.
    Translates relay events to SSE events so navigator.html stays in sync.
    """
    event = msg.get("event", "")

    if event == "snapshot":
        # Full state pushed on WS connect — find map_state and pilot_locations
        items = msg.get("items", [])
        map_state = next((i for i in items if i.get("type") == "map_state"), None)
        if map_state:
            _sync_broadcast_sse("map_state", map_state.get("data", {}))
        # Merge pilot locations
        pilots_by_sys: dict = {}
        for item in items:
            if item.get("type") == "pilot_location":
                d = item.get("data", {})
                sys_id = d.get("system_id")
                if sys_id:
                    pilots_by_sys.setdefault(str(sys_id), []).append(
                        d.get("char_name", "Unknown")
                    )
        if pilots_by_sys:
            _sync_broadcast_sse("pilot_location", {"pilots": pilots_by_sys})

    elif event == "data_updated":
        # Partial update — items include embedded data (relay sends it)
        items = msg.get("items", [])
        for item in items:
            dtype = item.get("type")
            if dtype == "map_state":
                _sync_broadcast_sse("map_state", item.get("data", {}))
            elif dtype == "map_op":
                _sync_broadcast_sse("map_op", item.get("data", {}))
            elif dtype == "pilot_location":
                d = item.get("data", {})
                sys_id = d.get("system_id")
                if sys_id:
                    _sync_broadcast_sse("pilot_location", {
                        "pilots": {str(sys_id): [d.get("char_name", "Unknown")]}
                    })

    elif event == "nav_connections_expired":
        keys = msg.get("keys", [])
        for key in keys:
            _sync_broadcast_sse("map_op", {
                "op": "expire_connection",
                "conn_key": key,
                "ts": msg.get("ts"),
            })

    elif event == "nav_traversed":
        _sync_broadcast_sse("map_op", {
            "op": "traverse_connection",
            "conn_key": msg.get("key"),
            "by_char": msg.get("by_char"),
            "ts": msg.get("ts"),
        })

    elif event == "data_deleted":
        if msg.get("type") == "map_op":
            pass  # deletions are handled by individual map_op events
        _sync_broadcast_sse("map_op", {
            "op": "data_deleted",
            "data_type": msg.get("type"),
            "data_key": msg.get("key"),
            "ts": msg.get("ts"),
        })


# ── Background loop: auto-connect relay on startup ────────────────────────────

async def _sync_client_loop() -> None:
    """
    On startup: if relay credentials are persisted, connect the relay client.
    Monitors connection state and broadcasts status updates to SSE listeners.
    """
    # Small delay to let app finish starting
    await asyncio.sleep(5)

    # Auto-connect if credentials saved
    url       = _sync_cfg_get("sync_url")
    token     = _sync_cfg_get("sync_token")
    room_code = _sync_cfg_get("sync_room_code")
    if url and token and room_code:
        logger.info("[SYNC] Auto-connecting relay room=%s", room_code)
        await _relay.connect(url, room_code, token, on_event=_relay_on_event)

    # Monitor connection state — broadcast status changes via SSE
    last_state: Optional[bool] = None
    while True:
        current = _relay.connected
        if current != last_state:
            last_state = current
            corp_name = _sync_cfg_get("sync_corp_name") or ""
            is_admin  = (_sync_cfg_get("sync_is_admin") or "0") == "1"
            _sync_broadcast_sse("status", {
                "connected": current,
                "corp_name": corp_name,
                "is_admin": is_admin,
                "room_code": _sync_cfg_get("sync_room_code") or "",
            })
        await asyncio.sleep(3)


# ── Helper: call the sync server REST API ────────────────────────────────────

async def _sync_rest(method: str, path: str,
                     token: Optional[str] = None,
                     **kwargs) -> dict:
    """Thin async wrapper around the sync server REST API."""
    url = _sync_cfg_get("sync_url")
    if not url:
        raise HTTPException(status_code=503, detail="Sync server not configured")
    full_url = url.rstrip("/") + path
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await getattr(client, method.lower())(full_url, headers=headers, **kwargs)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        raise HTTPException(status_code=e.response.status_code, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Sync server unreachable: {e}")


# ── Sync API endpoints ────────────────────────────────────────────────────────

@app.get(f"{API_PREFIX}/sync/status")
async def api_sync_status():
    """Return current relay sync connection state."""
    url       = _sync_cfg_get("sync_url")
    token     = _sync_cfg_get("sync_token")
    room_code = _sync_cfg_get("sync_room_code") or ""
    return {
        "configured":  bool(url and token and room_code),
        "connected":   _relay.connected,
        "url":         url or "",
        "room_code":   room_code,
        "corp_name":   _sync_cfg_get("sync_corp_name") or "",
        "corp_id":     int(_sync_cfg_get("sync_corp_id") or 0),
        "is_admin":    (_sync_cfg_get("sync_is_admin") or "0") == "1",
        "invite_token": _sync_cfg_get("sync_invite_token") or "",
        "default_relay_url": DEFAULT_RELAY_URL,
    }


@app.post(f"{API_PREFIX}/sync/setup")
async def api_sync_setup(request: Request):
    """
    Host setup: register the current character's corp on the relay.
    Creates a permanent room code for the corp and returns it.

    Body: { relay_url? }  — defaults to DEFAULT_RELAY_URL
    """
    body = await request.json()
    relay_url = (body.get("relay_url") or DEFAULT_RELAY_URL).rstrip("/")

    chars = eve_list_characters(LOCAL_USER_ID)
    if not chars:
        raise HTTPException(status_code=400, detail="No EVE character linked")
    char = next((c for c in chars if c.get("is_default")), chars[0])
    cid  = int(char["character_id"])
    char_name = char.get("character_name", "")

    # Look up corp_id via ESI public data
    try:
        pub = await esi_get_public_json(f"/characters/{cid}/")
        corp_id   = (pub or {}).get("corporation_id", 0)
        corp_name = ""
        if corp_id:
            corp_info = await esi_get_public_json(f"/corporations/{corp_id}/")
            corp_name = (corp_info or {}).get("name", "")
    except Exception:
        corp_id   = 0
        corp_name = ""

    if not corp_id:
        raise HTTPException(status_code=400, detail="Could not look up your corporation from ESI")

    # Register with relay
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                relay_url + "/register",
                json={
                    "corp_id":      corp_id,
                    "corp_name":    corp_name,
                    "character_id": cid,
                    "char_name":    char_name,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not reach relay: {exc}")

    room_code = data.get("room_code", "")
    token     = data.get("token", "")

    # Persist relay credentials
    _sync_cfg_set("sync_url",       relay_url)
    _sync_cfg_set("sync_token",     token)
    _sync_cfg_set("sync_room_code", room_code)
    _sync_cfg_set("sync_corp_id",   str(corp_id))
    _sync_cfg_set("sync_corp_name", corp_name)
    _sync_cfg_set("sync_is_admin",  "1")
    _sync_cfg_set("sync_invite_token", "")

    # (Re-)connect relay WS
    await _relay.connect(relay_url, room_code, token, on_event=_relay_on_event)

    return {
        "ok":        True,
        "room_code": room_code,
        "corp_name": corp_name,
        "corp_id":   corp_id,
        "character": char_name,
    }


@app.post(f"{API_PREFIX}/sync/join")
async def api_sync_join(request: Request):
    """
    Member join: enter a room code (SNEK-XXXX) to join the corp relay.
    No invite token needed — the room code IS the join credential.

    Body: { room_code, relay_url? }  — relay_url defaults to DEFAULT_RELAY_URL
    """
    body = await request.json()
    room_code = (body.get("room_code") or "").strip().upper()
    relay_url = (body.get("relay_url") or DEFAULT_RELAY_URL).rstrip("/")

    if not room_code:
        raise HTTPException(status_code=400, detail="room_code is required (format: SNEK-XXXX)")

    chars = eve_list_characters(LOCAL_USER_ID)
    if not chars:
        raise HTTPException(status_code=400, detail="No EVE character linked")
    char = next((c for c in chars if c.get("is_default")), chars[0])
    cid  = int(char["character_id"])
    char_name = char.get("character_name", "")

    # Join via relay
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{relay_url}/join/{room_code}",
                json={"character_id": cid, "char_name": char_name},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not join relay: {exc}")

    token     = data.get("token", "")
    corp_id   = data.get("corp_id", 0)
    corp_name = data.get("corp_name", "")

    # Persist credentials
    _sync_cfg_set("sync_url",       relay_url)
    _sync_cfg_set("sync_token",     token)
    _sync_cfg_set("sync_room_code", room_code)
    _sync_cfg_set("sync_corp_id",   str(corp_id))
    _sync_cfg_set("sync_corp_name", corp_name)
    _sync_cfg_set("sync_is_admin",  "0")
    _sync_cfg_set("sync_invite_token", "")

    # Connect relay WS
    await _relay.connect(relay_url, room_code, token, on_event=_relay_on_event)

    return {"ok": True, "corp_name": corp_name, "corp_id": corp_id, "room_code": room_code}


@app.delete(f"{API_PREFIX}/sync/disconnect")
async def api_sync_disconnect():
    """Clear relay credentials and disconnect."""
    await _relay.disconnect()
    _sync_cfg_clear()
    _sync_broadcast_sse("disconnected", {})
    return {"ok": True}


@app.get(f"{API_PREFIX}/sync/invite")
async def api_sync_invite():
    """Admin only: return current invite token + member list."""
    token = _sync_cfg_get("sync_token")
    if not token or _sync_cfg_get("sync_is_admin") != "1":
        raise HTTPException(status_code=403, detail="Admin only")
    data = await _sync_rest("GET", "/admin/invite", token=token)
    # Cache the invite token locally
    if data.get("invite_token"):
        _sync_cfg_set("sync_invite_token", data["invite_token"])
    return data


@app.post(f"{API_PREFIX}/sync/invite/regenerate")
async def api_sync_invite_regenerate():
    """Admin only: regenerate the invite token."""
    token = _sync_cfg_get("sync_token")
    if not token or _sync_cfg_get("sync_is_admin") != "1":
        raise HTTPException(status_code=403, detail="Admin only")
    data = await _sync_rest("POST", "/admin/invite/regenerate", token=token)
    if data.get("invite_token"):
        _sync_cfg_set("sync_invite_token", data["invite_token"])
    return data


@app.post(f"{API_PREFIX}/sync/map_op")
async def api_sync_map_op(request: Request):
    """
    Forward a local map operation to the relay and also broadcast to local SSE.
    Called by navigator.js after every corp map mutation.
    """
    if not _relay.connected:
        return {"ok": False, "reason": "not_connected"}
    body = await request.json()
    op   = body.get("op", "")
    # Push the op to relay as a map_op item
    key = f"{op}:{int(time.time() * 1000)}"   # unique key per op
    asyncio.create_task(_relay.push([{
        "type": "map_op",
        "key":  key,
        "data": body,
    }]))
    # Also broadcast locally so other tabs on this machine update immediately
    _sync_broadcast_sse("map_op", body)
    return {"ok": True}


@app.post(f"{API_PREFIX}/sync/push_map_state")
async def api_sync_push_map_state(request: Request):
    """
    Push the full current corp map state to the relay.
    Called after significant map changes so joining members get the latest state.
    """
    if not _relay.connected:
        return {"ok": False, "reason": "not_connected"}
    mem   = MemoryStore(_db_path())
    state = mem.map_full_state("corp")
    asyncio.create_task(_relay.push([{
        "type": "map_state",
        "key":  "corp",
        "data": state,
    }]))
    return {"ok": True}


@app.get(f"{API_PREFIX}/sync/events")
async def api_sync_events(request: Request):
    """SSE stream — browser subscribes here to receive real-time sync updates."""
    from starlette.responses import StreamingResponse

    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sync_sse_queues.append(q)

    async def event_stream():
        try:
            # Send current connection state immediately on subscribe
            status_msg = json.dumps({
                "type": "status",
                "data": {
                    "connected": _sync_ws_connected,
                    "corp_name": _sync_cfg_get("sync_corp_name") or "",
                    "is_admin": (_sync_cfg_get("sync_is_admin") or "0") == "1",
                }
            })
            yield f"data: {status_msg}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive comment
                    yield ": ping\n\n"
        finally:
            try:
                _sync_sse_queues.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Helper: get all refreshed character tokens ───────────────────────────────

def _get_all_tokens() -> List[dict]:
    """Return a list of dicts with access_token + character info for each linked char."""
    result = []
    try:
        with _connect() as con:
            if not _table_exists(con, "eve_characters"):
                return result
            rows = con.execute("SELECT * FROM eve_characters").fetchall()
        for row in rows:
            try:
                enc_rt = row["refresh_token"]
                rt = decrypt_refresh_token(enc_rt)
                token_data = refresh_access_token(rt)
                if not token_data:
                    continue
                char_info = verify_access_token(token_data["access_token"])
                result.append({
                    "alias": row["alias"] or row["character_name"],
                    "access_token": token_data["access_token"],
                    "character": char_info or {},
                })
            except Exception:
                pass
    except Exception:
        pass
    return result