#!/usr/bin/env python3
"""
Stellar Insight Relay Server
Passive WebSocket relay for corp data sharing.

Default port : 777
Default DB   : ~/.stellar_insight/relay.db

Usage:
    python relay_server.py [--port 777] [--host 0.0.0.0] [--db /path/to/relay.db]
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Set

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [relay] %(levelname)s  %(message)s",
)
log = logging.getLogger("relay")

# ── Constants ─────────────────────────────────────────────────────────────────
NAV_CONN_TTL      = 48 * 3600   # 48 hours: nav-map connection kill timer
TOKEN_TTL         = 30 * 86400  # 30 days: signed token lifetime
CLEANUP_INTERVAL  = 900         # 15 minutes between background cleanup passes
_RC_ALPHABET      = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I ambiguity

# ── CLI args ──────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stellar Insight Relay Server")
    p.add_argument("--port", type=int, default=777)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument(
        "--db",
        default=os.path.join(os.path.expanduser("~"), ".stellar_insight", "relay.db"),
    )
    # ignore unknown args when imported inside PyInstaller bundle
    args, _ = p.parse_known_args()
    return args


_args = _parse_args()
DB_PATH: str = _args.db

# ── Database helpers ──────────────────────────────────────────────────────────
def _raw_conn(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _conn():
    c = _raw_conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms (
                room_code  TEXT    PRIMARY KEY,
                corp_id    INTEGER NOT NULL UNIQUE,
                corp_name  TEXT    NOT NULL DEFAULT '',
                created_ts REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash   TEXT    PRIMARY KEY,
                room_code    TEXT    NOT NULL REFERENCES rooms(room_code),
                corp_id      INTEGER NOT NULL,
                character_id INTEGER NOT NULL,
                char_name    TEXT    NOT NULL DEFAULT '',
                issued_ts    REAL    NOT NULL,
                expires_ts   REAL    NOT NULL,
                revoked      INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_data (
                room_code  TEXT NOT NULL,
                data_type  TEXT NOT NULL,
                data_key   TEXT NOT NULL,
                data_json  TEXT NOT NULL,
                updated_ts REAL NOT NULL,
                updated_by INTEGER,
                PRIMARY KEY (room_code, data_type, data_key)
            );
            CREATE INDEX IF NOT EXISTS idx_sync_room ON sync_data(room_code);
            CREATE INDEX IF NOT EXISTS idx_tokens_room ON tokens(room_code);
        """)
        # Ensure a persistent server secret exists
        row = c.execute("SELECT value FROM config WHERE key='server_secret'").fetchone()
        if not row:
            c.execute(
                "INSERT INTO config(key,value) VALUES('server_secret',?)",
                (secrets.token_hex(32),),
            )
    log.info("DB initialised: %s", DB_PATH)


def _server_secret() -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key='server_secret'").fetchone()
    return row["value"]


# ── Token helpers ─────────────────────────────────────────────────────────────
def _make_token(
    corp_id: int,
    character_id: int,
    room_code: str,
    char_name: str = "",
) -> str:
    """Create a signed 30-day token, store its hash, return the raw string."""
    secret  = _server_secret()
    issued  = time.time()
    expires = issued + TOKEN_TTL
    payload = json.dumps(
        {
            "corp_id":      corp_id,
            "character_id": character_id,
            "room_code":    room_code,
            "char_name":    char_name,
            "iat":          issued,
            "exp":          expires,
            "jti":          secrets.token_hex(8),
        },
        separators=(",", ":"),
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig         = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    token       = f"{payload_b64}.{sig}"

    tok_hash = hashlib.sha256(token.encode()).hexdigest()
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO tokens
               (token_hash, room_code, corp_id, character_id, char_name,
                issued_ts, expires_ts, revoked)
               VALUES (?,?,?,?,?,?,?,0)""",
            (tok_hash, room_code, corp_id, character_id, char_name, issued, expires),
        )
    return token


def _verify_token(token: str) -> Optional[dict]:
    """
    Verify token: signature + expiry + not-revoked.
    Returns payload dict on success, None on any failure.
    """
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    secret   = _server_secret()
    expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "==").decode()
        )
    except Exception:
        return None
    if time.time() > payload.get("exp", 0):
        return None
    tok_hash = hashlib.sha256(token.encode()).hexdigest()
    with _conn() as c:
        row = c.execute(
            "SELECT revoked FROM tokens WHERE token_hash=?", (tok_hash,)
        ).fetchone()
    if not row or row["revoked"]:
        return None
    return payload


def _token_from_request(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token") or None


def _require_token(request: Request) -> dict:
    token   = _token_from_request(request)
    if not token:
        raise HTTPException(401, "Missing auth token")
    payload = _verify_token(token)
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    return payload


# ── Room helpers ──────────────────────────────────────────────────────────────
def _gen_room_code() -> str:
    """Generate a unique SNEK-XXXX room code."""
    while True:
        code = "SNEK-" + "".join(secrets.choice(_RC_ALPHABET) for _ in range(4))
        with _conn() as c:
            if not c.execute("SELECT 1 FROM rooms WHERE room_code=?", (code,)).fetchone():
                return code


def _get_or_create_room(corp_id: int, corp_name: str = "") -> str:
    """Return existing room code for corp, or create one."""
    with _conn() as c:
        row = c.execute(
            "SELECT room_code FROM rooms WHERE corp_id=?", (corp_id,)
        ).fetchone()
        if row:
            return row["room_code"]
        code = _gen_room_code()
        c.execute(
            "INSERT INTO rooms(room_code, corp_id, corp_name, created_ts) VALUES (?,?,?,?)",
            (code, corp_id, corp_name, time.time()),
        )
    return code


# ── WebSocket connection manager ──────────────────────────────────────────────
class _RelayManager:
    def __init__(self) -> None:
        self._rooms: Dict[str, Set[WebSocket]] = {}

    async def connect(self, room: str, ws: WebSocket) -> None:
        await ws.accept()
        self._rooms.setdefault(room, set()).add(ws)
        log.info("WS+ room=%s  online=%d", room, len(self._rooms[room]))

    def disconnect(self, room: str, ws: WebSocket) -> None:
        self._rooms.get(room, set()).discard(ws)
        log.info("WS- room=%s  online=%d", room, len(self._rooms.get(room, set())))

    async def broadcast(self, room: str, message: dict) -> None:
        dead: List[WebSocket] = []
        for ws in list(self._rooms.get(room, set())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(room, ws)

    def online_count(self, room: str) -> int:
        return len(self._rooms.get(room, set()))


_mgr = _RelayManager()


# ── Background cleanup task ───────────────────────────────────────────────────
async def _cleanup_loop() -> None:
    """Mark nav connections older than NAV_CONN_TTL as expired; broadcast."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now    = time.time()
        cutoff = now - NAV_CONN_TTL
        affected: Dict[str, List[str]] = {}
        with _conn() as c:
            rows = c.execute(
                """SELECT room_code, data_key, data_json
                   FROM sync_data
                   WHERE data_type='nav_connection' AND updated_ts < ?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                try:
                    d = json.loads(row["data_json"])
                except Exception:
                    d = {}
                if d.get("expired"):
                    continue  # already marked
                d["expired"] = True
                c.execute(
                    """UPDATE sync_data SET data_json=?, updated_ts=?
                       WHERE room_code=? AND data_type='nav_connection' AND data_key=?""",
                    (json.dumps(d), now, row["room_code"], row["data_key"]),
                )
                affected.setdefault(row["room_code"], []).append(row["data_key"])

        if affected:
            total = sum(len(v) for v in affected.values())
            log.info("Expired %d nav connections across %d rooms", total, len(affected))
            for room_code, keys in affected.items():
                await _mgr.broadcast(room_code, {
                    "event": "nav_connections_expired",
                    "keys":  keys,
                    "ts":    now,
                })


# ── FastAPI application ───────────────────────────────────────────────────────
app = FastAPI(title="Stellar Insight Relay", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    _init_db()
    asyncio.create_task(_cleanup_loop())


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


# ── Register (first host — creates permanent room for corp) ───────────────────
@app.post("/register")
async def register(request: Request):
    """
    Called once by the premium user hosting for their corp.
    Creates (or returns) the permanent room code for that corp_id.

    Body: { corp_id, corp_name?, character_id, char_name? }
    Returns: { room_code, token }
    """
    body = await request.json()
    corp_id      = int(body.get("corp_id") or 0)
    corp_name    = str(body.get("corp_name") or "")
    character_id = int(body.get("character_id") or 0)
    char_name    = str(body.get("char_name") or "")
    if not corp_id or not character_id:
        raise HTTPException(400, "corp_id and character_id are required")
    room_code = _get_or_create_room(corp_id, corp_name)
    token     = _make_token(corp_id, character_id, room_code, char_name)
    log.info("REGISTER  corp=%d  room=%s  char=%s", corp_id, room_code, char_name)
    return {"ok": True, "room_code": room_code, "token": token}


# ── Join (corp member — gets token using room code) ───────────────────────────
@app.post("/join/{room_code}")
async def join_room(room_code: str, request: Request):
    """
    Corp member joins using the shared room code.

    Body: { character_id, char_name? }
    Returns: { token, room_code, corp_id, corp_name }
    """
    body         = await request.json()
    character_id = int(body.get("character_id") or 0)
    char_name    = str(body.get("char_name") or "")
    if not character_id:
        raise HTTPException(400, "character_id is required")
    rc = room_code.upper()
    with _conn() as c:
        row = c.execute(
            "SELECT corp_id, corp_name FROM rooms WHERE room_code=?", (rc,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Room not found — check the room code")
    token = _make_token(int(row["corp_id"]), character_id, rc, char_name)
    log.info("JOIN  room=%s  char=%s  corp=%d", rc, char_name, row["corp_id"])
    return {
        "ok":        True,
        "token":     token,
        "room_code": rc,
        "corp_id":   row["corp_id"],
        "corp_name": row["corp_name"],
    }


# ── Push data ─────────────────────────────────────────────────────────────────
@app.post("/sync/{room_code}")
async def push_data(
    room_code: str,
    request:   Request,
    payload:   dict = Depends(_require_token),
):
    """
    Push one or more data objects into the relay store.
    Broadcasts an update event to all WebSocket clients in the room.

    Body: { items: [ { type, key, data } ] }
    Supported types: fit, skill_plan, route, nav_connection, signature, system_note
    """
    rc = room_code.upper()
    if payload["room_code"] != rc:
        raise HTTPException(403, "Token does not match this room")
    body  = await request.json()
    items = body.get("items") or []
    if not items:
        raise HTTPException(400, "items list is required and must not be empty")
    now     = time.time()
    char_id = payload["character_id"]
    stored: List[dict] = []
    with _conn() as c:
        for item in items:
            dtype = str(item.get("type") or "").strip()
            dkey  = str(item.get("key")  or "").strip()
            data  = item.get("data") or {}
            if not dtype or not dkey:
                continue
            c.execute(
                """INSERT OR REPLACE INTO sync_data
                   (room_code, data_type, data_key, data_json, updated_ts, updated_by)
                   VALUES (?,?,?,?,?,?)""",
                (rc, dtype, dkey, json.dumps(data), now, char_id),
            )
            # Include data in broadcast so clients don't need a separate pull
            stored.append({"type": dtype, "key": dkey, "data": data})
    if stored:
        await _mgr.broadcast(rc, {
            "event":   "data_updated",
            "items":   stored,
            "by_char": char_id,
            "ts":      now,
        })
    return {"ok": True, "stored": len(stored)}


# ── Pull data ─────────────────────────────────────────────────────────────────
@app.get("/sync/{room_code}")
def pull_data(
    room_code: str,
    request:   Request,
    payload:   dict = Depends(_require_token),
    data_type: Optional[str] = None,
):
    """
    Pull all (or type-filtered) data for a room.
    Query param: ?data_type=nav_connection

    Returns: { items: [ { type, key, data, updated_ts, updated_by } ] }
    """
    rc = room_code.upper()
    if payload["room_code"] != rc:
        raise HTTPException(403, "Token does not match this room")
    with _conn() as c:
        if data_type:
            rows = c.execute(
                """SELECT data_type, data_key, data_json, updated_ts, updated_by
                   FROM sync_data WHERE room_code=? AND data_type=?""",
                (rc, data_type),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT data_type, data_key, data_json, updated_ts, updated_by
                   FROM sync_data WHERE room_code=?""",
                (rc,),
            ).fetchall()
    items = []
    for row in rows:
        try:
            data = json.loads(row["data_json"])
        except Exception:
            data = {}
        items.append({
            "type":       row["data_type"],
            "key":        row["data_key"],
            "data":       data,
            "updated_ts": row["updated_ts"],
            "updated_by": row["updated_by"],
        })
    return {"ok": True, "room_code": rc, "items": items, "count": len(items)}


# ── Traverse nav connection (resets 48-hour kill timer) ───────────────────────
@app.post("/sync/{room_code}/traverse")
async def traverse_connection(
    room_code: str,
    request:   Request,
    payload:   dict = Depends(_require_token),
):
    """
    Called when a corp member traverses a wormhole/gate connection.
    Resets the 48-hour kill timer for that connection and broadcasts the event.

    Body: { key: "<from_id>:<to_id>" }
    """
    rc = room_code.upper()
    if payload["room_code"] != rc:
        raise HTTPException(403, "Token does not match this room")
    body = await request.json()
    dkey = str(body.get("key") or "").strip()
    if not dkey:
        raise HTTPException(400, "key is required")
    now = time.time()
    with _conn() as c:
        row = c.execute(
            """SELECT data_json FROM sync_data
               WHERE room_code=? AND data_type='nav_connection' AND data_key=?""",
            (rc, dkey),
        ).fetchone()
        if row:
            try:
                d = json.loads(row["data_json"])
            except Exception:
                d = {}
            d.pop("expired", None)          # un-expire if it was previously expired
            d["last_traversal_ts"] = now
            c.execute(
                """UPDATE sync_data SET data_json=?, updated_ts=?
                   WHERE room_code=? AND data_type='nav_connection' AND data_key=?""",
                (json.dumps(d), now, rc, dkey),
            )
    await _mgr.broadcast(rc, {
        "event":   "nav_traversed",
        "key":     dkey,
        "by_char": payload["character_id"],
        "ts":      now,
    })
    return {"ok": True}


# ── Delete a data item ────────────────────────────────────────────────────────
@app.delete("/sync/{room_code}/{data_type}/{data_key:path}")
async def delete_data(
    room_code: str,
    data_type: str,
    data_key:  str,
    request:   Request,
    payload:   dict = Depends(_require_token),
):
    rc = room_code.upper()
    if payload["room_code"] != rc:
        raise HTTPException(403, "Token does not match this room")
    with _conn() as c:
        c.execute(
            "DELETE FROM sync_data WHERE room_code=? AND data_type=? AND data_key=?",
            (rc, data_type, data_key),
        )
    await _mgr.broadcast(rc, {
        "event":   "data_deleted",
        "type":    data_type,
        "key":     data_key,
        "by_char": payload["character_id"],
        "ts":      time.time(),
    })
    return {"ok": True}


# ── Room info ─────────────────────────────────────────────────────────────────
@app.get("/room/{room_code}")
def room_info(
    room_code: str,
    request:   Request,
    payload:   dict = Depends(_require_token),
):
    rc = room_code.upper()
    if payload["room_code"] != rc:
        raise HTTPException(403, "Token does not match this room")
    with _conn() as c:
        room = c.execute(
            "SELECT * FROM rooms WHERE room_code=?", (rc,)
        ).fetchone()
        members = c.execute(
            """SELECT character_id, char_name, issued_ts
               FROM tokens WHERE room_code=? AND revoked=0 AND expires_ts>?""",
            (rc, time.time()),
        ).fetchall()
    if not room:
        raise HTTPException(404, "Room not found")
    return {
        "ok":        True,
        "room_code": room["room_code"],
        "corp_id":   room["corp_id"],
        "corp_name": room["corp_name"],
        "online":    _mgr.online_count(rc),
        "members": [
            {"character_id": m["character_id"], "char_name": m["char_name"]}
            for m in members
        ],
    }


# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """
    WebSocket: ws://relay/ws?room=SNEK-XXXX&token=<token>

    On connect:
      • Sends a full data snapshot for the room.
      • Then sends real-time events as other clients push changes.

    Keep-alive: client sends {"type":"ping"}, server replies {"type":"pong"}.
    Server sends {"type":"ping"} every 30 s if client is silent; disconnect on error.
    """
    rc    = ws.query_params.get("room", "").upper()
    token = ws.query_params.get("token", "")
    pay   = _verify_token(token)
    if not pay or pay["room_code"] != rc:
        await ws.close(code=4001)
        return

    await _mgr.connect(rc, ws)
    try:
        # Send current snapshot immediately on connect
        with _conn() as c:
            rows = c.execute(
                "SELECT data_type, data_key, data_json, updated_ts FROM sync_data WHERE room_code=?",
                (rc,),
            ).fetchall()
        snapshot = []
        for row in rows:
            try:
                d = json.loads(row["data_json"])
            except Exception:
                d = {}
            snapshot.append({
                "type":       row["data_type"],
                "key":        row["data_key"],
                "data":       d,
                "updated_ts": row["updated_ts"],
            })
        await ws.send_json({"event": "snapshot", "items": snapshot, "ts": time.time()})

        # Read loop: handle pings, ignore other client messages
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=30.0)
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send_json({"type": "pong", "ts": time.time()})
            except asyncio.TimeoutError:
                # Server-side keepalive ping
                await ws.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("WS error room=%s: %s", rc, exc)
    finally:
        _mgr.disconnect(rc, ws)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(
        "Starting Stellar Insight Relay  host=%s  port=%d  db=%s",
        _args.host, _args.port, DB_PATH,
    )
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="info")
