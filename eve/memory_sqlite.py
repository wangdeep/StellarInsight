import os
import sqlite3
import time
import re
from contextlib import contextmanager
from typing import Optional, Tuple, List, Dict, Any
import json

# Source of truth (prod): XYLON_MEMORY_DB.
# Default fallback should match docker-compose volume layout.
DEFAULT_DB_PATH = os.getenv("XYLON_MEMORY_DB", "/app/data/memory.db")


class MemoryStore:
    """SQLite-backed, per-channel short memory + per-channel summaries + reminders."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, keep_last: int = 24):
        self.db_path = db_path
        self.keep_last = int(keep_last)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        # WAL mode: readers and writers coexist without blocking each other.
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout = 10000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memory (
                    channel_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_channel_ts ON memory(channel_id, ts);")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS summaries (
                    channel_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_updated ON summaries(updated_ts);")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    due_ts REAL NOT NULL,
                    message TEXT NOT NULL,
                    target TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    sent_ts REAL,
                    repeat TEXT,
                    last_fire_ts REAL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_ts);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_channel ON reminders(channel_id);")

            # Bot-wide and user-wide memory (not channel-scoped)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bot_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    profile TEXT NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_profile (
                    user_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_profile_updated ON user_profile(updated_ts);")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_prefs (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(user_id, key)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_prefs_user ON user_prefs(user_id);")

            # Optional per-channel notes (channel-scoped, not user identity)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS channel_notes (
                    channel_id INTEGER NOT NULL,
                    note TEXT NOT NULL,
                    created_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_notes_channel_ts ON channel_notes(channel_id, created_ts);")

            # ----------------------------
            # EVE SSO + per-user EVE state
            # ----------------------------
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_pending_auth (
                    state TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_pending_user ON eve_pending_auth(user_id);")

            # Progressive EVE linking: store the next requested scope set per Discord user.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_link_intent (
                    user_id INTEGER PRIMARY KEY,
                    scopes TEXT NOT NULL,
                    updated_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_eve_link_intent_updated ON eve_link_intent(updated_ts);
                """
            )


            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_auth (
                    user_id INTEGER PRIMARY KEY,
                    character_id INTEGER NOT NULL,
                    character_name TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_auth_char ON eve_auth(character_id);")

            # Multi-character EVE links (canonical). Older DBs may only have eve_auth.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_characters (
                    user_id INTEGER NOT NULL,
                    character_id INTEGER NOT NULL,
                    character_name TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    alias TEXT,
                    is_default INTEGER NOT NULL,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(user_id, character_id)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_characters_user ON eve_characters(user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_characters_default ON eve_characters(user_id, is_default);")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_watch (
                    user_id INTEGER PRIMARY KEY,
                    notify_via_dm INTEGER NOT NULL,
                    notify_channel_id INTEGER,
                    watch_skills INTEGER NOT NULL,
                    watch_industry INTEGER NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_state_cache (
                    user_id INTEGER PRIMARY KEY,
                    skillqueue_last_end_ts REAL,
                    industry_active_jobs_json TEXT,
                    wallet_balance REAL,
                    orders_hash TEXT,
                    pi_state_json TEXT,
                    updated_ts REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eve_api_cache (
                    cache_key TEXT PRIMARY KEY,
                    value_json TEXT,
                    updated_ts REAL
                );
                """
            )




            # ----------------------------
            # ESI names cache (restart-safe, low-ESI usage)
            # kind: type|station|structure|system|planet|generic
            # ----------------------------
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS esi_names_cache (
                    kind TEXT NOT NULL,
                    id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    expires_ts REAL,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(kind, id)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_esi_names_cache_expires ON esi_names_cache(expires_ts);")

            # ----------------------------
            # Notifications feed (WebUI + Discord)
            # ----------------------------
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_user_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    delivered_mode TEXT NOT NULL,
                    read_ts REAL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_ts ON notifications(discord_user_id, created_ts DESC);")

            # ----------------------------
            # Custom EVE alerts (per-user)
            # ----------------------------
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    alert_type TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    notify_via_dm INTEGER NOT NULL,
                    notify_channel_id INTEGER,
                    last_fire_ts REAL,
                    created_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_alerts_user ON eve_alerts(user_id);")
            # Lightweight schema migrations for existing DBs
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Best-effort schema upgrades for older DB files."""
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders);").fetchall()}
            if "repeat" not in cols:
                conn.execute("ALTER TABLE reminders ADD COLUMN repeat TEXT")
            if "last_fire_ts" not in cols:
                conn.execute("ALTER TABLE reminders ADD COLUMN last_fire_ts REAL")
        except Exception:
            # If migration fails, don't kill the bot; reminders will still work in non-recurring mode.
            pass

        # channel_notes is safe to create even on older DBs.
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS channel_notes (
                    channel_id INTEGER NOT NULL,
                    note TEXT NOT NULL,
                    created_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_notes_channel_ts ON channel_notes(channel_id, created_ts);")
        except Exception:
            pass

        # EVE tables (safe to create on older DBs)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_pending_auth (
                    state TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_pending_user ON eve_pending_auth(user_id);")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_auth (
                    user_id INTEGER PRIMARY KEY,
                    character_id INTEGER NOT NULL,
                    character_name TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_auth_char ON eve_auth(character_id);")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_watch (
                    user_id INTEGER PRIMARY KEY,
                    notify_via_dm INTEGER NOT NULL,
                    notify_channel_id INTEGER,
                    watch_skills INTEGER NOT NULL,
                    watch_industry INTEGER NOT NULL,
                    updated_ts REAL NOT NULL
                );
                """
            )
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_state_cache (
                    user_id INTEGER PRIMARY KEY,
                    skillqueue_last_end_ts REAL,
                    industry_active_jobs_json TEXT,
                    wallet_balance REAL,
                    orders_hash TEXT,
                    pi_state_json TEXT,
                    updated_ts REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eve_api_cache (
                    cache_key TEXT PRIMARY KEY,
                    value_json TEXT,
                    updated_ts REAL
                );
                """
            )
        except Exception:
            pass

        # Canonical multi-character store + one-time migration from legacy eve_auth.
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_characters (
                    user_id INTEGER NOT NULL,
                    character_id INTEGER NOT NULL,
                    character_name TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    alias TEXT,
                    is_default INTEGER NOT NULL,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(user_id, character_id)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_characters_user ON eve_characters(user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_characters_default ON eve_characters(user_id, is_default);")

            # If eve_characters is empty but eve_auth has rows, migrate them.
            has_chars = conn.execute("SELECT 1 FROM eve_characters LIMIT 1").fetchone()
            has_legacy = conn.execute("SELECT 1 FROM eve_auth LIMIT 1").fetchone()
            if (not has_chars) and has_legacy:
                conn.executescript("""
                    INSERT OR IGNORE INTO eve_characters(
                        user_id, character_id, character_name, refresh_token, scopes, alias, is_default, updated_ts
                    )
                    SELECT user_id, character_id, character_name, refresh_token, scopes, NULL, 1, updated_ts
                    FROM eve_auth
                    """
                )
        except Exception:
            pass


        # Add new columns to eve_state_cache if missing
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(eve_state_cache);").fetchall()}
            if "wallet_balance" not in cols:
                conn.execute("ALTER TABLE eve_state_cache ADD COLUMN wallet_balance REAL")
            if "orders_hash" not in cols:
                conn.execute("ALTER TABLE eve_state_cache ADD COLUMN orders_hash TEXT")
            if "pi_state_json" not in cols:
                conn.execute("ALTER TABLE eve_state_cache ADD COLUMN pi_state_json TEXT")
        except Exception:
            pass

        # Notifications + EVE alerts (safe to create on older DBs)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_user_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    delivered_mode TEXT NOT NULL,
                    read_ts REAL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_ts ON notifications(discord_user_id, created_ts DESC);")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS eve_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    alert_type TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    notify_via_dm INTEGER NOT NULL,
                    notify_channel_id INTEGER,
                    last_fire_ts REAL,
                    created_ts REAL NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eve_alerts_user ON eve_alerts(user_id);")
        except Exception:
            pass

        # Clan-wide notes (shared memory across Discord + Web)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS clan_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_user_id INTEGER,
                    note TEXT NOT NULL,
                    created_ts REAL NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clan_notes_ts ON clan_notes(created_ts DESC);")
        except Exception:
            pass

        # Wormhole connections tracking
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wh_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_system TEXT NOT NULL,
                    to_system TEXT NOT NULL,
                    wh_type TEXT,
                    wh_class TEXT,
                    status TEXT DEFAULT 'active',
                    mass_status TEXT DEFAULT 'stable',
                    time_status TEXT DEFAULT 'fresh',
                    reported_by INTEGER,
                    reported_by_name TEXT,
                    created_ts REAL NOT NULL,
                    expires_ts REAL,
                    last_used_ts REAL,
                    notes TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wh_conn_status ON wh_connections(status, created_ts DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wh_conn_from ON wh_connections(lower(from_system));")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wh_conn_to ON wh_connections(lower(to_system));")
            # Migration: add last_used_ts if table already exists without it
            try:
                conn.execute("ALTER TABLE wh_connections ADD COLUMN last_used_ts REAL")
            except Exception:
                pass  # Column already exists
        except Exception:
            pass

        # r100: Saved fittings
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nav_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id INTEGER NOT NULL,
                    character_name TEXT,
                    system_id INTEGER NOT NULL,
                    system_name TEXT,
                    security_status REAL,
                    constellation_id INTEGER,
                    constellation_name TEXT,
                    region_id INTEGER,
                    region_name TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nav_move_char ON nav_movements(character_id, timestamp DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nav_move_sys ON nav_movements(system_id);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS nav_intel (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    system_id INTEGER NOT NULL,
                    character_id INTEGER,
                    character_name TEXT,
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nav_intel_sys ON nav_intel(system_id, timestamp DESC);")
            try:
                conn.execute("ALTER TABLE nav_intel ADD COLUMN source TEXT DEFAULT 'manual'")
            except Exception:
                pass
        except Exception:
            pass

        # r100: Saved fittings
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nav_region_cache (
                    region_id INTEGER PRIMARY KEY,
                    region_name TEXT,
                    data TEXT NOT NULL,
                    cached_ts REAL NOT NULL
                );
            """)
        except Exception:
            pass

        # r100: Saved fittings
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nav_signatures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    system_id INTEGER NOT NULL,
                    system_name TEXT,
                    sig_id TEXT NOT NULL,
                    sig_group TEXT,
                    sig_info TEXT,
                    description TEXT,
                    scanned_by INTEGER,
                    scanned_by_name TEXT,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nav_sig_sys ON nav_signatures(system_id);")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nav_sig_unique ON nav_signatures(system_id, sig_id);")

            # WH enrichment columns on nav_signatures
            for _col, _defn in [
                ("first_seen_ts",     "REAL"),
                ("last_confirmed_ts", "REAL"),
                ("wh_type_code",      "TEXT"),
                ("wh_dest_class",     "TEXT"),
                ("wh_max_jump_kg",    "REAL"),
                ("wh_total_kg",       "REAL"),
                ("wh_lifetime_h",     "INTEGER"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE nav_signatures ADD COLUMN {_col} {_defn}")
                except Exception:
                    pass
            try:
                conn.execute("""
                    UPDATE nav_signatures
                    SET first_seen_ts = created_ts,
                        last_confirmed_ts = updated_ts
                    WHERE first_seen_ts IS NULL
                """)
            except Exception:
                pass

            # r140: Manufacturing calculator presets
            conn.execute("""
                CREATE TABLE IF NOT EXISTS manufacturing_presets (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    preset_name TEXT    NOT NULL,
                    bp_type_id  INTEGER NOT NULL,
                    bp_name     TEXT    NOT NULL DEFAULT '',
                    runs        INTEGER NOT NULL DEFAULT 1,
                    me          INTEGER NOT NULL DEFAULT 0,
                    te          INTEGER NOT NULL DEFAULT 0,
                    activity    INTEGER NOT NULL DEFAULT 1,
                    facility_name TEXT  NOT NULL DEFAULT '',
                    facility_tax  REAL  NOT NULL DEFAULT 0.0,
                    scc_surcharge REAL  NOT NULL DEFAULT 4.0,
                    system_name TEXT    NOT NULL DEFAULT '',
                    cost_index  REAL    NOT NULL DEFAULT 0.0,
                    mat_hub     TEXT    NOT NULL DEFAULT 'Jita',
                    out_hub     TEXT    NOT NULL DEFAULT 'Jita',
                    extra_json  TEXT    NOT NULL DEFAULT '{}',
                    saved_ts    DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS manufacturing_global_defaults (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )""")

            # r100: Saved fittings
            conn.execute("""
                CREATE TABLE IF NOT EXISTS saved_fittings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    ship_type_id INTEGER,
                    ship_name TEXT,
                    fit_data TEXT NOT NULL,
                    eft_string TEXT,
                    saved_by_name TEXT,
                    saved_ts DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
        except Exception:
            pass

        # WH mass tracking per connection
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nav_wh_mass (
                    conn_id       TEXT PRIMARY KEY,
                    total_mass_kg REAL NOT NULL DEFAULT 2000000000,
                    used_mass_kg  REAL NOT NULL DEFAULT 0,
                    manual_state  TEXT,
                    last_jump_ts  REAL,
                    last_ship_name TEXT,
                    last_ship_mass_kg REAL,
                    updated_ts    REAL NOT NULL DEFAULT 0
                )
            """)
        except Exception:
            pass

        # r124: K-space neighbour cache (stargate adjacency from ESI)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nav_system_neighbours (
                    system_id INTEGER PRIMARY KEY,
                    system_name TEXT,
                    neighbour_ids TEXT NOT NULL,
                    cached_ts REAL NOT NULL
                );
            """)
        except Exception:
            pass

        # r124: Intel kill log — tracks last kill time per system for CLEAR logic
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intel_kill_log (
                    system_id INTEGER PRIMARY KEY,
                    system_name TEXT,
                    last_kill_ts REAL NOT NULL,
                    cleared_ts REAL
                );
            """)
        except Exception:
            pass

        # r126: Structure fuel alert log — tracks last Discord alert per structure
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fuel_alert_log (
                    structure_id INTEGER PRIMARY KEY,
                    structure_name TEXT,
                    last_alert_ts REAL NOT NULL,
                    days_remaining REAL
                );
            """)

            # ── Premium access tables ──────────────────────────────────────────
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS app_kv (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS premium_access (
                    character_id  INTEGER PRIMARY KEY,
                    granted_ts    REAL    NOT NULL,
                    granted_by    TEXT    NOT NULL,
                    amount_isk    REAL    NOT NULL DEFAULT 0,
                    note          TEXT
                );
                CREATE TABLE IF NOT EXISTS dev_whitelist (
                    character_id  INTEGER PRIMARY KEY,
                    note          TEXT,
                    added_ts      REAL    NOT NULL DEFAULT (unixepoch())
                );
                CREATE TABLE IF NOT EXISTS access_keys (
                    key           TEXT    PRIMARY KEY,
                    used          INTEGER NOT NULL DEFAULT 0,
                    used_by_char  INTEGER,
                    used_ts       REAL
                );
                CREATE TABLE IF NOT EXISTS reinstall_keys (
                    character_id  INTEGER PRIMARY KEY,
                    reinstall_key TEXT    NOT NULL,
                    generated_ts  REAL    NOT NULL,
                    granted_by    TEXT    NOT NULL DEFAULT 'isk_payment'
                );
            """)
        except Exception:
            pass

    # ── Premium access helpers ────────────────────────────────────────────────

    def kv_get(self, key: str) -> Optional[str]:
        """Get a value from the app key-value store."""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM app_kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        """Set a value in the app key-value store."""
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO app_kv(key,value) VALUES(?,?)", (key, value))

    def get_or_create_instance_id(self) -> str:
        """Return the permanent installation UUID, creating it on first call."""
        import uuid as _uuid
        existing = self.kv_get("instance_id")
        if existing:
            return existing
        new_id = str(_uuid.uuid4())
        self.kv_set("instance_id", new_id)
        return new_id

    def premium_is_granted(self, character_id: int) -> bool:
        """True if the character has any form of premium access."""
        cid = int(character_id)
        with self._conn() as conn:
            if conn.execute("SELECT 1 FROM dev_whitelist WHERE character_id=?", (cid,)).fetchone():
                return True
            if conn.execute("SELECT 1 FROM premium_access WHERE character_id=?", (cid,)).fetchone():
                return True
        return False

    def premium_grant(self, character_id: int, granted_by: str,
                      amount_isk: float = 0, note: str = None) -> None:
        """Record a premium grant for a character."""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO premium_access
                   (character_id, granted_ts, granted_by, amount_isk, note)
                   VALUES (?,?,?,?,?)""",
                (int(character_id), time.time(), granted_by, float(amount_isk), note)
            )

    def dev_whitelist_add(self, character_id: int, note: str = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO dev_whitelist(character_id,note) VALUES(?,?)",
                (int(character_id), note)
            )

    def dev_whitelist_remove(self, character_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM dev_whitelist WHERE character_id=?", (int(character_id),))

    def dev_whitelist_list(self) -> List[Dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT character_id, note, added_ts FROM dev_whitelist ORDER BY added_ts"
            ).fetchall()]

    def key_create(self, key: str) -> None:
        """Insert a new unused access key."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO access_keys(key,used) VALUES(?,0)", (key,)
            )

    def key_redeem(self, key: str, character_id: int) -> bool:
        """
        Redeem a key for a character. Returns True if successful, False if
        key doesn't exist, is already used, or is invalid.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT used FROM access_keys WHERE key=?", (key,)
            ).fetchone()
            if not row or row["used"]:
                return False
            conn.execute(
                "UPDATE access_keys SET used=1, used_by_char=?, used_ts=? WHERE key=?",
                (int(character_id), time.time(), key)
            )
        self.premium_grant(character_id, granted_by="key", note=f"key:{key}")
        return True

    def key_check_dev_code(self, code: str, character_id: int,
                           current_code: str = "",
                           code_hash: bytes = b"") -> bool:
        """
        Validate a developer secret code using bcrypt (C-1).
        Pass code_hash (bytes) for secure comparison; current_code is kept
        for backward compatibility but is ignored when code_hash is provided.
        """
        import bcrypt as _bcrypt
        if not code:
            return False
        try:
            if code_hash:
                # Preferred path: bcrypt hash comparison (constant-time)
                ok = _bcrypt.checkpw(code.strip().encode(), code_hash)
            else:
                # Legacy fallback (deprecated — do not use in production)
                ok = code.strip().lower() == current_code.strip().lower()
        except Exception:
            return False
        if not ok:
            return False
        self.dev_whitelist_add(character_id, note="dev_code")
        return True

    # ── Reinstall keys (character-bound, generated after premium grant) ──────

    def generate_reinstall_key(self, character_id: int, granted_by: str = "isk_payment") -> str:
        """
        Generate (or return existing) permanent reinstall key for a character.
        Format: SNEK-XXXX-XXXX-[char_id_short]
        The key is bound to this character_id and can only be redeemed by them.
        """
        import secrets as _sec
        import string as _str
        cid = int(character_id)
        # Return existing key if already generated
        existing = self.get_reinstall_key(cid)
        if existing:
            return existing
        # Generate a new unique key
        alphabet = _str.ascii_uppercase + _str.digits
        rand_a = "".join(_sec.choice(alphabet) for _ in range(4))
        rand_b = "".join(_sec.choice(alphabet) for _ in range(4))
        char_short = str(cid)[-4:].upper()
        key = f"SNEK-{rand_a}-{rand_b}-{char_short}"
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO reinstall_keys
                   (character_id, reinstall_key, generated_ts, granted_by)
                   VALUES (?,?,?,?)""",
                (cid, key, time.time(), granted_by)
            )
        return key

    def get_reinstall_key(self, character_id: int) -> Optional[str]:
        """Return the reinstall key for this character, or None if not generated yet."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT reinstall_key FROM reinstall_keys WHERE character_id=?",
                (int(character_id),)
            ).fetchone()
        return row["reinstall_key"] if row else None

    def redeem_reinstall_key(self, key: str, character_id: int) -> str:
        """
        Redeem a reinstall key. Returns:
          'ok'              — key valid, character matches, premium granted
          'wrong_character' — key exists but bound to a different character
          'not_found'       — key doesn't exist in the DB
        """
        cid = int(character_id)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT character_id FROM reinstall_keys WHERE reinstall_key=?",
                (key.strip().upper(),)
            ).fetchone()
        if not row:
            return "not_found"
        if int(row["character_id"]) != cid:
            return "wrong_character"
        # Grant access
        self.premium_grant(cid, granted_by="reinstall_key", note=f"reinstall:{key}")
        return "ok"

    # ----------------------------
    # Channel notes (scoped to a channel)
    # ----------------------------
    def add_channel_note(self, channel_id: int, note: str) -> None:
        note = (note or "").strip()
        if not note:
            return
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO channel_notes(channel_id, note, created_ts) VALUES(?,?,?)",
                (int(channel_id), str(note), float(now)),
            )

    def clear_channel_notes(self, channel_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM channel_notes WHERE channel_id = ?", (int(channel_id),))

    def list_channel_notes(self, channel_id: int, limit: int = 50) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT note FROM channel_notes WHERE channel_id = ? ORDER BY created_ts DESC LIMIT ?",
                (int(channel_id), int(limit)),
            ).fetchall()
        return [str(r[0]) for r in rows]

    # ----------------------------
    # Clan notes (global shared memory)
    # ----------------------------
    def add_clan_note(self, note: str, *, author_user_id: Optional[int] = None) -> None:
        """Add a shared clan note (inside joke / shared fact).
        Dedupe: if the same note already exists (case-insensitive), do not insert again.
        """
        note = (note or "").strip()
        if not note:
            return
        now = time.time()
        with self._conn() as conn:
            # Dedupe identical notes (case-insensitive)
            exists = conn.execute(
                "SELECT 1 FROM clan_notes WHERE lower(note)=lower(?) LIMIT 1",
                (str(note),),
            ).fetchone()
            if exists:
                return
            conn.execute(
                "INSERT INTO clan_notes(author_user_id, note, created_ts) VALUES(?,?,?)",
                (int(author_user_id) if author_user_id is not None else None, str(note), float(now)),
            )
    def search_clan_notes(self, query: str, limit: int = 5) -> List[str]:
        """Search clan notes (inside jokes / shared facts). Returns matching notes, newest first."""
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q.lower()}%"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT note FROM clan_notes WHERE lower(note) LIKE ? ORDER BY created_ts DESC LIMIT ?",
                (like, int(limit)),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def list_clan_notes(self, limit: int = 50) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT note FROM clan_notes ORDER BY created_ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [str(r[0]) for r in rows]


    def delete_clan_notes_matching(self, query: str) -> int:
        """Delete clan notes matching a user-provided phrase.
        Matching is token-based AND match (case-insensitive): all tokens must appear.
        Returns number of rows deleted.
        """
        q = (query or "").strip()
        if not q:
            return 0
        # Tokenize: words/numbers/@handles. Keep simple to avoid surprises.
        tokens = [t for t in re.split(r"\s+", q.lower()) if t]
        if not tokens:
            return 0
        where = " AND ".join(["lower(note) LIKE ?"] * len(tokens))
        params = [f"%{t}%" for t in tokens]
        with self._conn() as conn:
            # SQLite doesn't provide rowcount reliably on executescript; use changes().
            conn.execute(f"DELETE FROM clan_notes WHERE {where}", params)
            row = conn.execute("SELECT changes()").fetchone()
        return int(row[0] if row and row[0] is not None else 0)
    def clear_clan_notes(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM clan_notes")

    # ----------------------------
    # Wormhole connection tracking
    # ----------------------------
    WH_IDLE_EXPIRE_HOURS = 16  # Links expire after 16h without transit

    def wh_add_connection(self, from_system: str, to_system: str, *,
                          wh_type: str = None, wh_class: str = None,
                          mass_status: str = "stable", time_status: str = "fresh",
                          reported_by: int = None, reported_by_name: str = None,
                          expires_hours: float = 24.0, notes: str = None) -> int:
        """Add a wormhole connection. Returns the connection ID."""
        now = time.time()
        expires = now + (expires_hours * 3600)
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO wh_connections
                   (from_system, to_system, wh_type, wh_class, status, mass_status, time_status,
                    reported_by, reported_by_name, created_ts, expires_ts, last_used_ts, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(from_system).strip(), str(to_system).strip(),
                 wh_type, wh_class, "active", mass_status, time_status,
                 int(reported_by) if reported_by else None, reported_by_name,
                 float(now), float(expires), float(now), notes)
            )
            return cur.lastrowid

    def wh_touch_connection(self, from_system: str, to_system: str) -> bool:
        """Refresh the last_used_ts for a WH connection (called on transit). Returns True if found."""
        now = time.time()
        new_expires = now + (self.WH_IDLE_EXPIRE_HOURS * 3600)
        a, b = from_system.strip().lower(), to_system.strip().lower()
        with self._conn() as conn:
            conn.execute(
                """UPDATE wh_connections SET last_used_ts=?, expires_ts=?
                   WHERE status='active' AND (
                     (lower(from_system)=? AND lower(to_system)=?) OR
                     (lower(from_system)=? AND lower(to_system)=?)
                   )""",
                (now, new_expires, a, b, b, a)
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def wh_list_active(self, system: str = None, limit: int = 50) -> List[Dict]:
        """List active (non-expired) wormhole connections. Auto-expires idle connections."""
        now = time.time()
        idle_cutoff = now - (self.WH_IDLE_EXPIRE_HOURS * 3600)
        with self._conn() as conn:
            # Auto-expire: hard expiry OR idle expiry (last_used_ts older than 16h)
            conn.execute(
                "UPDATE wh_connections SET status='expired' WHERE status='active' AND (expires_ts < ? OR (last_used_ts IS NOT NULL AND last_used_ts < ?))",
                (now, idle_cutoff)
            )

            if system:
                s = system.strip().lower()
                rows = conn.execute(
                    """SELECT * FROM wh_connections WHERE status='active'
                       AND (lower(from_system)=? OR lower(to_system)=?)
                       ORDER BY created_ts DESC LIMIT ?""",
                    (s, s, int(limit))
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wh_connections WHERE status='active' ORDER BY created_ts DESC LIMIT ?",
                    (int(limit),)
                ).fetchall()

        cols = ["id", "from_system", "to_system", "wh_type", "wh_class", "status",
                "mass_status", "time_status", "reported_by", "reported_by_name",
                "created_ts", "expires_ts", "last_used_ts", "notes"]
        return [dict(zip(cols, r)) for r in rows]

    def wh_update_status(self, conn_id: int, *, mass_status: str = None,
                         time_status: str = None, status: str = None, notes: str = None) -> bool:
        """Update a wormhole connection's status."""
        updates = []
        params = []
        if mass_status:
            updates.append("mass_status=?")
            params.append(mass_status)
        if time_status:
            updates.append("time_status=?")
            params.append(time_status)
        if status:
            updates.append("status=?")
            params.append(status)
        if notes is not None:
            updates.append("notes=?")
            params.append(notes)
        if not updates:
            return False
        params.append(int(conn_id))
        with self._conn() as conn:
            conn.execute(f"UPDATE wh_connections SET {', '.join(updates)} WHERE id=?", params)
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def wh_close_connection(self, conn_id: int) -> bool:
        """Mark a connection as closed/collapsed."""
        with self._conn() as conn:
            conn.execute("UPDATE wh_connections SET status='closed' WHERE id=?", (int(conn_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def wh_close_all(self, system: str = None) -> int:
        """Close all active connections, optionally for a specific system."""
        with self._conn() as conn:
            if system:
                s = system.strip().lower()
                conn.execute(
                    "UPDATE wh_connections SET status='closed' WHERE status='active' AND (lower(from_system)=? OR lower(to_system)=?)",
                    (s, s))
            else:
                conn.execute("UPDATE wh_connections SET status='closed' WHERE status='active'")
            return conn.execute("SELECT changes()").fetchone()[0]

    def wh_history(self, system: str = None, limit: int = 20) -> List[Dict]:
        """Get recent wormhole connection history (all statuses)."""
        with self._conn() as conn:
            if system:
                s = system.strip().lower()
                rows = conn.execute(
                    """SELECT * FROM wh_connections
                       WHERE lower(from_system)=? OR lower(to_system)=?
                       ORDER BY created_ts DESC LIMIT ?""",
                    (s, s, int(limit))
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wh_connections ORDER BY created_ts DESC LIMIT ?",
                    (int(limit),)
                ).fetchall()
        cols = ["id", "from_system", "to_system", "wh_type", "wh_class", "status",
                "mass_status", "time_status", "reported_by", "reported_by_name",
                "created_ts", "expires_ts", "last_used_ts", "notes"]
        return [dict(zip(cols, r)) for r in rows]

    # ----------------------------
    # ── r155: Canvas Map Methods ───────────────────────────────────────────────

    def _ensure_map_tables(self):
        with self._conn() as conn:
            for sql in [
                """CREATE TABLE IF NOT EXISTS map_systems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    system_id INTEGER NOT NULL, system_name TEXT NOT NULL,
                    x_pos REAL NOT NULL DEFAULT 100, y_pos REAL NOT NULL DEFAULT 100,
                    temp_name TEXT, locked INTEGER NOT NULL DEFAULT 0, color TEXT,
                    sec_status REAL, region_name TEXT, region_id INTEGER,
                    is_wh INTEGER NOT NULL DEFAULT 0,
                    added_by TEXT, added_ts REAL NOT NULL DEFAULT (unixepoch()),
                    UNIQUE(map_id, system_id))""",
                """CREATE TABLE IF NOT EXISTS map_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    from_sys_id INTEGER NOT NULL, to_sys_id INTEGER NOT NULL,
                    wh_type TEXT, mass_status TEXT NOT NULL DEFAULT 'stable',
                    time_status TEXT NOT NULL DEFAULT 'fresh', eol_ts REAL,
                    save_mass INTEGER NOT NULL DEFAULT 0, mass_kg_passed REAL NOT NULL DEFAULT 0,
                    ship_count INTEGER NOT NULL DEFAULT 0, sig_id_from TEXT, sig_id_to TEXT,
                    created_by TEXT, created_ts REAL NOT NULL DEFAULT (unixepoch()),
                    UNIQUE(map_id, from_sys_id, to_sys_id))""",
                """CREATE TABLE IF NOT EXISTS map_structures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    system_id INTEGER NOT NULL, system_name TEXT, name TEXT NOT NULL,
                    struct_type TEXT, owner TEXT, status TEXT NOT NULL DEFAULT 'active',
                    notes TEXT, added_by TEXT, added_ts REAL NOT NULL DEFAULT (unixepoch()))""",
                """CREATE TABLE IF NOT EXISTS map_routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    name TEXT NOT NULL, system_name TEXT NOT NULL, system_id INTEGER,
                    added_by TEXT, added_ts REAL NOT NULL DEFAULT (unixepoch()))""",
            ]:
                conn.execute(sql)
            # Column migrations for existing tables
            for col, defn in [
                ('sec_status',  'REAL'),
                ('region_name', 'TEXT'),
                ('is_wh',       'INTEGER NOT NULL DEFAULT 0'),
                ('region_id',   'INTEGER'),
                ('tag',         'TEXT'),
            ]:
                try:
                    conn.execute(f"ALTER TABLE map_systems ADD COLUMN {col} {defn}")
                except Exception:
                    pass  # column already exists

    def map_get_systems(self, map_id: str = 'corp') -> list:
        self._ensure_map_tables()
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_systems WHERE map_id=? ORDER BY added_ts", (map_id,)
            ).fetchall()]

    def map_add_system(self, system_id: int, system_name: str, x: float, y: float,
                       map_id: str = 'corp', added_by: str = None,
                       sec_status: float = None, region_name: str = None,
                       region_id: int = None, is_wh: bool = False) -> dict:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            # Prevent name-based duplicates: if same name exists patch missing fields instead.
            existing = conn.execute(
                "SELECT * FROM map_systems WHERE map_id=? AND LOWER(system_name)=LOWER(?)",
                (map_id, str(system_name))
            ).fetchone()
            if existing:
                row = dict(existing)
                updates = {}
                if sec_status is not None and (row.get("sec_status") is None or row.get("sec_status") == 0.0):
                    updates["sec_status"] = sec_status
                if region_name and not row.get("region_name"):
                    updates["region_name"] = region_name
                if updates:
                    sets = ", ".join(f'"{k}"=?' for k in updates)
                    conn.execute(f"UPDATE map_systems SET {sets} WHERE map_id=? AND system_id=?",
                                 list(updates.values()) + [map_id, row["system_id"]])
                return {"id": row["id"], "system_id": row["system_id"], "system_name": system_name,
                        "x_pos": row["x_pos"], "y_pos": row["y_pos"], "action": "exists"}
            try:
                cur = conn.execute(
                    """INSERT INTO map_systems
                       (map_id, system_id, system_name, x_pos, y_pos, sec_status, region_name, region_id, is_wh, added_by, added_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (map_id, int(system_id), str(system_name), float(x), float(y),
                     sec_status, region_name, region_id, 1 if is_wh else 0, added_by, _t.time())
                )
                return {"id": cur.lastrowid, "system_id": system_id, "system_name": system_name,
                        "x_pos": x, "y_pos": y, "action": "added"}
            except Exception:
                return {"error": "already_exists"}

    def map_update_system(self, system_id: int, map_id: str = 'corp', **kwargs) -> bool:
        allowed = {'x_pos', 'y_pos', 'temp_name', 'locked', 'color', 'sec_status', 'region_name', 'region_id', 'is_wh', 'tag'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        self._ensure_map_tables()
        sets = ', '.join(f'"{k}"=?' for k in updates)
        with self._conn() as conn:
            conn.execute(f"UPDATE map_systems SET {sets} WHERE map_id=? AND system_id=?",
                         list(updates.values()) + [map_id, int(system_id)])
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_delete_system(self, system_id: int, map_id: str = 'corp') -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_connections WHERE map_id=? AND (from_sys_id=? OR to_sys_id=?)",
                         (map_id, int(system_id), int(system_id)))
            conn.execute("DELETE FROM map_systems WHERE map_id=? AND system_id=?",
                         (map_id, int(system_id)))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_get_connections(self, map_id: str = 'corp') -> list:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            # Auto-expire EOL connections
            conn.execute("DELETE FROM map_connections WHERE eol_ts IS NOT NULL AND eol_ts < ?",
                         (_t.time(),))
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_connections WHERE map_id=? ORDER BY created_ts", (map_id,)
            ).fetchall()]

    def map_add_connection(self, from_sys_id: int, to_sys_id: int, map_id: str = 'corp',
                           wh_type: str = None, created_by: str = None) -> dict:
        import time as _t
        self._ensure_map_tables()
        a, b = min(from_sys_id, to_sys_id), max(from_sys_id, to_sys_id)
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO map_connections (map_id, from_sys_id, to_sys_id, wh_type, created_by, created_ts)
                       VALUES (?,?,?,?,?,?)""",
                    (map_id, a, b, wh_type, created_by, _t.time())
                )
                return {"id": cur.lastrowid, "action": "added"}
            except Exception:
                return {"error": "already_exists"}

    def map_update_connection(self, conn_id: int, **kwargs) -> bool:
        allowed = {'wh_type', 'mass_status', 'time_status', 'eol_ts', 'save_mass',
                   'mass_kg_passed', 'ship_count', 'sig_id_from', 'sig_id_to'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        self._ensure_map_tables()
        sets = ', '.join(f'"{k}"=?' for k in updates)
        with self._conn() as conn:
            conn.execute(f"UPDATE map_connections SET {sets} WHERE id=?",
                         list(updates.values()) + [int(conn_id)])
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_delete_connection(self, conn_id: int) -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_connections WHERE id=?", (int(conn_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_get_structures(self, system_id: int, map_id: str = 'corp') -> list:
        self._ensure_map_tables()
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_structures WHERE map_id=? AND system_id=? ORDER BY added_ts",
                (map_id, int(system_id))
            ).fetchall()]

    def map_add_structure(self, system_id: int, system_name: str, name: str,
                          struct_type: str = None, owner: str = None, notes: str = None,
                          map_id: str = 'corp', added_by: str = None) -> int:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO map_structures (map_id, system_id, system_name, name, struct_type, owner, notes, added_by, added_ts)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (map_id, int(system_id), system_name, name, struct_type, owner, notes, added_by, _t.time())
            )
            return cur.lastrowid

    def map_delete_structure(self, struct_id: int) -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_structures WHERE id=?", (int(struct_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_get_routes(self, map_id: str = 'corp') -> list:
        self._ensure_map_tables()
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_routes WHERE map_id=? ORDER BY name", (map_id,)
            ).fetchall()]

    def map_add_route(self, name: str, system_name: str, system_id: int = None,
                      map_id: str = 'corp', added_by: str = None) -> int:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO map_routes (map_id, name, system_name, system_id, added_by, added_ts) VALUES (?,?,?,?,?,?)",
                (map_id, name, system_name, system_id, added_by, _t.time())
            )
            return cur.lastrowid

    def map_delete_route(self, route_id: int) -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_routes WHERE id=?", (int(route_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_full_state(self, map_id: str = 'corp') -> dict:
        """Return complete map state: systems + connections + structures + routes."""
        return {
            "systems":     self.map_get_systems(map_id),
            "connections": self.map_get_connections(map_id),
            "routes":      self.map_get_routes(map_id),
        }

    # r100: Saved fittings
    # ----------------------------
    def nav_log_movement(self, character_id: int, system_id: int, system_name: str,
                         security_status: float = 0.0, constellation_id: int = None,
                         constellation_name: str = None, region_id: int = None,
                         region_name: str = None, character_name: str = None) -> None:
        """Log a system movement. Only inserts if system changed from last entry."""
        with self._conn() as conn:
            last = conn.execute(
                "SELECT system_id FROM nav_movements WHERE character_id=? ORDER BY timestamp DESC LIMIT 1",
                (int(character_id),)
            ).fetchone()
            if last and last[0] == int(system_id):
                return  # Same system, skip
            conn.execute(
                """INSERT INTO nav_movements
                   (character_id, character_name, system_id, system_name, security_status,
                    constellation_id, constellation_name, region_id, region_name)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (int(character_id), character_name, int(system_id), system_name,
                 float(security_status), constellation_id, constellation_name,
                 region_id, region_name)
            )

    def nav_get_movements(self, character_id: int, limit: int = 50) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nav_movements WHERE character_id=? ORDER BY timestamp DESC LIMIT ?",
                (int(character_id), int(limit))
            ).fetchall()
        cols = ["id", "character_id", "character_name", "system_id", "system_name",
                "security_status", "constellation_id", "constellation_name",
                "region_id", "region_name", "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    def nav_get_all_recent_movements(self, limit: int = 100) -> List[Dict]:
        """Get latest position per character (deduplicated)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.character_id, m.character_name, m.system_id, m.system_name,
                          m.security_status, m.region_id, m.region_name, m.timestamp
                   FROM nav_movements m
                   INNER JOIN (
                       SELECT character_id, MAX(timestamp) as max_ts
                       FROM nav_movements
                       WHERE timestamp > datetime('now', '-24 hours')
                       GROUP BY character_id
                   ) latest ON m.character_id = latest.character_id AND m.timestamp = latest.max_ts
                   ORDER BY m.timestamp DESC LIMIT ?""",
                (int(limit),)
            ).fetchall()
        cols = ["character_id", "character_name", "system_id", "system_name",
                "security_status", "region_id", "region_name", "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    def nav_add_intel(self, system_id: int, content: str,
                      character_id: int = None, character_name: str = None,
                      source: str = 'manual') -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO nav_intel (system_id, character_id, character_name, content, source) VALUES (?,?,?,?,?)",
                (int(system_id), int(character_id) if character_id else None, character_name, str(content), str(source))
            )
            return cur.lastrowid

    def nav_get_intel(self, system_id: int, limit: int = 20) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nav_intel WHERE system_id=? ORDER BY timestamp DESC LIMIT ?",
                (int(system_id), int(limit))
            ).fetchall()
        cols = ["id", "system_id", "character_id", "character_name", "content", "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    def nav_get_recent_intel(self, limit: int = 50) -> List[Dict]:
        """Get recent intel across all systems."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nav_intel ORDER BY timestamp DESC LIMIT ?",
                (int(limit),)
            ).fetchall()
        cols = ["id", "system_id", "character_id", "character_name", "content", "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    # ----------------------------
    # ── r155: Canvas Map Methods ───────────────────────────────────────────────

    def _ensure_map_tables(self):
        with self._conn() as conn:
            for sql in [
                """CREATE TABLE IF NOT EXISTS map_systems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    system_id INTEGER NOT NULL, system_name TEXT NOT NULL,
                    x_pos REAL NOT NULL DEFAULT 100, y_pos REAL NOT NULL DEFAULT 100,
                    temp_name TEXT, locked INTEGER NOT NULL DEFAULT 0, color TEXT,
                    sec_status REAL, region_name TEXT, region_id INTEGER,
                    is_wh INTEGER NOT NULL DEFAULT 0,
                    added_by TEXT, added_ts REAL NOT NULL DEFAULT (unixepoch()),
                    UNIQUE(map_id, system_id))""",
                """CREATE TABLE IF NOT EXISTS map_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    from_sys_id INTEGER NOT NULL, to_sys_id INTEGER NOT NULL,
                    wh_type TEXT, mass_status TEXT NOT NULL DEFAULT 'stable',
                    time_status TEXT NOT NULL DEFAULT 'fresh', eol_ts REAL,
                    save_mass INTEGER NOT NULL DEFAULT 0, mass_kg_passed REAL NOT NULL DEFAULT 0,
                    ship_count INTEGER NOT NULL DEFAULT 0, sig_id_from TEXT, sig_id_to TEXT,
                    created_by TEXT, created_ts REAL NOT NULL DEFAULT (unixepoch()),
                    UNIQUE(map_id, from_sys_id, to_sys_id))""",
                """CREATE TABLE IF NOT EXISTS map_structures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    system_id INTEGER NOT NULL, system_name TEXT, name TEXT NOT NULL,
                    struct_type TEXT, owner TEXT, status TEXT NOT NULL DEFAULT 'active',
                    notes TEXT, added_by TEXT, added_ts REAL NOT NULL DEFAULT (unixepoch()))""",
                """CREATE TABLE IF NOT EXISTS map_routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, map_id TEXT NOT NULL DEFAULT 'corp',
                    name TEXT NOT NULL, system_name TEXT NOT NULL, system_id INTEGER,
                    added_by TEXT, added_ts REAL NOT NULL DEFAULT (unixepoch()))""",
            ]:
                conn.execute(sql)
            # Column migrations for existing tables
            for col, defn in [
                ('sec_status',  'REAL'),
                ('region_name', 'TEXT'),
                ('is_wh',       'INTEGER NOT NULL DEFAULT 0'),
                ('region_id',   'INTEGER'),
                ('tag',         'TEXT'),
            ]:
                try:
                    conn.execute(f"ALTER TABLE map_systems ADD COLUMN {col} {defn}")
                except Exception:
                    pass  # column already exists

    def map_get_systems(self, map_id: str = 'corp') -> list:
        self._ensure_map_tables()
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_systems WHERE map_id=? ORDER BY added_ts", (map_id,)
            ).fetchall()]

    def map_add_system(self, system_id: int, system_name: str, x: float, y: float,
                       map_id: str = 'corp', added_by: str = None,
                       sec_status: float = None, region_name: str = None,
                       region_id: int = None, is_wh: bool = False) -> dict:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            # Prevent name-based duplicates: if same name exists patch missing fields instead.
            existing = conn.execute(
                "SELECT * FROM map_systems WHERE map_id=? AND LOWER(system_name)=LOWER(?)",
                (map_id, str(system_name))
            ).fetchone()
            if existing:
                row = dict(existing)
                updates = {}
                if sec_status is not None and (row.get("sec_status") is None or row.get("sec_status") == 0.0):
                    updates["sec_status"] = sec_status
                if region_name and not row.get("region_name"):
                    updates["region_name"] = region_name
                if updates:
                    sets = ", ".join(f'"{k}"=?' for k in updates)
                    conn.execute(f"UPDATE map_systems SET {sets} WHERE map_id=? AND system_id=?",
                                 list(updates.values()) + [map_id, row["system_id"]])
                return {"id": row["id"], "system_id": row["system_id"], "system_name": system_name,
                        "x_pos": row["x_pos"], "y_pos": row["y_pos"], "action": "exists"}
            try:
                cur = conn.execute(
                    """INSERT INTO map_systems
                       (map_id, system_id, system_name, x_pos, y_pos, sec_status, region_name, region_id, is_wh, added_by, added_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (map_id, int(system_id), str(system_name), float(x), float(y),
                     sec_status, region_name, region_id, 1 if is_wh else 0, added_by, _t.time())
                )
                return {"id": cur.lastrowid, "system_id": system_id, "system_name": system_name,
                        "x_pos": x, "y_pos": y, "action": "added"}
            except Exception:
                return {"error": "already_exists"}

    def map_update_system(self, system_id: int, map_id: str = 'corp', **kwargs) -> bool:
        allowed = {'x_pos', 'y_pos', 'temp_name', 'locked', 'color', 'sec_status', 'region_name', 'region_id', 'is_wh', 'tag'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        self._ensure_map_tables()
        sets = ', '.join(f'"{k}"=?' for k in updates)
        with self._conn() as conn:
            conn.execute(f"UPDATE map_systems SET {sets} WHERE map_id=? AND system_id=?",
                         list(updates.values()) + [map_id, int(system_id)])
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_delete_system(self, system_id: int, map_id: str = 'corp') -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_connections WHERE map_id=? AND (from_sys_id=? OR to_sys_id=?)",
                         (map_id, int(system_id), int(system_id)))
            conn.execute("DELETE FROM map_systems WHERE map_id=? AND system_id=?",
                         (map_id, int(system_id)))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_get_connections(self, map_id: str = 'corp') -> list:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            # Auto-expire EOL connections
            conn.execute("DELETE FROM map_connections WHERE eol_ts IS NOT NULL AND eol_ts < ?",
                         (_t.time(),))
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_connections WHERE map_id=? ORDER BY created_ts", (map_id,)
            ).fetchall()]

    def map_add_connection(self, from_sys_id: int, to_sys_id: int, map_id: str = 'corp',
                           wh_type: str = None, created_by: str = None) -> dict:
        import time as _t
        self._ensure_map_tables()
        a, b = min(from_sys_id, to_sys_id), max(from_sys_id, to_sys_id)
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO map_connections (map_id, from_sys_id, to_sys_id, wh_type, created_by, created_ts)
                       VALUES (?,?,?,?,?,?)""",
                    (map_id, a, b, wh_type, created_by, _t.time())
                )
                return {"id": cur.lastrowid, "action": "added"}
            except Exception:
                return {"error": "already_exists"}

    def map_update_connection(self, conn_id: int, **kwargs) -> bool:
        allowed = {'wh_type', 'mass_status', 'time_status', 'eol_ts', 'save_mass',
                   'mass_kg_passed', 'ship_count', 'sig_id_from', 'sig_id_to'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        self._ensure_map_tables()
        sets = ', '.join(f'"{k}"=?' for k in updates)
        with self._conn() as conn:
            conn.execute(f"UPDATE map_connections SET {sets} WHERE id=?",
                         list(updates.values()) + [int(conn_id)])
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_delete_connection(self, conn_id: int) -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_connections WHERE id=?", (int(conn_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_get_structures(self, system_id: int, map_id: str = 'corp') -> list:
        self._ensure_map_tables()
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_structures WHERE map_id=? AND system_id=? ORDER BY added_ts",
                (map_id, int(system_id))
            ).fetchall()]

    def map_add_structure(self, system_id: int, system_name: str, name: str,
                          struct_type: str = None, owner: str = None, notes: str = None,
                          map_id: str = 'corp', added_by: str = None) -> int:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO map_structures (map_id, system_id, system_name, name, struct_type, owner, notes, added_by, added_ts)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (map_id, int(system_id), system_name, name, struct_type, owner, notes, added_by, _t.time())
            )
            return cur.lastrowid

    def map_delete_structure(self, struct_id: int) -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_structures WHERE id=?", (int(struct_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_get_routes(self, map_id: str = 'corp') -> list:
        self._ensure_map_tables()
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM map_routes WHERE map_id=? ORDER BY name", (map_id,)
            ).fetchall()]

    def map_add_route(self, name: str, system_name: str, system_id: int = None,
                      map_id: str = 'corp', added_by: str = None) -> int:
        import time as _t
        self._ensure_map_tables()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO map_routes (map_id, name, system_name, system_id, added_by, added_ts) VALUES (?,?,?,?,?,?)",
                (map_id, name, system_name, system_id, added_by, _t.time())
            )
            return cur.lastrowid

    def map_delete_route(self, route_id: int) -> bool:
        self._ensure_map_tables()
        with self._conn() as conn:
            conn.execute("DELETE FROM map_routes WHERE id=?", (int(route_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def map_full_state(self, map_id: str = 'corp') -> dict:
        """Return complete map state: systems + connections + structures + routes."""
        return {
            "systems":     self.map_get_systems(map_id),
            "connections": self.map_get_connections(map_id),
            "routes":      self.map_get_routes(map_id),
        }

    # r100: Saved fittings
    # ----------------------------
    def nav_upsert_sig(self, system_id: int, sig_id: str, *, system_name: str = None,
                       sig_group: str = None, sig_info: str = None, description: str = None,
                       scanned_by: int = None, scanned_by_name: str = None) -> Dict:
        """Insert or update a signature. Returns {action: 'new'|'updated', sig}."""
        now = time.time()
        sig_id = sig_id.strip().upper()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id, sig_group, sig_info, description FROM nav_signatures WHERE system_id=? AND sig_id=?",
                (int(system_id), sig_id)
            ).fetchone()
            if existing:
                # r100: Saved fittings
                new_group = sig_group or (existing[1] if existing else None)
                new_info = sig_info or (existing[2] if existing else None)
                new_desc = description or (existing[3] if existing else None)
                conn.execute(
                    """UPDATE nav_signatures SET sig_group=?, sig_info=?, description=?,
                       scanned_by=?, scanned_by_name=?, updated_ts=?, last_confirmed_ts=?
                       WHERE system_id=? AND sig_id=?""",
                    (new_group, new_info, new_desc, scanned_by, scanned_by_name, now, now,
                     int(system_id), sig_id)
                )
                return {"action": "updated", "sig_id": sig_id, "group": new_group, "info": new_info}
            else:
                conn.execute(
                    """INSERT INTO nav_signatures (system_id, system_name, sig_id, sig_group, sig_info,
                       description, scanned_by, scanned_by_name, created_ts, updated_ts,
                       first_seen_ts, last_confirmed_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(system_id), system_name, sig_id, sig_group, sig_info, description,
                     scanned_by, scanned_by_name, now, now, now, now)
                )
                return {"action": "new", "sig_id": sig_id, "group": sig_group, "info": sig_info}

    def nav_get_sigs(self, system_id: int) -> List[Dict]:
        """Get all signatures for a system."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nav_signatures WHERE system_id=? ORDER BY sig_id",
                (int(system_id),)
            ).fetchall()
        cols = ["id", "system_id", "system_name", "sig_id", "sig_group", "sig_info",
                "description", "scanned_by", "scanned_by_name", "created_ts", "updated_ts",
                "first_seen_ts", "last_confirmed_ts", "wh_type_code", "wh_dest_class",
                "wh_max_jump_kg", "wh_total_kg", "wh_lifetime_h"]
        return [dict(zip(cols, r)) for r in rows]

    def nav_delete_sig(self, sig_db_id: int) -> bool:
        """Delete a signature by its database ID."""
        with self._conn() as conn:
            conn.execute("DELETE FROM nav_signatures WHERE id=?", (int(sig_db_id),))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def nav_delete_all_sigs(self, system_id: int) -> int:
        """Delete all signatures for a system."""
        with self._conn() as conn:
            conn.execute("DELETE FROM nav_signatures WHERE system_id=?", (int(system_id),))
            return conn.execute("SELECT changes()").fetchone()[0]

    def nav_sig_confirm(self, sig_db_id: int) -> bool:
        """Reset last_confirmed_ts to now for a sig."""
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "UPDATE nav_signatures SET last_confirmed_ts=?, updated_ts=? WHERE id=?",
                (now, now, int(sig_db_id))
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def nav_sig_set_wh_type(self, sig_db_id: int, wh_type_code: str,
                             wh_dest_class: str = None, wh_max_jump_kg: float = None,
                             wh_total_kg: float = None, wh_lifetime_h: int = None) -> bool:
        """Set WH enrichment data on a sig row."""
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """UPDATE nav_signatures
                   SET wh_type_code=?, wh_dest_class=?, wh_max_jump_kg=?,
                       wh_total_kg=?, wh_lifetime_h=?, updated_ts=?
                   WHERE id=?""",
                (wh_type_code, wh_dest_class, wh_max_jump_kg,
                 wh_total_kg, wh_lifetime_h, now, int(sig_db_id))
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def nav_sig_version(self, system_id: int) -> int:
        """Return an integer version counter for sigs in a system."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(updated_ts) FROM nav_signatures WHERE system_id=?",
                (int(system_id),)
            ).fetchone()
        val = row[0] if row and row[0] else 0
        return int(float(val) * 1000) if val else 0

    # WH Mass Tracking
    def wh_mass_get(self, conn_id: str) -> Optional[Dict]:
        """Return the mass record for a connection or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM nav_wh_mass WHERE conn_id=?", (conn_id,)
            ).fetchone()
        if not row:
            return None
        cols = ["conn_id", "total_mass_kg", "used_mass_kg", "manual_state",
                "last_jump_ts", "last_ship_name", "last_ship_mass_kg", "updated_ts"]
        return dict(zip(cols, row))

    def wh_mass_upsert(self, conn_id: str, *, total_mass_kg: float = None,
                       used_mass_kg: float = None, manual_state: str = None,
                       last_ship_name: str = None, last_ship_mass_kg: float = None) -> Dict:
        """Create or update a mass record. Partial update — only non-None args change."""
        import time as _time
        now = _time.time()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT total_mass_kg, used_mass_kg FROM nav_wh_mass WHERE conn_id=?",
                (conn_id,)
            ).fetchone()
            if existing is None:
                total = total_mass_kg if total_mass_kg is not None else 2_000_000_000.0
                used  = used_mass_kg  if used_mass_kg  is not None else 0.0
                conn.execute(
                    """INSERT INTO nav_wh_mass
                       (conn_id, total_mass_kg, used_mass_kg, manual_state,
                        last_jump_ts, last_ship_name, last_ship_mass_kg, updated_ts)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (conn_id, total, used, manual_state,
                     now if last_ship_name else None,
                     last_ship_name, last_ship_mass_kg, now)
                )
            else:
                updates = {"updated_ts": now}
                if total_mass_kg  is not None: updates["total_mass_kg"]  = total_mass_kg
                if used_mass_kg   is not None: updates["used_mass_kg"]   = used_mass_kg
                if manual_state   is not None: updates["manual_state"]   = manual_state
                if last_ship_name is not None:
                    updates["last_ship_name"]    = last_ship_name
                    updates["last_ship_mass_kg"] = last_ship_mass_kg
                    updates["last_jump_ts"]      = now
                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE nav_wh_mass SET {set_clause} WHERE conn_id=?",
                    list(updates.values()) + [conn_id]
                )
        return self.wh_mass_get(conn_id)

    def wh_mass_reset(self, conn_id: str) -> bool:
        """Delete mass record (e.g. when connection is re-mapped as a new hole)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM nav_wh_mass WHERE conn_id=?", (conn_id,))
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def wh_mass_state(self, conn_id: str) -> str:
        """Return computed mass state string: fresh|halflife|critical|collapsed."""
        rec = self.wh_mass_get(conn_id)
        if not rec:
            return "fresh"
        if rec.get("manual_state"):
            return rec["manual_state"]
        total = rec["total_mass_kg"] or 1
        used  = rec["used_mass_kg"]  or 0
        pct   = used / total
        if pct >= 1.0:  return "collapsed"
        if pct >= 0.9:  return "critical"
        if pct >= 0.5:  return "halflife"
        return "fresh"

    def nav_parse_and_upsert_sigs(self, system_id: int, raw_text: str, *,
                                   system_name: str = None, scanned_by: int = None,
                                   scanned_by_name: str = None) -> Dict:
        """Parse EVE probe scanner paste and upsert all sigs. Returns summary."""
        lines = raw_text.strip().split('\n')
        new_sigs = []
        updated_sigs = []
        for line in lines:
            parts = line.split('\t')
            if len(parts) < 2:
                parts = line.split('  ')  # fallback to double-space
                parts = [p.strip() for p in parts if p.strip()]
            if len(parts) < 2:
                continue
            sig_id = parts[0].strip()
            if not sig_id or len(sig_id) < 3:
                continue
            # Skip header row
            if sig_id.lower() in ('id', 'sig', 'signature'):
                continue
            sig_group = parts[1].strip() if len(parts) > 1 else None
            sig_info = parts[2].strip() if len(parts) > 2 else None
            description = parts[3].strip() if len(parts) > 3 else None
            
            result = self.nav_upsert_sig(
                system_id, sig_id,
                system_name=system_name, sig_group=sig_group,
                sig_info=sig_info, description=description,
                scanned_by=scanned_by, scanned_by_name=scanned_by_name,
            )
            if result["action"] == "new":
                new_sigs.append(result)
            else:
                updated_sigs.append(result)
        
        return {"new": new_sigs, "updated": updated_sigs, "total_parsed": len(new_sigs) + len(updated_sigs)}
    def clear_user_profile(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM user_profile WHERE user_id = ?", (int(user_id),))

    def clear_user_prefs(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM user_prefs WHERE user_id = ?", (int(user_id),))

    def clear_user_context(self, user_id: int) -> None:
        self.clear_user_profile(user_id)
        self.clear_user_prefs(user_id)

    # ----------------------------
    # EVE SSO helpers
    # ----------------------------
    def eve_create_pending_state(self, user_id: int, state: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO eve_pending_auth(state, user_id, created_ts) VALUES(?,?,?)",
                (str(state), int(user_id), float(now)),
            )

    def eve_consume_pending_state(self, state: str) -> Optional[int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM eve_pending_auth WHERE state = ?",
                (str(state),),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM eve_pending_auth WHERE state = ?", (str(state),))
        try:
            return int(row[0])
        except Exception:
            return None

    def eve_peek_pending_state(self, state: str) -> Optional[int]:
        """Return user_id for a pending EVE auth state WITHOUT consuming it."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM eve_pending_auth WHERE state = ?",
                (str(state),),
            ).fetchone()
            if row is None:
                return None
            try:
                return int(row[0])
            except Exception:
                return None



    def eve_set_link_intent(self, user_id: int, scopes: str) -> None:
        """Store the next scope set to request on the next `eve link` call (progressive scopes)."""
        scopes = (scopes or "").strip()
        if not scopes:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO eve_link_intent(user_id, scopes, updated_ts) VALUES(?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET scopes=excluded.scopes, updated_ts=excluded.updated_ts",
                (int(user_id), scopes, time.time()),
            )

    def eve_get_link_intent(self, user_id: int) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT scopes FROM eve_link_intent WHERE user_id=?", (int(user_id),)).fetchone()
            return str(row[0]) if row and row[0] else None

    def eve_clear_link_intent(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM eve_link_intent WHERE user_id=?", (int(user_id),))

    def eve_upsert_auth(
        self,
        *,
        user_id: int | None = None,
        discord_user_id: int | None = None,
        character_id: int,
        character_name: str,
        refresh_token: str | None = None,
        refresh_token_enc: str | None = None,
        scopes: str,
        alias: str | None = None,
        is_default: int | None = None,
        token_expires_at: int | None = None,
        expires_at: int | None = None,
    ) -> None:
        """Upsert an EVE character link for a Discord user.

        Compatibility: accepts legacy parameter names used by web callback code.
        - user_id or discord_user_id
        - refresh_token or refresh_token_enc
        - token_expires_at/expires_at are accepted but not persisted
        """
        uid = user_id if user_id is not None else discord_user_id
        if uid is None:
            raise ValueError("user_id (or discord_user_id) is required")
        rt = refresh_token if refresh_token is not None else refresh_token_enc
        if rt is None:
            raise ValueError("refresh_token (or refresh_token_enc) is required")
        user_id = int(uid)
        refresh_token = str(rt)
        now = time.time()
        with self._conn() as conn:
            # Canonical multi-character store
            # Normalize optional flags
            alias_norm = str(alias).strip() if alias is not None and str(alias).strip() else None
            is_default_norm = 1 if (is_default is None) else (1 if int(is_default) else 0)

            # If caller explicitly sets a default, clear other defaults for that user.
            if is_default is not None and is_default_norm == 1:
                conn.execute(
                    "UPDATE eve_characters SET is_default=0 WHERE user_id=?",
                    (int(user_id),),
                )

            conn.execute(
                """
                INSERT INTO eve_characters(user_id, character_id, character_name, refresh_token, scopes, alias, is_default, updated_ts)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id, character_id) DO UPDATE SET
                    character_name=excluded.character_name,
                    refresh_token=excluded.refresh_token,
                    scopes=excluded.scopes,
                    alias=COALESCE(excluded.alias, eve_characters.alias),
                    is_default=CASE WHEN excluded.is_default=1 THEN 1 ELSE eve_characters.is_default END,
                    updated_ts=excluded.updated_ts
                """,
                (
                    int(user_id),
                    int(character_id),
                    str(character_name),
                    str(refresh_token),
                    str(scopes),
                    alias_norm,
                    is_default_norm,
                    float(now),
                ),
            )

            # If user has no default character, set this one as default.
            cur = conn.execute(
                "SELECT character_id FROM eve_characters WHERE user_id=? AND is_default=1 LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if not cur:
                conn.execute("UPDATE eve_characters SET is_default=0 WHERE user_id=?", (int(user_id),))
                conn.execute(
                    "UPDATE eve_characters SET is_default=1 WHERE user_id=? AND character_id=?",
                    (int(user_id), int(character_id)),
                )

            # Legacy single-character store (router compatibility)
            # Keep eve_auth in sync with the user's default character.
            # Older router paths read only from eve_auth.
            cur_def = conn.execute(
                "SELECT character_id, character_name, refresh_token, scopes FROM eve_characters WHERE user_id=? AND is_default=1 LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if cur_def:
                conn.execute(
                    "INSERT INTO eve_auth(user_id, character_id, character_name, refresh_token, scopes, updated_ts) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET character_id=excluded.character_id, character_name=excluded.character_name, refresh_token=excluded.refresh_token, scopes=excluded.scopes, updated_ts=excluded.updated_ts",
                    (
                        int(user_id),
                        int(cur_def[0]),
                        str(cur_def[1]),
                        str(cur_def[2]),
                        str(cur_def[3]),
                        float(now),
                    ),
                )


    # ----------------------------
    # EVE auth / character helpers (router + legacy compatibility)
    # ----------------------------
    @staticmethod
    def _uid_variants(user_id: Any) -> tuple[int, str]:
        """Return (int_id, str_id) variants for robust lookups.

        We've seen Discord IDs stored as INTEGER or TEXT depending on older
        migrations / manual inserts. Using both variants keeps lookups stable.
        """
        try:
            int_id = int(user_id)
        except Exception:
            # Fall back to 0 (won't match) but keep string for debugging.
            int_id = 0
        return int_id, str(user_id)

    def eve_get_auth(self, user_id: int) -> Optional[dict]:
        """Return the *default* EVE auth record for a Discord user.

        Router paths historically read from eve_auth (single-character).
        Newer code stores multi-character links in eve_characters. This
        method provides a stable interface for both.
        """
        int_id, str_id = self._uid_variants(user_id)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id, character_id, character_name, refresh_token, scopes, updated_ts FROM eve_auth WHERE user_id IN (?, ?) LIMIT 1",
                (int_id, str_id),
            ).fetchone()
            if not row:
                # Fallback: derive from eve_characters default
                row = conn.execute(
                    "SELECT user_id, character_id, character_name, refresh_token, scopes, updated_ts FROM eve_characters WHERE user_id IN (?, ?) AND is_default=1 LIMIT 1",
                    (int_id, str_id),
                ).fetchone()
        if not row:
            return None
        return {
            "user_id": int(row[0]),
            "character_id": int(row[1]),
            "character_name": str(row[2]),
            "refresh_token": str(row[3]),
            "scopes": str(row[4]),
            "updated_ts": float(row[5]),
        }

    def eve_delete_auth(self, user_id: int) -> int:
        """Delete *all* EVE auth for a user (legacy + multi-character).

        Returns number of rows deleted from legacy eve_auth.
        """
        with self._conn() as conn:
            conn.execute("DELETE FROM eve_characters WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_link_intent WHERE user_id=?", (int(user_id),))
            # Pending-state table name varied across patches; current schema uses eve_pending_auth.
            try:
                conn.execute("DELETE FROM eve_pending_auth WHERE user_id=?", (int(user_id),))
            except Exception:
                try:
                    conn.execute("DELETE FROM eve_pending_state WHERE user_id=?", (int(user_id),))
                except Exception:
                    pass
            conn.execute("DELETE FROM eve_state_cache WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_api_cache WHERE cache_key LIKE ?", (f"{int(user_id)}:%",))
            conn.execute("DELETE FROM eve_watch WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_alerts WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_structures WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_assets WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_orders WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_wallet WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_pi WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_skillqueue WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM eve_industry WHERE user_id=?", (int(user_id),))

            conn.execute("DELETE FROM eve_auth WHERE user_id=?", (int(user_id),))
            row = conn.execute("SELECT changes()" ).fetchone()
        return int(row[0] if row and row[0] is not None else 0)

    def eve_list_characters(self, user_id: int) -> List[dict]:
        """List all linked EVE characters for a user."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT character_id, character_name, scopes, alias, is_default, updated_ts
                FROM eve_characters
                WHERE user_id=?
                ORDER BY is_default DESC, updated_ts DESC
                """,
                (int(user_id),),
            ).fetchall()
        out: List[dict] = []
        for r in rows:
            out.append({
                "character_id": int(r[0]),
                "character_name": str(r[1]),
                "scopes": str(r[2]),
                "alias": (str(r[3]) if r[3] is not None else None),
                "is_default": int(r[4]) == 1,
                "updated_ts": float(r[5]),
            })
        return out

    def eve_get_character_by_alias(self, user_id: int, alias: str) -> Optional[dict]:
        a = (alias or "").strip()
        if not a:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT character_id, character_name, refresh_token, scopes, alias, is_default, updated_ts
                FROM eve_characters
                WHERE user_id=? AND (
                    lower(alias)=lower(?) OR lower(character_name)=lower(?)
                )
                LIMIT 1
                """,
                (int(user_id), str(a), str(a)),
            ).fetchone()
        if not row:
            return None
        return {
            "user_id": int(user_id),
            "character_id": int(row[0]),
            "character_name": str(row[1]),
            "refresh_token": str(row[2]),
            "scopes": str(row[3]),
            "alias": (str(row[4]) if row[4] is not None else None),
            "is_default": int(row[5]) == 1,
            "updated_ts": float(row[6]),
        }

    def eve_set_default_character(self, user_id: int, character_id: int) -> bool:
        """Mark a character as the default for this user."""
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM eve_characters WHERE user_id=? AND character_id=? LIMIT 1",
                (int(user_id), int(character_id)),
            ).fetchone()
            if not exists:
                return False
            conn.execute("UPDATE eve_characters SET is_default=0 WHERE user_id=?", (int(user_id),))
            conn.execute(
                "UPDATE eve_characters SET is_default=1 WHERE user_id=? AND character_id=?",
                (int(user_id), int(character_id)),
            )

            # Keep legacy default in sync
            drow = conn.execute(
                "SELECT character_id, character_name, refresh_token, scopes, updated_ts FROM eve_characters WHERE user_id=? AND is_default=1 LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if drow:
                conn.execute(
                    """
                    INSERT INTO eve_auth(user_id, character_id, character_name, refresh_token, scopes, updated_ts)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        character_id=excluded.character_id,
                        character_name=excluded.character_name,
                        refresh_token=excluded.refresh_token,
                        scopes=excluded.scopes,
                        updated_ts=excluded.updated_ts
                    """,
                    (int(user_id), int(drow[0]), str(drow[1]), str(drow[2]), str(drow[3]), float(drow[4])),
                )
        return True

    def eve_get_watch(self, user_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT notify_via_dm, notify_channel_id, watch_skills, watch_industry, updated_ts FROM eve_watch WHERE user_id=? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "notify_via_dm": int(row[0]),
            "notify_channel_id": (int(row[1]) if row[1] is not None else None),
            "watch_skills": int(row[2]),
            "watch_industry": int(row[3]),
            "updated_ts": float(row[4]),
        }

    def eve_set_watch(
        self,
        *,
        user_id: int,
        notify_via_dm: bool = True,
        notify_channel_id: int | None = None,
        watch_skills: bool = True,
        watch_industry: bool = True,
    ) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO eve_watch(user_id, notify_via_dm, notify_channel_id, watch_skills, watch_industry, updated_ts)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    notify_via_dm=excluded.notify_via_dm,
                    notify_channel_id=excluded.notify_channel_id,
                    watch_skills=excluded.watch_skills,
                    watch_industry=excluded.watch_industry,
                    updated_ts=excluded.updated_ts
                """,
                (
                    int(user_id),
                    1 if notify_via_dm else 0,
                    int(notify_channel_id) if notify_channel_id is not None else None,
                    1 if watch_skills else 0,
                    1 if watch_industry else 0,
                    float(now),
                ),
            )

            # Keep legacy table in sync with the current default so older code paths keep working.
            drow = conn.execute(
                "SELECT character_id, character_name, refresh_token, scopes, updated_ts FROM eve_characters WHERE user_id=? AND is_default=1 LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if drow:
                conn.execute(
                    """
                    INSERT INTO eve_auth(user_id, character_id, character_name, refresh_token, scopes, updated_ts)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        character_id=excluded.character_id,
                        character_name=excluded.character_name,
                        refresh_token=excluded.refresh_token,
                        scopes=excluded.scopes,
                        updated_ts=excluded.updated_ts
                    """,
                    (
                        int(user_id),
                        int(drow[0]),
                        str(drow[1]),
                        str(drow[2]),
                        str(drow[3]),
                        float(drow[4]),
                    ),
                )

    def eve_alert_add(
        self,
        *,
        user_id: int,
        alert_type: str,
        params: dict,
        enabled: bool = True,
        notify_via_dm: bool = True,
        notify_channel_id: int | None = None,
    ) -> int:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO eve_alerts(user_id, alert_type, params_json, enabled, notify_via_dm, notify_channel_id, last_fire_ts, created_ts)
                VALUES(?,?,?,?,?,?,NULL,?)
                """,
                (
                    int(user_id),
                    str(alert_type),
                    json.dumps(params or {}),
                    1 if enabled else 0,
                    1 if notify_via_dm else 0,
                    int(notify_channel_id) if notify_channel_id is not None else None,
                    float(now),
                ),
            )
            return int(cur.lastrowid or 0)

    def eve_alert_list(self, user_id: int) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT id, alert_type, params_json, enabled, notify_via_dm, notify_channel_id, last_fire_ts, created_ts
                FROM eve_alerts
                WHERE user_id=?
                ORDER BY id DESC
                """,
                (int(user_id),),
            ).fetchall()
        out=[]
        for r in rows:
            try:
                params=json.loads(r[2]) if r[2] else {}
            except Exception:
                params={}
            out.append({
                "id": int(r[0]),
                "alert_type": str(r[1]),
                "params": params,
                "enabled": int(r[3] or 0),
                "notify_via_dm": int(r[4] or 0),
                "notify_channel_id": r[5],
                "last_fire_ts": r[6],
                "created_ts": r[7],
            })
        return out

    def eve_alert_delete(self, user_id: int, alert_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM eve_alerts WHERE user_id=? AND id=?", (int(user_id), int(alert_id)))
            return cur.rowcount > 0

    def eve_alert_set_enabled(self, user_id: int, alert_id: int, enabled: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE eve_alerts SET enabled=? WHERE user_id=? AND id=?",
                (1 if enabled else 0, int(user_id), int(alert_id)),
            )
            return cur.rowcount > 0

    def eve_alert_touch_fire(self, user_id: int, alert_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE eve_alerts SET last_fire_ts=? WHERE user_id=? AND id=?",
                (float(time.time()), int(user_id), int(alert_id)),
            )

    # ----------------------------
    # Short-term message memory
    # ----------------------------
    def add_message(self, channel_id: int, role: str, content: str, author_id: int | None = None, **kwargs) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO memory(channel_id, role, content, ts) VALUES(?,?,?,?)",
                (int(channel_id), str(role), str(content), float(now)),
            )
        self.clear_channel_keep_last(channel_id, self.keep_last)

    def get_messages(self, channel_id: int, limit: int = 24) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content, ts FROM memory WHERE channel_id = ? ORDER BY ts DESC LIMIT ?",
                (int(channel_id), int(limit)),
            ).fetchall()
        # return oldest->newest
        out = [dict(r) for r in rows]
        out.reverse()
        return out

    def clear_channel_keep_last(self, channel_id: int, keep_last: int = 12) -> None:
        keep_last = max(0, int(keep_last))
        if keep_last == 0:
            with self._conn() as conn:
                conn.execute("DELETE FROM memory WHERE channel_id = ?", (int(channel_id),))
            return
        with self._conn() as conn:
            conn.execute("""
                DELETE FROM memory
                WHERE channel_id = ?
                  AND ts NOT IN (
                    SELECT ts FROM memory
                    WHERE channel_id = ?
                    ORDER BY ts DESC
                    LIMIT ?
                  )
                """,
                (int(channel_id), int(channel_id), int(keep_last)),
            )

    def clear_channel(self, channel_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM memory WHERE channel_id = ?", (int(channel_id),))

    # ----------------------------
    # Channel summaries
    # ----------------------------
    def set_summary(self, channel_id: int, summary: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO summaries(channel_id, summary, updated_ts) VALUES(?,?,?) "
                "ON CONFLICT(channel_id) DO UPDATE SET summary=excluded.summary, updated_ts=excluded.updated_ts",
                (int(channel_id), str(summary), float(now)),
            )

    def get_summary(self, channel_id: int) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT summary FROM summaries WHERE channel_id = ?",
                (int(channel_id),),
            ).fetchone()
        return str(row["summary"]) if row else None

    def clear_summary(self, channel_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM summaries WHERE channel_id = ?", (int(channel_id),))

    def get_summary_meta(self, channel_id: int) -> Tuple[Optional[str], Optional[float]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT summary, updated_ts FROM summaries WHERE channel_id = ?",
                (int(channel_id),),
            ).fetchone()
        if not row:
            return None, None
        return str(row["summary"]), float(row["updated_ts"])

    # ----------------------------
    # Reminders
    # ----------------------------
    def add_reminder(self, guild_id: int, channel_id: int, user_id: int, due_ts: float, message: str, target: str) -> int:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO reminders(guild_id, channel_id, user_id, due_ts, message, target, created_ts) VALUES (?,?,?,?,?,?,?)",
                (int(guild_id), int(channel_id), int(user_id), float(due_ts), str(message), str(target), float(now)),
            )
            return int(cur.lastrowid)

    def add_recurring_reminder(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        due_ts: float,
        message: str,
        target: str,
        repeat: str,
    ) -> int:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO reminders(guild_id, channel_id, user_id, due_ts, message, target, created_ts, repeat) VALUES (?,?,?,?,?,?,?,?)",
                (int(guild_id), int(channel_id), int(user_id), float(due_ts), str(message), str(target), float(now), str(repeat)),
            )
            return int(cur.lastrowid)

    def get_due_reminders(self, now_ts: float | None = None, limit: int = 20) -> List[Dict]:
        now_ts = float(now_ts if now_ts is not None else time.time())
        with self._conn() as conn:
            try:
                # New schema (recurring reminders supported)
                rows = conn.execute(
                    "SELECT id, guild_id, channel_id, user_id, due_ts, message, target, repeat "
                    "FROM reminders WHERE (sent_ts IS NULL OR repeat IS NOT NULL) AND due_ts <= ? ORDER BY due_ts ASC LIMIT ?",
                    (now_ts, int(limit)),
                ).fetchall()
            except sqlite3.OperationalError as e:
                # Backward-compatible fallback for older DBs that haven't been migrated yet.
                # (e.g., existing ./data/memory.db volume from before recurring support)
                if "no such column: repeat" in str(e).lower():
                    # Try migrating in-place, then fall back to one-shot reminders only.
                    try:
                        self._migrate_schema(conn)
                    except Exception:
                        pass
                    rows = conn.execute(
                        "SELECT id, guild_id, channel_id, user_id, due_ts, message, target "
                        "FROM reminders WHERE sent_ts IS NULL AND due_ts <= ? ORDER BY due_ts ASC LIMIT ?",
                        (now_ts, int(limit)),
                    ).fetchall()
                else:
                    raise
        return [dict(r) for r in rows]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        """Mark a one-shot reminder as delivered."""
        with self._conn() as conn:
            conn.execute("UPDATE reminders SET sent_ts = ? WHERE id = ?", (float(time.time()), int(reminder_id)))

    def reschedule_recurring(self, reminder_id: int, next_due_ts: float) -> None:
        """Advance a recurring reminder to its next due time."""
        now = float(time.time())
        with self._conn() as conn:
            conn.execute(
                "UPDATE reminders SET due_ts = ?, last_fire_ts = ?, sent_ts = NULL WHERE id = ?",
                (float(next_due_ts), now, int(reminder_id)),
            )

    def cancel_pending_reminders(self, user_id: int, channel_id: int | None = None) -> int:
        """Cancel (delete) any pending reminders for a user.

        We only remove reminders that have not been sent yet (sent_ts IS NULL).
        By default, we scope cancellation to the current channel if channel_id is provided.

        Returns the number of rows removed.
        """
        uid = int(user_id)
        with self._conn() as conn:
            if channel_id is None:
                cur = conn.execute("DELETE FROM reminders WHERE user_id = ? AND sent_ts IS NULL", (uid,))
            else:
                cur = conn.execute(
                    "DELETE FROM reminders WHERE user_id = ? AND channel_id = ? AND sent_ts IS NULL",
                    (uid, int(channel_id)),
                )
            return int(cur.rowcount or 0)

    def cancel_reminder_by_id(self, reminder_id: int, *, user_id: int | None = None, channel_id: int | None = None) -> int:
        """Cancel a specific reminder by id.

        If user_id and/or channel_id are provided, we only cancel the reminder if it matches.
        Returns number of rows removed (0 or 1).
        """
        rid = int(reminder_id)
        clauses = ["id = ?"]
        params: list = [rid]
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(int(user_id))
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(int(channel_id))
        where = " AND ".join(clauses)
        with self._conn() as conn:
            cur = conn.execute(f"DELETE FROM reminders WHERE {where}", tuple(params))
            return int(cur.rowcount or 0)

    def cancel_last_active_reminder(self, *, user_id: int, channel_id: int) -> int:
        """Cancel the most recently created *active* reminder for a user in a channel.

        Active means:
        - one-shot: sent_ts IS NULL
        - recurring: repeat IS NOT NULL

        Returns number of rows removed (0 or 1).
        """
        uid = int(user_id)
        cid = int(channel_id)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM reminders WHERE user_id = ? AND channel_id = ? AND (sent_ts IS NULL OR repeat IS NOT NULL) ORDER BY created_ts DESC LIMIT 1",
                (uid, cid),
            ).fetchone()
            if not row:
                return 0
            rid = int(row["id"])
            cur = conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
            return int(cur.rowcount or 0)

    # ----------------------------
    # Bot profile (global)
    # ----------------------------
    def get_bot_profile(self) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT profile FROM bot_profile WHERE id = 1").fetchone()
        return str(row["profile"]) if row else ""

    def set_bot_profile(self, profile: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bot_profile(id, profile, updated_ts) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET profile=excluded.profile, updated_ts=excluded.updated_ts",
                (str(profile), float(now)),
            )

    def append_bot_profile(self, extra: str) -> None:
        cur = (self.get_bot_profile() or "").strip()
        extra = (extra or "").strip()
        if not extra:
            return
        joined = (cur + "\n" + extra).strip() if cur else extra
        self.set_bot_profile(joined)

    # ----------------------------
    # User profile + preferences (cross-channel)
    # ----------------------------
    def get_user_profile(self, user_id: int) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT summary FROM user_profile WHERE user_id = ?", (int(user_id),)).fetchone()
        return str(row["summary"]) if row else ""

    def upsert_user_profile(self, user_id: int, summary: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO user_profile(user_id, summary, updated_ts) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary, updated_ts=excluded.updated_ts",
                (int(user_id), str(summary), float(now)),
            )

    def append_user_profile(self, user_id: int, extra: str) -> None:
        cur = (self.get_user_profile(user_id) or "").strip()
        extra = (extra or "").strip()
        if not extra:
            return
        joined = (cur + "\n" + extra).strip() if cur else extra
        self.upsert_user_profile(user_id, joined)

    # clear_user_context is implemented earlier in the file
    def get_user_pref(self, user_id: int, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE user_id = ? AND key = ?",
                (int(user_id), str(key)),
            ).fetchone()
        return str(row["value"]) if row and row["value"] is not None else None

    def incr_user_pref_int(self, user_id: int, key: str, delta: int = 1) -> int:
        """Atomically increment an integer-valued user_pref and return the new value."""
        now = time.time()
        k = str(key)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE user_id = ? AND key = ?",
                (int(user_id), k),
            ).fetchone()
            try:
                cur = int(row["value"]) if row and row["value"] is not None else 0
            except Exception:
                cur = 0
            newv = int(cur) + int(delta)
            conn.execute(
                "INSERT INTO user_prefs(user_id, key, value, updated_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (int(user_id), k, str(newv), float(now)),
            )
        return int(newv)

    def set_user_pref(self, user_id: int, key: str, value: str) -> None:
            now = time.time()
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO user_prefs(user_id, key, value, updated_ts) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                    (int(user_id), str(key), str(value), float(now)),
                )

    def clear_user_pref(self, user_id: int, key: str) -> None:
            with self._conn() as conn:
                conn.execute("DELETE FROM user_prefs WHERE user_id = ? AND key = ?", (int(user_id), str(key)))

    def list_user_prefs(self, user_id: int) -> List[Dict[str, str]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT key, value, updated_ts FROM user_prefs WHERE user_id = ? ORDER BY key ASC",
                    (int(user_id),),
                ).fetchall()
            return [dict(r) for r in rows]

        # Channel notes helpers are implemented earlier in the file


        # ----------------------------
        # ESI name cache helpers
        # ----------------------------
    def esi_cache_get(self, kind: str, ids: List[int]) -> Dict[int, str]:
            """Return cached names for ids that are not expired."""
            kind = str(kind)
            ids = [int(x) for x in (ids or []) if x is not None]
            if not ids:
                return {}
            now = time.time()
            q = (
                "SELECT id, name, expires_ts FROM esi_names_cache "
                "WHERE kind = ? AND id IN (%s)" % (" ,".join(["?"] * len(ids)))
            )
            params = [kind] + ids
            out: Dict[int, str] = {}
            with self._conn() as conn:
                rows = conn.execute(q, params).fetchall()
            for r in rows:
                exp = r["expires_ts"]
                if exp is not None and float(exp) < now:
                    continue
                out[int(r["id"]) ] = str(r["name"]) 
            return out

    def esi_cache_set(self, kind: str, mapping: Dict[int, str], ttl_seconds: Optional[int] = None) -> None:
            """Upsert cached names with optional TTL."""
            kind = str(kind)
            if not mapping:
                return
            now = time.time()
            exp = float(now + int(ttl_seconds)) if ttl_seconds else None
            rows = [(kind, int(i), str(n), exp, float(now)) for i, n in mapping.items()]
            with self._conn() as conn:
                conn.executemany(
                    "INSERT INTO esi_names_cache(kind,id,name,expires_ts,updated_ts) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(kind,id) DO UPDATE SET name=excluded.name, expires_ts=excluded.expires_ts, updated_ts=excluded.updated_ts",
                    rows,
                )

        # ---- Compatibility shims (added for agent loops / legacy callers) ----
    def list_reminders(self, *, guild_id: int, user_id: int, limit: int = 25) -> List[dict]:
            """List upcoming (unsent) reminders for a user."""
            with self._conn() as conn:
                rows = conn.execute(
                    """
                    SELECT id, channel_id, due_ts, message, target, created_ts, repeat
                    FROM reminders
                    WHERE guild_id=? AND user_id=? AND sent_ts IS NULL
                    ORDER BY due_ts ASC
                    LIMIT ?
                    """,
                    (int(guild_id), int(user_id), int(limit)),
                ).fetchall()
            out=[]
            for r in rows:
                out.append({
                    "id": int(r[0]),
                    "channel_id": int(r[1]),
                    "due_ts": float(r[2]),
                    "message": str(r[3]),
                    "target": str(r[4]),
                    "created_ts": float(r[5]),
                    "repeat": r[6],
                })
            return out

    def eve_watch_add(
            self,
            *,
            user_id: int,
            notify_via_dm: bool = True,
            notify_channel_id: int | None = None,
            watch_skills: bool = True,
            watch_industry: bool = True,
        ) -> None:
            """Legacy convenience wrapper for enabling EVE watch flags."""
            self.eve_set_watch(
                user_id=int(user_id),
                notify_via_dm=bool(notify_via_dm),
                notify_channel_id=int(notify_channel_id) if notify_channel_id is not None else None,
                watch_skills=bool(watch_skills),
                watch_industry=bool(watch_industry),
            )

    def eve_watch_remove(self, user_id: int) -> None:
            """Legacy convenience wrapper for disabling/removing EVE watch."""
            with self._conn() as conn:
                conn.execute("DELETE FROM eve_watch WHERE user_id=?", (int(user_id),))


        # r100: Saved fittings

    def fitting_save(self, name: str, ship_type_id: int, ship_name: str,
                         fit_data: str, eft_string: str, saved_by_name: str) -> int:
            """Save a named fitting. Returns the new row id."""
            with self._conn() as conn:
                cur = conn.execute(
                    """INSERT INTO saved_fittings
                       (name, ship_type_id, ship_name, fit_data, eft_string, saved_by_name)
                       VALUES (?,?,?,?,?,?)""",
                    (str(name), int(ship_type_id or 0), str(ship_name or ""),
                     str(fit_data), str(eft_string or ""), str(saved_by_name or ""))
                )
                return cur.lastrowid

    def fitting_list(self) -> list:
            """Return all saved fittings, newest first."""
            with self._conn() as conn:
                rows = conn.execute(
                    """SELECT id, name, ship_type_id, ship_name, saved_by_name, saved_ts
                       FROM saved_fittings ORDER BY saved_ts DESC"""
                ).fetchall()
                return [dict(r) for r in rows]

    def fitting_get(self, fit_id: int) -> dict | None:
            """Return a single saved fitting by id."""
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM saved_fittings WHERE id=?", (int(fit_id),)
                ).fetchone()
                return dict(row) if row else None

    def fitting_delete(self, fit_id: int) -> bool:
            """Delete a saved fitting. Returns True if a row was deleted."""
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM saved_fittings WHERE id=?", (int(fit_id),)
                )
                return cur.rowcount > 0
    # ── r140: Manufacturing Calculator Presets ─────────────────────────────────

    def mfg_preset_save(self, data: dict) -> int:
        """Save a manufacturing preset. Returns new row id."""
        import json
        self._ensure_mfg_tables()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO manufacturing_presets
                   (preset_name, bp_type_id, bp_name, runs, me, te, activity,
                    facility_name, facility_tax, scc_surcharge, system_name,
                    cost_index, mat_hub, out_hub, extra_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(data.get("preset_name","Preset")),
                 int(data.get("bp_type_id",0)),
                 str(data.get("bp_name","")),
                 int(data.get("runs",1)),
                 int(data.get("me",0)),
                 int(data.get("te",0)),
                 int(data.get("activity",1)),
                 str(data.get("facility_name","")),
                 float(data.get("facility_tax",0.0)),
                 float(data.get("scc_surcharge",4.0)),
                 str(data.get("system_name","")),
                 float(data.get("cost_index",0.0)),
                 str(data.get("mat_hub","Jita")),
                 str(data.get("out_hub","Jita")),
                 json.dumps(data.get("extra",{})))
            )
            return cur.lastrowid

    def _ensure_mfg_tables(self) -> None:
        """Migration: create manufacturing tables if they don't exist (for DBs pre-r140)."""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS manufacturing_presets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preset_name TEXT NOT NULL,
                    bp_type_id INTEGER NOT NULL,
                    bp_name TEXT NOT NULL DEFAULT '',
                    runs INTEGER NOT NULL DEFAULT 1,
                    me INTEGER NOT NULL DEFAULT 0,
                    te INTEGER NOT NULL DEFAULT 0,
                    activity INTEGER NOT NULL DEFAULT 1,
                    facility_name TEXT NOT NULL DEFAULT '',
                    facility_tax REAL NOT NULL DEFAULT 0.0,
                    scc_surcharge REAL NOT NULL DEFAULT 4.0,
                    system_name TEXT NOT NULL DEFAULT '',
                    cost_index REAL NOT NULL DEFAULT 0.0,
                    mat_hub TEXT NOT NULL DEFAULT 'Jita',
                    out_hub TEXT NOT NULL DEFAULT 'Jita',
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    saved_ts DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS manufacturing_global_defaults (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS map_systems (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    map_id       TEXT    NOT NULL DEFAULT 'corp',
                    system_id    INTEGER NOT NULL,
                    system_name  TEXT    NOT NULL,
                    x_pos        REAL    NOT NULL DEFAULT 100,
                    y_pos        REAL    NOT NULL DEFAULT 100,
                    temp_name    TEXT,
                    locked       INTEGER NOT NULL DEFAULT 0,
                    color        TEXT,
                    sec_status   REAL,
                    region_name  TEXT,
                    is_wh        INTEGER NOT NULL DEFAULT 0,
                    added_by     TEXT,
                    added_ts     REAL    NOT NULL DEFAULT (unixepoch()),
                    UNIQUE(map_id, system_id)
                )""")
            # Migration: add new columns if table already exists
            for col, defn in [('sec_status','REAL'), ('region_name','TEXT'), ('is_wh','INTEGER DEFAULT 0'), ('tag','TEXT')]:
                try:
                    conn.execute(f"ALTER TABLE map_systems ADD COLUMN {col} {defn}")
                except Exception:
                    pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS map_connections (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    map_id          TEXT    NOT NULL DEFAULT 'corp',
                    from_sys_id     INTEGER NOT NULL,
                    to_sys_id       INTEGER NOT NULL,
                    wh_type         TEXT,
                    mass_status     TEXT    NOT NULL DEFAULT 'stable',
                    time_status     TEXT    NOT NULL DEFAULT 'fresh',
                    eol_ts          REAL,
                    save_mass       INTEGER NOT NULL DEFAULT 0,
                    mass_kg_passed  REAL    NOT NULL DEFAULT 0,
                    ship_count      INTEGER NOT NULL DEFAULT 0,
                    sig_id_from     TEXT,
                    sig_id_to       TEXT,
                    created_by      TEXT,
                    created_ts      REAL    NOT NULL DEFAULT (unixepoch()),
                    UNIQUE(map_id, from_sys_id, to_sys_id)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS map_structures (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    map_id      TEXT    NOT NULL DEFAULT 'corp',
                    system_id   INTEGER NOT NULL,
                    system_name TEXT,
                    name        TEXT    NOT NULL,
                    struct_type TEXT,
                    owner       TEXT,
                    status      TEXT    NOT NULL DEFAULT 'active',
                    notes       TEXT,
                    added_by    TEXT,
                    added_ts    REAL    NOT NULL DEFAULT (unixepoch())
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS map_routes (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    map_id      TEXT    NOT NULL DEFAULT 'corp',
                    name        TEXT    NOT NULL,
                    system_name TEXT    NOT NULL,
                    system_id   INTEGER,
                    added_by    TEXT,
                    added_ts    REAL    NOT NULL DEFAULT (unixepoch())
                )""")


    def mfg_preset_list(self) -> list:
        """Return all manufacturing presets, newest first."""
        self._ensure_mfg_tables()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, preset_name, bp_type_id, bp_name, runs, me, te,
                          activity, facility_name, system_name, mat_hub, out_hub, saved_ts
                   FROM manufacturing_presets ORDER BY saved_ts DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def mfg_preset_get(self, preset_id: int) -> dict | None:
        """Return a single preset by id."""
        import json
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM manufacturing_presets WHERE id=?", (int(preset_id),)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["extra"] = json.loads(d.get("extra_json") or "{}")
            except Exception:
                d["extra"] = {}
            return d

    def mfg_preset_delete(self, preset_id: int) -> bool:
        """Delete a preset. Returns True if deleted."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM manufacturing_presets WHERE id=?", (int(preset_id),)
            )
            return cur.rowcount > 0

    def mfg_global_get(self) -> dict:
        """Return all global manufacturing defaults as a dict."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM manufacturing_global_defaults"
            ).fetchall()
            return {r["key"]: r["value"] for r in rows}

    def mfg_global_set(self, key: str, value: str) -> None:
        """Upsert a global default key."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO manufacturing_global_defaults (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(key), str(value))
            )

