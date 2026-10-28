from __future__ import annotations

import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aibackup import FORMAT_VERSION, __version__
from aibackup.adapters import ADAPTERS
from aibackup.archive import ArchiveWriter, entry_name
from aibackup.env import Env
from aibackup.progress import NullProgress, Progress, human
from aibackup.scan import existing_roots, scan
from aibackup.sqlite_snapshot import snapshot


def default_output_name() -> str:
    host = "".join(c if c.isalnum() or c in "-_" else "-" for c in socket.gethostname()) or "host"
    return f"aibackup-{host}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"


def run_backup(
    env: Env,
    agents: list[str],
    include: set[str],
    output: str,
    log,
    compression: str = "fast",
    progress: Progress | None = None,
) -> dict:
    progress = progress or NullProgress()
    manifest = {
        "format": FORMAT_VERSION,
        "tool": f"aibackup {__version__}",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "source": {"os": env.os, "home": env.home_path},
        "include": sorted(include),
        "agents": {},
        "errors": [],
    }

    def on_error(path: Path, exc: Exception) -> None:
        manifest["errors"].append({"path": str(path), "error": str(exc)})
        log.warn(f"skipped {path}: {exc}")

    # Scan everything first so the progress display knows the total.
    progress.set_label("scanning", force=True)
    plan = []
    for agent_id in agents:
        adapter = ADAPTERS[agent_id]
        roots = existing_roots(adapter, env)
        if not roots:
            log.info(f"{agent_id}: not found")
            continue
        plan.append((adapter, roots, list(scan(adapter, env, include, on_error))))
    progress.begin(sum(item.size for _, _, items in plan for item in items))

    writer = ArchiveWriter(output, compression)
    ok = False
    try:
        with tempfile.TemporaryDirectory(prefix="aibackup-") as tmp:
            snap = Path(tmp) / "snapshot.db"
            for adapter, roots, items in plan:
                agent_id = adapter.id
                files = []
                total = 0
                for item in items:
                    name = entry_name(agent_id, item.root.name, item.rel)
                    entry = {"root": item.root.name, "path": item.rel, "category": item.category, "kind": item.kind}
                    try:
                        if item.kind == "sqlite":
                            progress.set_label(f"{agent_id}: snapshot of {item.rel} ({human(item.size)})", force=True)
                            scrub = () if "secret" in include else adapter.scrub_tables(item.root.name, item.rel)
                            scrubbed = snapshot(item.path, snap, scrub)
                            if scrubbed:
                                entry["scrubbed"] = scrubbed
                            progress.set_label(f"{agent_id}: {item.rel}")
                            size, sha = writer.add_file(name, snap, item.mtime, progress.advance)
                            snap.unlink(missing_ok=True)
                        else:
                            progress.set_label(f"{agent_id}: {item.rel}")
                            size, sha = writer.add_file(name, item.path, item.mtime, progress.advance)
                    except Exception as e:  # noqa: BLE001 - one broken file must not abort the backup
                        on_error(item.path, e)
                        continue
                    entry.update(size=size, mtime=item.mtime, sha256=sha)
                    files.append(entry)
                    total += size
                    log.debug(f"  {item.category:<10} {item.root.name}/{item.rel}")
                manifest["agents"][agent_id] = {
                    "roots": {r.name: env.logical(r.path) for r in roots},
                    "files": files,
                }
                log.info(f"{agent_id}: {len(files)} files, {human(total)}")
        writer.add_manifest(manifest)
        ok = True
    finally:
        writer.close(success=ok)
        progress.clear()
    return manifest
