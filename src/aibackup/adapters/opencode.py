from __future__ import annotations

import sqlite3
from fnmatch import fnmatchcase

from aibackup.adapters.base import Adapter, Root, rewrite_json, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper
from aibackup.sqlite_snapshot import rewrite_columns

_PATH_COLUMNS = {
    "project": ("worktree",),
    "session": ("directory",),
    "project_directory": ("directory",),
    "workspace": ("directory",),
}


class OpenCode(Adapter):
    id = "opencode"
    name = "OpenCode"
    scrub = {("data", "opencode.db"): ("account", "account_state", "control_account", "credential", "session_share")}

    def roots(self, env: Env) -> list[Root]:
        # OpenCode uses XDG directories on every OS, including Windows.
        return [
            Root(
                "data",
                env.xdg_data() / "opencode",
                rules(
                    ("secret", "auth.json", "mcp-auth.json"),
                    ("checkpoint", "snapshot/*"),
                    ("cache", "bin/*", "log/*", "repos/*", "tool-output/*", "cache/*"),
                ),
            ),
            Root(
                "config",
                env.xdg_config() / "opencode",
                rules(("cache", "node_modules/*")),
                default="config",
            ),
        ]

    def rewrite_file(self, root: str, rel: str, data: bytes, mapper: PathMapper) -> bytes | None:
        if root != "data":
            return None
        if fnmatchcase(rel, "storage/project/*.json"):
            return rewrite_json(data, (("worktree",),), mapper)
        if fnmatchcase(rel, "storage/session/*.json"):
            return rewrite_json(data, (("directory",),), mapper)
        return None

    def rewrite_sqlite(self, root: str, rel: str, conn: sqlite3.Connection, mapper: PathMapper) -> int:
        if rel != "opencode.db":
            return 0
        return sum(rewrite_columns(conn, t, cols, mapper.map) for t, cols in _PATH_COLUMNS.items())
