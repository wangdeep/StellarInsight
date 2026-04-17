"""Scope guard helpers for EVE ESI calls."""
from eve.memory_sqlite import MemoryStore
from eve.eve_scopes import parse_scopes

def check_scope(scopes: str, required: str) -> bool:
    return required in parse_scopes(scopes)
