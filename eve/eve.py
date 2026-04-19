from __future__ import annotations

import time
import requests
from typing import Any, Dict, List, Optional, Tuple


ESI_BASE = "https://esi.evetech.net/latest"
ESI_HEADERS = {"User-Agent": "XylonBot (Stellar Forge Nexus)"}

# Jita/The Forge defaults
THE_FORGE_REGION_ID = 10000002
JITA_SYSTEM_ID = 30000142


_cache: Dict[str, Tuple[float, Any]] = {}


def _cached_get(key: str, ttl: int):
    now = time.time()
    v = _cache.get(key)
    if not v:
        return None
    ts, data = v
    if now - ts > ttl:
        return None
    return data


def _cache_set(key: str, data: Any):
    _cache[key] = (time.time(), data)


def esi_search(name: str, categories: List[str]) -> Dict[str, List[int]]:
    """Fuzzy search via ESI `/search/` (strict=false)."""
    name = name.strip()
    if not name:
        return {}
    params = {
        "search": name,
        "categories": ",".join(categories),
        "strict": "false",
    }
    r = requests.get(f"{ESI_BASE}/search/", headers=ESI_HEADERS, params=params, timeout=10)
    r.raise_for_status()
    return r.json() or {}


def resolve_type_id(item_name: str) -> Tuple[Optional[int], List[int]]:
    data = esi_search(item_name, ["inventory_type"])
    ids = data.get("inventory_type") or []
    return (ids[0] if ids else None, ids)


def get_type_name(type_id: int) -> str:
    key = f"type_name:{type_id}"
    cached = _cached_get(key, 24 * 3600)
    if cached:
        return cached
    r = requests.get(f"{ESI_BASE}/universe/types/{int(type_id)}/", headers=ESI_HEADERS, timeout=10)
    r.raise_for_status()
    name = (r.json() or {}).get("name") or str(type_id)
    _cache_set(key, name)
    return name


def eve_price_jita(item_name: str) -> str:
    """Market price using free CCP ESI (no third-party paid APIs).

    Note: ESI exposes region orders. We default to **The Forge** (Jita region) and
    report best buy/sell in that region.
    """
    type_id, candidates = resolve_type_id(item_name)
    if not type_id:
        return f"❌ I couldn't find an EVE item named `{item_name}`. Try a more specific name."

    item_label = get_type_name(type_id)

    # Compute from ESI region orders (The Forge)
    try:
        orders = _get_region_orders(THE_FORGE_REGION_ID, type_id)
        sells = [o["price"] for o in orders if o.get("is_buy_order") is False]
        buys = [o["price"] for o in orders if o.get("is_buy_order") is True]
        best_sell = min(sells) if sells else None
        best_buy = max(buys) if buys else None
        if best_sell is None and best_buy is None:
            return f"💠 **{item_label}**\nNo market orders found in The Forge right now."
        lines = [f"💠 **{item_label}** (The Forge - Jita region)"]
        if best_sell is not None:
            lines.append(f"• Best sell: {best_sell:,.2f} ISK")
        if best_buy is not None:
            lines.append(f"• Best buy: {best_buy:,.2f} ISK")
        lines.append("Source: CCP ESI region orders")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ EVE price lookup failed: {e}"


def _get_region_orders(region_id: int, type_id: int) -> List[Dict[str, Any]]:
    key = f"region_orders:{region_id}:{type_id}"
    cached = _cached_get(key, 60)  # 1 min cache
    if cached:
        return cached

    orders: List[Dict[str, Any]] = []
    page = 1
    while True:
        r = requests.get(
            f"{ESI_BASE}/markets/{int(region_id)}/orders/",
            headers=ESI_HEADERS,
            params={"type_id": int(type_id), "page": int(page)},
            timeout=15,
        )
        if r.status_code == 404:
            break
        r.raise_for_status()
        chunk = r.json() or []
        if not chunk:
            break
        orders.extend(chunk)
        # Pagination ends when less than 1000 returned
        if len(chunk) < 1000:
            break
        page += 1
        if page > 20:
            break

    _cache_set(key, orders)
    return orders


def eve_route(from_name: str, to_name: str) -> str:
    try:
        a = esi_search(from_name, ["solar_system"]).get("solar_system") or []
        b = esi_search(to_name, ["solar_system"]).get("solar_system") or []
        if not a or not b:
            return f"❌ Couldn't resolve route. Check system names: `{from_name}` -> `{to_name}`"
        origin = a[0]
        dest = b[0]

        r = requests.get(f"{ESI_BASE}/route/{origin}/{dest}/", headers=ESI_HEADERS, timeout=10)
        r.raise_for_status()
        path = r.json() or []
        if len(path) <= 1:
            return "✅ Same system."

        # Security status: fetch first, last, and compute min
        sec_vals: List[float] = []
        for sys_id in path:
            sec_vals.append(get_system_security(sys_id))
        min_sec = min(sec_vals) if sec_vals else 1.0

        return (
            f"🧭 Route **{from_name}** → **{to_name}**\n"
            f"• Jumps: {len(path) - 1}\n"
            f"• Lowest security on route: {min_sec:.2f}"
        )
    except Exception as e:
        return f"❌ Route lookup failed: {e}"


def get_system_security(system_id: int) -> float:
    key = f"sys_sec:{system_id}"
    cached = _cached_get(key, 24 * 3600)
    if cached is not None:
        return float(cached)
    r = requests.get(f"{ESI_BASE}/universe/systems/{int(system_id)}/", headers=ESI_HEADERS, timeout=10)
    r.raise_for_status()
    sec = float((r.json() or {}).get("security_status") or 0.0)
    _cache_set(key, sec)
    return sec


def pilot_stats(pilot_name: str) -> str:
    try:
        ids = esi_search(pilot_name, ["character"]).get("character") or []
        if not ids:
            return f"❌ I couldn't find a pilot named `{pilot_name}`."
        char_id = ids[0]

        r = requests.get(f"{ESI_BASE}/characters/{int(char_id)}/", headers=ESI_HEADERS, timeout=10)
        r.raise_for_status()
        info = r.json() or {}
        sec = info.get("security_status")
        name = info.get("name") or pilot_name

        # zKill danger ratio (best-effort)
        danger = _zkill_danger_ratio(char_id)
        if danger is None:
            return f"🧑‍🚀 **{name}**\n• Security status: {sec:.2f}" if isinstance(sec, (int, float)) else f"🧑‍🚀 **{name}**"
        return (
            f"🧑‍🚀 **{name}**\n"
            + (f"• Security status: {sec:.2f}\n" if isinstance(sec, (int, float)) else "")
            + f"• zKill danger ratio: {danger:.2f}"
        )
    except Exception as e:
        return f"❌ Pilot stats failed: {e}"


# In-memory cache for character zkill stats: {char_id: (fetched_at, value)}
_char_stats_cache: dict = {}
_CHAR_STATS_TTL = 3600  # 1 hour — character danger ratio doesn't change minute-to-minute


def _zkill_danger_ratio(char_id: int) -> Optional[float]:
    """zKillboard danger ratio — cached in memory for 1 hour to avoid hammering the API."""
    cid = int(char_id)
    cached = _char_stats_cache.get(cid)
    if cached and (time.time() - cached[0]) < _CHAR_STATS_TTL:
        return cached[1]
    try:
        url = f"https://zkillboard.com/api/stats/characterID/{cid}/"
        r = requests.get(url, headers={"User-Agent": "StellarInsight/1.0"}, timeout=10)
        if r.status_code != 200:
            _char_stats_cache[cid] = (time.time(), None)
            return None
        data = r.json() or {}
        ratio = None
        for key in ("dangerRatio", "danger_ratio", "danger"):
            if key in data and isinstance(data[key], (int, float)):
                ratio = float(data[key])
                break
        _char_stats_cache[cid] = (time.time(), ratio)
        return ratio
    except Exception:
        return None


def system_activity(system_name: str) -> str:
    try:
        ids = esi_search(system_name, ["solar_system"]).get("solar_system") or []
        if not ids:
            return f"❌ I couldn't find a system named `{system_name}`."
        sys_id = ids[0]

        key = "system_kills"
        cached = _cached_get(key, 30)
        if cached is None:
            r = requests.get(f"{ESI_BASE}/universe/system_kills/", headers=ESI_HEADERS, timeout=10)
            r.raise_for_status()
            cached = r.json() or []
            _cache_set(key, cached)

        row = next((x for x in cached if x.get("system_id") == sys_id), None)
        if not row:
            return f"🛰️ **{system_name}**\nNo reported kills in the last hour."
        return (
            f"🛰️ **{system_name}** (last hour)\n"
            f"• Ship kills: {row.get('ship_kills', 0)}\n"
            f"• Pod kills: {row.get('pod_kills', 0)}\n"
            f"• NPC kills: {row.get('npc_kills', 0)}"
        )
    except Exception as e:
        return f"❌ System activity failed: {e}"
