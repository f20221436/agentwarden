"""Platform store (MongoDB)."""

from __future__ import annotations

from agentwarden.config import Settings
from agentwarden.store.base import PlatformStore, StoreUnavailable
from agentwarden.store.mongo import MongoStore

__all__ = ["PlatformStore", "StoreUnavailable", "MongoStore", "connect"]


def connect(settings: Settings) -> MongoStore:
    """Connect, verify reachability (fail closed) and ensure indexes."""
    store = MongoStore(settings.resolved_mongo_uri(), settings.mongo_db)
    store.ping()
    store.ensure_indexes()
    return store
