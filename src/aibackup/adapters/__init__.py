from aibackup.adapters.base import CATEGORIES, DEFAULT_CATEGORIES, Adapter, Root
from aibackup.adapters.claude_code import ClaudeCode
from aibackup.adapters.codex import Codex
from aibackup.adapters.command_code import CommandCode
from aibackup.adapters.opencode import OpenCode
from aibackup.adapters.zed import Zed

ADAPTERS: dict[str, Adapter] = {a.id: a for a in (ClaudeCode(), Codex(), CommandCode(), OpenCode(), Zed())}

__all__ = ["ADAPTERS", "CATEGORIES", "DEFAULT_CATEGORIES", "Adapter", "Root"]
