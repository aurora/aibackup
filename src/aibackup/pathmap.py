"""Prefix-based mapping of absolute paths between systems."""

from __future__ import annotations

from dataclasses import dataclass

from aibackup.env import join


def split(path: str, os_name: str) -> list[str]:
    """Split into components; the first one is the drive (``C:``) or ``""`` for ``/``."""
    if os_name == "windows":
        p = path.replace("/", "\\")
        if p.startswith("\\\\?\\"):
            p = p[4:]
        parts = p.split("\\")
    else:
        parts = path.split("/")
    return [parts[0]] + [x for x in parts[1:] if x not in ("", ".")]


def _key(parts: list[str], os_name: str) -> tuple[str, ...]:
    if os_name == "windows":
        return tuple(p.lower() for p in parts)
    return tuple(parts)


def clean(path: str, os_name: str) -> str:
    """Strip trailing separators (but keep a bare root)."""
    stripped = path.rstrip("/\\")
    if not stripped:
        return path[:1] or path
    if os_name == "windows" and len(stripped) == 2 and stripped[1] == ":":
        return stripped + "\\"
    return stripped


@dataclass(frozen=True)
class Rule:
    src: str
    dst: str
    key: tuple[str, ...]
    identity: bool


class PathMapper:
    def __init__(self, src_os: str, dst_os: str, rules: list[tuple[str, str]] = ()):
        self.src_os = src_os
        self.dst_os = dst_os
        self.rules: list[Rule] = []
        # Every non-identity mapping that was applied, as (rule, remaining components).
        self.used: set[tuple[Rule, tuple[str, ...]]] = set()
        # When set, mappings are also collected here (to see what a single file refers to).
        self.trace: set[tuple[Rule, tuple[str, ...]]] | None = None
        for src, dst in rules:
            self.add(src, dst)

    def add(self, src: str, dst: str) -> None:
        src = clean(src, self.src_os)
        dst = clean(dst, self.dst_os)
        key = _key(split(src, self.src_os), self.src_os)
        if any(r.key == key for r in self.rules):
            return  # first rule wins (explicit mappings are added first)
        identity = self.src_os == self.dst_os and key == _key(split(dst, self.dst_os), self.dst_os)
        self.rules.append(Rule(src, dst, key, identity))

    @property
    def is_identity(self) -> bool:
        return all(r.identity for r in self.rules)

    def best_rule(self, path: str) -> tuple[Rule, list[str]] | None:
        parts = split(path, self.src_os)
        key = _key(parts, self.src_os)
        best: Rule | None = None
        for rule in self.rules:
            n = len(rule.key)
            if n <= len(key) and key[:n] == rule.key and (best is None or n > len(best.key)):
                best = rule
        if best is None:
            return None
        return best, parts[len(best.key):]

    def map(self, path: str) -> str:
        """Map ``path``; unmatched paths and identity mappings are returned unchanged."""
        if not path:
            return path
        found = self.best_rule(path)
        if found is None or found[0].identity:
            return path
        rule, rest = found
        self.used.add((rule, tuple(rest)))
        if self.trace is not None:
            self.trace.add((rule, tuple(rest)))
        return join(rule.dst, rest, self.dst_os)


def parse_mapping(spec: str) -> tuple[str, str]:
    src, sep, dst = spec.partition("=")
    if not sep or not src or not dst:
        raise ValueError(f"invalid mapping {spec!r}, expected FROM=TO")
    return src, dst
