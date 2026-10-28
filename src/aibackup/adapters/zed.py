from __future__ import annotations

import sqlite3

from aibackup.adapters.base import Adapter, Root, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper
from aibackup.sqlite_snapshot import rewrite_columns


class Zed(Adapter):
    id = "zed"
    name = "Zed Agent"

    def roots(self, env: Env) -> list[Root]:
        if env.os == "windows":
            data, config = env.localappdata() / "Zed", env.appdata() / "Zed"
        elif env.os == "macos":
            data, config = env.mac_app_support() / "Zed", env.home / ".config" / "zed"
        else:
            data, config = env.xdg_data() / "zed", env.xdg_config() / "zed"
        return [
            # The data dir mostly holds runtimes (Node, language servers,
            # extensions), so anything not listed here counts as cache.
            Root(
                "data",
                data,
                rules(
                    ("cache", "prompts/*/lock.mdb"),
                    ("data", "threads/*", "conversations/*", "prompts/*"),
                ),
                default="cache",
            ),
            Root("config", config, default="config"),
        ]

    def rewrite_sqlite(self, root: str, rel: str, conn: sqlite3.Connection, mapper: PathMapper) -> int:
        if rel != "threads/threads.db":
            return 0

        def map_lines(value: str) -> str:
            return "\n".join(mapper.map(line) for line in value.split("\n"))

        return rewrite_columns(conn, "threads", ("folder_paths",), map_lines)
