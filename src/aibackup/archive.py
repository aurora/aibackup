"""ZIP container with a JSON manifest."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

from aibackup import FORMAT_VERSION

MANIFEST = "manifest.json"
CHUNK = 1 << 20

# name -> (zip method, zlib level). Level 1 is ~2x faster than 6 for ~10-25% more size.
COMPRESSION = {
    "none": (zipfile.ZIP_STORED, None),
    "fast": (zipfile.ZIP_DEFLATED, 1),
    "best": (zipfile.ZIP_DEFLATED, 9),
}


def entry_name(agent: str, root: str, rel: str) -> str:
    return f"agents/{agent}/{root}/{rel}"


def safe_rel(rel: str) -> bool:
    """Reject paths that could escape the target directory."""
    if not rel or "\\" in rel or rel.startswith("/") or ":" in rel.split("/")[0]:
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def _zip_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    t = time.localtime(max(mtime, 315532800))  # ZIP cannot store dates before 1980
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


class ArchiveWriter:
    def __init__(self, output: str, compression: str = "fast"):
        self.output = output
        self.method, self.level = COMPRESSION[compression]
        if output == "-":
            self._target = None
            fileobj = sys.stdout.buffer
        else:
            self._target = Path(output)
            self._partial = self._target.with_name(self._target.name + ".partial")
            fileobj = self._partial.open("wb")
        self._fileobj = fileobj
        self.zip = zipfile.ZipFile(fileobj, "w")

    def _info(self, name: str, mtime: float) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=_zip_time(mtime))
        info.compress_type = self.method
        info.external_attr = 0o644 << 16
        # ZipFile ignores its own compresslevel for explicit ZipInfo objects.
        if self.level is not None:
            if hasattr(zipfile.ZipInfo, "compress_level"):  # Python >= 3.13
                info.compress_level = self.level
            else:
                info._compresslevel = self.level
        return info

    def add_file(self, name: str, src: Path, mtime: float, on_chunk=None) -> tuple[int, str]:
        """Stream ``src`` into the archive; returns (size, sha256) of what was stored."""
        info = self._info(name, mtime)
        digest = hashlib.sha256()
        size = 0
        with src.open("rb") as r, self.zip.open(info, "w", force_zip64=src.stat().st_size > (1 << 30)) as w:
            while chunk := r.read(CHUNK):
                digest.update(chunk)
                w.write(chunk)
                size += len(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
        return size, digest.hexdigest()

    def add_manifest(self, manifest: dict) -> None:
        data = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
        self.zip.writestr(self._info(MANIFEST, time.time()), data)

    def close(self, success: bool = True) -> None:
        self.zip.close()
        if self._target is None:
            self._fileobj.flush()
            return
        self._fileobj.close()
        if success:
            os.replace(self._partial, self._target)
        else:
            self._partial.unlink(missing_ok=True)


class ArchiveReader:
    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path)
        try:
            self.manifest = json.loads(self.zip.read(MANIFEST))
        except KeyError:
            raise ValueError(f"{path} is not an aibackup archive (no {MANIFEST})") from None
        version = self.manifest.get("format")
        if version != FORMAT_VERSION:
            raise ValueError(f"unsupported archive format {version!r} (expected {FORMAT_VERSION})")

    def files(self, agent: str) -> list[dict]:
        return self.manifest["agents"].get(agent, {}).get("files", [])

    def open(self, agent: str, entry: dict):
        return self.zip.open(entry_name(agent, entry["root"], entry["path"]))

    def read(self, agent: str, entry: dict) -> bytes:
        data = self.zip.read(entry_name(agent, entry["root"], entry["path"]))
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("checksum mismatch")
        return data

    def extract_to(self, agent: str, entry: dict, dst: Path, on_chunk=None) -> None:
        digest = hashlib.sha256()
        with self.open(agent, entry) as r, dst.open("wb") as w:
            while chunk := r.read(CHUNK):
                digest.update(chunk)
                w.write(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
        if digest.hexdigest() != entry["sha256"]:
            dst.unlink(missing_ok=True)
            raise ValueError("checksum mismatch")

    def verify(self) -> list[str]:
        problems = []
        names = set(self.zip.namelist())
        expected = {MANIFEST}
        for agent, info in self.manifest["agents"].items():
            for entry in info.get("files", []):
                name = entry_name(agent, entry["root"], entry["path"])
                expected.add(name)
                if name not in names:
                    problems.append(f"missing: {name}")
                    continue
                digest = hashlib.sha256()
                try:
                    with self.zip.open(name) as r:
                        while chunk := r.read(CHUNK):
                            digest.update(chunk)
                except (zipfile.BadZipFile, OSError, EOFError) as e:
                    problems.append(f"unreadable: {name}: {e}")
                    continue
                if digest.hexdigest() != entry["sha256"]:
                    problems.append(f"checksum mismatch: {name}")
        for name in sorted(names - expected):
            problems.append(f"not in manifest: {name}")
        return problems

    def close(self) -> None:
        self.zip.close()
