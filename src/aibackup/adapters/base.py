"""Building blocks shared by all agent adapters."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from aibackup.env import Env
from aibackup.pathmap import PathMapper

CATEGORIES = ("data", "config", "checkpoint", "secret", "cache")
DEFAULT_CATEGORIES = frozenset({"data", "config"})

# Leftovers of our own restores must never end up in the next backup.
_GLOBAL_RULES = (("*.aibackup-*.bak", "cache"), ("*.aibackup-tmp", "cache"), ("*.aibackup-*/*", "cache"))


@dataclass(frozen=True)
class Rule:
    pattern: str
    category: str


def rules(*groups: tuple[str, ...]) -> tuple[Rule, ...]:
    """``rules(("cache", "a/*", "b/*"), ("secret", "x"))`` -> flat, ordered rule list."""
    return tuple(Rule(p, group[0]) for group in groups for p in group[1:])


def _literal_prefix(pattern: str) -> str:
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern


@dataclass(frozen=True)
class Root:
    """A data directory of an agent.

    Files are classified by the first matching rule (glob on the POSIX relative
    path); unmatched files get ``default``. A rule ``dir/*`` claims the whole
    subtree, which lets the scanner skip excluded directories entirely.
    """

    name: str
    path: Path
    rules: tuple[Rule, ...] = ()
    default: str = "data"
    only: tuple[str, ...] | None = None

    def classify(self, rel: str) -> str:
        for pattern, category in _GLOBAL_RULES:
            if fnmatchcase(rel, pattern):
                return category
        for rule in self.rules:
            if fnmatchcase(rel, rule.pattern):
                return rule.category
        return self.default

    def classify_dir(self, rel: str) -> str | None:
        """Category of the whole subtree, or ``None`` if files inside may differ."""
        probe = rel + "/"
        for rule in self.rules:
            if rule.pattern.endswith("/*") and fnmatchcase(probe, rule.pattern):
                return rule.category
            lit = _literal_prefix(rule.pattern)
            if lit.startswith(probe) or probe.startswith(lit):
                return None
        return self.default


class Adapter:
    id: str = ""
    name: str = ""
    # (root name, glob on rel path) -> tables to empty in snapshots unless secrets are included
    scrub: dict[tuple[str, str], tuple[str, ...]] = {}
    # (root name, glob on a directory's rel path): directories that are only consistent as a
    # whole, such as LevelDB databases. An existing one is never mixed with archived files.
    atomic_dirs: tuple[tuple[str, str], ...] = ()
    # (root name, glob on rel path): SQLite files that ``merge_sqlite`` can merge into an
    # existing database instead of reporting a conflict.
    sqlite_merges: tuple[tuple[str, str], ...] = ()

    def roots(self, env: Env) -> list[Root]:
        raise NotImplementedError

    def scrub_tables(self, root: str, rel: str) -> tuple[str, ...]:
        for (r, pattern), tables in self.scrub.items():
            if r == root and fnmatchcase(rel, pattern):
                return tables
        return ()

    def atomic_dir(self, root: str, rel: str) -> str | None:
        """The atomic directory containing ``rel``, if any."""
        parts = rel.split("/")
        for r, pattern in self.atomic_dirs:
            n = pattern.count("/") + 1
            if r == root and len(parts) > n and fnmatchcase("/".join(parts[:n]), pattern):
                return "/".join(parts[:n])
        return None

    def merges_sqlite(self, root: str, rel: str) -> bool:
        return any(r == root and fnmatchcase(rel, pattern) for r, pattern in self.sqlite_merges)

    def merge_sqlite(self, root: str, rel: str, live: Path, archived: Path) -> int:
        """Merge ``archived`` into the existing ``live`` database; returns the number of changes.

        Raises ``ValueError`` if merging is not possible right now (e.g. the app is running).
        """
        raise NotImplementedError

    def after_restore(self, sidelined: dict[Path, Path], dry_run: bool) -> list[str]:
        """Called with existing atomic dirs -> where their archived copy went; returns notes."""
        return [f"{live} exists; the archived copy was restored to {side}" for live, side in sidelined.items()]

    # Restore hooks; only called when the path mapping is not an identity.

    def restore_path(self, root: str, rel: str, mapper: PathMapper) -> str:
        return rel

    def rewrite_file(self, root: str, rel: str, data: bytes, mapper: PathMapper) -> bytes | None:
        return None

    def rewrite_sqlite(self, root: str, rel: str, conn: sqlite3.Connection, mapper: PathMapper) -> int:
        return 0

    # Called when a restored file conflicts with an existing one (without --overwrite).

    def merge(self, root: str, rel: str, existing: bytes, incoming: bytes) -> bytes | None:
        return None


# JSON helpers -----------------------------------------------------------------

KeyPath = tuple[str, ...]


def _apply(obj, keypath: KeyPath, mapper: PathMapper) -> bool:
    if not keypath:
        return False
    head, rest = keypath[0], keypath[1:]
    if head == "*":
        if not isinstance(obj, list):
            return False
        return any([_apply(item, rest, mapper) for item in obj])
    if not isinstance(obj, dict) or head not in obj:
        return False
    if rest:
        return _apply(obj[head], rest, mapper)
    value = obj[head]
    if isinstance(value, str):
        new = mapper.map(value)
        if new != value:
            obj[head] = new
            return True
    return False


def rewrite_json_obj(obj, keypaths: tuple[KeyPath, ...], mapper: PathMapper) -> bool:
    return any([_apply(obj, kp, mapper) for kp in keypaths])


def _dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _key_regexes(keypaths: tuple[KeyPath, ...]) -> tuple[re.Pattern, re.Pattern]:
    keys = b"|".join(re.escape(k.encode()) for k in sorted({kp[-1] for kp in keypaths}))
    any_key = re.compile(rb'"(?:' + keys + rb')"\s*:')
    string_value = re.compile(rb'"(?:' + keys + rb')"\s*:\s*"((?:[^"\\]|\\.)*)"')
    return any_key, string_value


_TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"|[{}\[\]]')


def _depth(line: bytes, pos: int) -> int:
    """Nesting depth of ``pos`` in a JSON line (1 = inside the top-level object)."""
    depth = 0
    for token in _TOKEN.finditer(line, 0, pos):
        c = token.group()[:1]
        if c in b"{[":
            depth += 1
        elif c in b"}]":
            depth -= 1
    return depth


def rewrite_jsonl(data: bytes, keypaths: tuple[KeyPath, ...], mapper: PathMapper) -> bytes | None:
    """Rewrite path fields line by line; untouched bytes stay identical.

    Fast path for top-level keys: if a line contains the key exactly once and it
    sits directly in the top-level object, only that string value is replaced
    textually. Escaped occurrences inside JSON strings (``\\"cwd\\"``) never
    match. Parsing a line with large tool outputs just to change one field is
    ~10x slower. All other lines are parsed so only the exact key path changes.
    """
    any_key, string_value = _key_regexes(keypaths)
    fast = all(len(kp) == 1 for kp in keypaths)
    lines = data.split(b"\n")
    changed = False
    for i, line in enumerate(lines):
        hits = any_key.findall(line)
        if not hits:
            continue
        if fast and len(hits) == 1:
            m = string_value.search(line)
            if m is None or _depth(line, m.start()) != 1:
                continue  # non-string value, or a nested key (e.g. in tool input)
            try:
                value = json.loads(b'"' + m.group(1) + b'"')
            except ValueError:
                continue
            new = mapper.map(value)
            if new != value:
                encoded = json.dumps(new, ensure_ascii=False)[1:-1].encode("utf-8")
                lines[i] = line[: m.start(1)] + encoded + line[m.end(1) :]
                changed = True
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if rewrite_json_obj(obj, keypaths, mapper):
            lines[i] = _dumps(obj) + (b"\r" if line.endswith(b"\r") else b"")
            changed = True
    return b"\n".join(lines) if changed else None


def rewrite_json(data: bytes, keypaths: tuple[KeyPath, ...], mapper: PathMapper) -> bytes | None:
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not rewrite_json_obj(obj, keypaths, mapper):
        return None
    if b"\n" in data.strip():
        return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    return _dumps(obj)


_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _map_any(obj, mapper: PathMapper):
    """Map every string (and dict key) that is an absolute path of the source OS."""
    if isinstance(obj, dict):
        return {_map_any(k, mapper): _map_any(v, mapper) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_any(v, mapper) for v in obj]
    if isinstance(obj, str) and "\n" not in obj:
        absolute = _WIN_ABS.match(obj) if mapper.src_os == "windows" else obj.startswith("/")
        if absolute:
            return mapper.map(obj)
    return obj


def map_json_paths(text: str, mapper: PathMapper) -> str:
    """Like :func:`rewrite_json_paths` for a JSON string; non-JSON text is returned as is."""
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    new = _map_any(obj, mapper)
    return text if new == obj else json.dumps(new, ensure_ascii=False, separators=(",", ":"))


def rewrite_json_paths(data: bytes, mapper: PathMapper) -> bytes | None:
    """For JSON files with many, version-dependent path fields: map all absolute paths."""
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    new = _map_any(obj, mapper)
    if new == obj:
        return None
    if b"\n" in data.strip():
        return json.dumps(new, ensure_ascii=False, indent=2).encode("utf-8")
    return _dumps(new)


def merge_jsonl(existing: bytes, incoming: bytes, sort_key: str | None = None) -> bytes:
    """Union of lines (existing first), optionally sorted by a numeric/string field."""
    seen: set[bytes] = set()
    lines: list[bytes] = []
    for line in existing.splitlines() + incoming.splitlines():
        if line.strip() and line not in seen:
            seen.add(line)
            lines.append(line)
    if sort_key:
        def key(line: bytes):
            try:
                value = json.loads(line).get(sort_key)
            except (ValueError, AttributeError):
                value = None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return (0, value, "")
            if isinstance(value, str):
                return (1, 0, value)
            return (2, 0, "")

        lines.sort(key=key)
    return b"\n".join(lines) + b"\n"
