from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aibackup.adapters import ADAPTERS, Adapter
from aibackup.archive import CHUNK, ArchiveReader, safe_rel
from aibackup.env import Env, join
from aibackup.pathmap import PathMapper, clean
from aibackup.progress import NullProgress, Progress
from aibackup.sqlite_snapshot import SIDECAR_SUFFIXES, in_use

# Files above this size are streamed and never rewritten or merged.
LARGE_FILE = 256 << 20


@dataclass
class AgentReport:
    agent: str
    counts: Counter = field(default_factory=Counter)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rewritten: int = 0
    mapper: PathMapper | None = None
    # Missing target folder -> (corresponding source path, names of subfolders sessions refer to)
    missing: dict[str, tuple[str, set[str]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def build_mapper(adapter: Adapter, env: Env, manifest: dict, explicit: list[tuple[str, str]]) -> PathMapper:
    """Explicit mappings first, then data roots, then home -> home (longest prefix wins)."""
    mapper = PathMapper(manifest["source"]["os"], env.os)
    for src, dst in explicit:
        mapper.add(src, dst)
    targets = {r.name: r for r in adapter.roots(env)}
    for name, src in manifest["agents"][adapter.id]["roots"].items():
        if name in targets:
            mapper.add(src, env.logical(targets[name].path))
    mapper.add(manifest["source"]["home"], env.home_path)
    return mapper


def find_missing(
    mapper: PathMapper, env: Env, skip_dsts: set[str], used: set | None = None
) -> dict[str, tuple[str, set[str]]]:
    """Project folders that mapped paths point to but that don't exist on the target.

    Agents attach sessions to these paths, so sessions of a missing folder are
    invisible. Only the topmost missing folder of each path is reported, and
    nothing inside an existing repository (e.g. build output or old worktrees).
    """
    cache: dict[str, bool] = {}

    def exists(logical: str) -> bool:
        if logical not in cache:
            local = env.local(logical)
            cache[logical] = local is None or local.exists()
        return cache[logical]

    def in_repo(rule, rest: tuple[str, ...]) -> bool:
        return any(exists(join(rule.dst, rest[:j] + (".git",), mapper.dst_os)) for j in range(1, len(rest)))

    missing: dict[str, tuple[str, set[str]]] = {}
    for rule, rest in mapper.used if used is None else used:
        if rule.dst in skip_dsts:
            continue  # paths inside the agent's own data directories
        for k in range(len(rest) + 1):
            dst = join(rule.dst, rest[:k], mapper.dst_os)
            if not exists(dst):
                if in_repo(rule, rest[:k]) and not _missing_worktree(rest, k):
                    break
                src, children = missing.setdefault(dst, (join(rule.src, rest[:k], mapper.src_os), set()))
                if k < len(rest):
                    children.add(rest[k])
                break
    return missing


def _missing_worktree(parts: tuple[str, ...], k: int) -> bool:
    """Whether ``parts[:k]`` is (an ancestor of) a Claude Code session worktree.

    These live in ``<repo>/.claude/worktrees/<name>`` and are projects of their own.
    """
    for i in range(len(parts) - 2):
        if parts[i : i + 2] == (".claude", "worktrees"):
            return k <= i + 3
    return False


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class Restorer:
    def __init__(
        self,
        reader: ArchiveReader,
        env: Env,
        *,
        dry_run: bool,
        overwrite: bool,
        log,
        checksum: bool = False,
        progress: Progress | None = None,
        allow_missing: bool = False,
    ):
        self.reader = reader
        self.env = env
        self.dry_run = dry_run
        self.overwrite = overwrite
        self.allow_missing = allow_missing
        self.checksum = checksum
        self.log = log
        self.progress = progress or NullProgress()
        self.stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._streamed = 0

    def _on_chunk(self, n: int) -> None:
        self._streamed += n
        self.progress.advance(n)

    def _quick_unchanged(self, target: Path, entry: dict, rewrite: bool) -> bool:
        """rsync-style check: we restore mtimes, so an untouched file still has the archived one.

        Rewritten files differ in size from the archive, so only the mtime counts for them.
        """
        if self.checksum:
            return False
        try:
            st = target.stat()
        except OSError:
            return False
        return abs(st.st_mtime - entry["mtime"]) < 0.001 and (rewrite or st.st_size == entry["size"])

    def restore_agent(self, agent_id: str, explicit: list[tuple[str, str]]) -> AgentReport:
        adapter = ADAPTERS[agent_id]
        report = AgentReport(agent_id)
        mapper = build_mapper(adapter, self.env, self.reader.manifest, explicit)
        report.mapper = mapper
        rewrite = not mapper.is_identity
        roots = {r.name: r for r in adapter.roots(self.env)}
        data_dsts = {clean(self.env.logical(r.path), self.env.os) for r in roots.values()}
        data_dsts.discard(clean(self.env.home_path, self.env.os))
        self._data_dsts = data_dsts
        # Caches hold platform-specific binaries (runtimes, language servers).
        skip_cache = self.reader.manifest["source"]["os"] != self.env.os
        # Atomic dir "root/rel" -> (existing dir, where the archived copy goes instead) or None.
        redirects: dict[str, tuple[Path, Path] | None] = {}
        # Roots of running desktop apps: they rewrite their state on exit, undoing our changes.
        busy = {name: reason for name, r in roots.items() if (reason := adapter.busy(r))}

        for entry in self.reader.files(agent_id):
            if skip_cache and entry["category"] == "cache":
                report.counts["skipped (cache)"] += 1
                self.progress.advance(entry["size"])
                continue
            label = f"{entry['root']}/{entry['path']}"
            root = roots.get(entry["root"])
            if root is None or not safe_rel(entry["path"]):
                report.counts["error"] += 1
                report.errors.append(f"{label}: unknown root or unsafe path")
                continue
            rel = adapter.restore_path(root.name, entry["path"], mapper) if rewrite else entry["path"]
            if not safe_rel(rel):
                report.counts["error"] += 1
                report.errors.append(f"{label}: unsafe target path {rel!r}")
                continue
            target = root.path.joinpath(*rel.split("/"))
            self.progress.set_label(f"{agent_id}: {rel}", force=entry["size"] > LARGE_FILE or entry["kind"] == "sqlite")
            self._streamed = 0
            atomic = adapter.atomic_dir(root.name, rel)
            if atomic is not None:
                key = f"{root.name}/{atomic}"
                if key not in redirects:
                    live = root.path.joinpath(*atomic.split("/"))
                    redirects[key] = (live, live.with_name(f"{live.name}.aibackup-{self.stamp}")) if live.exists() else None
                if redirects[key] is not None:
                    target = redirects[key][1].joinpath(*rel[len(atomic) + 1 :].split("/"))
            if root.name in busy and not self._quick_unchanged(target, entry, rewrite):
                report.counts["refused"] += 1
                if not any(e.startswith(f"{root.name}/:") for e in report.errors):
                    report.errors.append(f"{root.name}/: {busy[root.name]}; quit it and restore again")
                self.progress.advance(entry["size"])
                continue
            try:
                if self._quick_unchanged(target, entry, rewrite):
                    status = "unchanged"
                elif entry["kind"] == "sqlite" or entry["size"] > LARGE_FILE:
                    status = self._restore_stream(adapter, agent_id, entry, root.name, rel, target, mapper, rewrite, report)
                else:
                    status = self._restore_small(adapter, agent_id, entry, root.name, rel, target, mapper, rewrite, report)
            except Exception as e:  # noqa: BLE001 - report and continue with the next file
                status = "error"
                report.errors.append(f"{label}: {e}")
            self.progress.advance(max(entry["size"] - self._streamed, 0))
            report.counts[status] += 1
            if status == "conflict":
                report.conflicts.append(str(target))
            self.log.debug(f"  {status:<11} {target}")
        sidelined = dict(v for v in redirects.values() if v is not None)
        if sidelined:
            try:
                report.notes += adapter.after_restore(sidelined, self.dry_run)
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"after restore: {e}")
        if rewrite:
            report.missing = find_missing(mapper, self.env, data_dsts)
        return report

    def _refuse_missing(self, trace: set, label: str, report: AgentReport) -> bool:
        """Refuse to replace a file whose rewritten paths point to missing project folders.

        Agents hide (OpenCode even archives) sessions of folders that don't exist, so
        replacing working data with such a file looks like data loss.
        """
        if self.allow_missing or not trace:
            return False
        missing = find_missing(report.mapper, self.env, self._data_dsts, trace)
        if not missing:
            return False
        shown = ", ".join(sorted(missing)[:3]) + (", ..." if len(missing) > 3 else "")
        report.errors.append(
            f"{label}: not overwritten, it would point to {len(missing)} missing project folder(s) ({shown}); "
            "map them with --map-path or pass --allow-missing"
        )
        return True

    def _rewrite_traced(self, mapper: PathMapper, fn) -> tuple[object, set]:
        mapper.trace = set()
        try:
            return fn(), mapper.trace
        finally:
            mapper.trace = None

    def _restore_small(self, adapter, agent_id, entry, root, rel, target, mapper, rewrite, report) -> str:
        data = self.reader.read(agent_id, entry)
        trace: set = set()
        if rewrite:
            new, trace = self._rewrite_traced(mapper, lambda: adapter.rewrite_file(root, rel, data, mapper))
            if new is not None:
                data = new
                report.rewritten += 1
        keep_mtime = True
        if target.exists():
            existing = target.read_bytes()
            if existing == data:
                return "unchanged"
            if self.overwrite:
                if self._refuse_missing(trace, f"{root}/{rel}", report):
                    return "refused"
                status = "overwritten"
            else:
                merged = adapter.merge(root, rel, existing, data)
                if merged is None:
                    return "conflict"
                if merged == existing:
                    return "unchanged"
                data, status, keep_mtime = merged, "merged", False
        else:
            status = "created"
        if not self.dry_run:
            tmp = self._tmp(target)
            tmp.write_bytes(data)
            self._place(tmp, target, entry["mtime"] if keep_mtime else None, sqlite=False)
        return status

    def _restore_stream(self, adapter, agent_id, entry, root, rel, target, mapper, rewrite, report) -> str:
        sqlite = entry["kind"] == "sqlite"
        exists = target.exists()
        merge = sqlite and exists and not self.overwrite and adapter.merges_sqlite(root, rel)
        if exists and not rewrite:
            if target.stat().st_size == entry["size"] and _sha_file(target) == entry["sha256"]:
                return "unchanged"
            if not self.overwrite and not merge:
                return "conflict"
        if exists and sqlite and in_use(target):
            # Replacing it under a running agent (or its server) loses whatever it writes next.
            report.errors.append(
                f"{root}/{rel}: in use by another program; quit {adapter.name} (and any server it started) first"
            )
            return "refused"
        if self.dry_run:
            # Whether a rewritten database equals the target is only known after rewriting.
            if merge:
                return "merged"
            return ("overwritten" if self.overwrite else "conflict") if exists else "created"
        tmp = self._tmp(target)
        trace: set = set()
        try:
            self.reader.extract_to(agent_id, entry, tmp, self._on_chunk)
            if sqlite and rewrite:
                conn = sqlite3.connect(tmp)
                try:
                    changed, trace = self._rewrite_traced(
                        mapper, lambda: adapter.rewrite_sqlite(root, rel, conn, mapper)
                    )
                    if changed:
                        report.rewritten += 1
                    conn.commit()
                finally:
                    conn.close()
                for suffix in SIDECAR_SUFFIXES:
                    tmp.with_name(tmp.name + suffix).unlink(missing_ok=True)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        status = "created"
        if exists:
            if rewrite and _sha_file(tmp) == _sha_file(target):
                tmp.unlink()
                return "unchanged"
            if merge:
                try:
                    return "merged" if adapter.merge_sqlite(root, rel, target, tmp) else "unchanged"
                finally:
                    tmp.unlink(missing_ok=True)
            if not self.overwrite:
                tmp.unlink()
                return "conflict"
            if self._refuse_missing(trace, f"{root}/{rel}", report):
                tmp.unlink()
                return "refused"
            status = "overwritten"
            if sqlite and target.with_name(target.name + "-wal").exists():
                self.log.warn(f"{target} has a WAL file; make sure {adapter.name} is closed")
        self._place(tmp, target, entry["mtime"], sqlite=sqlite)
        return status

    def _tmp(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.with_name(target.name + ".aibackup-tmp")

    def _backup_name(self, path: Path) -> Path:
        return path.with_name(f"{path.name}.aibackup-{self.stamp}.bak")

    def _place(self, tmp: Path, target: Path, mtime: float | None, *, sqlite: bool) -> None:
        if target.exists():
            os.replace(target, self._backup_name(target))
        if sqlite:
            # A stale WAL next to a replaced database would corrupt it.
            for suffix in SIDECAR_SUFFIXES:
                side = target.with_name(target.name + suffix)
                if side.exists():
                    os.replace(side, self._backup_name(side))
        os.replace(tmp, target)
        if mtime is not None:
            os.utime(target, (mtime, mtime))
