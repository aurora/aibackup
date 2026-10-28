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
from aibackup.env import Env
from aibackup.pathmap import PathMapper
from aibackup.progress import NullProgress, Progress
from aibackup.sqlite_snapshot import SIDECAR_SUFFIXES

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
    ):
        self.reader = reader
        self.env = env
        self.dry_run = dry_run
        self.overwrite = overwrite
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

        for entry in self.reader.files(agent_id):
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
        return report

    def _restore_small(self, adapter, agent_id, entry, root, rel, target, mapper, rewrite, report) -> str:
        data = self.reader.read(agent_id, entry)
        if rewrite:
            new = adapter.rewrite_file(root, rel, data, mapper)
            if new is not None:
                data = new
                report.rewritten += 1
        keep_mtime = True
        if target.exists():
            existing = target.read_bytes()
            if existing == data:
                return "unchanged"
            if self.overwrite:
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
        if exists and not rewrite:
            if target.stat().st_size == entry["size"] and _sha_file(target) == entry["sha256"]:
                return "unchanged"
            if not self.overwrite:
                return "conflict"
        if self.dry_run:
            # Whether a rewritten database equals the target is only known after rewriting.
            return ("overwritten" if self.overwrite else "conflict") if exists else "created"
        tmp = self._tmp(target)
        try:
            self.reader.extract_to(agent_id, entry, tmp, self._on_chunk)
            if sqlite and rewrite:
                conn = sqlite3.connect(tmp)
                try:
                    if adapter.rewrite_sqlite(root, rel, conn, mapper):
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
            if not self.overwrite:
                tmp.unlink()
                return "conflict"
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
