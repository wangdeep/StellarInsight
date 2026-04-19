from __future__ import annotations

import json
import os
import asyncio
from typing import Any, Dict, List, Optional

import aiohttp
import logging

logger = logging.getLogger(__name__)


ESI_BASE = "https://esi.evetech.net/latest"


def _ua() -> str:
    return (os.getenv("EVE_ESI_USER_AGENT") or os.getenv("EVE_SSO_USER_AGENT") or "xylon-bot").strip()


RETRY_STATUSES = {502, 503, 504}  # 420 = rate limit; retrying makes it worse
DEFAULT_TIMEOUT = int(os.getenv("EVE_ESI_TIMEOUT", "20"))
DEFAULT_RETRIES = int(os.getenv("EVE_ESI_RETRIES", "2"))


async def _esi_request(method: str, url: str, *, headers: dict, params=None, json_body=None, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> tuple[int, str]:
    """ESI request wrapper with retry/backoff for transient Tranquility/ESI issues."""
    params = params or {}
    last_txt = ""
    last_status = 0
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                if method.upper() == "POST":
                    async with session.post(url, headers=headers, params=params, json=json_body, timeout=timeout) as resp:
                        last_status = resp.status
                        last_txt = await resp.text()
                else:
                    async with session.get(url, headers=headers, params=params, timeout=timeout) as resp:
                        last_status = resp.status
                        last_txt = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            raise RuntimeError(f"ESI request failed: {e}")

        if last_status in RETRY_STATUSES and attempt < retries:
            await asyncio.sleep(0.6 * (attempt + 1))
            continue
        return last_status, last_txt


async def esi_get_json(path: str, *, access_token: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{ESI_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": _ua(),
        "Accept": "application/json",
    }
    status, txt = await _esi_request("GET", url, headers=headers, params=params or {}, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES)
    if status >= 400:
        raise RuntimeError(f"ESI {path} failed ({status}): {txt[:300]}")
    if not txt:
        return None
    return json.loads(txt)



async def esi_get_json_public(path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
    """Public GET (no auth) against ESI."""
    url = f"{ESI_BASE}{path}"
    headers = {
        "User-Agent": _ua(),
        "Accept": "application/json",
    }
    status, txt = await _esi_request("GET", url, headers=headers, params=params or {}, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES)
    if status >= 400:
        raise RuntimeError(f"ESI {path} failed ({status}): {txt[:300]}")
    if not txt:
        return None
    return json.loads(txt)



async def universe_names(ids: List[int]) -> Dict[int, str]:
    """Resolve EVE IDs to names via ESI /universe/names/.

    This endpoint is public and does not require auth.
    """
    # /universe/names only accepts int32 IDs. Many ESI values (notably structure IDs)
    # are int64 and will 400 if sent here. Filter to int32 to avoid breaking callers.
    clean: List[int] = []
    for x in ids or []:
        try:
            xi = int(x)
        except Exception:
            continue
        if 0 < xi <= 2147483647:
            clean.append(xi)
    if not clean:
        return {}

    url = f"{ESI_BASE}/universe/names/"
    headers = {
        "User-Agent": _ua(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    status, txt = await _esi_request("POST", url, headers=headers, params={}, json_body=clean, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES)
    if status >= 400:
        raise RuntimeError(f"ESI /universe/names failed ({status}): {txt[:300]}")
    data = json.loads(txt) if txt else []

    out: Dict[int, str] = {}
    for row in data or []:
        try:
            out[int(row.get("id"))] = str(row.get("name") or row.get("id"))
        except Exception:
            continue
    return out


async def universe_ids(names: List[str]) -> Dict[str, Any]:
    """Resolve names to IDs via ESI /universe/ids/ (public POST).

    This is useful for turning a solar system name like "Gamdis" into a system ID.
    """
    clean: List[str] = []
    for n in names or []:
        s = (n or "").strip()
        if not s:
            continue
        clean.append(s)
        if len(clean) >= 50:
            break

    if not clean:
        return {}

    url = f"{ESI_BASE}/universe/ids/"
    headers = {
        "User-Agent": _ua(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    status, txt = await _esi_request(
        "POST",
        url,
        headers=headers,
        params={},
        json_body=clean,
        timeout=DEFAULT_TIMEOUT,
        retries=DEFAULT_RETRIES,
    )
    if status >= 400:
        raise RuntimeError(f"ESI /universe/ids failed ({status}): {txt[:300]}")
    return json.loads(txt) if txt else {}


async def resolve_system_id(system_name: str) -> Optional[int]:
    """Resolve a solar system name to an ID using /universe/ids/."""
    s = (system_name or "").strip()
    if not s:
        return None
    data = await universe_ids([s])
    systems = data.get("systems") or []
    if not systems:
        return None
    try:
        return int(systems[0].get("id") or 0) or None
    except Exception:
        return None



async def esi_get_public_json(path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
    """Public ESI GET (no auth)."""
    url = f"{ESI_BASE}{path}"
    headers = {
        "User-Agent": _ua(),
        "Accept": "application/json",
    }
    status, txt = await _esi_request("GET", url, headers=headers, params=params or {}, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES)
    if status >= 400:
        raise RuntimeError(f"ESI {path} failed ({status}): {txt[:300]}")
    return json.loads(txt) if txt else None



async def get_character_public(character_id: int) -> Dict[str, Any]:
    """Public character info (includes corporation_id)."""
    return await esi_get_public_json(f"/characters/{int(character_id)}/")


async def get_corp_wallets(corp_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    return await esi_get_json(f"/corporations/{int(corp_id)}/wallets/", access_token=access_token)


async def get_corp_wallet_journal(corp_id: int, division: int = 1, *, access_token: str) -> List[Dict[str, Any]]:
    return await esi_get_json(
        f"/corporations/{int(corp_id)}/wallets/{int(division)}/journal/",
        access_token=access_token,
    )


async def get_corp_orders(corp_id: int, *, access_token: str, page: int = 1) -> List[Dict[str, Any]]:
    return await esi_get_json(
        f"/corporations/{int(corp_id)}/orders/",
        access_token=access_token,
        params={"page": int(page)},
    )


async def get_corp_industry_jobs(corp_id: int, *, access_token: str, include_completed: bool = False) -> List[Dict[str, Any]]:
    params = {"include_completed": "true"} if include_completed else None
    return await esi_get_json(f"/corporations/{int(corp_id)}/industry/jobs/", access_token=access_token, params=params)


async def get_corp_contracts(corp_id: int, *, access_token: str, page: int = 1) -> List[Dict[str, Any]]:
    return await esi_get_json(
        f"/corporations/{int(corp_id)}/contracts/",
        access_token=access_token,
        params={"page": int(page)},
    )


async def get_corp_assets(corp_id: int, *, access_token: str, page: int = 1) -> List[Dict[str, Any]]:
    return await esi_get_json(
        f"/corporations/{int(corp_id)}/assets/",
        access_token=access_token,
        params={"page": int(page)},
    )


async def get_corp_structures(corp_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    return await esi_get_json(f"/corporations/{int(corp_id)}/structures/", access_token=access_token)


async def get_skillqueue(character_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    return await esi_get_json(f"/characters/{int(character_id)}/skillqueue/", access_token=access_token)


async def get_industry_jobs(character_id: int, *, access_token: str, include_completed: bool = False) -> List[Dict[str, Any]]:
    params = {"include_completed": "true"} if include_completed else None
    return await esi_get_json(f"/characters/{int(character_id)}/industry/jobs/", access_token=access_token, params=params)


async def get_location(character_id: int, *, access_token: str) -> Dict[str, Any]:
    """Get the character's current location (solar system + station/structure when docked)."""
    return await esi_get_json(f"/characters/{int(character_id)}/location/", access_token=access_token)


async def get_ship(character_id: int, *, access_token: str) -> Dict[str, Any]:
    """Get the character's currently piloted ship."""
    return await esi_get_json(f"/characters/{int(character_id)}/ship/", access_token=access_token)


# ------------------------
# Corporation endpoints
# ------------------------


async def get_corp_industry_jobs(corp_id: int, *, access_token: str, include_completed: bool = False) -> List[Dict[str, Any]]:
    params = {"include_completed": "true"} if include_completed else None
    return await esi_get_json(
        f"/corporations/{int(corp_id)}/industry/jobs/",
        access_token=access_token,
        params=params,
    )


async def get_corp_contracts(corp_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    return await esi_get_json(f"/corporations/{int(corp_id)}/contracts/", access_token=access_token)


async def get_corp_assets(corp_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    # Corp assets are paginated in ESI; this fetches the first page for now.
    return await esi_get_json(f"/corporations/{int(corp_id)}/assets/", access_token=access_token)


async def get_corp_structures(corp_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    return await esi_get_json(f"/corporations/{int(corp_id)}/structures/", access_token=access_token)


async def get_online(character_id: int, *, access_token: str) -> Dict[str, Any]:
    """Get online status + last login/logout times."""
    return await esi_get_json(f"/characters/{int(character_id)}/online/", access_token=access_token)


async def get_wallet_balance(character_id: int, *, access_token: str) -> float:
    """Get character wallet balance (ISK)."""
    data = await esi_get_json(f"/characters/{int(character_id)}/wallet/", access_token=access_token)
    try:
        return float(data)
    except Exception:
        return 0.0


async def get_wallet_transactions(character_id: int, *, access_token: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Get recent wallet transactions."""
    limit = max(1, min(50, int(limit)))
    return await esi_get_json(
        f"/characters/{int(character_id)}/wallet/transactions/",
        access_token=access_token,
        params={"limit": str(limit)},
    )


async def get_character_orders(character_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    """Get active character market orders."""
    return await esi_get_json(f"/characters/{int(character_id)}/orders/", access_token=access_token)


async def get_skills(character_id: int, *, access_token: str) -> Dict[str, Any]:
    """Get character skills (trained skill list, totals)."""
    return await esi_get_json(f"/characters/{int(character_id)}/skills/", access_token=access_token)


async def get_wallet_journal(character_id: int, *, access_token: str, page: int = 1) -> List[Dict[str, Any]]:
    """Get wallet journal entries (page-based)."""
    page = max(1, int(page))
    return await esi_get_json(
        f"/characters/{int(character_id)}/wallet/journal/",
        access_token=access_token,
        params={"page": str(page)},
    )


async def get_planets(character_id: int, *, access_token: str) -> List[Dict[str, Any]]:
    """List PI colonies for the character."""
    return await esi_get_json(f"/characters/{int(character_id)}/planets/", access_token=access_token)


async def get_planet_details(character_id: int, planet_id: int, *, access_token: str) -> Dict[str, Any]:
    """Get PI colony layout/details for a specific planet."""
    return await esi_get_json(f"/characters/{int(character_id)}/planets/{int(planet_id)}/", access_token=access_token)


# ------------------------
# Character assets / contracts / blueprints / clones, etc.
# ------------------------

async def get_character_assets(character_id: int, *, access_token: str, page: int = 1) -> List[Dict[str, Any]]:
    return await esi_get_json(
        f"/characters/{int(character_id)}/assets/",
        access_token=access_token,
        params={"page": int(page)},
    )


async def get_all_character_assets(character_id: int, *, access_token: str, max_pages: int = 100) -> List[Dict[str, Any]]:
    """Fetch all pages of character assets.

    ESI returns 404 on non-existent pages for some endpoints; stop cleanly.
    """
    out: List[Dict[str, Any]] = []
    for page in range(1, int(max_pages) + 1):
        try:
            rows = await get_character_assets(character_id, access_token=access_token, page=page)
        except RuntimeError as e:
            msg=str(e)
            if '(404)' in msg:
                break
            raise
        if not rows:
            break
        out.extend(rows)
    return out


async def get_character_contracts(character_id: int, *, access_token: str, page: int = 1) -> List[Dict[str, Any]]:
    return await esi_get_json(
        f"/characters/{int(character_id)}/contracts/",
        access_token=access_token,
        params={"page": int(page)},
    )


async def get_character_blueprints(character_id: int, *, access_token: str, page: int = 1) -> List[Dict[str, Any]]:
    return await esi_get_json(
        f"/characters/{int(character_id)}/blueprints/",
        access_token=access_token,
        params={"page": int(page)},
    )


async def get_character_clones(character_id: int, *, access_token: str) -> Dict[str, Any]:
    return await esi_get_json(f"/characters/{int(character_id)}/clones/", access_token=access_token)


async def get_character_implants(character_id: int, *, access_token: str) -> List[int]:
    return await esi_get_json(f"/characters/{int(character_id)}/implants/", access_token=access_token)


# ------------------------
# Human-readable resolvers with SQLite cache
# ------------------------

INT32_MAX = 2147483647

def _esi_cache_get_safe(memory, kind: str, ids: List[int]) -> Dict[int, str]:
    """Best-effort cache get; tolerates older MemoryStore implementations."""
    if not memory:
        return {}
    fn = getattr(memory, "esi_cache_get", None)
    if not callable(fn):
        return {}
    try:
        return fn(kind, ids) or {}
    except Exception:
        return {}

def _esi_cache_set_safe(memory, kind: str, mapping: Dict[int, str], ttl_seconds: Optional[int] = None) -> None:
    """Best-effort cache set; tolerates older MemoryStore implementations."""
    if not memory or not mapping:
        return
    fn = getattr(memory, "esi_cache_set", None)
    if not callable(fn):
        return
    try:
        fn(kind, mapping, ttl_seconds=ttl_seconds)
    except Exception:
        return



def _int32_ids(ids: List[int]) -> List[int]:
    out: List[int] = []
    for x in ids or []:
        try:
            xi=int(x)
        except Exception:
            continue
        if 0 < xi <= INT32_MAX:
            out.append(xi)
    return out


async def resolve_type_names(type_ids: List[int], *, memory=None) -> Dict[int, str]:
    """Resolve type_ids (and other int32 IDs) to names with cache."""
    ids=_int32_ids(type_ids)
    if not ids:
        return {}
    cached = _esi_cache_get_safe(memory, 'type', ids)
    missing = [i for i in ids if i not in cached]
    fresh: Dict[int,str] = {}
    if missing:
        fresh = {}
        for i in range(0, len(missing), 500):
            chunk = missing[i:i+500]
            try:
                fresh.update(await universe_names(chunk))
            except Exception:
                continue
        if memory and fresh:
            _esi_cache_set_safe(memory, 'type', fresh, ttl_seconds=60*60*24*30)
    out = {**cached, **fresh}
    return out


async def resolve_station_names(station_ids: List[int], *, memory=None) -> Dict[int, str]:
    ids=_int32_ids(station_ids)
    if not ids:
        return {}
    cached = _esi_cache_get_safe(memory, 'station', ids)
    missing=[i for i in ids if i not in cached]
    fresh: Dict[int,str]={}
    if missing:
        fresh = {}
        for i in range(0, len(missing), 500):
            chunk = missing[i:i+500]
            try:
                fresh.update(await universe_names(chunk))
            except Exception:
                continue
        if memory and fresh:
            _esi_cache_set_safe(memory, 'station', fresh, ttl_seconds=60*60*24*30)
    return {**cached, **fresh}


async def resolve_system_names(system_ids: List[int], *, memory=None) -> Dict[int, str]:
    """Resolve solar_system_id -> name (cache)."""
    ids=_int32_ids(system_ids)
    if not ids:
        return {}
    cached = _esi_cache_get_safe(memory, 'system', ids)
    missing=[i for i in ids if i not in cached]
    fresh: Dict[int,str]={}
    if missing:
        for i in range(0, len(missing), 500):
            chunk = missing[i:i+500]
            try:
                fresh.update(await universe_names(chunk))
            except Exception:
                continue
        if memory and fresh:
            _esi_cache_set_safe(memory, 'system', fresh, ttl_seconds=60*60*24*30)
    return {**cached, **fresh}


async def get_structure_name(structure_id: int, *, access_token: str) -> Optional[str]:
    data = await esi_get_json(f"/universe/structures/{int(structure_id)}/", access_token=access_token)
    try:
        return str(data.get('name') or '') or None
    except Exception:
        return None


async def resolve_structure_names(structure_ids: List[int], *, access_token: str, memory=None) -> Dict[int, str]:
    ids=[]
    for x in structure_ids or []:
        try:
            xi=int(x)
        except Exception:
            continue
        if xi>INT32_MAX:
            ids.append(xi)
    if not ids:
        return {}
    cached = _esi_cache_get_safe(memory, 'structure', ids)
    missing=[i for i in ids if i not in cached]
    fresh: Dict[int,str]={}
    for sid in missing:
        try:
            name = await get_structure_name(sid, access_token=access_token)
            if name:
                fresh[sid]=name
        except Exception as e:
            logger.debug(f"Could not resolve structure {sid}: {e}")
            continue
    if memory and fresh:
        _esi_cache_set_safe(memory, 'structure', fresh, ttl_seconds=60*60*6)
    return {**cached, **fresh}


async def get_universe_planet(planet_id: int) -> Dict[str, Any]:
    return await esi_get_public_json(f"/universe/planets/{int(planet_id)}/")


async def get_universe_system(system_id: int) -> Dict[str, Any]:
    return await esi_get_public_json(f"/universe/systems/{int(system_id)}/")


async def resolve_planet_and_system(planet_ids: List[int], *, memory=None) -> Dict[int, Dict[str, Any]]:
    """Return mapping planet_id -> {planet_name, system_id, system_name}."""
    ids=[]
    for x in planet_ids or []:
        try:
            ids.append(int(x))
        except Exception:
            continue
    if not ids:
        return {}
    # planets are int32, but use dedicated endpoints.
    cached_planet = _esi_cache_get_safe(memory, 'planet', ids)
    # cached_planet maps id->name only; system mapping stored separately
    # We'll also cache planet->system_id as 'planet_sys' kind with name being system_id string.
    cached_sysid = {}
    if memory:
        raw = _esi_cache_get_safe(memory, 'planet_sys', ids)
        cached_sysid = {pid:int(s) for pid,s in raw.items() if str(s).isdigit()}
    missing=[pid for pid in ids if pid not in cached_planet or pid not in cached_sysid]
    planet_name_new={}
    planet_sys_new={}
    sys_ids=set()
    for pid in missing:
        try:
            pdata = await get_universe_planet(pid)
            pname = pdata.get('name')
            sid = pdata.get('system_id')
            if pname:
                planet_name_new[pid]=str(pname)
            if sid is not None:
                planet_sys_new[pid]=str(int(sid))
                sys_ids.add(int(sid))
        except Exception:
            continue
    if memory and planet_name_new:
        _esi_cache_set_safe(memory, 'planet', planet_name_new, ttl_seconds=60*60*24*30)
    if memory and planet_sys_new:
        _esi_cache_set_safe(memory, 'planet_sys', planet_sys_new, ttl_seconds=60*60*24*30)
    planet_name={**cached_planet, **planet_name_new}
    planet_sys={**cached_sysid, **{k:int(v) for k,v in planet_sys_new.items() if str(v).isdigit()}}

    # resolve systems
    sys_ids=list(set(list(sys_ids) + list(planet_sys.values())))
    cached_sys = _esi_cache_get_safe(memory, 'system', sys_ids)
    miss_sys=[sid for sid in sys_ids if sid not in cached_sys]
    sys_new={}
    for sid in miss_sys:
        try:
            sdata = await get_universe_system(sid)
            sname = sdata.get('name')
            if sname:
                sys_new[sid]=str(sname)
        except Exception:
            continue
    if memory and sys_new:
        _esi_cache_set_safe(memory, 'system', sys_new, ttl_seconds=60*60*24*30)
    sys_name={**cached_sys, **sys_new}

    out={}
    for pid in ids:
        sid = planet_sys.get(pid)
        out[pid]={
            'planet_name': planet_name.get(pid) or str(pid),
            'system_id': sid,
            'system_name': sys_name.get(sid) if sid else None,
        }
    return out
