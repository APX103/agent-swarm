"""Storage layer — SQLite-backed persistence (zero deps, Python built-in)."""
from src.storage.sqlite_store import SQLiteStore

__all__ = ["SQLiteStore"]
