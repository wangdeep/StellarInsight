from __future__ import annotations

from typing import Dict, Set


# Feature -> required ESI scopes
# Keep this list tight and add scopes only when a feature is actually implemented and exposed.
FEATURE_SCOPES: Dict[str, Set[str]] = {
    # Location / presence
    "location": {"esi-location.read_location.v1"},
    "ship": {"esi-location.read_ship_type.v1"},
    "online": {"esi-location.read_online.v1"},

    # Character economy / markets
    "wallet": {"esi-wallet.read_character_wallet.v1"},
    "transactions": {"esi-wallet.read_character_wallet.v1"},
    "orders": {"esi-markets.read_character_orders.v1"},

    # Skills / industry / PI
    "skillqueue": {"esi-skills.read_skillqueue.v1"},
    "skills": {"esi-skills.read_skills.v1"},
    "industry_jobs": {"esi-industry.read_character_jobs.v1"},
    "pi": {"esi-planets.manage_planets.v1"},

    # Corporation (roles still apply)
    "corp_structures": {"esi-corporations.read_structures.v1"},
    "corp_wallets": {"esi-wallet.read_corporation_wallets.v1"},
    "corp_orders": {"esi-markets.read_corporation_orders.v1"},
    "corp_industry_jobs": {"esi-industry.read_corporation_jobs.v1"},
    "corp_assets": {"esi-assets.read_corporation_assets.v1"},
    "corp_contracts": {"esi-contracts.read_corporation_contracts.v1"},
}


def parse_scopes(scopes_str: str | None) -> Set[str]:
    return {s.strip() for s in (scopes_str or "").split() if s.strip()}


def required_scopes_for(features: list[str]) -> Set[str]:
    req: Set[str] = set()
    for f in features:
        req |= FEATURE_SCOPES.get(f, set())
    return req
