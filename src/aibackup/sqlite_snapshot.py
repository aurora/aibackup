"""Consistent snapshots of (possibly open) SQLite databases."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".db3", ".vscdb"}
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_HEADER = b"SQLite format 3\x00"


def is_sqlite(path: Path) -> bool:
    if path.suffix.lower() not in SQLITE_SUFFIXES:
        return False
    try:
        with path.open("rb") as f:
            return f.read(16) == _HEADER
    except OSError:
        return False


def sidecar_base(path: Path) -> Path | None:
    """For ``x.db-wal`` return ``x.db``."""
    for suffix in SIDECAR_SUFFIXES:
        if path.name.endswith(suffix):
            return path.with_name(path.name[: -len(suffix)])
    return None


def _backup(src: sqlite3.Connection, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")
    finally:
        out.close()


# Byte ranges SQLite locks (see os_unix.c / os_win.c): connections in WAL mode hold a
# shared lock on the -shm "DMS" byte as long as they are open; readers and writers of
# rollback-journal databases lock the SHARED range while they access the file.
_SHARED_FIRST, _SHARED_SIZE = 0x40000002, 510
_SHM_DMS = 128


def _locked(path: Path, start: int, length: int) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, start, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, length)
            os.lseek(fd, start, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, length)
        else:
            import fcntl

            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, length, start)
            fcntl.lockf(fd, fcntl.LOCK_UN, length, start)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def in_use(path: Path) -> bool:
    """Whether another process has the database open (reliable for WAL databases).

    Only probes locks, so the database is never opened or modified. An idle
    connection to a rollback-journal database holds no lock and goes unnoticed.
    """
    shm = path.with_name(path.name + "-shm")
    return _locked(path, _SHARED_FIRST, _SHARED_SIZE) or _locked(shm, _SHM_DMS, 1)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def snapshot(src: Path, dst: Path, scrub_tables: tuple[str, ...] = ()) -> list[str]:
    """Write a consistent copy of ``src`` to ``dst``; empties ``scrub_tables`` in the copy.

    Returns the tables that were scrubbed.
    """
    try:
        conn = sqlite3.connect(f"{src.as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            _backup(conn, dst)
        finally:
            conn.close()
    except sqlite3.Error:
        # Read-only access fails e.g. when a WAL file exists without its -shm.
        # Work on a private copy so the agent's files stay untouched.
        with tempfile.TemporaryDirectory(prefix="aibackup-") as tmp:
            copy = Path(tmp) / src.name
            shutil.copy2(src, copy)
            for suffix in ("-wal", "-shm"):
                side = src.with_name(src.name + suffix)
                if side.exists():
                    shutil.copy2(side, copy.with_name(copy.name + suffix))
            conn = sqlite3.connect(copy, timeout=30)
            try:
                _backup(conn, dst)
            finally:
                conn.close()

    scrubbed: list[str] = []
    if scrub_tables:
        out = sqlite3.connect(dst)
        try:
            out.execute("PRAGMA secure_delete=ON")
            for table in scrub_tables:
                if table_exists(out, table):
                    out.execute(f'DELETE FROM "{table}"')
                    scrubbed.append(table)
            out.commit()
        finally:
            out.close()
    return scrubbed


def rewrite_columns(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], fn) -> int:
    """Apply ``fn`` to text values of ``table.columns``; returns the number of changed values.

    Unknown tables/columns are skipped so schema changes in agents don't break restores.
    """
    if not table_exists(conn, table):
        return 0
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    changed = 0
    for col in columns:
        if col not in existing:
            continue
        rows = conn.execute(f'SELECT rowid, "{col}" FROM "{table}" WHERE typeof("{col}") = \'text\'').fetchall()
        for rowid, value in rows:
            new = fn(value)
            if new != value:
                conn.execute(f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?', (new, rowid))
                changed += 1
    return changed
