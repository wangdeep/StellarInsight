"""
esi_cache.py — ESI character data caching layer (r92)

Caches character data (blueprints, assets, skills) in SQLite.
Serves cached data instantly on page load.
Background task auto-refreshes every 30 minutes.
Manual refresh via API endpoint.
"""

import sqlite3
import json
import time
import logging
import os
from typing import Dict, Optional, Any

logger = logging.getLogger("xylon.esi_cache")

_DB_PATH = os.environ.get("XYLON_DB_PATH", "/app/data/memory.db")


def _connect():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ensure_table():
    """Create the esi_cache table, upgrading from legacy schema if needed."""
    with _connect() as con:
        # Check if table exists with old schema (missing character_id column)
        cur = con.execute("PRAGMA table_info(esi_cache)")
        cols = {row["name"] for row in cur.fetchall()}
        if cols and "character_id" not in cols:
            logger.info("[ESI_CACHE] Legacy table detected (missing character_id) — dropping and recreating")
            con.execute("DROP TABLE esi_cache")

        con.execute("""CREATE TABLE IF NOT EXISTS esi_cache (
            cache_key TEXT PRIMARY KEY,
            character_id INTEGER,
            alias TEXT,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_esi_cache_char ON esi_cache(character_id)")
        con.commit()


_ensure_table()

# Cache TTL: 30 minutes
CACHE_TTL = 30 * 60


def get_cached(cache_key: str) -> Optional[Dict]:
    """Get cached ESI data. Returns {data, fetched_at, age_seconds} or None if expired/missing."""
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT data, fetched_at FROM esi_cache WHERE cache_key=?",
                (cache_key,)
            ).fetchone()
            if row:
                age = time.time() - row["fetched_at"]
                return {
                    "data": json.loads(row["data"]),
                    "fetched_at": row["fetched_at"],
                    "age_seconds": round(age),
                    "stale": age > CACHE_TTL,
                }
    except Exception as e:
        logger.warning(f"[ESI_CACHE] get error: {e}")
    return None


def set_cached(cache_key: str, data: Any, character_id: int = 0, alias: str = ""):
    """Store ESI data in cache."""
    try:
        with _connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO esi_cache (cache_key, character_id, alias, data, fetched_at) "
                "VALUES (?,?,?,?,?)",
                (cache_key, character_id, alias, json.dumps(data), time.time())
            )
            con.commit()
        logger.info(f"[ESI_CACHE] cached {cache_key}")
    except Exception as e:
        logger.warning(f"[ESI_CACHE] set error: {e}")


def clear_cache(character_id: int = None) -> int:
    """Clear cache entries. If character_id given, only clear that character's data."""
    try:
        with _connect() as con:
            if character_id:
                con.execute("DELETE FROM esi_cache WHERE character_id=?", (character_id,))
            else:
                con.execute("DELETE FROM esi_cache")
            con.commit()
            count = con.execute("SELECT changes()").fetchone()[0]
            logger.info(f"[ESI_CACHE] cleared {count} entries")
            return count
    except Exception as e:
        logger.warning(f"[ESI_CACHE] clear error: {e}")
        return 0


def cache_status() -> Dict:
    """Get cache status for diagnostics."""
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT cache_key, character_id, alias, fetched_at FROM esi_cache ORDER BY fetched_at DESC"
            ).fetchall()
            entries = []
            for r in rows:
                age = time.time() - r["fetched_at"]
                entries.append({
                    "key": r["cache_key"],
                    "character_id": r["character_id"],
                    "alias": r["alias"],
                    "age_seconds": round(age),
                    "age_human": f"{int(age//60)}m {int(age%60)}s ago",
                    "stale": age > CACHE_TTL,
                })
            return {"entries": entries, "count": len(entries), "ttl_seconds": CACHE_TTL}
    except Exception as e:
        return {"error": str(e)}
