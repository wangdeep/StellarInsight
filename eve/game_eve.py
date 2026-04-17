# xylon/games/eve.py
# Full EVE module with stable name->ID resolution via POST /universe/ids/
# Exports expected by xylon/router.py:
#   eve_price_jita, eve_route, pilot_stats, system_activity, threat_check, home_heat
#
# Notes:
# - Avoids ESI /search for systems/characters (can 404).
# - Uses ESI public endpoints + best-effort zKillboard API for recent killmail activity.
# - All external calls are free; caching should be handled by the caller if present.

from __future__ import annotations

import os
import re
import json
import time
import math
from typing import Any, Dict, List, Optional, Tuple

import requests

ESI_BASE = os.getenv("EVE_ESI_BASE", "https://esi.evetech.net")
ESI_DATASOURCE = os.getenv("EVE_ESI_DATASOURCE", "tranquility")

# Home/hubs (used for "home_heat" + contextual advice)
EVE_HOME_SYSTEM = os.getenv("EVE_HOME_SYSTEM", "Gamdis")
EVE_LOCAL_HUBS = [s.strip() for s in os.getenv("EVE_LOCAL_HUBS", "Aband,Amarr,Jita").split(",") if s.strip()]
EVE_LOCAL_RADIUS_JUMPS = int(os.getenv("EVE_LOCAL_RADIUS_JUMPS", "6"))

# Market defaults
EVE_DEFAULT_REGION = int(os.getenv("EVE_DEFAULT_REGION", "10000002"))  # The Forge
EVE_JITA_STATION_ID = int(os.getenv("EVE_JITA_STATION_ID", "60003760"))  # Jita IV - Moon 4 - Caldari Navy Assembly Plant

_UA = "XylonEVE/1.0 (+StellarForgeNexus)"

_session = requests.Session()
_session.headers.update({"User-Agent": _UA, "Accept": "application/json"})


# ---------------------------
# ESI helpers
# ---------------------------

def _esi_url(version: str, path: str) -> str:
    return f"{ESI_BASE}/{version}{path}"

def _esi_get_json(path: str, params: Optional[dict] = None, timeout: int = 12) -> Any:
    if params is None:
        params = {}
    params = dict(params)
    params.setdefault("datasource", ESI_DATASOURCE)

    last_err: Optional[Exception] = None
    for version in ("latest", "v5", "v4", "v3", "v2", "v1"):
        try:
            url = _esi_url(version, path)
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 404:
                last_err = requests.HTTPError(f"404 for {url}")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("ESI request failed")

def _esi_post_json(path: str, body: Any, params: Optional[dict] = None, timeout: int = 12) -> Any:
    if params is None:
        params = {}
    params = dict(params)
    params.setdefault("datasource", ESI_DATASOURCE)

    last_err: Optional[Exception] = None
    for version in ("latest", "v1", "v2", "v3"):
        try:
            url = _esi_url(version, path)
            r = _session.post(url, params=params, data=json.dumps(body), headers={"Content-Type": "application/json"}, timeout=timeout)
            if r.status_code == 404:
                last_err = requests.HTTPError(f"404 for {url}")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("ESI POST failed")


def _name_variants(name: str) -> List[str]:
    n = (name or "").strip()
    if not n:
        return []
    # Try original + title case; ESI ids lookup is case-insensitive but exact spelling matters.
    def titleish(s: str) -> str:
        parts = re.split(r"(\s+)", s)
        out = []
        for p in parts:
            if p.isspace() or not p:
                out.append(p)
            else:
                out.append(p[:1].upper() + p[1:].lower())
        return "".join(out)
    v = [n]
    tc = titleish(n)
    if tc not in v:
        v.append(tc)
    up = n.upper()
    if up not in v:
        v.append(up)
    lo = n.lower()
    if lo not in v:
        v.append(lo)
    return v[:4]


def _universe_ids(names: List[str]) -> dict:
    # POST /universe/ids/ -> {characters:[...], systems:[...], inventory_types:[...], ...}
    return _esi_post_json("/universe/ids/", names)


def resolve_system_id(system_name: str) -> Optional[int]:
    names = _name_variants(system_name)
    if not names:
        return None
    data = _universe_ids(names)
    systems = data.get("systems") or []
    if not systems:
        return None
    target = system_name.strip().lower()
    for s in systems:
        if (s.get("name") or "").strip().lower() == target:
            return int(s["id"])
    return int(systems[0]["id"])


def resolve_character_id(char_name: str) -> Optional[int]:
    names = _name_variants(char_name)
    if not names:
        return None
    data = _universe_ids(names)
    chars = data.get("characters") or []
    if not chars:
        return None
    target = char_name.strip().lower()
    for c in chars:
        if (c.get("name") or "").strip().lower() == target:
            return int(c["id"])
    return int(chars[0]["id"])


def resolve_type_id(item_name: str) -> Optional[int]:
    # Be forgiving about natural-language filler ("a/an/the Punisher").
    cleaned = re.sub(r"^(?:a|an|the)\s+", "", (item_name or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"[\s\.:;,!\?]+$", "", cleaned)
    names = _name_variants(cleaned)
    if not names:
        return None
    data = _universe_ids(names)
    types_ = data.get("inventory_types") or []
    if not types_:
        return None
    target = cleaned.strip().lower()
    for t in types_:
        if (t.get("name") or "").strip().lower() == target:
            return int(t["id"])
    return int(types_[0]["id"])


def get_system_info(system_id: int) -> dict:
    return _esi_get_json(f"/universe/systems/{system_id}/")


def get_system_kills() -> List[dict]:
    return _esi_get_json("/universe/system_kills/")


def get_character_info(character_id: int) -> dict:
    return _esi_get_json(f"/characters/{character_id}/")


def get_route(origin_system_id: int, dest_system_id: int) -> List[int]:
    return _esi_get_json(f"/route/{origin_system_id}/{dest_system_id}/", params={"flag": "shortest"})


# ---------------------------
# zKillboard best-effort
# ---------------------------

def _zkill_api(url: str, timeout: int = 12) -> Any:
    # zKill asks for a user agent; also be polite.
    headers = {"User-Agent": _UA, "Accept-Encoding": "gzip"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def zkill_character_url(character_id: int) -> str:
    return f"https://zkillboard.com/character/{character_id}/"

def zkill_recent_kills(character_id: int, limit: int = 50) -> List[dict]:
    # Unofficial but commonly used endpoint:
    # https://zkillboard.com/api/kills/characterID/<id>/
    url = f"https://zkillboard.com/api/kills/characterID/{character_id}/"
    data = _zkill_api(url)
    return (data or [])[:limit]

def zkill_recent_losses(character_id: int, limit: int = 50) -> List[dict]:
    url = f"https://zkillboard.com/api/losses/characterID/{character_id}/"
    data = _zkill_api(url)
    return (data or [])[:limit]


def _parse_kill_time(k: dict) -> Optional[float]:
    # zKill returns killmail_time in ISO like "2026-01-16T06:05:00Z"
    s = (k.get("killmail_time") or "").strip()
    if not s:
        return None
    try:
        # Simple parse without dateutil
        # YYYY-MM-DDTHH:MM:SSZ
        import datetime as _dt
        dt = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=_dt.timezone.utc).timestamp()
    except Exception:
        return None

def _count_recent(events: List[dict], window_seconds: int) -> int:
    now = time.time()
    c = 0
    for e in events:
        ts = _parse_kill_time(e)
        if ts is None:
            continue
        if now - ts <= window_seconds:
            c += 1
    return c

def _solo_fleet_counts(kills: List[dict], window_seconds: int) -> Tuple[int, int]:
    now = time.time()
    solo = 0
    fleet = 0
    for k in kills:
        ts = _parse_kill_time(k)
        if ts is None or (now - ts > window_seconds):
            continue
        attackers = k.get("attackers") or []
        # zKill sometimes includes lots; interpret:
        if len(attackers) <= 1:
            solo += 1
        elif len(attackers) >= 6:
            fleet += 1
    return solo, fleet


# ---------------------------
# Public command functions (exports)
# ---------------------------

def system_activity(system_name: str) -> str:
    sid = resolve_system_id(system_name)
    if sid is None:
        return f"I couldn't find a system named **{system_name}**."

    kills = get_system_kills()
    row = next((r for r in kills if int(r.get("system_id", -1)) == sid), None)
    if not row:
        return f"**{system_name}**: no activity data found right now."

    ship = int(row.get("ship_kills", 0))
    pod = int(row.get("pod_kills", 0))
    npc = int(row.get("npc_kills", 0))

    # heat label
    pvp = ship + pod
    if pvp >= 20:
        heat = "HIGH"
    elif pvp >= 5:
        heat = "MED"
    else:
        heat = "LOW"

    # quick interpretation
    if ship + pod == 0 and npc >= 20:
        interp = "Mostly PvE activity (NPC kills high, PvP low)."
    elif ship + pod >= 5:
        interp = "PvP activity detected (ship/pod kills present)."
    else:
        interp = "Looks quiet."

    return (
        f"**System:** {system_name}\n"
        f"**Action level:** {heat}\n"
        f"Last hour: **Ship kills:** {ship} | **Pod kills:** {pod} | **NPC kills:** {npc}\n"
        f"{interp}"
    )


def eve_route(origin: str, destination: str) -> str:
    a_id = resolve_system_id(origin)
    b_id = resolve_system_id(destination)
    if a_id is None:
        return f"I couldn't find a system named **{origin}**."
    if b_id is None:
        return f"I couldn't find a system named **{destination}**."

    path = get_route(a_id, b_id)
    if not path:
        return f"No route returned from **{origin}** to **{destination}**."

    # Fetch security statuses (best effort; cache is handled elsewhere)
    secs: List[float] = []
    low_count = 0
    null_count = 0
    first_non_high: Optional[Tuple[str, float]] = None

    for sid in path:
        info = get_system_info(int(sid))
        sec = float(info.get("security_status", 0.0))
        secs.append(sec)
        name = info.get("name", str(sid))
        if sec < 0.5:
            low_count += 1
            if first_non_high is None:
                first_non_high = (name, sec)
        if sec <= 0.0:
            null_count += 1
            if first_non_high is None:
                first_non_high = (name, sec)

    jumps = max(0, len(path) - 1)
    lowest = min(secs) if secs else 0.0

    # Advice (simple + practical)
    advice_lines = []
    if lowest < 0.5:
        advice_lines.append("**Advice:** This route enters **lowsec/null**. Use a scout, avoid autopilot, stay aligned, and expect gate camps.")
        if first_non_high:
            advice_lines.append(f"First non-highsec system: **{first_non_high[0]}** (sec {first_non_high[1]:.2f}).")
    else:
        advice_lines.append("**Advice:** Mostly **highsec** route. Still watch for ganks near hubs; avoid hauling bling through trade lanes.")

    # Home/hub awareness (light touch)
    hubs = ", ".join([EVE_HOME_SYSTEM] + EVE_LOCAL_HUBS)
    advice_lines.append(f"Ops area context: home **{EVE_HOME_SYSTEM}**, hubs **{', '.join(EVE_LOCAL_HUBS)}** (watch the pipes).")

    return (
        f"**Route:** {origin} → {destination}\n"
        f"**Jumps:** {jumps}\n"
        f"**Lowest security:** {lowest:.2f}\n"
        f"**Lowsec systems on route:** {low_count} | **Nullsec:** {null_count}\n"
        + "\n".join(advice_lines)
    )



def eve_price_jita(item_name: str) -> str:
    tid = resolve_type_id(item_name)
    if tid is None:
        return f"I couldn't resolve an item named **{item_name}**."

    JITA_STATION_ID = 60003760  # Jita IV - Moon 4 - Caldari Navy Assembly Plant
    try:
        r = requests.get(
            "https://market.fuzzwork.co.uk/aggregates/",
            params={"station": JITA_STATION_ID, "types": str(tid)},
            timeout=15
        )
        r.raise_for_status()
        data = r.json() or {}
        node = data.get(str(tid)) or {}
        buy = node.get("buy") or {}
        sell = node.get("sell") or {}

        best_sell = sell.get("min")
        best_buy = buy.get("max")

        if best_sell is None and best_buy is None:
            return f"💠 **{item_name}**\nNo market orders found for this item at Jita right now."

        lines = [f"💠 **{item_name}** (Jita IV - Moon 4)"]
        if best_sell is not None:
            lines.append(f"• Best sell: {float(best_sell):,.2f} ISK")
        if best_buy is not None:
            lines.append(f"• Best buy: {float(best_buy):,.2f} ISK")
        lines.append("Source: Fuzzwork Market Data")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ EVE price lookup failed: {e}"

def pilot_stats(pilot_name: str) -> str:
    cid = resolve_character_id(pilot_name)
    if cid is None:
        return (
            f"I couldn't resolve a pilot named **{pilot_name}**.\n"
            f"Tip: pilot name resolution needs the exact character name spelling."
        )

    info = get_character_info(cid)
    sec = info.get("security_status")
    name = info.get("name", pilot_name)
    return (
        f"**Pilot:** {name}\n"
        f"**Security status:** {sec}\n"
        f"**zKillboard:** {zkill_character_url(cid)}"
    )


def threat_check(pilot_name: str, context_system: Optional[str] = None) -> str:
    cid = resolve_character_id(pilot_name)
    if cid is None:
        return (
            f"I couldn't resolve a pilot named **{pilot_name}**.\n"
            f"Tip: use the exact character name spelling."
        )

    info = get_character_info(cid)
    sec = float(info.get("security_status", 0.0))

    # zKill best-effort
    try:
        kills = zkill_recent_kills(cid, limit=80)
    except Exception:
        kills = []
    try:
        losses = zkill_recent_losses(cid, limit=80)
    except Exception:
        losses = []

    kills_24h = _count_recent(kills, 24 * 3600)
    kills_7d = _count_recent(kills, 7 * 24 * 3600)
    losses_7d = _count_recent(losses, 7 * 24 * 3600)

    solo_7d, fleet_7d = _solo_fleet_counts(kills, 7 * 24 * 3600)

    # Scores: simple, explainable
    solo_score = min(100, solo_7d * 12 + kills_24h * 18)
    fleet_score = min(100, fleet_7d * 8 + max(0, kills_7d - solo_7d) * 2)
    overall = max(solo_score, fleet_score)

    if overall >= 70:
        threat = "HIGH"
    elif overall >= 35:
        threat = "MED"
    else:
        threat = "LOW"

    # Advice
    advice = []
    if solo_score >= 70:
        advice.append("**Solo threat:** HIGH — avoid 1v1. Stay aligned, keep range, assume tackle, use d-scan and pings.")
    elif solo_score >= 35:
        advice.append("**Solo threat:** MED — don't take isolated fights; move with at least one buddy and watch bait.")
    else:
        advice.append("**Solo threat:** LOW — standard precautions still apply.")

    if fleet_score >= 70:
        advice.append("**Fleet threat:** HIGH — assume backup. Scout gates, don't commit until you confirm numbers.")
    elif fleet_score >= 35:
        advice.append("**Fleet threat:** MED — could have friends nearby; watch local spikes and intel channels.")
    else:
        advice.append("**Fleet threat:** LOW — not much sign of group activity recently.")

    # Context sensitivity (home/hubs); we don't compute jump radius here to stay light.
    if context_system:
        cs = context_system.strip()
        if cs.lower() in {EVE_HOME_SYSTEM.lower(), *(h.lower() for h in EVE_LOCAL_HUBS)}:
            advice.append(f"**Context:** This is a watched hub/home system (**{cs}**). Treat threat one level higher operationally.")

    return (
        f"**Threat Report:** {info.get('name', pilot_name)}\n"
        f"**Overall threat:** {threat} (score {overall}/100)\n"
        f"**Security status:** {sec:.2f}\n"
        f"Recent: **Kills 24h:** {kills_24h} | **Kills 7d:** {kills_7d} | **Losses 7d:** {losses_7d}\n"
        f"Profile: **Solo kills 7d:** {solo_7d} | **Fleet-involved kills 7d:** {fleet_7d}\n"
        + "\n".join(advice) + "\n"
        f"**zKillboard:** {zkill_character_url(cid)}"
    )


def home_heat() -> str:
    # Quick activity summary for home + hubs (no graph expansion to keep it fast and reliable).
    systems = [EVE_HOME_SYSTEM] + EVE_LOCAL_HUBS
    kills = get_system_kills()

    lines = ["**Home Heat (last hour):**"]
    for s in systems:
        sid = resolve_system_id(s)
        if sid is None:
            lines.append(f"- {s}: (unknown system)")
            continue
        row = next((r for r in kills if int(r.get("system_id", -1)) == sid), None)
        if not row:
            lines.append(f"- {s}: no data")
            continue
        ship = int(row.get("ship_kills", 0))
        pod = int(row.get("pod_kills", 0))
        npc = int(row.get("npc_kills", 0))
        pvp = ship + pod
        heat = "HIGH" if pvp >= 20 else "MED" if pvp >= 5 else "LOW"
        lines.append(f"- **{s}**: {heat} | ship {ship}, pod {pod}, npc {npc}")

    return "\n".join(lines)


def eve_materials(item_name: str, runs: int = 1) -> str:
    """Manufacturing bill of materials using EVE Ref industry cost API."""
    tid = resolve_type_id(item_name)
    if tid is None:
        return f"I couldn't resolve an EVE item named **{item_name}**."

    try:
        r = requests.get(
            "https://api.everef.net/v1/industry/cost",
            params={"product_id": str(tid), "runs": str(runs)},
            timeout=20
        )
        r.raise_for_status()
        data = r.json() or {}
        mfg = data.get("manufacturing") or {}

        entry = None
        if isinstance(mfg, dict) and mfg:
            entry = next(iter(mfg.values()))
        if not entry:
            return f"I couldn't find manufacturing data for **{item_name}** (type_id {tid})."

        mats = entry.get("materials") or {}
        items = []
        for v in mats.values():
            try:
                items.append((int(v.get("type_id")), float(v.get("quantity", 0))))
            except Exception:
                continue
        items.sort(key=lambda x: x[1], reverse=True)

        lines = [f"🧱 **Bill of Materials:** {item_name} (runs: {runs})"]
        for type_id, qty in items[:20]:
            lines.append(f"• {_safe_type_name(type_id)}: {qty:,.0f}")
        if len(items) > 20:
            lines.append(f"…and {len(items)-20} more material types")

        lines.append("\n**Notes / Advice:**")
        lines.append("• Quantities change with blueprint ME, structure rigs, and skills.")
        lines.append("• Compare build cost vs Jita buy/sell and your local hub.")
        lines.append("Source: EVE Ref Industry Cost API")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ BOM lookup failed: {e}"

def _safe_type_name(type_id: int) -> str:
    try:
        info = _esi_get_json(f"/universe/types/{type_id}/")
        return info.get("name") or str(type_id)
    except Exception:
        return str(type_id)
