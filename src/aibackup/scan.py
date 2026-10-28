"""Discovery and classification of agent files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from aibackup.adapters import Adapter, Root
from aibackup.env import Env
from aibackup.sqlite_snapshot import is_sqlite, sidecar_base


@dataclass
class Item:
    root: Root
    rel: str
    path: Path
    category: str
    kind: str  # "file" | "sqlite"
    size: int
    mtime: float


def existing_roots(adapter: Adapter, env: Env) -> list[Root]:
    return [r for r in adapter.roots(env) if r.path.is_dir()]


def scan(adapter: Adapter, env: Env, include: set[str] | None = None, on_error=None) -> Iterator[Item]:
    """Yield the files of ``adapter``; ``include=None`` yields every category."""
    for root in existing_roots(adapter, env):
        if root.only is not None:
            candidates = ((rel, root.path / rel) for rel in root.only)
            for rel, path in candidates:
                if path.is_file() and (include is None or root.classify(rel) in include):
                    item = _item(root, rel, path, on_error)
                    if item:
                        yield item
            continue
        yield from _walk(root, root.path, "", include, on_error)


def _walk(root: Root, directory: Path, prefix: str, include, on_error) -> Iterator[Item]:
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError as e:
        if on_error:
            on_error(directory, e)
        return
    names = {e.name for e in entries}
    for entry in entries:
        rel = prefix + entry.name
        try:
            if entry.is_dir(follow_symlinks=False):
                category = root.classify_dir(rel)
                if include is not None and category is not None and category not in include:
                    continue
                yield from _walk(root, Path(entry.path), rel + "/", include, on_error)
            elif entry.is_file():
                if include is not None and root.classify(rel) not in include:
                    continue
                path = Path(entry.path)
                base = sidecar_base(path)
                if base is not None and base.name in names and is_sqlite(base):
                    continue  # contained in the snapshot of the main database
                item = _item(root, rel, path, on_error, entry)
                if item:
                    yield item
        except OSError as e:
            if on_error:
                on_error(Path(entry.path), e)


def _item(root: Root, rel: str, path: Path, on_error, entry: os.DirEntry | None = None) -> Item | None:
    try:
        # On Windows DirEntry.stat() is served from the directory listing; a
        # separate stat() opens every file (and wakes up the virus scanner).
        st = entry.stat() if entry is not None else path.stat()
    except OSError as e:
        if on_error:
            on_error(path, e)
        return None
    kind = "sqlite" if is_sqlite(path) else "file"
    return Item(root, rel, path, root.classify(rel), kind, st.st_size, st.st_mtime)
