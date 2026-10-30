from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from aibackup.adapters.base import Adapter, Root, map_json_paths, rewrite_json, rewrite_json_paths, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper
from aibackup.sqlite_snapshot import rewrite_columns, table_exists

_PATH_COLUMNS = {
    "project": ("worktree",),
    "session": ("directory",),
    "project_directory": ("directory",),
    "workspace": ("directory",),
}

# OpenCode Desktop keeps its UI state in drafts.sqlite (table ``state``: name, key, value).
# The sidebar only lists the projects in ``usedProjectIDs`` of the ``server`` entry, so
# without it restored sessions stay invisible. Older versions kept the same entries in
# JSON files (``default.dat``, ``opencode.*.dat``) that Desktop imports on start.
DESKTOP_STATE = "drafts.sqlite"
_GLOBAL_STORE = "opencode.global.dat"
_SERVER_KEY = "server"
LOCAL_SCOPE = "local"


def desktop_dir(env: Env) -> Path:
    return env.user_config_dir() / "ai.opencode.desktop"


def desktop_running(path: Path) -> bool:
    """Whether OpenCode Desktop (an Electron app) has its user data dir open."""
    if os.path.lexists(path / "SingletonLock"):
        return True
    lockfile = path / "lockfile"  # Windows
    if lockfile.exists():
        try:
            with lockfile.open("ab"):
                pass
        except OSError:
            return True
    return False


def _server_state(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM state WHERE name = ? AND key = ?", (_GLOBAL_STORE, _SERVER_KEY)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _save_server_state(conn: sqlite3.Connection, value: dict, now_ms: int) -> None:
    conn.execute(
        "INSERT INTO state (name, key, value, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (name, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (_GLOBAL_STORE, _SERVER_KEY, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now_ms),
    )


def add_used_projects(conn: sqlite3.Connection, used: dict[str, list[str]], now_ms: int) -> int:
    """Append project ids per server scope to the sidebar list; returns how many were added."""
    server = _server_state(conn) or {}
    current = server.get("usedProjectIDs")
    if not isinstance(current, dict):
        current = server["usedProjectIDs"] = {}
    added = 0
    for scope, ids in used.items():
        listed = current.get(scope) if isinstance(current.get(scope), list) else []
        new = [i for i in ids if i not in listed]
        if new:
            current[scope] = listed + new
            added += len(new)
    if added:
        _save_server_state(conn, server, now_ms)
    return added


def _is_window_store(name: str) -> bool:
    # Per-window state (open tabs) belongs to windows that don't exist on the target.
    return name.startswith("opencode.window.")


def merge_desktop_state(live: Path, archived: Path, now_ms: int) -> int:
    """Copy missing UI state and sidebar projects from ``archived`` into ``live``."""
    src = sqlite3.connect(archived)
    dst = sqlite3.connect(live, timeout=5)
    try:
        if not (table_exists(src, "state") and table_exists(dst, "state")):
            return 0
        changes = 0
        with dst:
            for name, key, value, updated in src.execute("SELECT name, key, value, updated_at FROM state"):
                if _is_window_store(name) or (name, key) == (_GLOBAL_STORE, _SERVER_KEY):
                    continue
                cur = dst.execute(
                    "INSERT INTO state (name, key, value, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (name, key, value, updated),
                )
                changes += cur.rowcount
            archived_server = _server_state(src) or {}
            used = archived_server.get("usedProjectIDs")
            if isinstance(used, dict):
                ids = {s: [i for i in v if isinstance(i, str)] for s, v in used.items() if isinstance(v, list)}
                changes += add_used_projects(dst, ids, now_ms)
        return changes
    finally:
        src.close()
        dst.close()


@dataclass
class ProjectListResult:
    storage: Path
    added: list[str] = field(default_factory=list)  # worktrees
    listed: int = 0
    missing: dict[str, int] = field(default_factory=dict)  # worktree -> sessions


def rebuild_project_list(env: Env, *, dry_run: bool) -> ProjectListResult:
    """Add every project with sessions whose folder exists to OpenCode Desktop's sidebar."""
    db = env.xdg_data() / "opencode" / "opencode.db"
    desktop = desktop_dir(env)
    state = desktop / DESKTOP_STATE
    if not db.is_file():
        raise ValueError(f"{db} not found")
    if not state.is_file():
        raise ValueError(f"{state} not found; start OpenCode Desktop once")
    if not dry_run and desktop_running(desktop):
        raise ValueError("OpenCode Desktop is running; quit it and retry")
    result = ProjectListResult(state)
    src = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        columns = {r[1] for r in src.execute("PRAGMA table_info(project)")}
        archived = "AND p.time_archived IS NULL" if "time_archived" in columns else ""
        rows = src.execute(
            "SELECT p.id, p.worktree, count(s.id) FROM project p JOIN session s ON s.project_id = p.id "
            f"WHERE p.id != 'global' {archived} GROUP BY p.id ORDER BY max(s.time_updated) DESC"
        ).fetchall()
    finally:
        src.close()
    conn = sqlite3.connect(state, timeout=5)
    try:
        server = _server_state(conn) or {}
        listed = (server.get("usedProjectIDs") or {}).get(LOCAL_SCOPE) or []
        ids = []
        for pid, worktree, sessions in rows:
            local = env.local(worktree) if worktree else None
            if pid in listed:
                result.listed += 1
            elif local is None or not local.is_dir():
                result.missing[str(worktree)] = sessions
            else:
                ids.append(pid)
                result.added.append(worktree)
        if ids and not dry_run:
            with conn:
                add_used_projects(conn, {LOCAL_SCOPE: ids}, int(time.time() * 1000))
    finally:
        conn.close()
    return result


class OpenCode(Adapter):
    id = "opencode"
    name = "OpenCode"
    scrub = {("data", "opencode.db"): ("account", "account_state", "control_account", "credential", "session_share")}
    sqlite_merges = (("desktop", DESKTOP_STATE),)

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
            # OpenCode Desktop (Electron); everything but its UI state is Chromium cache.
            Root(
                "desktop",
                desktop_dir(env),
                rules(
                    ("data", DESKTOP_STATE, "default.dat", "opencode.*.dat"),
                    ("config", "opencode.settings"),
                ),
                default="cache",
            ),
        ]

    def rewrite_file(self, root: str, rel: str, data: bytes, mapper: PathMapper) -> bytes | None:
        if root == "desktop":
            return rewrite_json_paths(data, mapper) if rel.endswith(".dat") else None
        if root != "data":
            return None
        if fnmatchcase(rel, "storage/project/*.json"):
            return rewrite_json(data, (("worktree",),), mapper)
        if fnmatchcase(rel, "storage/session/*.json"):
            return rewrite_json(data, (("directory",),), mapper)
        return None

    def rewrite_sqlite(self, root: str, rel: str, conn: sqlite3.Connection, mapper: PathMapper) -> int:
        if root == "desktop" and rel == DESKTOP_STATE:
            return rewrite_columns(conn, "state", ("value",), lambda v: map_json_paths(v, mapper))
        if rel != "opencode.db":
            return 0
        return sum(rewrite_columns(conn, t, cols, mapper.map) for t, cols in _PATH_COLUMNS.items())

    def merge_sqlite(self, root: str, rel: str, live: Path, archived: Path) -> int:
        if desktop_running(live.parent):
            raise ValueError("OpenCode Desktop is running; quit it and restore again to merge its project list")
        return merge_desktop_state(live, archived, int(time.time() * 1000))
