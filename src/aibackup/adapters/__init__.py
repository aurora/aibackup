from aibackup.adapters.base import CATEGORIES, DEFAULT_CATEGORIES, Adapter, Root

ADAPTERS: dict[str, Adapter] = {}

__all__ = ["ADAPTERS", "CATEGORIES", "DEFAULT_CATEGORIES", "Adapter", "Root"]
