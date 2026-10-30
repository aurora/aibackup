"""Rebuild Claude Desktop's list of Code sessions from Claude Code transcripts.

Writes the records Desktop's own "Import Claude Code CLI Sessions" creates
(``claude-code-sessions/<account>/<org>/local_<cli-session-id>.json``). Needed when
the list was not backed up; existing records are never touched.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aibackup import chromium_leveldb
from aibackup.adapters.claude_code import ClaudeCode
from aibackup.env import Env

TITLE_MAX = 60


@dataclass
class IndexResult:
    storage: Path
    created: list[tuple[str, str]] = field(default_factory=list)  # (cwd, title)
    existing: int = 0
    missing: dict[str, int] = field(default_factory=dict)  # cwd -> sessions


def _ms(ts: str) -> int | None:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _user_text(obj: dict) -> str | None:
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        content = next((c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"), None)
    if not isinstance(content, str) or content.startswith("<"):
        return None  # tool results, slash commands, system reminders
    return " ".join(content.split()) or None


def read_transcript(path: Path) -> dict:
    info: dict = {"cwd": None, "first": None, "title": None, "prompt": None}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if info["cwd"] is None and isinstance(obj.get("cwd"), str):
                info["cwd"] = obj["cwd"]
            if info["first"] is None and isinstance(obj.get("timestamp"), str):
                info["first"] = _ms(obj["timestamp"])
            kind = obj.get("type")
            if kind == "custom-title" and obj.get("customTitle"):
                info["title"] = obj["customTitle"]
            elif kind == "ai-title" and obj.get("aiTitle") and not info["title"]:
                info["title"] = obj["aiTitle"]
            elif kind == "user" and info["prompt"] is None and not obj.get("isSidechain"):
                info["prompt"] = _user_text(obj)
    return info


def storage_dir(desktop: Path, org: str | None = None) -> Path:
    try:
        config = json.loads((desktop / "config.json").read_text(encoding="utf-8"))
        account = config["lastKnownAccountUuid"]
    except (OSError, ValueError, KeyError):
        raise ValueError(f"no signed-in account found in {desktop}; start Claude Desktop once and sign in") from None
    base = desktop / "claude-code-sessions" / account
    orgs = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if org is not None:
        if org not in orgs:
            raise ValueError(f"organization {org} not found in {base} (found: {', '.join(orgs) or 'none'})")
        return base / org
    if len(orgs) != 1:
        raise ValueError(
            f"expected one organization in {base}, found {len(orgs)}"
            + (f" ({', '.join(orgs)}); choose one with --org" if orgs else "; start Claude Desktop once and sign in")
        )
    return base / orgs[0]


def desktop_running(desktop: Path) -> bool:
    return chromium_leveldb.in_use(desktop / "Local Storage" / "leveldb")


def rebuild(env: Env, *, dry_run: bool, org: str | None = None) -> IndexResult:
    adapter = ClaudeCode()
    roots = {r.name: r.path for r in adapter.roots(env)}
    projects, desktop = roots["config"] / "projects", roots["desktop"]
    if not dry_run and desktop_running(desktop):
        raise ValueError("Claude Desktop is running; quit it completely (not just its window) and retry")
    result = IndexResult(storage_dir(desktop, org))
    now = int(time.time() * 1000)

    for transcript in sorted(projects.glob("*/*.jsonl")):
        cli_id = transcript.stem
        record_path = result.storage / f"local_{cli_id}.json"
        if record_path.exists():
            result.existing += 1
            continue
        info = read_transcript(transcript)
        cwd = info["cwd"]
        local = env.local(cwd) if cwd else None
        if local is None or not local.is_dir():
            result.missing[str(cwd)] = result.missing.get(str(cwd), 0) + 1
            continue
        mtime = int(transcript.stat().st_mtime * 1000)
        title = info["title"] or info["prompt"] or "Imported session"
        if len(title) > TITLE_MAX:
            title = title[: TITLE_MAX - 1] + "\u2026"
        result.created.append((cwd, title))
        if dry_run:
            continue
        record = {
            "sessionId": f"local_{cli_id}",
            "cliSessionId": cli_id,
            "cwd": cwd,
            "originCwd": cwd,
            "createdAt": info["first"] or mtime,
            "lastActivityAt": mtime,
            "isArchived": False,
            "title": title,
            "indexedAt": now,
            "adoptedFromOtherSurface": True,
            "alwaysAllowedReasons": [],
            "sessionPermissionUpdates": [],
        }
        tmp = record_path.with_name(record_path.name + ".aibackup-tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(record_path)
    return result
