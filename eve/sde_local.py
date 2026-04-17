"""
sde_local.py — Local SDE SQLite query module (r92)

Replaces all Fuzzwork API proxy calls with direct SQLite queries
against the embedded Fuzzwork SDE dump at /app/data/sde.sqlite.

Tables used:
  invTypes         — type IDs, names, groups, market groups
  invGroups        — group names, category IDs
  invCategories    — category names (Ships=6, Modules=7, etc.)
  invMarketGroups  — market group tree (parentGroupID, name)
  industryActivityMaterials — blueprint input materials
  industryActivityProducts  — blueprint output products
  industryActivity          — build times per activity
  dgmTypeAttributes         — dogma attributes per type (CPU, PG, slots, etc.)
  dgmAttributeTypes         — attribute ID → name mapping
"""

import sqlite3
import logging
import os
import sys
from typing import Dict, List, Optional, Any

logger = logging.getLogger("xylon.sde_local")


def _default_sde_path() -> str:
    """Return the path to sde.sqlite next to the executable (or next to this file
    when running from source). Respects SDE_SQLITE_PATH env override."""
    if "SDE_SQLITE_PATH" in os.environ:
        return os.environ["SDE_SQLITE_PATH"]
    # PyInstaller sets sys.frozen and sys.executable to the bundle path.
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        # Running from source: place data/ next to the eve/ package's parent dir.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "sde.sqlite")


SDE_PATH = _default_sde_path()

_sde_conn: Optional[sqlite3.Connection] = None
_ships_cache: Optional[List[Dict]] = None


def _get_sde() -> sqlite3.Connection:
    """Get or create a read-only connection to the SDE database.
    Reopens if the connection has gone stale (e.g. after container restart).
    """
    global _sde_conn
    if _sde_conn is not None:
        # Verify connection is still alive
        try:
            _sde_conn.execute("SELECT 1")
        except Exception:
            logger.warning("[SDE] Stale connection detected, reopening.")
            try:
                _sde_conn.close()
            except Exception:
                pass
            _sde_conn = None
    if _sde_conn is None:
        if not os.path.exists(SDE_PATH):
            raise FileNotFoundError(
                f"SDE database not found at {SDE_PATH}. "
                f"Run: bash scripts/download_sde.sh"
            )
        _sde_conn = sqlite3.connect(f"file:{SDE_PATH}?mode=ro", uri=True)
        _sde_conn.row_factory = sqlite3.Row
        logger.info(f"[SDE] Opened local SDE database: {SDE_PATH}")
    return _sde_conn


def sde_available() -> bool:
    """Check if the SDE database file exists."""
    return os.path.exists(SDE_PATH)


# ── Type Names ─────────────────────────────────────────────────────────────

def get_type_names(type_ids: List[int]) -> Dict[int, str]:
    """Resolve type IDs to names. Returns {typeID: typeName}."""
    if not type_ids:
        return {}
    con = _get_sde()
    result = {}
    # SQLite has a variable limit, batch in chunks of 500
    for i in range(0, len(type_ids), 500):
        chunk = type_ids[i:i+500]
        placeholders = ",".join("?" for _ in chunk)
        rows = con.execute(
            f"SELECT typeID, typeName FROM invTypes WHERE typeID IN ({placeholders})",
            chunk
        ).fetchall()
        for r in rows:
            result[r["typeID"]] = r["typeName"]
    return result


def get_type_name(type_id: int) -> Optional[str]:
    """Resolve a single type ID to its name."""
    names = get_type_names([type_id])
    return names.get(type_id)


# ── Market Groups (full tree) ─────────────────────────────────────────────

def get_market_groups() -> List[Dict]:
    """Get the full market group tree. Returns list of {marketGroupID, parentGroupID, marketGroupName}."""
    con = _get_sde()
    rows = con.execute(
        "SELECT marketGroupID, parentGroupID, marketGroupName, description "
        "FROM invMarketGroups ORDER BY marketGroupName"
    ).fetchall()
    return [dict(r) for r in rows]


def get_market_group_items(group_id: int) -> List[Dict]:
    """Get all items in a market group. Returns [{typeID, typeName}, ...]."""
    con = _get_sde()
    rows = con.execute(
        "SELECT typeID, typeName FROM invTypes "
        "WHERE marketGroupID = ? AND published = 1 "
        "ORDER BY typeName",
        (group_id,)
    ).fetchall()
    return [{"typeID": r["typeID"], "typeName": r["typeName"]} for r in rows]


# ── Blueprint Details ──────────────────────────────────────────────────────

def get_blueprint_details(blueprint_type_id: int, activity_id: int = 1) -> Dict:
    """Get manufacturing materials and products for a blueprint.
    
    activity_id: 1=Manufacturing, 5=Copying, 8=Invention, 11=Reactions
    Returns {manufacturing: {materials: [{typeID, quantity}], products: [{typeID, quantity}], time: int}}
    """
    con = _get_sde()
    
    materials = con.execute(
        "SELECT materialTypeID as typeID, quantity "
        "FROM industryActivityMaterials "
        "WHERE typeID = ? AND activityID = ?",
        (blueprint_type_id, activity_id)
    ).fetchall()
    
    products = con.execute(
        "SELECT productTypeID as typeID, quantity "
        "FROM industryActivityProducts "
        "WHERE typeID = ? AND activityID = ?",
        (blueprint_type_id, activity_id)
    ).fetchall()
    
    time_row = con.execute(
        "SELECT time FROM industryActivity "
        "WHERE typeID = ? AND activityID = ?",
        (blueprint_type_id, activity_id)
    ).fetchone()

    # Also check what activities this blueprint supports
    supported = [r[0] for r in con.execute(
        "SELECT activityID FROM industryActivity WHERE typeID = ?",
        (blueprint_type_id,)
    ).fetchall()]
    
    act_key = {1: "manufacturing", 5: "copying", 8: "invention", 11: "reactions"}.get(activity_id, "manufacturing")
    return {
        str(blueprint_type_id): {
            act_key: {
                "materials": [{"typeID": r["typeID"], "quantity": r["quantity"]} for r in materials],
                "products":  [{"typeID": r["typeID"], "quantity": r["quantity"]} for r in products],
                "time": time_row["time"] if time_row else 0,
            },
            "supported_activities": supported,
        }
    }


# ── Ships (grouped by class + faction) ────────────────────────────────────

# Ship category = 6 in invCategories
FACTION_KEYWORDS = [
    "Amarr", "Caldari", "Gallente", "Minmatar",
    "Angel", "Blood", "Guristas", "Sansha", "Serpentis",
    "Sisters", "Society", "Triglavian", "Mordu", "ORE",
    "SOCT", "EDENCOM", "Upwell",
]

SHIP_NAME_PREFIXES = {
    "Omen": "Amarr", "Punisher": "Amarr", "Tormentor": "Amarr", "Harbinger": "Amarr",
    "Abaddon": "Amarr", "Apocalypse": "Amarr", "Maller": "Amarr", "Augoror": "Amarr",
    "Zealot": "Amarr", "Sacrilege": "Amarr", "Devoter": "Amarr", "Guardian": "Amarr",
    "Prophecy": "Amarr", "Armageddon": "Amarr", "Paladin": "Amarr", "Redeemer": "Amarr",
    "Revelation": "Amarr", "Archon": "Amarr", "Aeon": "Amarr", "Avatar": "Amarr",
    "Merlin": "Caldari", "Kestrel": "Caldari", "Heron": "Caldari", "Griffin": "Caldari",
    "Condor": "Caldari", "Moa": "Caldari", "Caracal": "Caldari", "Osprey": "Caldari",
    "Blackbird": "Caldari", "Cerberus": "Caldari", "Eagle": "Caldari", "Onyx": "Caldari",
    "Basilisk": "Caldari", "Drake": "Caldari", "Ferox": "Caldari", "Naga": "Caldari",
    "Raven": "Caldari", "Rokh": "Caldari", "Scorpion": "Caldari", "Golem": "Caldari",
    "Widow": "Caldari", "Phoenix": "Caldari", "Chimera": "Caldari", "Wyvern": "Caldari",
    "Leviathan": "Caldari",
    "Tristan": "Gallente", "Incursus": "Gallente", "Atron": "Gallente", "Maulus": "Gallente",
    "Navitas": "Gallente", "Imicus": "Gallente", "Vexor": "Gallente", "Thorax": "Gallente",
    "Celestis": "Gallente", "Exequror": "Gallente", "Ishtar": "Gallente", "Deimos": "Gallente",
    "Phobos": "Gallente", "Oneiros": "Gallente", "Brutix": "Gallente", "Myrmidon": "Gallente",
    "Talos": "Gallente", "Megathron": "Gallente", "Dominix": "Gallente", "Hyperion": "Gallente",
    "Kronos": "Gallente", "Sin": "Gallente", "Moros": "Gallente", "Thanatos": "Gallente",
    "Nyx": "Gallente", "Erebus": "Gallente",
    "Rifter": "Minmatar", "Slasher": "Minmatar", "Breacher": "Minmatar", "Probe": "Minmatar",
    "Vigil": "Minmatar", "Burst": "Minmatar", "Rupture": "Minmatar", "Stabber": "Minmatar",
    "Bellicose": "Minmatar", "Scythe": "Minmatar", "Vagabond": "Minmatar", "Muninn": "Minmatar",
    "Broadsword": "Minmatar", "Scimitar": "Minmatar", "Hurricane": "Minmatar",
    "Cyclone": "Minmatar", "Tornado": "Minmatar", "Tempest": "Minmatar",
    "Maelstrom": "Minmatar", "Typhoon": "Minmatar", "Vargur": "Minmatar",
    "Panther": "Minmatar", "Naglfar": "Minmatar", "Nidhoggur": "Minmatar",
    "Hel": "Minmatar", "Ragnarok": "Minmatar",
}


def _detect_faction(name: str) -> str:
    """Detect ship faction from its name."""
    name_lower = name.lower()
    for kw in FACTION_KEYWORDS:
        if kw.lower() in name_lower:
            return kw
    first_word = name.split()[0] if name else ""
    return SHIP_NAME_PREFIXES.get(first_word, "Other")


def get_all_ships() -> List[Dict]:
    """Get all published ships grouped by class and faction. Cached after first call."""
    global _ships_cache
    if _ships_cache is not None:
        return _ships_cache
    con = _get_sde()
    rows = con.execute(
        "SELECT t.typeID, t.typeName, g.groupName, g.groupID "
        "FROM invTypes t "
        "JOIN invGroups g ON t.groupID = g.groupID "
        "WHERE g.categoryID = 6 AND t.published = 1 "
        "ORDER BY g.groupName, t.typeName"
    ).fetchall()

    ships = []
    for r in rows:
        ships.append({
            "typeID": r["typeID"],
            "name": r["typeName"],
            "group": r["groupName"],
            "groupID": r["groupID"],
            "faction": _detect_faction(r["typeName"]),
        })
    logger.info(f"[SDE] get_all_ships: {len(ships)} ships (cached)")
    _ships_cache = ships
    return _ships_cache


# ── Dogma Attributes ──────────────────────────────────────────────────────

def get_type_dogma(type_id: int) -> Dict:
    """Get dogma attributes for a type (ship/module).
    
    Returns {typeName, dogmaAttributes: [{attributeID, attributeName, value}]}
    """
    con = _get_sde()
    
    name_row = con.execute(
        "SELECT typeName FROM invTypes WHERE typeID = ?", (type_id,)
    ).fetchone()
    
    attrs = con.execute(
        "SELECT a.attributeID, a.attributeName, COALESCE(ta.valueFloat, ta.valueInt) as value "
        "FROM dgmTypeAttributes ta "
        "JOIN dgmAttributeTypes a ON ta.attributeID = a.attributeID "
        "WHERE ta.typeID = ?",
        (type_id,)
    ).fetchall()
    
    effects = con.execute(
        "SELECT effectID FROM dgmTypeEffects WHERE typeID = ?",
        (type_id,)
    ).fetchall()

    return {
        "typeName": name_row["typeName"] if name_row else f"Type #{type_id}",
        "typeID": type_id,
        "dogmaAttributes": [
            {"attributeID": r["attributeID"], "attributeName": r["attributeName"], "value": r["value"]}
            for r in attrs
        ],
        "dogmaEffects": [r["effectID"] for r in effects],
    }


# ── Type Search ───────────────────────────────────────────────────────────

def type_search(query: str, limit: int = 80) -> List[Dict]:
    """Search types by name. Returns [{typeID, typeName, groupName}]."""
    if not query or len(query) < 2:
        return []
    con = _get_sde()
    rows = con.execute(
        "SELECT t.typeID, t.typeName, g.groupName "
        "FROM invTypes t "
        "JOIN invGroups g ON t.groupID = g.groupID "
        "WHERE t.typeName LIKE ? AND t.published = 1 "
        "ORDER BY t.typeName LIMIT ?",
        (f"%{query}%", limit)
    ).fetchall()
    return [{"typeID": r["typeID"], "typeName": r["typeName"], "groupName": r["groupName"]} for r in rows]


# Slot effect IDs in dgmTypeEffects:
#   12 = hiPower (high slot)
#   13 = medPower (medium slot)
#   11 = loPower (low slot)
#   2663 = rigSlot
SLOT_EFFECT_IDS = {
    "high": 12,
    "med": 13,
    "low": 11,
    "rig": 2663,
}


def module_search(query: str = "", slot: str = "", limit: int = 80) -> List[Dict]:
    """Search modules by name, optionally filtered to a slot type.
    
    slot: 'high', 'med', 'low', 'rig', or '' for all
    Returns [{typeID, typeName, groupName, slot}]
    """
    con = _get_sde()
    effect_id = SLOT_EFFECT_IDS.get(slot)

    if effect_id and query:
        # Filter by slot AND name search
        rows = con.execute(
            "SELECT DISTINCT t.typeID, t.typeName, g.groupName "
            "FROM invTypes t "
            "JOIN invGroups g ON t.groupID = g.groupID "
            "JOIN dgmTypeEffects e ON t.typeID = e.typeID "
            "WHERE e.effectID = ? AND t.typeName LIKE ? AND t.published = 1 "
            "ORDER BY t.typeName LIMIT ?",
            (effect_id, f"%{query}%", limit)
        ).fetchall()
    elif effect_id:
        # Browse all modules for a slot type (no search query — return popular/common)
        rows = con.execute(
            "SELECT DISTINCT t.typeID, t.typeName, g.groupName "
            "FROM invTypes t "
            "JOIN invGroups g ON t.groupID = g.groupID "
            "JOIN dgmTypeEffects e ON t.typeID = e.typeID "
            "WHERE e.effectID = ? AND t.published = 1 "
            "ORDER BY t.typeName LIMIT ?",
            (effect_id, limit)
        ).fetchall()
    elif query:
        # Search equippable modules only — must have a slot effect (hi/med/low/rig)
        # This excludes charges, implants, deployables, etc. that are in category 7
        rows = con.execute(
            "SELECT DISTINCT t.typeID, t.typeName, g.groupName "
            "FROM invTypes t "
            "JOIN invGroups g ON t.groupID = g.groupID "
            "JOIN dgmTypeEffects e ON t.typeID = e.typeID "
            "WHERE g.categoryID = 7 AND e.effectID IN (11,12,13,2663) "
            "AND t.typeName LIKE ? AND t.published = 1 "
            "ORDER BY t.typeName LIMIT ?",
            (f"%{query}%", limit)
        ).fetchall()
    else:
        return []

    return [{"typeID": r["typeID"], "typeName": r["typeName"], "groupName": r["groupName"]} for r in rows]





def get_default_charge(charge_group_id: int) -> Dict:
    """Get the cheapest/smallest T1 charge for a given chargeGroup ID.
    Used to estimate weapon DPS when no charge is selected.
    Returns {typeID, typeName, emDamage, thermalDamage, kineticDamage, explosiveDamage}
    """
    if not charge_group_id:
        return {}
    con = _get_sde()
    # Find published types in this charge group, prefer smaller/T1 (lower typeID often = older/T1)
    rows = con.execute(
        "SELECT t.typeID, t.typeName FROM invTypes t "
        "WHERE t.groupID = ? AND t.published = 1 ORDER BY t.typeID LIMIT 20",
        (charge_group_id,)
    ).fetchall()
    if not rows:
        return {}
    # Pick the first one (usually T1 base ammo)
    charge_id = rows[0]["typeID"]
    attrs = con.execute(
        "SELECT attributeID, COALESCE(valueFloat, valueInt) as value "
        "FROM dgmTypeAttributes WHERE typeID = ? AND attributeID IN (114,116,118,120)",
        (charge_id,)
    ).fetchall()
    result = {"typeID": charge_id, "typeName": rows[0]["typeName"],
              "emDamage": 0, "thermalDamage": 0, "kineticDamage": 0, "explosiveDamage": 0}
    id_map = {114:"emDamage", 116:"thermalDamage", 118:"kineticDamage", 120:"explosiveDamage"}
    for a in attrs:
        key = id_map.get(a["attributeID"])
        if key:
            result[key] = a["value"] or 0
    return result


def get_slot_types(type_ids: List[int]) -> Dict[int, str]:
    """Return slot type for each type ID. Returns {typeID: 'high'|'med'|'low'|'rig'|None}.
    Types without a slot effect return None (not equippable as a module).
    """
    if not type_ids:
        return {}
    con = _get_sde()
    # effectID 12=hiPower, 13=medPower, 11=loPower, 2663=rigSlot
    EFFECT_TO_SLOT = {12: 'high', 13: 'med', 11: 'low', 2663: 'rig'}
    placeholders = ','.join('?' * len(type_ids))
    rows = con.execute(
        f"SELECT typeID, effectID FROM dgmTypeEffects "
        f"WHERE typeID IN ({placeholders}) AND effectID IN (11, 12, 13, 2663)",
        type_ids
    ).fetchall()
    result: Dict[int, str] = {}
    for row in rows:
        slot = EFFECT_TO_SLOT.get(row['effectID'])
        if slot:
            result[row['typeID']] = slot
    return result


def drone_search(query: str = "", limit: int = 100) -> List[Dict]:
    """Search drones (category 18). Returns [{typeID, typeName, groupName}]."""
    con = _get_sde()
    if query:
        rows = con.execute(
            "SELECT t.typeID, t.typeName, g.groupName "
            "FROM invTypes t "
            "JOIN invGroups g ON t.groupID = g.groupID "
            "WHERE g.categoryID = 18 AND t.typeName LIKE ? AND t.published = 1 "
            "ORDER BY t.typeName LIMIT ?",
            (f"%{query}%", limit)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT t.typeID, t.typeName, g.groupName "
            "FROM invTypes t "
            "JOIN invGroups g ON t.groupID = g.groupID "
            "WHERE g.categoryID = 18 AND t.published = 1 "
            "ORDER BY g.groupName, t.typeName LIMIT ?",
            (limit,)
        ).fetchall()
    return [{"typeID": r["typeID"], "typeName": r["typeName"], "groupName": r["groupName"]} for r in rows]


# ── r181: System Detail — Map & Celestial Queries ─────────────────────────────

# Planet type → PI quality mapping
_PI_PLANET_QUALITY = {
    "Temperate": {"quality": "excellent", "resources": "Aqueous, Industrial Fibers, Livestock, Autotrophs"},
    "Oceanic": {"quality": "excellent", "resources": "Aqueous, Complex Organisms, Micro Organisms, Planktic Colonies"},
    "Lava": {"quality": "good", "resources": "Felsic Magma, Suspended Plasma, Base Metals, Heavy Metals"},
    "Gas": {"quality": "good", "resources": "Aqueous, Ionic Solutions, Reactive Gas, Noble Gas"},
    "Storm": {"quality": "good", "resources": "Aqueous, Suspended Plasma, Ionic Solutions, Noble Gas"},
    "Barren": {"quality": "decent", "resources": "Aqueous, Base Metals, Noble Metals, Micro Organisms"},
    "Plasma": {"quality": "decent", "resources": "Suspended Plasma, Noble Gas, Base Metals, Heavy Metals"},
    "Ice": {"quality": "poor", "resources": "Aqueous, Heavy Metals, Noble Gas, Micro Organisms"},
    "Shattered": {"quality": "none", "resources": "Not extractable"},
}

# Trade hub system IDs for route calculation
_TRADE_HUBS = {
    30000142: "Jita",
    30002187: "Amarr",
    30002659: "Dodixie",
    30002053: "Hek",
    30002510: "Rens",
}

# Cached jump graph + distances
_jump_graph: Optional[Dict[int, List[int]]] = None
_hub_distances: Dict[int, Dict[int, int]] = {}  # system_id -> {hub_id: jumps}


def get_system_info_sde(system_id: int) -> Optional[Dict]:
    """Get basic system info from mapSolarSystems."""
    if not sde_available():
        return None
    try:
        con = _get_sde()
        row = con.execute(
            "SELECT solarSystemID, solarSystemName, constellationID, regionID, "
            "security, luminosity "
            "FROM mapSolarSystems WHERE solarSystemID=?",
            (system_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "system_id": row["solarSystemID"],
            "name": row["solarSystemName"],
            "constellation_id": row["constellationID"],
            "region_id": row["regionID"],
            "security": round(row["security"], 4),
            "luminosity": row["luminosity"],
        }
    except Exception as e:
        logger.debug(f"[SDE] get_system_info_sde error: {e}")
        return None


def get_region_name_sde(system_id: int) -> Optional[str]:
    """r184: Resolve system_id → region name via SDE (mapSolarSystems → mapRegions)."""
    if not sde_available():
        return None
    try:
        con = _get_sde()
        row = con.execute(
            "SELECT r.regionName FROM mapSolarSystems s "
            "JOIN mapConstellations c ON s.constellationID = c.constellationID "
            "JOIN mapRegions r ON c.regionID = r.regionID "
            "WHERE s.solarSystemID = ?",
            (system_id,)
        ).fetchone()
        return row["regionName"] if row else None
    except Exception as e:
        logger.debug(f"[SDE] get_region_name_sde error: {e}")
        return None


def get_system_description(system_id: int) -> str:
    """Get system lore description from mapSolarSystems (descriptionID → text)."""
    if not sde_available():
        return ""
    try:
        con = _get_sde()
        # Fuzzwork SDE stores description directly or via descriptionID
        # Try multiple approaches
        row = con.execute(
            "SELECT description FROM mapSolarSystems WHERE solarSystemID=?",
            (system_id,)
        ).fetchone()
        if row and row["description"]:
            return row["description"]
    except Exception:
        pass
    return ""


def get_constellation_info(constellation_id: int) -> Optional[Dict]:
    """Get constellation name, region, and all systems in it."""
    if not sde_available():
        return None
    try:
        con = _get_sde()
        crow = con.execute(
            "SELECT constellationName, regionID FROM mapConstellations WHERE constellationID=?",
            (constellation_id,)
        ).fetchone()
        if not crow:
            return None
        region_row = con.execute(
            "SELECT regionName FROM mapRegions WHERE regionID=?",
            (crow["regionID"],)
        ).fetchone()
        systems = con.execute(
            "SELECT solarSystemID, solarSystemName, security FROM mapSolarSystems "
            "WHERE constellationID=? ORDER BY solarSystemName",
            (constellation_id,)
        ).fetchall()
        return {
            "constellation_id": constellation_id,
            "name": crow["constellationName"],
            "region_id": crow["regionID"],
            "region_name": region_row["regionName"] if region_row else "",
            "systems": [{"id": s["solarSystemID"], "name": s["solarSystemName"],
                         "sec": round(s["security"], 2)} for s in systems],
        }
    except Exception as e:
        logger.debug(f"[SDE] get_constellation_info error: {e}")
        return None


def get_system_celestials(system_id: int) -> Dict:
    """Query mapDenormalize for all celestials in a system.
    Returns counts and typed lists for planets, moons, belts, stations, stargates."""
    if not sde_available():
        return {}
    try:
        con = _get_sde()
        rows = con.execute(
            "SELECT d.itemID, d.typeID, d.groupID, COALESCE(t.typeName, '') as typeName, "
            "COALESCE(d.itemName, '') as itemName "
            "FROM mapDenormalize d "
            "LEFT JOIN invTypes t ON d.typeID = t.typeID "
            "WHERE d.solarSystemID=? AND d.groupID IN (6,7,8,9,10,15)",
            (system_id,)
        ).fetchall()

        planets = []
        moons = []
        belts = []
        stations = []
        stargates = 0
        ice_belts = 0

        for r in rows:
            gid = r["groupID"]
            tname = r["typeName"] or ""
            iname = r["itemName"] or ""
            if gid == 7:  # Planet
                ptype = tname.replace("Planet (", "").replace(")", "") if "Planet (" in tname else tname
                pi = _PI_PLANET_QUALITY.get(ptype, {})
                planets.append({
                    "name": iname, "type": ptype,
                    "pi_quality": pi.get("quality", "unknown"),
                    "pi_resources": pi.get("resources", ""),
                })
            elif gid == 8:  # Moon
                moons.append({"name": iname})
            elif gid == 9:  # Asteroid Belt
                belts.append({"name": iname, "type": tname})
                if "ice" in tname.lower():
                    ice_belts += 1
            elif gid == 15:  # Station
                stations.append({"name": iname, "type": tname})
            elif gid == 10:  # Stargate
                stargates += 1

        return {
            "planets": planets,
            "moons_count": len(moons),
            "belts": belts,
            "belts_count": len(belts),
            "ice_belts": ice_belts,
            "stations": stations,
            "stations_count": len(stations),
            "stargates_count": stargates,
        }
    except Exception as e:
        logger.debug(f"[SDE] get_system_celestials error: {e}")
        return {}


def get_npc_stations(system_id: int) -> List[Dict]:
    """Get NPC stations from staStations with services info."""
    if not sde_available():
        return []
    try:
        con = _get_sde()
        rows = con.execute(
            "SELECT stationID, stationName, stationTypeID, "
            "corporationID, reprocessingEfficiency "
            "FROM staStations WHERE solarSystemID=?",
            (system_id,)
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "station_id": r["stationID"],
                "name": r["stationName"],
                "corp_id": r["corporationID"],
                "reprocessing": round(r["reprocessingEfficiency"] * 100, 1) if r["reprocessingEfficiency"] else 0,
            })
        return results
    except Exception as e:
        logger.debug(f"[SDE] get_npc_stations error: {e}")
        return []


def get_neighboring_systems(system_id: int) -> List[Dict]:
    """Get systems connected by stargates from mapSolarSystemJumps."""
    if not sde_available():
        return []
    try:
        con = _get_sde()
        rows = con.execute(
            "SELECT j.toSolarSystemID, s.solarSystemName, s.security "
            "FROM mapSolarSystemJumps j "
            "JOIN mapSolarSystems s ON j.toSolarSystemID = s.solarSystemID "
            "WHERE j.fromSolarSystemID=? ORDER BY s.solarSystemName",
            (system_id,)
        ).fetchall()
        return [{"id": r["toSolarSystemID"], "name": r["solarSystemName"],
                 "sec": round(r["security"], 2)} for r in rows]
    except Exception as e:
        logger.debug(f"[SDE] get_neighboring_systems error: {e}")
        return []


def _build_jump_graph() -> Dict[int, List[int]]:
    """Build the full K-space jump graph from mapSolarSystemJumps. Cached."""
    global _jump_graph
    if _jump_graph is not None:
        return _jump_graph
    if not sde_available():
        return {}
    try:
        con = _get_sde()
        rows = con.execute(
            "SELECT fromSolarSystemID, toSolarSystemID FROM mapSolarSystemJumps"
        ).fetchall()
        graph: Dict[int, List[int]] = {}
        for r in rows:
            f, t = r["fromSolarSystemID"], r["toSolarSystemID"]
            graph.setdefault(f, []).append(t)
        _jump_graph = graph
        logger.info(f"[SDE] Jump graph built: {len(graph)} systems")
        return graph
    except Exception as e:
        logger.debug(f"[SDE] _build_jump_graph error: {e}")
        return {}


def get_trade_hub_distances(system_id: int) -> Dict[str, int]:
    """BFS shortest path from system_id to each trade hub. Results cached."""
    if system_id in _hub_distances:
        return _hub_distances[system_id]

    graph = _build_jump_graph()
    if not graph or system_id not in graph:
        return {}

    # BFS from system_id
    from collections import deque
    visited = {system_id: 0}
    queue = deque([system_id])
    hub_ids = set(_TRADE_HUBS.keys())
    found = {}

    while queue and len(found) < len(hub_ids):
        current = queue.popleft()
        dist = visited[current]
        if current in hub_ids:
            found[_TRADE_HUBS[current]] = dist
        for neighbor in graph.get(current, []):
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)

    _hub_distances[system_id] = found
    return found


def get_nearest_sec_entry(system_id: int, target_sec: str = "highsec") -> Optional[Dict]:
    """BFS from system_id to find the nearest highsec or lowsec entry system.
    target_sec: 'highsec' (>=0.5) or 'lowsec' (>0.0 and <0.5)"""
    if not sde_available():
        return None
    graph = _build_jump_graph()
    if not graph or system_id not in graph:
        return None

    con = _get_sde()
    from collections import deque
    visited = {system_id: 0}
    queue = deque([system_id])

    while queue:
        current = queue.popleft()
        dist = visited[current]

        if current != system_id:
            row = con.execute(
                "SELECT solarSystemName, security FROM mapSolarSystems WHERE solarSystemID=?",
                (current,)
            ).fetchone()
            if row:
                sec = row["security"]
                if target_sec == "highsec" and sec >= 0.45:  # True highsec rounds to 0.5+
                    return {"id": current, "name": row["solarSystemName"],
                            "sec": round(sec, 2), "jumps": dist}
                elif target_sec == "lowsec" and 0.0 < sec < 0.45:
                    return {"id": current, "name": row["solarSystemName"],
                            "sec": round(sec, 2), "jumps": dist}

        # Cap BFS at 50 jumps to avoid runaway
        if dist >= 50:
            continue

        for neighbor in graph.get(current, []):
            if neighbor not in visited:
                visited[neighbor] = dist + 1
                queue.append(neighbor)

    return None


# ── Skills ───────────────────────────────────────────────────────────────

_ATTR_ID_MAP = {
    164: "charisma",
    165: "intelligence",
    166: "memory",
    167: "perception",
    168: "willpower",
}

# SP needed per level (multiplied by skill rank)
_SP_PER_LEVEL = [0, 250, 1414, 8000, 45255, 256000]


def get_all_skills() -> List[Dict]:
    """Return all published skills grouped by skill group, with rank and attribute data."""
    con = _get_sde()
    rows = con.execute(
        """
        SELECT
            t.typeID, t.typeName, g.groupName, g.groupID,
            COALESCE(ar.valueInt, ar.valueFloat)  AS rank,
            COALESCE(ap.valueInt, ap.valueFloat)  AS primaryAttrID,
            COALESCE(as_.valueInt, as_.valueFloat) AS secondaryAttrID
        FROM invTypes t
        JOIN invGroups g ON t.groupID = g.groupID
        LEFT JOIN dgmTypeAttributes ar  ON ar.typeID  = t.typeID AND ar.attributeID  = 275
        LEFT JOIN dgmTypeAttributes ap  ON ap.typeID  = t.typeID AND ap.attributeID  = 180
        LEFT JOIN dgmTypeAttributes as_ ON as_.typeID = t.typeID AND as_.attributeID = 181
        WHERE g.categoryID = 16 AND t.published = 1
        ORDER BY g.groupName, t.typeName
        """
    ).fetchall()

    skills = []
    for r in rows:
        rank = int(r["rank"]) if r["rank"] else 1
        pid  = int(r["primaryAttrID"]) if r["primaryAttrID"] else 0
        sid  = int(r["secondaryAttrID"]) if r["secondaryAttrID"] else 0
        skills.append({
            "typeID":       r["typeID"],
            "name":         r["typeName"],
            "group":        r["groupName"],
            "groupID":      r["groupID"],
            "rank":         rank,
            "primaryAttr":  _ATTR_ID_MAP.get(pid, "intelligence"),
            "secondaryAttr": _ATTR_ID_MAP.get(sid, "memory"),
            # SP needed to train each level (raw, caller multiplies by rank)
            "spPerLevel":   [_SP_PER_LEVEL[lvl] * rank for lvl in range(6)],
        })
    return skills


def get_skill_names(type_ids: List[int]) -> Dict[int, str]:
    """Resolve a list of skill type IDs to names. Falls back gracefully."""
    if not type_ids:
        return {}
    if not sde_available():
        return {}
    try:
        con = _get_sde()
        placeholders = ",".join("?" * len(type_ids))
        rows = con.execute(
            f"SELECT typeID, typeName FROM invTypes WHERE typeID IN ({placeholders})",
            type_ids,
        ).fetchall()
        return {r["typeID"]: r["typeName"] for r in rows}
    except Exception:
        return {}


# ── SDE Info / Diagnostics ────────────────────────────────────────────────

def sde_info() -> Dict:
    """Get diagnostic info about the SDE database."""
    if not sde_available():
        return {"available": False, "error": f"SDE not found at {SDE_PATH}"}
    try:
        con = _get_sde()
        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        type_count = con.execute("SELECT COUNT(*) as c FROM invTypes WHERE published=1").fetchone()["c"]
        group_count = con.execute("SELECT COUNT(*) as c FROM invMarketGroups").fetchone()["c"]
        ship_count = con.execute(
            "SELECT COUNT(*) as c FROM invTypes t JOIN invGroups g ON t.groupID=g.groupID "
            "WHERE g.categoryID=6 AND t.published=1"
        ).fetchone()["c"]
        bp_count = con.execute(
            "SELECT COUNT(DISTINCT typeID) as c FROM industryActivityMaterials WHERE activityID=1"
        ).fetchone()["c"]
        return {
            "available": True,
            "path": SDE_PATH,
            "tables": [r["name"] for r in tables],
            "published_types": type_count,
            "market_groups": group_count,
            "ships": ship_count,
            "blueprints_with_materials": bp_count,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}
