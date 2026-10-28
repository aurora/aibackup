"""Roo Code and its fork Zoo Code (VS Code extensions sharing one storage format)."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase

from aibackup.adapters.base import Adapter, Root, rewrite_json, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper

# VS Code and forks that can host the extension: (root name, user data dir name)
EDITORS = (
    ("code", "Code"),
    ("code-insiders", "Code - Insiders"),
    ("vscodium", "VSCodium"),
    ("cursor", "Cursor"),
    ("windsurf", "Windsurf"),
)

_WORKTREE = re.compile(r"^(\s*worktree\s*=\s*)(.*?)\s*$", re.MULTILINE)


def _git_unescape(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace("\\\\", "\\")


def _git_escape(value: str) -> str:
    return value.replace("\\", "\\\\")


class RooFamily(Adapter):
    extension_id = ""

    def roots(self, env: Env) -> list[Root]:
        base = env.user_config_dir()
        return [
            Root(
                name,
                base / folder / "User" / "globalStorage" / self.extension_id,
                rules(
                    ("checkpoint", "tasks/*/checkpoints/*"),
                    ("secret", "settings/mcp_settings.json"),
                    ("cache", "cache/*", "puppeteer/*"),
                    ("config", "settings/*"),
                ),
            )
            for name, folder in EDITORS
        ]

    def rewrite_file(self, root: str, rel: str, data: bytes, mapper: PathMapper) -> bytes | None:
        if rel == "tasks/_index.json":
            return rewrite_json(data, (("entries", "*", "workspace"),), mapper)
        if fnmatchcase(rel, "tasks/*/history_item.json"):
            return rewrite_json(data, (("workspace",),), mapper)
        if fnmatchcase(rel, "tasks/*/checkpoints/.git/config"):
            return self._rewrite_git_config(data, mapper)
        return None

    @staticmethod
    def _rewrite_git_config(data: bytes, mapper: PathMapper) -> bytes | None:
        text = data.decode("utf-8", errors="surrogateescape")

        def repl(m: re.Match) -> str:
            return m.group(1) + _git_escape(mapper.map(_git_unescape(m.group(2))))

        new = _WORKTREE.sub(repl, text)
        return new.encode("utf-8", errors="surrogateescape") if new != text else None

    def merge(self, root: str, rel: str, existing: bytes, incoming: bytes) -> bytes | None:
        if rel != "tasks/_index.json":
            return None
        try:
            old, new = json.loads(existing), json.loads(incoming)
            entries = {e["id"]: e for e in old.get("entries", [])}
            for e in new.get("entries", []):
                entries.setdefault(e["id"], e)
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        merged = dict(old)
        merged["entries"] = sorted(entries.values(), key=lambda e: e.get("ts") or 0, reverse=True)
        merged["updatedAt"] = max(old.get("updatedAt") or 0, new.get("updatedAt") or 0)
        return json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ZooCode(RooFamily):
    id = "zoo"
    name = "Zoo Code"
    extension_id = "zoocodeorganization.zoo-code"


class RooCode(RooFamily):
    id = "roo"
    name = "Roo Code"
    extension_id = "rooveterinaryinc.roo-cline"
