from __future__ import annotations

import re

from aibackup.adapters.base import Adapter, Root, merge_jsonl, rewrite_jsonl, rules
from aibackup.env import Env
from aibackup.pathmap import PathMapper

_ENCODE = re.compile(r"[^a-zA-Z0-9]")


def encode_project_dir(path: str) -> str:
    """Claude Code's directory name for a project path (``C:\\a\\b`` -> ``C--a-b``)."""
    return _ENCODE.sub("-", path)


class ClaudeCode(Adapter):
    id = "claude-code"
    name = "Claude Code"

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
        ]

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
        if root != "config":
            return None
        if rel.startswith("projects/") and rel.endswith(".jsonl"):
            return rewrite_jsonl(data, (("cwd",),), mapper)
        if rel == "history.jsonl":
            return rewrite_jsonl(data, (("project",),), mapper)
        return None

    def merge(self, root: str, rel: str, existing: bytes, incoming: bytes) -> bytes | None:
        if root == "config" and rel == "history.jsonl":
            return merge_jsonl(existing, incoming, "timestamp")
        return None
