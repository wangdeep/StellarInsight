from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

import aiohttp

# esi_scopes module provides live CCP ESI swagger cache — not needed for PKCE desktop app.
# Provide stubs so get_sso_scopes() works without network calls at import time.
def all_valid_scopes() -> str:
    return ""

def filter_to_valid(sc: str) -> str:
    return sc


try:
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover
    Fernet = None


EVE_AUTH_URL    = "https://login.eveonline.com/v2/oauth/authorize/"
EVE_TOKEN_URL   = "https://login.eveonline.com/v2/oauth/token"
EVE_VERIFY_URL  = "https://login.eveonline.com/oauth/verify"   # deprecated — kept for fallback
EVE_JWKS_URL    = "https://login.eveonline.com/oauth/jwks"
EVE_ISSUER      = "login.eveonline.com"

# M-7: db path for auto-generating / persisting the Fernet key.
# Set via set_token_key_db() called from app startup.
_token_key_db_path: Optional[str] = None

def set_token_key_db(db_path: str) -> None:
    """Tell eve_sso where to persist the auto-generated Fernet key (M-7)."""
    global _token_key_db_path
    _token_key_db_path = db_path

# L-4: in-memory JWKS cache  {keys: [...], fetched_at: float}
_jwks_cache: Dict[str, Any] = {}
_JWKS_CACHE_TTL = 86400  # 24 hours


"""EVE SSO helpers.

Scope strategy:
  - min: minimal character scopes for core status + wallet/orders/skills/industry/location
  - max: expanded character scopes for "show me everything" (adds PI, mail, clones, etc.)
  - max_corp: max + corporation scopes (requires character roles)

Env overrides:
  - EVE_SSO_SCOPES (legacy): overrides the min scope set
  - EVE_SSO_SCOPES_ALL (legacy): overrides the max scope set
  - EVE_SSO_SCOPES_MAX: overrides the max scope set
  - EVE_SSO_SCOPES_MAX_CORP: overrides the max_corp scope set

Important:
  - Do NOT include the legacy "publicData" scope.
"""


DEFAULT_SCOPES_MIN = " ".join(
    [
        "esi-location.read_location.v1",
        "esi-location.read_ship_type.v1",
        "esi-location.read_online.v1",
        "esi-wallet.read_character_wallet.v1",
        "esi-markets.read_character_orders.v1",
        "esi-skills.read_skillqueue.v1",
        "esi-industry.read_character_jobs.v1",
    ]
).strip()


# Expanded character scopes. Contains every character-level ESI scope.
DEFAULT_SCOPES_MAX = " ".join(
    [
        # Presence / location
        "esi-location.read_location.v1",
        "esi-location.read_ship_type.v1",
        "esi-location.read_online.v1",

        # Economy / markets
        "esi-wallet.read_character_wallet.v1",
        "esi-markets.read_character_orders.v1",
        "esi-markets.structure_markets.v1",

        # Skills / industry
        "esi-skills.read_skillqueue.v1",
        "esi-skills.read_skills.v1",
        "esi-industry.read_character_jobs.v1",
        "esi-industry.read_character_mining.v1",

        # Assets
        "esi-assets.read_assets.v1",

        # Planetary Industry (PI)
        "esi-planets.manage_planets.v1",

        # Mail
        "esi-mail.read_mail.v1",
        "esi-mail.organize_mail.v1",
        "esi-mail.send_mail.v1",

        # Contacts
        "esi-characters.read_contacts.v1",
        "esi-characters.write_contacts.v1",

        # Clones / implants
        "esi-clones.read_clones.v1",
        "esi-clones.read_implants.v1",

        # Contracts
        "esi-contracts.read_character_contracts.v1",

        # Fittings
        "esi-fittings.read_fittings.v1",
        "esi-fittings.write_fittings.v1",

        # Fleets
        "esi-fleets.read_fleet.v1",
        "esi-fleets.write_fleet.v1",

        # Killmails
        "esi-killmails.read_killmails.v1",

        # Bookmarks NOTE:
        # CCP has deprecated/removed the old bookmark endpoints; the related
        # bookmark scopes have been reported as invalid/useless and may cause
        # SSO invalid_scope errors on login. Keep bookmarks out of the default
        # scope packs. If CCP reintroduces bookmark endpoints/scopes, you can
        # add them back via EVE_SSO_SCOPES_MAX / EVE_SSO_SCOPES_MAX_CORP.

        # Calendar
        "esi-calendar.read_calendar_events.v1",
        "esi-calendar.respond_calendar_events.v1",

        # Character info & standings
        "esi-characters.read_agents_research.v1",
        "esi-characters.read_blueprints.v1",
        "esi-characters.read_corporation_roles.v1",
        "esi-characters.read_fatigue.v1",
        "esi-characters.read_fw_stats.v1",
        "esi-characters.read_loyalty.v1",
        "esi-characters.read_medals.v1",
        "esi-characters.read_notifications.v1",
        "esi-characters.read_standings.v1",
        "esi-characters.read_titles.v1",

        # Structure search (for resolving structure IDs in some contexts)
        "esi-search.search_structures.v1",

        # Resolve Upwell structure names via ESI /universe/structures/{id}/
        # Required to turn large (int64) location/facility IDs into readable names.
        "esi-universe.read_structures.v1",

        # UI helpers
        "esi-ui.open_window.v1",
        "esi-ui.write_waypoint.v1",
    ]
).strip()



# Scopes that the bot relies on for core corp features. These are always enforced for corp modes.
ESSENTIAL_CORP_SCOPES = [
    "esi-wallet.read_corporation_wallets.v1",
    "esi-corporations.read_structures.v1",
    "esi-assets.read_corporation_assets.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-markets.read_corporation_orders.v1",
]
DEFAULT_SCOPES_MAX_CORP = " ".join(
    [
        DEFAULT_SCOPES_MAX,

        # ── Alliance ──────────────────────────────────────────────────────────
        "esi-alliances.read_contacts.v1",

        # ── Calendar ──────────────────────────────────────────────────────────
        "esi-calendar.respond_calendar_events.v1",

        # ── Character extras (missing from MAX) ───────────────────────────────
        "esi-characters.read_loyalty.v1",
        "esi-characters.read_medals.v1",
        "esi-characters.read_notifications.v1",
        "esi-characters.read_standings.v1",
        "esi-characters.read_titles.v1",

        # ── Corporation – membership & roles ──────────────────────────────────
        "esi-corporations.read_corporation_membership.v1",  # members, roles, role history
        "esi-corporations.track_members.v1",                # membertracking, member limit

        # ── Corporation – assets & industry ───────────────────────────────────
        "esi-assets.read_corporation_assets.v1",
        "esi-industry.read_corporation_jobs.v1",
        "esi-industry.read_character_mining.v1",
        "esi-industry.read_corporation_mining.v1",          # moon mining, extractions, observers

        # ── Corporation – markets & wallet ────────────────────────────────────
        "esi-markets.read_corporation_orders.v1",
        "esi-wallet.read_corporation_wallets.v1",           # wallets, journal, transactions, shareholders

        # ── Corporation – contracts & killmails ───────────────────────────────
        "esi-contracts.read_corporation_contracts.v1",
        "esi-killmails.read_corporation_killmails.v1",

        # ── Corporation – structures & facilities ─────────────────────────────
        "esi-corporations.read_structures.v1",              # Upwell structures
        "esi-corporations.read_starbases.v1",               # POSes / starbases
        "esi-corporations.read_facilities.v1",              # factory/refinery facilities
        "esi-planets.read_customs_offices.v1",              # PI customs offices

        # ── Corporation – contacts, standings & diplomacy ─────────────────────
        "esi-corporations.read_contacts.v1",
        "esi-corporations.read_standings.v1",

        # ── Corporation – internal admin ──────────────────────────────────────
        "esi-corporations.read_blueprints.v1",
        "esi-corporations.read_container_logs.v1",          # audit log for containers
        "esi-corporations.read_divisions.v1",               # wallet/hangar division names
        "esi-corporations.read_fw_stats.v1",                # factional warfare stats
        "esi-corporations.read_medals.v1",                  # medals issued to members
        "esi-corporations.read_titles.v1",                  # member titles

        # NOTE: bookmark scopes intentionally omitted – CCP removed the
        # bookmark endpoints; requesting the scopes triggers invalid_scope errors.
    ]
).strip()


DEFAULT_SCOPES_ALL_FEATURES = "esi-calendar.respond_calendar_events.v1 esi-calendar.read_calendar_events.v1 esi-location.read_location.v1 esi-location.read_ship_type.v1 esi-mail.organize_mail.v1 esi-mail.read_mail.v1 esi-mail.send_mail.v1 esi-skills.read_skills.v1 esi-skills.read_skillqueue.v1 esi-wallet.read_character_wallet.v1 esi-wallet.read_corporation_wallet.v1 esi-search.search_structures.v1 esi-clones.read_clones.v1 esi-characters.read_contacts.v1 esi-universe.read_structures.v1 esi-killmails.read_killmails.v1 esi-corporations.read_corporation_membership.v1 esi-assets.read_assets.v1 esi-planets.manage_planets.v1 esi-fleets.read_fleet.v1 esi-fleets.write_fleet.v1 esi-ui.open_window.v1 esi-ui.write_waypoint.v1 esi-characters.write_contacts.v1 esi-fittings.read_fittings.v1 esi-fittings.write_fittings.v1 esi-markets.structure_markets.v1 esi-corporations.read_structures.v1 esi-characters.read_loyalty.v1 esi-characters.read_chat_channels.v1 esi-characters.read_medals.v1 esi-characters.read_standings.v1 esi-characters.read_agents_research.v1 esi-industry.read_character_jobs.v1 esi-markets.read_character_orders.v1 esi-characters.read_blueprints.v1 esi-characters.read_corporation_roles.v1 esi-location.read_online.v1 esi-contracts.read_character_contracts.v1 esi-clones.read_implants.v1 esi-characters.read_fatigue.v1 esi-killmails.read_corporation_killmails.v1 esi-corporations.track_members.v1 esi-wallet.read_corporation_wallets.v1 esi-characters.read_notifications.v1 esi-corporations.read_divisions.v1 esi-corporations.read_contacts.v1 esi-assets.read_corporation_assets.v1 esi-corporations.read_titles.v1 esi-corporations.read_blueprints.v1 esi-contracts.read_corporation_contracts.v1 esi-corporations.read_standings.v1 esi-corporations.read_starbases.v1 esi-industry.read_corporation_jobs.v1 esi-markets.read_corporation_orders.v1 esi-corporations.read_container_logs.v1 esi-industry.read_character_mining.v1 esi-industry.read_corporation_mining.v1 esi-planets.read_customs_offices.v1 esi-corporations.read_facilities.v1 esi-corporations.read_medals.v1 esi-characters.read_titles.v1 esi-alliances.read_contacts.v1 esi-characters.read_fw_stats.v1 esi-corporations.read_fw_stats.v1 esi-corporations.read_projects.v1 esi-corporations.read_freelance_jobs.v1 esi-characters.read_freelance_jobs.v1"




def intersect_scopes(preferred: str, allowed: str) -> str:
    """Return scopes in `preferred` that are present in `allowed` (space-separated strings)."""
    pref = [s for s in preferred.split() if s]
    allow_set = set([s for s in allowed.split() if s])
    out = [s for s in pref if s in allow_set]
    return " ".join(out).strip()

def normalize_scopes(scopes: str) -> str:
    """Normalize an EVE SSO scopes string.

    Accepts space-separated scopes (preferred). Also tolerates values copied
    accidentally with a leading env key (e.g. 'EVE_SSO_SCOPES=...') and values
    containing '+' or commas.
    """
    s = (scopes or "").strip().strip('"').strip("'")
    if not s:
        return ""

    # If someone pasted a whole env assignment line into the value, strip the key.
    for key in (
        "EVE_SSO_SCOPES",
        "EVE_SSO_SCOPES_MAX",
        "EVE_SSO_SCOPES_MAX_CORP",
        "EVE_SSO_SCOPES_ALL",
        "EVE_SSO_SCOPES_ALL_FEATURES",
        "EVE_SCOPES",
    ):
        prefix = key + "="
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break

    # Common separators to spaces
    s = s.replace("+", " ").replace(",", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    parts = [p.strip() for p in s.split(" ") if p.strip()]

    # Deduplicate while preserving order
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)

def get_sso_scopes(*, mode: str = "max_corp") -> str:
    """Return scopes for EVE SSO.

    mode:
      - min: minimal character scopes
      - max: expanded character scopes
      - max_corp: max character + corporation scopes (DEFAULT)
      - all: scopes required by all implemented features (character + corp)

    Env overrides:
      - EVE_SSO_SCOPES (legacy) -> min
      - EVE_SSO_SCOPES_ALL (legacy) -> max
      - EVE_SSO_SCOPES_MAX -> max
      - EVE_SSO_SCOPES_MAX_CORP -> max_corp
      - EVE_SSO_SCOPES_ALL_FEATURES -> all
    """
    m = (mode or "max_corp").strip().lower()

    # Corp-wide feature set (character + corp).
    if m in {"max_corp", "corp", "corp_max"}:
        sc = normalize_scopes((os.getenv("EVE_SSO_SCOPES_MAX_CORP") or DEFAULT_SCOPES_MAX_CORP).strip())
        sc = normalize_scopes(sc + " " + " ".join(ESSENTIAL_CORP_SCOPES))
        return filter_to_valid(sc) or sc

    # All implemented features (character + corp). This is what `eve link all` uses.
    if m in {"all", "features", "all_features"}:
        # Prefer authoritative scope list from CCP ESI swagger (cached), but NEVER request scopes your app doesn't have enabled.
        # Your app's enabled scopes can be provided via EVE_SSO_SCOPES_PORTAL_ENABLED (paste from CCP portal),
        # otherwise we fall back to DEFAULT_SCOPES_ALL_FEATURES.
        enabled = normalize_scopes((os.getenv("EVE_SSO_SCOPES_PORTAL_ENABLED") or DEFAULT_SCOPES_ALL_FEATURES).strip())
        enabled = normalize_scopes(enabled.replace("publicData", ""))
        cached_all = all_valid_scopes().strip()
        if cached_all:
            # Intersect: request only scopes that are BOTH valid (swagger) AND enabled in the CCP app registration.
            return intersect_scopes(enabled, cached_all) or filter_to_valid(enabled)
        sc = normalize_scopes((os.getenv("EVE_SSO_SCOPES_ALL_FEATURES") or DEFAULT_SCOPES_ALL_FEATURES).strip())
        return filter_to_valid(sc) or sc

    # Expanded character scopes.
    if m in {"max", "full"}:
        return (os.getenv("EVE_SSO_SCOPES_MAX") or os.getenv("EVE_SSO_SCOPES_ALL") or DEFAULT_SCOPES_MAX).strip()

    # Minimal character scopes.
    return normalize_scopes((os.getenv("EVE_SSO_SCOPES") or DEFAULT_SCOPES_MIN).strip())


def _get_or_create_key_in_db() -> Optional[str]:
    """
    M-7: Load the Fernet key from SQLite, or auto-generate + persist a new one.
    Returns the key as a base64 string, or None if no DB path is configured.
    """
    if not _token_key_db_path or Fernet is None:
        return None
    try:
        import sqlite3
        con = sqlite3.connect(_token_key_db_path)
        con.row_factory = sqlite3.Row
        with con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS kv_secrets ("
                "  key   TEXT PRIMARY KEY,"
                "  value TEXT NOT NULL"
                ")"
            )
            row = con.execute(
                "SELECT value FROM kv_secrets WHERE key='fernet_token_key'"
            ).fetchone()
            if row:
                return row["value"]
            # Auto-generate a new key and persist it
            new_key = Fernet.generate_key().decode()
            con.execute(
                "INSERT INTO kv_secrets(key, value) VALUES('fernet_token_key', ?)",
                (new_key,),
            )
        return new_key
    except Exception:
        return None


def _get_fernet() -> Optional["Fernet"]:
    """Return a Fernet instance for refresh-token encryption.

    Key resolution order (M-7):
      1. EVE_TOKEN_KEY env var (explicit operator config)
      2. XYLON_EVE_TOKEN_KEY env var (older deployments)
      3. SQLite kv_secrets table — auto-generated on first run
      4. WEB_SESSION_SECRET env var (legacy fallback; not recommended)
    """
    key = (os.getenv("EVE_TOKEN_KEY") or "").strip() or (os.getenv("XYLON_EVE_TOKEN_KEY") or "").strip()
    if not key:
        key = _get_or_create_key_in_db() or ""
    if not key:
        key = (os.getenv("WEB_SESSION_SECRET") or "").strip()
    if not key or Fernet is None:
        return None

    # Fernet expects a urlsafe base64-encoded 32-byte key.
    # If we were given an arbitrary secret string, derive a stable key from it.
    try:
        return Fernet(key.encode("utf-8"))
    except Exception:
        try:
            derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
            return Fernet(derived)
        except Exception:
            return None


def encrypt_refresh_token(token: str) -> str:
    f = _get_fernet()
    if not f:
        return token
    return f.encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_refresh_token(token: str) -> str | None:
    f = _get_fernet()
    if not f:
        return token

    token = (token or "").strip()
    if not token:
        return ""

    # Backward compatible: if a refresh token was stored while EVE_TOKEN_KEY was
    # not configured, it will be plaintext. When a key is later added, trying to
    # decrypt plaintext would fail and make the bot "forget" the link. Treat
    # non-Fernet-looking tokens as plaintext.
    if not token.startswith("gAAAA"):
        return token

    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        # If key changed or token is corrupted, we cannot recover the plaintext
        # refresh token. Returning None (rather than "") lets callers distinguish
        # between "not linked" and "linked but token is unreadable".
        return None


def build_authorize_url(*, state: str, scopes: Optional[str] = None) -> str:
    client_id = (os.getenv("EVE_SSO_CLIENT_ID") or os.getenv("EVE_CLIENT_ID") or "").strip()
    redirect_uri = (
        os.getenv("EVE_SSO_CALLBACK_URL")
        or os.getenv("EVE_CALLBACK_URL")
        or os.getenv("EVE_REDIRECT_URI")
        or os.getenv("EVE_OAUTH_CALLBACK_URL")
        or ""
    ).strip()
    scopes = normalize_scopes((scopes or get_sso_scopes(mode="max_corp")).strip())
    if not client_id or not redirect_uri:
        raise RuntimeError("EVE_CLIENT_ID and EVE_CALLBACK_URL must be set")

    # Use query params manually to avoid adding a dependency.
    from urllib.parse import urlencode

    qs = urlencode(
        {
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "scope": scopes,
            "state": state,
        }
    )
    return f"{EVE_AUTH_URL}?{qs}"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
    issued_ts: float
    # Optional convenience field. Some call-sites expect this attribute.
    scopes: str = ""


async def exchange_code(code: str) -> TokenSet:
    """Compatibility wrapper.

    Older web code imports `exchange_code` and calls it with a positional
    argument. Internally we delegate to `exchange_code_for_tokens`.
    """
    return await exchange_code_for_tokens(code=code)


async def exchange_code_for_tokens(*, code: str) -> TokenSet:
    # Support both the modern EVE_SSO_* names and older legacy names.
    client_id = (os.getenv("EVE_SSO_CLIENT_ID") or os.getenv("EVE_CLIENT_ID") or "").strip()
    client_secret = (
        os.getenv("EVE_SSO_CLIENT_SECRET")
        or os.getenv("EVE_SSO_SECRET_KEY")  # legacy alias used in older env templates
        or os.getenv("EVE_CLIENT_SECRET")
        or ""
    ).strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "EVE SSO is not configured. Set EVE_SSO_CLIENT_ID and EVE_SSO_CLIENT_SECRET (or legacy EVE_CLIENT_ID/EVE_CLIENT_SECRET)."
        )

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": (os.getenv("EVE_SSO_USER_AGENT") or "xylon-bot").strip(),
    }
    data = {"grant_type": "authorization_code", "code": code}

    async with aiohttp.ClientSession() as session:
        async with session.post(EVE_TOKEN_URL, headers=headers, data=data, timeout=20) as resp:
            txt = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Token exchange failed ({resp.status}): {txt[:300]}")
            payload = json.loads(txt)

    return TokenSet(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_in=int(payload.get("expires_in") or 0),
        token_type=str(payload.get("token_type") or "Bearer"),
        issued_ts=time.time(),
    )


async def refresh_access_token(*, refresh_token: str) -> TokenSet:
    """Refresh an EVE SSO access token.

    Supports both PKCE (native/desktop, no client secret) and confidential
    (server-side, client secret) flows. PKCE is used when no client secret
    is configured — which is the correct mode for the standalone desktop app.
    """
    client_id = (os.getenv("EVE_SSO_CLIENT_ID") or os.getenv("EVE_CLIENT_ID") or "").strip()
    if not client_id:
        raise RuntimeError("EVE_SSO_CLIENT_ID is not set.")

    client_secret = (
        os.getenv("EVE_SSO_CLIENT_SECRET")
        or os.getenv("EVE_SSO_SECRET_KEY")
        or os.getenv("EVE_CLIENT_SECRET")
        or ""
    ).strip()

    headers: Dict[str, str] = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": (os.getenv("EVE_SSO_USER_AGENT") or "xylon-eve").strip(),
    }
    data: Dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }

    if client_secret:
        # Confidential client: use HTTP Basic auth (server deployments)
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    # else: PKCE / native client — client_id in body is sufficient, no secret needed

    async with aiohttp.ClientSession() as session:
        async with session.post(EVE_TOKEN_URL, headers=headers, data=data, timeout=20) as resp:
            txt = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Token refresh failed ({resp.status}): {txt[:300]}")
            payload = json.loads(txt)

    # EVE may or may not rotate refresh tokens; handle both.
    new_refresh = str(payload.get("refresh_token") or "") or refresh_token
    return TokenSet(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=new_refresh,
        expires_in=int(payload.get("expires_in") or 0),
        token_type=str(payload.get("token_type") or "Bearer"),
        issued_ts=time.time(),
    )


async def _fetch_jwks() -> Dict[str, Any]:
    """
    L-4: Fetch EVE's JWKS from the well-known endpoint and cache it locally.
    Cache TTL is 24 hours; a fresh fetch is forced on cache miss or expiry.
    """
    now = time.time()
    if _jwks_cache.get("keys") and now - _jwks_cache.get("fetched_at", 0) < _JWKS_CACHE_TTL:
        return _jwks_cache

    async with aiohttp.ClientSession() as session:
        async with session.get(EVE_JWKS_URL, timeout=15) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch JWKS ({resp.status})")
            data = await resp.json(content_type=None)

    _jwks_cache.clear()
    _jwks_cache.update(data)
    _jwks_cache["fetched_at"] = now
    return _jwks_cache


async def verify_access_token(access_token: str) -> Dict[str, Any]:
    """
    L-4: Validate an EVE SSO access token locally using CCP's published JWKS.
    Falls back to the deprecated remote-verify endpoint if PyJWT is not available.

    Returns the decoded JWT claims dict, matching the legacy verify endpoint shape:
      CharacterID, CharacterName, CharacterOwnerHash, Scopes, ExpiresOn, ...
    """
    try:
        import jwt as _jwt
        from jwt import PyJWKClient as _PyJWKClient
    except ImportError:
        _jwt = None

    if _jwt is None:
        # Fallback: deprecated remote verify (L-4 partially mitigated)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": (os.getenv("EVE_SSO_USER_AGENT") or "xylon-bot").strip(),
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(EVE_VERIFY_URL, headers=headers, timeout=20) as resp:
                txt = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"Token verify failed ({resp.status}): {txt[:300]}")
                return json.loads(txt)

    # ── Local JWKS validation ──────────────────────────────────────────────────
    # Fetch JWKS (cached)
    jwks = await _fetch_jwks()

    # Build a temporary JWK set for PyJWT
    import json as _json
    jwks_str = _json.dumps({"keys": jwks.get("keys", [])})

    def _decode_local(tok: str, jwks_s: str) -> Dict[str, Any]:
        from jwt import PyJWKClient
        client = PyJWKClient.__new__(PyJWKClient)
        # Use the in-memory JWKS rather than fetching from a URL
        client.jwk_set_data = _json.loads(jwks_s)
        client.jwks_uri = EVE_JWKS_URL
        client.cache_jwk_set = False
        client.cache_keys = True
        client.jwk_set_cache = None
        # Actually easier: construct using the URI but override the data directly
        signing_key = PyJWKClient(EVE_JWKS_URL)
        # This fetches from URL — use the simpler dict approach instead
        raise NotImplementedError

    # Simpler approach: use jwt.decode with a JWK key fetched via PyJWKClient
    # PyJWKClient will call the URI (once per TTL), but we also keep our own cache.
    client = _PyJWKClient(EVE_JWKS_URL, cache_jwk_set=True, lifespan=_JWKS_CACHE_TTL)
    try:
        signing_key = client.get_signing_key_from_jwt(access_token)
        payload = _jwt.decode(
            access_token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=EVE_ISSUER,
            options={"verify_aud": False},  # EVE tokens don't set aud for public clients
        )
    except Exception as exc:
        raise RuntimeError(f"JWT validation failed: {exc}") from exc

    # Map claim names to the legacy verify-endpoint shape for backward compatibility
    sub = payload.get("sub", "")   # "CHARACTER:EVE:12345678"
    char_id = 0
    if sub.startswith("CHARACTER:EVE:"):
        try:
            char_id = int(sub.split(":")[-1])
        except ValueError:
            pass

    scopes = payload.get("scp", [])
    if isinstance(scopes, str):
        scopes = scopes.split()

    return {
        "CharacterID":        char_id,
        "CharacterName":      payload.get("name", ""),
        "CharacterOwnerHash": payload.get("owner", ""),
        "Scopes":             " ".join(scopes),
        "ExpiresOn":          payload.get("exp", 0),
        "TokenType":          "Character",
        # Also expose raw claims for callers that want them
        "_jwt_payload":       payload,
    }

# ── PKCE helpers (for native/desktop app distribution) ────────────────────────

def build_pkce_authorize_url(
    *,
    client_id: str,
    state: str,
    scopes: str,
    redirect_uri: str,
    code_challenge: str,
) -> str:
    """Build an EVE SSO authorization URL using PKCE (no client secret required).

    Register your application at https://developers.eveonline.com as a
    'Native Application' to enable PKCE. Bundle only the client_id — the
    code_verifier stays in the session and never leaves the machine.
    """
    from urllib.parse import urlencode

    qs = urlencode({
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "scope": normalize_scopes(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"{EVE_AUTH_URL}?{qs}"


async def exchange_code_pkce(
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    redirect_uri: str,
) -> "TokenSet":
    """Exchange an authorization code for tokens using PKCE.

    No client_secret is required — the code_verifier proves ownership.
    """
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": (os.getenv("EVE_SSO_USER_AGENT") or "xylon-eve").strip(),
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(EVE_TOKEN_URL, headers=headers, data=data, timeout=20) as resp:
            txt = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"PKCE token exchange failed ({resp.status}): {txt[:300]}")
            payload = json.loads(txt)

    return TokenSet(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_in=int(payload.get("expires_in") or 0),
        token_type=str(payload.get("token_type") or "Bearer"),
        issued_ts=time.time(),
    )
