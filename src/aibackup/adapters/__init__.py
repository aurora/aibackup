from aibackup.adapters.base import CATEGORIES, DEFAULT_CATEGORIES, Adapter, Root
from aibackup.adapters.claude_code import ClaudeCode
from aibackup.adapters.codex import Codex

ADAPTERS: dict[str, Adapter] = {a.id: a for a in (ClaudeCode(), Codex())}

__all__ = ["ADAPTERS", "CATEGORIES", "DEFAULT_CATEGORIES", "Adapter", "Root"]
