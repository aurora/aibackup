from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from aibackup.adapters.base import Adapter, Root, electron_running, merge_jsonl, rewrite_jsonl, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper

# Port of the decamelize step of @sindresorhus/slugify, which Command Code uses
# to name project directories (~/.commandcode/projects/<slugify(cwd)>/).
_DECAMELIZE = (
    (re.compile(r"([A-Z]{2,})(\d+)"), r"\1 \2"),
    (re.compile(r"([a-z\d]+)([A-Z]{2,})"), r"\1 \2"),
    (re.compile(r"([a-z\d])([A-Z])"), r"\1 \2"),
    (re.compile(r"([A-Z]+)([A-Z][a-rt-z\d]+)"), r"\1 \2"),
)
_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def slugify(path: str) -> str:
    """Command Code's directory name for a project path.

    Transliteration only covers German umlauts and accents; other scripts may
    differ from the JavaScript library.
    """
    text = unicodedata.normalize("NFKD", path.translate(_GERMAN))
    text = "".join(c for c in text if not unicodedata.combining(c))
    for pattern, repl in _DECAMELIZE:
        text = pattern.sub(repl, text)
    return re.sub(r"[^a-z\d]+", "-", text.lower()).strip("-")


# The desktop app (Electron) lists projects and chats from its own state file. Without
# its entries, restored sessions stay invisible; trusted roots decide whether the app
# reads a project's sessions without asking.
APP_STATE = "app-state.json"


def desktop_dir(env: Env) -> Path:
    return env.user_config_dir() / "Command Code"


def _load_state(data: bytes) -> dict | None:
    try:
        state = json.loads(data)
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def _dump_state(state: dict) -> bytes:
    return json.dumps(state, ensure_ascii=False, indent="\t").encode("utf-8")


def _items(value) -> list:
    return value if isinstance(value, list) else []


def _rewrite_state(state: dict, mapper: PathMapper) -> bool:
    """Map the fields that link projects and chats to folders; chat contents stay as they are."""
    changed = False

    def m(value):
        nonlocal changed
        if isinstance(value, str) and value:
            new = mapper.map(value)
            changed |= new != value
            return new
        return value

    app = state.get("appState")
    if isinstance(app, dict):
        for project in _items(app.get("projects")):
            if isinstance(project, dict):
                project["path"] = m(project.get("path"))
        for thread in _items(app.get("threads")):
            if isinstance(thread, dict):
                for key in ("projectPath", "worktreePath"):
                    if key in thread:
                        thread[key] = m(thread[key])
        if "projectPath" in app:
            app["projectPath"] = m(app["projectPath"])
        if isinstance(app.get("expandedProjectPaths"), list):
            app["expandedProjectPaths"] = [m(p) for p in app["expandedProjectPaths"]]
        if isinstance(app.get("removedProjects"), dict):
            app["removedProjects"] = {m(k): v for k, v in app["removedProjects"].items()}
    if isinstance(state.get("trustedProjectRoots"), list):
        state["trustedProjectRoots"] = [m(p) for p in state["trustedProjectRoots"]]
    return changed


def add_projects(state: dict, paths: list[str], now_ms: int) -> list[str]:
    """Register project folders in the sidebar unless listed or removed by the user."""
    app = state.get("appState")
    if not isinstance(app, dict):
        app = state["appState"] = {}
    projects = app["projects"] = _items(app.get("projects"))
    listed = {p.get("path") for p in projects if isinstance(p, dict)}
    removed = app.get("removedProjects") if isinstance(app.get("removedProjects"), dict) else {}
    added = [p for p in dict.fromkeys(paths) if p not in listed and p not in removed]
    projects += [{"path": p, "registeredAt": now_ms} for p in added]
    return added


def merge_app_state(existing: dict, archived: dict, now_ms: int) -> int:
    """Add archived projects, chats and trusted roots to ``existing``; existing entries win."""
    old_app = archived.get("appState") if isinstance(archived.get("appState"), dict) else {}
    paths = [p.get("path") for p in _items(old_app.get("projects")) if isinstance(p, dict) and p.get("path")]
    changes = len(add_projects(existing, paths, now_ms))
    app = existing["appState"]
    threads = app["threads"] = _items(app.get("threads"))
    known = {t.get("id") for t in threads if isinstance(t, dict)}
    deleted = app.get("deletedThreads") if isinstance(app.get("deletedThreads"), dict) else {}
    removed = app.get("removedProjects") if isinstance(app.get("removedProjects"), dict) else {}
    for thread in _items(old_app.get("threads")):
        if not isinstance(thread, dict) or thread.get("id") in known or thread.get("id") in deleted:
            continue
        if thread.get("projectPath") in removed:
            continue
        threads.append(thread)
        known.add(thread.get("id"))
        changes += 1
    # Only trust what was trusted on the source system.
    trusted = existing["trustedProjectRoots"] = _items(existing.get("trustedProjectRoots"))
    for root in _items(archived.get("trustedProjectRoots")):
        if isinstance(root, str) and root not in trusted and root not in removed:
            trusted.append(root)
            changes += 1
    return changes


def find_project_dir(slug: str, start: Path = Path("/")) -> Path | None:
    """The folder whose slug is ``slug``, found by descending only into matching prefixes."""
    try:
        children = sorted(p for p in start.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return None
    for child in children:
        s = slugify(str(child))
        if s == slug:
            return child
        if s and slug.startswith(s + "-"):
            found = find_project_dir(slug, child)
            if found:
                return found
    return None


def _session_cwd(directory: Path) -> str | None:
    """Newer transcripts start with {"type":"session","cwd":...}; older ones carry no path."""
    for transcript in sorted(directory.glob("*.jsonl")):
        if transcript.name.endswith(".checkpoints.jsonl"):
            continue
        try:
            with transcript.open(encoding="utf-8", errors="replace") as f:
                header = json.loads(f.readline() or "null")
        except (OSError, ValueError):
            continue
        if isinstance(header, dict) and header.get("type") == "session" and isinstance(header.get("cwd"), str):
            return header["cwd"]
    return None


@dataclass
class ProjectListResult:
    storage: Path
    added: list[str] = field(default_factory=list)
    listed: int = 0
    missing: dict[str, int] = field(default_factory=dict)  # project dir name -> sessions


def rebuild_project_list(env: Env, *, dry_run: bool) -> ProjectListResult:
    """Register every project with sessions whose folder exists in the desktop app.

    Folders are not marked as trusted; the app asks when a project is first opened.
    """
    projects_dir = env.home / ".commandcode" / "projects"
    state_file = desktop_dir(env) / APP_STATE
    if not state_file.is_file():
        raise ValueError(f"{state_file} not found; start Command Code once")
    if not dry_run and electron_running(state_file.parent):
        raise ValueError("Command Code is running; quit it and retry")
    state = _load_state(state_file.read_bytes())
    if state is None:
        raise ValueError(f"{state_file} is not readable JSON")
    result = ProjectListResult(state_file)
    paths = []
    for directory in sorted(p for p in projects_dir.iterdir() if p.is_dir()) if projects_dir.is_dir() else []:
        sessions = [f for f in directory.glob("*.jsonl") if not f.name.endswith(".checkpoints.jsonl")]
        if not sessions:
            continue
        cwd = _session_cwd(directory)
        local = env.local(cwd) if cwd else None
        if local is None or not local.is_dir() or slugify(cwd) != directory.name:
            fs_root = env.local("/")  # None if the target's root isn't reachable from here
            found = find_project_dir(directory.name, fs_root) if fs_root else None
            cwd = env.logical(found) if found else None
        if cwd is None:
            result.missing[directory.name] = len(sessions)
        else:
            paths.append(cwd)
    added = add_projects(state, paths, int(time.time() * 1000))
    result.added, result.listed = added, len(set(paths)) - len(added)
    if added and not dry_run:
        tmp = state_file.with_name(state_file.name + ".aibackup-tmp")
        tmp.write_bytes(_dump_state(state))
        os.replace(tmp, state_file)
    return result


class CommandCode(Adapter):
    id = "command-code"
    name = "Command Code"

    def roots(self, env: Env) -> list[Root]:
        return [
            Root(
                "home",
                env.home / ".commandcode",
                rules(
                    ("secret", "auth.json", "auth.*.json", "mcp-tokens.json", "mcp.json", "projects/*/mcp.json"),
                    ("checkpoint", "file-history/*"),
                    ("cache", "cache/*", "logs/*", "ide/*", "update.lock", "updates.json", "telemetry-install-id"),
                    (
                        "config",
                        "config.json",
                        "settings.json",
                        "settings.local.json",
                        "trusted-hooks.json",
                        "AGENTS.md",
                        "agents/*",
                        "commands/*",
                        "skills/*",
                        "projects/*/settings.json",
                    ),
                ),
            ),
            # Desktop app: its state file (projects, chats, trusted roots) is all that matters;
            # thread-index.db is a cache it rebuilds, the rest is Chromium cache.
            Root("desktop", desktop_dir(env), rules(("data", APP_STATE)), default="cache"),
        ]

    def busy(self, root: Root) -> str | None:
        return "Command Code is running" if root.name == "desktop" and electron_running(root.path) else None

    def restore_path(self, root: str, rel: str, mapper: PathMapper) -> str:
        parts = rel.split("/")
        if root != "home" or len(parts) < 3 or parts[0] != "projects":
            return rel
        parts[1] = self._map_project_dir(parts[1], mapper)
        return "/".join(parts)

    @staticmethod
    def _map_project_dir(name: str, mapper: PathMapper) -> str:
        # Slugs are lowercase and lossy, so map on the slug: replace the longest
        # slugified rule source that is a prefix (at a "-" boundary) of the name.
        best = None
        for rule in mapper.rules:
            slug = slugify(rule.src)
            if slug and (name == slug or name.startswith(slug + "-")):
                if best is None or len(slug) > len(best[1]):
                    best = (rule, slug)
        if best is None or best[0].identity:
            return name
        rule, slug = best
        return slugify(rule.dst) + name[len(slug) :]

    def rewrite_file(self, root: str, rel: str, data: bytes, mapper: PathMapper) -> bytes | None:
        if root == "desktop":
            state = _load_state(data) if rel == APP_STATE else None
            return _dump_state(state) if state is not None and _rewrite_state(state, mapper) else None
        parts = rel.split("/")
        transcript = rel.endswith(".jsonl") and not rel.endswith(".checkpoints.jsonl")
        if len(parts) == 3 and parts[0] == "projects" and transcript:
            # Only newer sessions have a header line {"type":"session","cwd":...}.
            return rewrite_jsonl(data, (("cwd",),), mapper)
        return None

    def merge(self, root: str, rel: str, existing: bytes, incoming: bytes) -> bytes | None:
        if root == "desktop" and rel == APP_STATE:
            current, archived = _load_state(existing), _load_state(incoming)
            if current is None or archived is None:
                return None
            return _dump_state(current) if merge_app_state(current, archived, int(time.time() * 1000)) else existing
        if root == "home" and rel == "history.jsonl":
            return merge_jsonl(existing, incoming, "t")
        return None
