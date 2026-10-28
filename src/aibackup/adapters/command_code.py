from __future__ import annotations

import re
import unicodedata

from aibackup.adapters.base import Adapter, Root, merge_jsonl, rewrite_jsonl, rules
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
            )
        ]

    def restore_path(self, root: str, rel: str, mapper: PathMapper) -> str:
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] != "projects":
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
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "projects" and rel.endswith(".jsonl") and not rel.endswith(".checkpoints.jsonl"):
            # Only newer sessions have a header line {"type":"session","cwd":...}.
            return rewrite_jsonl(data, (("cwd",),), mapper)
        return None

    def merge(self, root: str, rel: str, existing: bytes, incoming: bytes) -> bytes | None:
        if rel == "history.jsonl":
            return merge_jsonl(existing, incoming, "t")
        return None
