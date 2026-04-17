"""
chain_mapper.py — Wormhole chain state, route finder, and intel log watcher.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("chain_mapper")

# ── DB path helper ─────────────────────────────────────────────────────────────
def _db_path() -> str:
    base = os.environ.get("XYLON_EVE_DATA", "")
    if base:
        return os.path.join(base, "xylon_eve.db")
    here = os.path.dirname(os.path.abspath(__file__))
    if "_MEI" not in here:
        return os.path.join(os.path.dirname(here), "data", "xylon_eve.db")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        base = os.path.join(appdata, "StellarInsight")
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        base = os.path.join(xdg, "StellarInsight")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "xylon_eve.db")


def _conn() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 3000;")
    return con


# ── Schema ─────────────────────────────────────────────────────────────────────
def ensure_schema() -> None:
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS chain_systems (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT    NOT NULL,
            class    TEXT    DEFAULT '',
            security REAL    DEFAULT 0,
            x        REAL    DEFAULT 0,
            y        REAL    DEFAULT 0,
            is_home  INTEGER DEFAULT 0,
            notes    TEXT    DEFAULT '',
            added_at REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chain_connections (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id    INTEGER NOT NULL REFERENCES chain_systems(id) ON DELETE CASCADE,
            to_id      INTEGER NOT NULL REFERENCES chain_systems(id) ON DELETE CASCADE,
            wh_type    TEXT    DEFAULT '',
            mass       TEXT    DEFAULT 'stable',
            eol        TEXT    DEFAULT 'fresh',
            is_frig    INTEGER DEFAULT 0,
            notes      TEXT    DEFAULT '',
            added_at   REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS intel_config (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            log_dir       TEXT    DEFAULT '',
            channels      TEXT    DEFAULT '[]',
            watch_systems TEXT    DEFAULT '[]',
            updated_at    REAL    DEFAULT 0
        );
    """)
    con.commit()
    con.close()


# ── Chain systems ──────────────────────────────────────────────────────────────
def chain_state() -> Dict:
    """Return full chain: systems list + connections list."""
    ensure_schema()
    con = _conn()
    systems = [dict(r) for r in con.execute(
        "SELECT * FROM chain_systems ORDER BY added_at"
    ).fetchall()]
    connections = [dict(r) for r in con.execute(
        "SELECT * FROM chain_connections ORDER BY added_at"
    ).fetchall()]
    con.close()
    return {"systems": systems, "connections": connections}


def add_system(name: str, sys_class: str = "", security: float = 0.0,
               x: float = 0.0, y: float = 0.0, is_home: bool = False,
               notes: str = "") -> Dict:
    ensure_schema()
    con = _conn()
    # Prevent duplicate names
    existing = con.execute(
        "SELECT id FROM chain_systems WHERE LOWER(name)=LOWER(?)", (name,)
    ).fetchone()
    if existing:
        con.close()
        return {"error": f"System '{name}' already in chain", "id": existing["id"]}
    cur = con.execute(
        "INSERT INTO chain_systems (name, class, security, x, y, is_home, notes, added_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, sys_class, security, x, y, int(is_home), notes, time.time())
    )
    new_id = cur.lastrowid
    con.commit()
    con.close()
    return {"ok": True, "id": new_id}


def move_system(sys_id: int, x: float, y: float) -> Dict:
    ensure_schema()
    con = _conn()
    con.execute("UPDATE chain_systems SET x=?, y=? WHERE id=?", (x, y, sys_id))
    con.commit()
    con.close()
    return {"ok": True}


def update_system(sys_id: int, **kwargs) -> Dict:
    allowed = {"name", "class", "security", "is_home", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return {"error": "No valid fields to update"}
    ensure_schema()
    con = _conn()
    sets = ", ".join(f"{k}=?" for k in updates)
    con.execute(f"UPDATE chain_systems SET {sets} WHERE id=?",
                (*updates.values(), sys_id))
    con.commit()
    con.close()
    return {"ok": True}


def remove_system(sys_id: int) -> Dict:
    ensure_schema()
    con = _conn()
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("DELETE FROM chain_systems WHERE id=?", (sys_id,))
    con.commit()
    con.close()
    return {"ok": True}


def set_home(sys_id: int) -> Dict:
    ensure_schema()
    con = _conn()
    con.execute("UPDATE chain_systems SET is_home=0")
    con.execute("UPDATE chain_systems SET is_home=1 WHERE id=?", (sys_id,))
    con.commit()
    con.close()
    return {"ok": True}


# ── Chain connections ──────────────────────────────────────────────────────────
def add_connection(from_id: int, to_id: int, wh_type: str = "",
                   mass: str = "stable", eol: str = "fresh",
                   is_frig: bool = False, notes: str = "") -> Dict:
    ensure_schema()
    con = _conn()
    # Prevent duplicate connections
    existing = con.execute(
        "SELECT id FROM chain_connections WHERE "
        "(from_id=? AND to_id=?) OR (from_id=? AND to_id=?)",
        (from_id, to_id, to_id, from_id)
    ).fetchone()
    if existing:
        con.close()
        return {"error": "Connection already exists", "id": existing["id"]}
    cur = con.execute(
        "INSERT INTO chain_connections (from_id, to_id, wh_type, mass, eol, is_frig, notes, added_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (from_id, to_id, wh_type, mass, eol, int(is_frig), notes, time.time())
    )
    new_id = cur.lastrowid
    con.commit()
    con.close()
    return {"ok": True, "id": new_id}


def update_connection(conn_id: int, **kwargs) -> Dict:
    allowed = {"wh_type", "mass", "eol", "is_frig", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return {"error": "No valid fields to update"}
    ensure_schema()
    con = _conn()
    sets = ", ".join(f"{k}=?" for k in updates)
    con.execute(f"UPDATE chain_connections SET {sets} WHERE id=?",
                (*updates.values(), conn_id))
    con.commit()
    con.close()
    return {"ok": True}


def remove_connection(conn_id: int) -> Dict:
    ensure_schema()
    con = _conn()
    con.execute("DELETE FROM chain_connections WHERE id=?", (conn_id,))
    con.commit()
    con.close()
    return {"ok": True}


def clear_chain() -> Dict:
    ensure_schema()
    con = _conn()
    con.execute("DELETE FROM chain_connections")
    con.execute("DELETE FROM chain_systems")
    con.commit()
    con.close()
    return {"ok": True}


# ── Route finder ───────────────────────────────────────────────────────────────
def _classify_system(name: str, security: float) -> str:
    """Return HS / LS / NS / WH based on name and security."""
    if name.startswith("J") and name[1:].isdigit():
        return "WH"
    if name.lower() == "thera":
        return "Thera"
    if security >= 0.45:
        return "HS"
    if security > 0.0:
        return "LS"
    return "NS"


def find_route(
    origin: str,
    destination: str,
    use_chain: bool = True,
    use_thera: bool = True,
    avoid_wh: bool = False,
    thera_connections: Optional[List[Dict]] = None,
) -> Dict:
    """
    Find shortest route from origin to destination.
    Layers: k-space gate graph (SDE) + mapped chain + optional Thera.
    Returns a list of hop dicts with system name, type (gate/wormhole/thera), etc.
    """
    from .sde_local import _build_jump_graph, sde_available

    if not sde_available():
        return {"error": "SDE not available — download it in Settings first"}

    # Build base k-space graph from SDE  {name -> [name, ...]}
    # We need name<->id mapping
    try:
        from .sde_local import _get_sde
        sde = _get_sde()
        rows = sde.execute(
            "SELECT solarSystemID, solarSystemName, security FROM mapSolarSystems"
        ).fetchall()
    except Exception as e:
        return {"error": f"SDE read error: {e}"}

    id_to_name: Dict[int, str] = {}
    name_to_id: Dict[str, int] = {}
    id_to_sec:  Dict[int, float] = {}
    for r in rows:
        id_to_name[r["solarSystemID"]] = r["solarSystemName"]
        name_to_id[r["solarSystemName"].lower()] = r["solarSystemID"]
        id_to_sec[r["solarSystemID"]] = r["security"]

    origin_lo = origin.strip().lower()
    dest_lo   = destination.strip().lower()

    origin_id = name_to_id.get(origin_lo)
    dest_id   = name_to_id.get(dest_lo)

    if not origin_id:
        return {"error": f"Origin system '{origin}' not found in SDE"}
    if not dest_id:
        return {"error": f"Destination system '{destination}' not found in SDE"}

    # Build adjacency {id -> [(id, hop_type), ...]}
    graph: Dict[int, List[Tuple[int, str]]] = {}

    def _add_edge(a: int, b: int, hop_type: str):
        graph.setdefault(a, []).append((b, hop_type))
        graph.setdefault(b, []).append((a, hop_type))

    # K-space gates
    try:
        gate_rows = sde.execute(
            "SELECT fromSolarSystemID, toSolarSystemID FROM mapSolarSystemJumps"
        ).fetchall()
        for r in gate_rows:
            _add_edge(r["fromSolarSystemID"], r["toSolarSystemID"], "gate")
    except Exception as e:
        return {"error": f"Gate graph error: {e}"}

    if use_chain:
        # Read from the Navigator's map tables (map_systems / map_connections)
        # which store real EVE solarSystemIDs — no pseudo-ID mapping required.
        try:
            from .memory_sqlite import MemoryStore
            _store = MemoryStore(_db_path())
            _seen_chain: set = set()
            for _mid in ("corp", "personal"):
                # Ensure WH systems that may not appear in SDE's k-space listing
                # are present in our lookup dicts.
                for _s in _store.map_get_systems(_mid):
                    _sid = _s["system_id"]
                    if _sid and _sid not in id_to_name:
                        id_to_name[_sid] = _s["system_name"]
                        id_to_sec[_sid]  = _s.get("sec_status") or 0.0

                for _c in _store.map_get_connections(_mid):
                    _ms = (_c.get("mass_status") or "").lower()
                    _ts = (_c.get("time_status") or "").lower()
                    if _ms == "crit" or _ts == "eol":
                        continue  # skip dangerous / end-of-life connections
                    _fid = _c.get("from_sys_id")
                    _tid = _c.get("to_sys_id")
                    if not _fid or not _tid:
                        continue
                    _key = (min(_fid, _tid), max(_fid, _tid))
                    if _key in _seen_chain:
                        continue
                    _seen_chain.add(_key)
                    _add_edge(_fid, _tid, "wormhole")
        except Exception as _e:
            logger.warning("chain use_chain error: %s", _e)

    # Thera connections
    if use_thera and thera_connections:
        for t in thera_connections:
            in_sys = t.get("inSystem", {}).get("name", "")
            out_sys = t.get("outSystem", {}).get("name", "")
            in_id  = name_to_id.get(in_sys.lower())
            out_id = name_to_id.get(out_sys.lower())
            # Thera itself (pseudo or real)
            thera_id = name_to_id.get("thera")
            if in_id and thera_id:
                _add_edge(in_id, thera_id, "thera")
            if out_id and thera_id:
                _add_edge(out_id, thera_id, "thera")

    # BFS with path tracking
    visited: Dict[int, Tuple[int, str]] = {}  # id -> (came_from_id, hop_type)
    visited[origin_id] = (-1, "start")
    queue = deque([origin_id])

    while queue:
        current = queue.popleft()
        if current == dest_id:
            break
        for (neighbor, hop_type) in graph.get(current, []):
            if neighbor not in visited:
                visited[neighbor] = (current, hop_type)
                queue.append(neighbor)

    if dest_id not in visited:
        return {"error": f"No route found from '{origin}' to '{destination}'", "hops": []}

    # Reconstruct path
    path: List[Dict] = []
    cur = dest_id
    while cur != origin_id:
        came_from, hop_type = visited[cur]
        path.append({
            "system": id_to_name.get(cur, str(cur)),
            "system_id": cur,
            "via": hop_type,
            "security": round(id_to_sec.get(cur, 0.0), 2),
        })
        cur = came_from

    path.append({
        "system": id_to_name.get(origin_id, origin),
        "system_id": origin_id,
        "via": "start",
        "security": round(id_to_sec.get(origin_id, 0.0), 2),
    })
    path.reverse()

    return {
        "jumps": len(path) - 1,
        "hops": path,
        "used_chain": use_chain,
        "used_thera": use_thera,
    }


# ── Intel log watcher ──────────────────────────────────────────────────────────
def default_log_dir() -> str:
    """Guess the default EVE chat log directory for this OS."""
    if os.name == "nt":
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        return os.path.join(docs, "EVE", "logs", "Chatlogs")
    else:
        return os.path.join(os.path.expanduser("~"), "EVE", "logs", "Chatlogs")


def get_intel_config() -> Dict:
    ensure_schema()
    con = _conn()
    row = con.execute("SELECT * FROM intel_config WHERE id=1").fetchone()
    con.close()
    if not row:
        return {
            "log_dir": default_log_dir(),
            "channels": [],
            "watch_systems": [],
        }
    return {
        "log_dir": row["log_dir"] or default_log_dir(),
        "channels": json.loads(row["channels"] or "[]"),
        "watch_systems": json.loads(row["watch_systems"] or "[]"),
    }


def save_intel_config(log_dir: str, channels: List[str], watch_systems: List[str]) -> Dict:
    ensure_schema()
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO intel_config (id, log_dir, channels, watch_systems, updated_at) "
        "VALUES (1, ?, ?, ?, ?)",
        (log_dir, json.dumps(channels), json.dumps(watch_systems), time.time())
    )
    con.commit()
    con.close()
    return {"ok": True}


def list_chat_logs(log_dir: str) -> List[Dict]:
    """List available EVE chat log files in the given directory."""
    p = Path(log_dir)
    if not p.exists():
        return []
    files = []
    for f in sorted(p.glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)[:100]:
        try:
            files.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })
        except Exception:
            pass
    return files


# System name tokeniser — grab uppercase words ≥3 chars that could be system names
_SYSTEM_RE = re.compile(r'\b([A-Z0-9][A-Z0-9\-]{2,})\b')

def read_intel_feed(log_dir: str, channels: List[str], since: float = 0,
                   watch_systems: Optional[List[str]] = None) -> List[Dict]:
    """
    Read EVE chat log files for the given channel names (partial filename match).
    Returns messages since `since` (unix timestamp), parsed for system mentions.
    """
    p = Path(log_dir)
    if not p.exists():
        return []

    watch_upper = {s.upper() for s in (watch_systems or [])}
    messages: List[Dict] = []

    # Find the most-recent log file per channel
    channel_files: Dict[str, Path] = {}
    for f in p.glob("*.txt"):
        for ch in channels:
            if ch.lower() in f.name.lower():
                existing = channel_files.get(ch)
                if existing is None or f.stat().st_mtime > existing.stat().st_mtime:
                    channel_files[ch] = f

    # Parse line format: [ YYYY.MM.DD HH:MM:SS ] PilotName > message
    LINE_RE = re.compile(
        r'\[\s*(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]\s*([^>]+)>\s*(.*)'
    )

    import datetime
    for ch, fpath in channel_files.items():
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                m = LINE_RE.match(line.strip())
                if not m:
                    continue
                ts_str, pilot, msg = m.group(1), m.group(2).strip(), m.group(3).strip()
                try:
                    dt = datetime.datetime.strptime(ts_str, "%Y.%m.%d %H:%M:%S")
                    ts = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
                except Exception:
                    ts = 0
                if ts <= since:
                    continue

                # Extract system mentions
                sys_mentions = _SYSTEM_RE.findall(msg.upper())
                is_alert = bool(watch_upper & set(sys_mentions))

                messages.append({
                    "ts": ts,
                    "ts_str": ts_str,
                    "channel": ch,
                    "pilot": pilot,
                    "msg": msg,
                    "systems": list(set(sys_mentions)),
                    "alert": is_alert,
                })
        except Exception as e:
            logger.debug("Intel read error %s: %s", fpath, e)

    messages.sort(key=lambda x: x["ts"])
    return messages[-200:]  # cap at 200 most recent
