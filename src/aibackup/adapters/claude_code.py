from __future__ import annotations

import json
import re
from pathlib import Path

from aibackup import chromium_leveldb
from aibackup.adapters.base import Adapter, Root, merge_jsonl, rewrite_json_paths, rewrite_jsonl, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper

_ENCODE = re.compile(r"[^a-zA-Z0-9]")

# The Desktop app's page (claude.ai) keeps its sidebar in this Local Storage entry. Which
# group a local session belongs to is never synced to the account, only stored here.
_SIDEBAR_KEY = chromium_leveldb.local_storage_key("https://claude.ai", "dframe-store")
_LOCAL_STORAGE = "Local Storage/leveldb"


def encode_project_dir(path: str) -> str:
    """Claude Code's directory name for a project path (``C:\\a\\b`` -> ``C--a-b``)."""
    return _ENCODE.sub("-", path)


def merge_sidebar_state(src: dict, dst: dict) -> int:
    """Add the archived custom groups and assignments to ``dst``; existing ones win.

    Groups are matched by id, then by name. Returns the number of changes. The page
    derives the sidebar sections from these groups when it loads.
    """
    changes = 0
    dst_scopes = dst.setdefault("customGroupsByScope", {})
    for scope, archived in (src.get("customGroupsByScope") or {}).items():
        if not isinstance(archived, dict):
            continue
        target = dst_scopes.setdefault(scope, {"groups": [], "assignments": {}, "order": {}})
        groups = target.setdefault("groups", [])
        assignments = target.setdefault("assignments", {})
        by_id = {g.get("id"): g for g in groups}
        by_name = {g.get("name"): g.get("id") for g in groups}
        ids: dict[str, str] = {}
        for group in archived.get("groups", []):
            gid, name = group.get("id"), group.get("name")
            if gid in by_id:
                ids[gid] = gid
            elif name in by_name:
                ids[gid] = by_name[name]
            else:
                groups.append(dict(group))
                by_id[gid], by_name[name], ids[gid] = group, gid, gid
                changes += 1
        for key, gid in (archived.get("assignments") or {}).items():
            if key not in assignments and gid in ids:
                assignments[key] = ids[gid]
                changes += 1
    return changes


class ClaudeCode(Adapter):
    id = "claude-code"
    name = "Claude Code"
    atomic_dirs = (("desktop", _LOCAL_STORAGE), ("desktop", "IndexedDB/*"))

    def roots(self, env: Env) -> list[Root]:
        return [
            Root(
                "config",
                env.var_path("CLAUDE_CONFIG_DIR", ".claude"),
                rules(
                    ("data", "projects/*", "history.jsonl", "plans/*", "todos/*"),
                    ("secret", ".credentials.json", "backups/*"),
                    ("checkpoint", "file-history/*"),
                    (
                        "cache",
                        "cache/*",
                        "debug/*",
                        "ide/*",
                        "logs/*",
                        "paste-cache/*",
                        "session-env/*",
                        "shell-snapshots/*",
                        "statsig/*",
                        "telemetry/*",
                        "plugins/cache/*",
                        "plugins/marketplaces/*",
                        "plugins/repos/*",
                        "plugins/*cache*.json",
                        "policy-limits.json*",
                        "remote-settings.json*",
                        "*.lock",
                    ),
                    (
                        "config",
                        "settings.json*",
                        "settings.local.json",
                        "keybindings.json",
                        "agents/*",
                        "commands/*",
                        "hooks/*",
                        "output-styles/*",
                        "plugins/*",
                        "skills/*",
                        "*.md",
                    ),
                ),
            ),
            # ~/.claude.json holds the OAuth account and API key next to per-project state.
            Root("home", env.home, rules(("secret", ".claude.json")), only=(".claude.json",)),
            # Claude Desktop: its list of Code sessions, local (Cowork) chats and the page's
            # storage with the sidebar groups. Everything else there is Electron cache.
            Root(
                "desktop",
                self._desktop_dir(env),
                rules(
                    ("cache", "claude-code-sessions/*/imported-staging/*"),
                    ("data", "claude-code-sessions/*", "local-agent-mode-sessions/*", "Local Storage/*", "IndexedDB/*"),
                    # MCP server definitions often carry tokens in their environment.
                    ("secret", "claude_desktop_config.json"),
                ),
                default="cache",
            ),
        ]

    @staticmethod
    def _desktop_dir(env: Env) -> Path:
        path = env.user_config_dir() / "Claude"
        if env.os == "windows" and not path.is_dir():
            # The Microsoft Store build redirects %APPDATA% into its package directory.
            for candidate in sorted(env.localappdata().glob("Packages/Claude_*/LocalCache/Roaming/Claude")):
                return candidate
        return path

    def restore_path(self, root: str, rel: str, mapper: PathMapper) -> str:
        if root != "config" or not rel.startswith("projects/"):
            return rel
        parts = rel.split("/")
        if len(parts) < 3:
            return rel
        parts[1] = self._map_project_dir(parts[1], mapper)
        return "/".join(parts)

    @staticmethod
    def _map_project_dir(name: str, mapper: PathMapper) -> str:
        # The encoding is lossy, so map on the encoded form: replace the longest
        # encoded rule source that is a prefix (at a "-" boundary) of the name.
        fold = mapper.src_os == "windows"
        cmp_name = name.lower() if fold else name
        best = None
        for rule in mapper.rules:
            enc = encode_project_dir(rule.src).rstrip("-")
            cmp_enc = enc.lower() if fold else enc
            if cmp_name == cmp_enc or cmp_name.startswith(cmp_enc + "-"):
                if best is None or len(enc) > len(best[1]):
                    best = (rule, enc)
        if best is None or best[0].identity:
            return name
        rule, enc = best
        return encode_project_dir(rule.dst).rstrip("-") + name[len(enc):]

    def rewrite_file(self, root: str, rel: str, data: bytes, mapper: PathMapper) -> bytes | None:
        if rel == ".claude.json":
            return self._rewrite_project_keys(data, mapper)
        if root == "desktop":
            if rel.endswith(".json") and rel.startswith(("claude-code-sessions/", "local-agent-mode-sessions/")):
                return rewrite_json_paths(data, mapper)
            return None
        if root != "config":
            return None
        if rel.startswith("projects/") and rel.endswith(".jsonl"):
            return rewrite_jsonl(data, (("cwd",),), mapper)
        if rel == "history.jsonl":
            return rewrite_jsonl(data, (("project",),), mapper)
        return None

    @staticmethod
    def _rewrite_project_keys(data: bytes, mapper: PathMapper) -> bytes | None:
        """``projects`` in .claude.json is keyed by project path (trust, allowed tools, ...)."""
        try:
            obj = json.loads(data)
        except ValueError:
            return None
        projects = obj.get("projects") if isinstance(obj, dict) else None
        if not isinstance(projects, dict):
            return None
        mapped: dict = {}
        for key, value in projects.items():
            new_key = mapper.map(key)
            if new_key in mapped:
                # Windows has both C:\x and C:/x variants; they collapse into one key.
                if isinstance(value, dict) and isinstance(mapped[new_key], dict):
                    mapped[new_key] = {**value, **mapped[new_key]}
            else:
                mapped[new_key] = value
        if list(mapped) == list(projects):
            return None
        obj["projects"] = mapped
        return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")

    def after_restore(self, sidelined: dict[Path, Path], dry_run: bool) -> list[str]:
        notes = []
        for live, side in sidelined.items():
            if live.as_posix().endswith(_LOCAL_STORAGE):
                notes.append(self._merge_sidebar(live, side, dry_run))
            else:
                notes.append(f"{live} exists and was left as is; the archived copy is in {side}")
        return notes

    @staticmethod
    def _merge_sidebar(live: Path, side: Path, dry_run: bool) -> str:
        """Copy sidebar groups and session assignments from the archived Local Storage."""
        if dry_run:
            return f"sidebar groups of Claude Desktop would be merged into {live}"
        archived, _ = chromium_leveldb.load(side)
        if _SIDEBAR_KEY not in archived:
            return f"no sidebar groups in the archived Local Storage ({side})"
        if chromium_leveldb.in_use(live):
            return f"Claude Desktop is running; sidebar groups not merged (archived copy in {side})"
        current, _ = chromium_leveldb.load(live)
        src = json.loads(chromium_leveldb.decode_local_storage(archived[_SIDEBAR_KEY]))
        dst = (
            json.loads(chromium_leveldb.decode_local_storage(current[_SIDEBAR_KEY]))
            if _SIDEBAR_KEY in current
            else {"state": {}, "version": src.get("version", 1)}
        )
        added = merge_sidebar_state(src.get("state", {}), dst.setdefault("state", {}))
        if not added:
            return "sidebar groups of Claude Desktop are already up to date"
        text = json.dumps(dst, ensure_ascii=False, separators=(",", ":"))
        chromium_leveldb.put(live, {_SIDEBAR_KEY: chromium_leveldb.encode_local_storage(text)})
        return f"merged {added} sidebar group assignment(s) into Claude Desktop (archived copy in {side})"

    def merge(self, root: str, rel: str, existing: bytes, incoming: bytes) -> bytes | None:
        if root == "config" and rel == "history.jsonl":
            return merge_jsonl(existing, incoming, "timestamp")
        return None
