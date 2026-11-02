from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aibackup.adapters.base import Adapter, Root, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper
from aibackup.sqlite_snapshot import in_use, rewrite_columns, table_exists


def data_dir(env: Env) -> Path:
    if env.os == "windows":
        return env.localappdata() / "Zed"
    if env.os == "macos":
        return env.mac_app_support() / "Zed"
    return env.xdg_data() / "zed"


# Zed's agent sidebar lists threads from ``sidebar_threads`` in the workspace database
# (db/0-<channel>/db.sqlite), grouped by ``folder_paths``. Zed filled it once from
# threads.db when the sidebar was introduced, so threads restored later stay invisible,
# and rows copied from another OS keep paths that never match an open project.
_SIDEBAR = "sidebar_threads"


@dataclass
class SidebarResult:
    storage: Path
    added: list[str] = field(default_factory=list)  # thread titles
    fixed: list[str] = field(default_factory=list)
    listed: int = 0
    missing: dict[str, int] = field(default_factory=dict)  # folder -> threads


def _exists(env: Env, folder_paths: str | None) -> bool:
    if not folder_paths:
        return True  # threads without a project are listed as such
    for path in folder_paths.split("\n"):
        local = env.local(path)
        if local is None or not local.is_dir():
            return False
    return True


def rebuild_sidebar(env: Env, *, dry_run: bool, channel: str = "stable") -> SidebarResult:
    """Make every top-level thread of threads.db visible in Zed's agent sidebar.

    Adds missing sidebar rows and, where a row points to folders that don't exist but
    threads.db (already migrated) points to existing ones, takes the paths from there.
    """
    threads_db = data_dir(env) / "threads" / "threads.db"
    workspace_db = data_dir(env) / "db" / f"0-{channel}" / "db.sqlite"
    for db in (threads_db, workspace_db):
        if not db.is_file():
            raise ValueError(f"{db} not found; start Zed once")
    if not dry_run and in_use(workspace_db):
        raise ValueError("Zed is running; quit it and retry")
    result = SidebarResult(workspace_db)

    src = sqlite3.connect(f"{threads_db.as_uri()}?mode=ro", uri=True)
    try:
        columns = {r[1] for r in src.execute("PRAGMA table_info(threads)")}
        if not {"id", "summary", "updated_at", "folder_paths", "parent_id"} <= columns:
            raise ValueError(f"{threads_db}: unknown schema")
        order = "folder_paths_order" if "folder_paths_order" in columns else "NULL"
        created = "created_at" if "created_at" in columns else "updated_at"
        threads = src.execute(
            f"SELECT id, summary, updated_at, {created}, folder_paths, {order} FROM threads WHERE parent_id IS NULL"
        ).fetchall()
    finally:
        src.close()

    conn = sqlite3.connect(workspace_db, timeout=5)
    try:
        if not table_exists(conn, _SIDEBAR):
            raise ValueError(f"{workspace_db}: this Zed version has no agent sidebar")
        side_columns = {r[1] for r in conn.execute(f"PRAGMA table_info({_SIDEBAR})")}
        rows = {
            session: (folders, worktrees)
            for session, folders, worktrees in conn.execute(
                f"SELECT session_id, folder_paths, main_worktree_paths FROM {_SIDEBAR}"
            )
        }
        inserts, updates = [], []
        for tid, title, updated, created_at, folders, folders_order in threads:
            if not _exists(env, folders):
                result.missing[folders] = result.missing.get(folders, 0) + 1
                continue
            if tid not in rows:
                inserts.append((uuid.uuid4().bytes, tid, title, updated, created_at, folders, folders_order))
                result.added.append(title)
                continue
            side_folders, side_worktrees = rows[tid]
            if side_folders != folders and not _exists(env, side_folders):
                worktrees = folders if side_worktrees in (None, side_folders) else side_worktrees
                updates.append((folders, folders_order, worktrees, folders_order, tid))
                result.fixed.append(title)
            else:
                result.listed += 1
        if not dry_run and (inserts or updates):
            has_worktrees = "main_worktree_paths" in side_columns
            with conn:
                for thread_id, tid, title, updated, created_at, folders, folders_order in inserts:
                    values = {
                        "thread_id": thread_id,
                        "session_id": tid,
                        "title": title,
                        "updated_at": updated,
                        "created_at": created_at,
                        "folder_paths": folders,
                        "folder_paths_order": folders_order,
                        "archived": 0,
                    }
                    if has_worktrees:
                        values.update(main_worktree_paths=folders, main_worktree_paths_order=folders_order)
                    names = ", ".join(values)
                    conn.execute(
                        f"INSERT INTO {_SIDEBAR} ({names}) VALUES ({', '.join('?' * len(values))})",
                        tuple(values.values()),
                    )
                for folders, folders_order, worktrees, worktrees_order, tid in updates:
                    set_worktrees = ", main_worktree_paths = ?, main_worktree_paths_order = ?" if has_worktrees else ""
                    params = (folders, folders_order) + ((worktrees, worktrees_order) if has_worktrees else ())
                    conn.execute(
                        f"UPDATE {_SIDEBAR} SET folder_paths = ?, folder_paths_order = ?{set_worktrees} "
                        "WHERE session_id = ?",
                        params + (tid,),
                    )
    finally:
        conn.close()
    return result


class Zed(Adapter):
    id = "zed"
    name = "Zed Agent"

    def roots(self, env: Env) -> list[Root]:
        if env.os == "windows":
            config = env.appdata() / "Zed"
        elif env.os == "macos":
            config = env.home / ".config" / "zed"
        else:
            config = env.xdg_config() / "zed"
        return [
            # The data dir mostly holds runtimes (Node, language servers,
            # extensions), so anything not listed here counts as cache.
            Root(
                "data",
                data_dir(env),
                rules(
                    ("cache", "prompts/*/lock.mdb"),
                    ("data", "threads/*", "conversations/*", "prompts/*"),
                ),
                default="cache",
            ),
            Root("config", config, default="config"),
        ]

    def rewrite_sqlite(self, root: str, rel: str, conn: sqlite3.Connection, mapper: PathMapper) -> int:
        def map_lines(value: str) -> str:
            return "\n".join(mapper.map(line) for line in value.split("\n"))

        if rel == "threads/threads.db":
            return rewrite_columns(conn, "threads", ("folder_paths",), map_lines)
        if rel.startswith("db/") and rel.endswith("/db.sqlite"):
            # Only present in archives that include the cache category.
            return rewrite_columns(conn, _SIDEBAR, ("folder_paths", "main_worktree_paths"), map_lines)
        return 0
