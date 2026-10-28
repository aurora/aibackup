from __future__ import annotations

import sqlite3
from fnmatch import fnmatchcase

from aibackup.adapters.base import Adapter, Root, merge_jsonl, rewrite_jsonl, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper
from aibackup.sqlite_snapshot import rewrite_columns

_PATH_COLUMNS = {
    "threads": ("cwd", "rollout_path"),
    "local_thread_catalog": ("cwd",),
    "automation_runs": ("source_cwd",),
}


class Codex(Adapter):
    id = "codex"
    name = "Codex CLI"

    def roots(self, env: Env) -> list[Root]:
        return [
            Root(
                "home",
                env.var_path("CODEX_HOME", ".codex"),
                rules(
                    ("secret", "auth.json", ".credentials.json"),
                    (
                        "cache",
                        ".tmp/*",
                        "tmp/*",
                        "cache/*",
                        "log/*",
                        "logs_*.sqlite*",
                        "plugins/cache/*",
                        "plugins/.plugin-appserver/*",
                        "vendor_imports/*",
                        "models_cache.json",
                        "version.json",
                        "installation_id",
                    ),
                    ("config", "config.toml", "AGENTS.md", "plugins/*", "prompts/*", "rules/*", "skills/*"),
                ),
            )
        ]

    def rewrite_file(self, root: str, rel: str, data: bytes, mapper: PathMapper) -> bytes | None:
        if fnmatchcase(rel, "sessions/*.jsonl") or fnmatchcase(rel, "archived_sessions/*.jsonl"):
            return rewrite_jsonl(data, (("payload", "cwd"),), mapper)
        return None

    def rewrite_sqlite(self, root: str, rel: str, conn: sqlite3.Connection, mapper: PathMapper) -> int:
        return sum(rewrite_columns(conn, t, cols, mapper.map) for t, cols in _PATH_COLUMNS.items())

    def merge(self, root: str, rel: str, existing: bytes, incoming: bytes) -> bytes | None:
        if rel == "history.jsonl":
            return merge_jsonl(existing, incoming, "ts")
        return None
