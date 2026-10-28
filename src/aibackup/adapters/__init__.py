from aibackup.adapters.base import CATEGORIES, DEFAULT_CATEGORIES, Adapter, Root
from aibackup.adapters.claude_code import ClaudeCode

ADAPTERS: dict[str, Adapter] = {a.id: a for a in (ClaudeCode(),)}

__all__ = ["ADAPTERS", "CATEGORIES", "DEFAULT_CATEGORIES", "Adapter", "Root"]
