from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

from aibackup import __version__
from aibackup.adapters import ADAPTERS, CATEGORIES, DEFAULT_CATEGORIES
from aibackup.archive import COMPRESSION, ArchiveReader
from aibackup.backup import default_output_name, run_backup
from aibackup.env import OS_NAMES, Env
from aibackup.pathmap import parse_mapping
from aibackup.progress import Progress, duration, human
from aibackup.restore import Restorer
from aibackup.scan import existing_roots, scan


class Log:
    def __init__(self, verbose: bool):
        self.verbose = verbose
        # Per-file output (-v) and a progress line don't mix.
        self.progress = Progress(enabled=False if verbose else None)

    def _print(self, msg: str) -> None:
        self.progress.clear()
        print(msg, file=sys.stderr)

    def info(self, msg: str) -> None:
        self._print(msg)

    def warn(self, msg: str) -> None:
        self._print(f"warning: {msg}")

    def debug(self, msg: str) -> None:
        if self.verbose:
            self._print(msg)


def _csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out += [v.strip() for v in value.split(",") if v.strip()]
    return out


def _agents(values: list[str] | None) -> list[str]:
    agents = _csv(values)
    if not agents or agents == ["all"]:
        return list(ADAPTERS)
    unknown = [a for a in agents if a not in ADAPTERS]
    if unknown:
        raise SystemExit(f"error: unknown agent(s): {', '.join(unknown)} (known: {', '.join(ADAPTERS)})")
    return agents


def _categories(include: list[str] | None, exclude: list[str] | None) -> set[str]:
    selected = set(DEFAULT_CATEGORIES)
    for name in _csv(include):
        if name == "all":
            selected |= set(CATEGORIES)
        elif name in CATEGORIES:
            selected.add(name)
        else:
            raise SystemExit(f"error: unknown category {name!r} (known: {', '.join(CATEGORIES)}, all)")
    for name in _csv(exclude):
        if name not in CATEGORIES:
            raise SystemExit(f"error: unknown category {name!r}")
        selected.discard(name)
    return selected


def _env(args) -> Env:
    try:
        return Env.custom(args.platform, Path(args.home) if args.home else None, args.home_path)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from None


# Commands ---------------------------------------------------------------------


def cmd_list(args, log: Log) -> int:
    env = _env(args)
    print(f"System: {env.os}, home {env.home_path}")
    for agent_id in _agents(args.agent):
        adapter = ADAPTERS[agent_id]
        roots = existing_roots(adapter, env)
        print(f"\n{agent_id} ({adapter.name})")
        if not roots:
            print("  not found")
            continue
        for root in roots:
            print(f"  {root.name:<14} {env.logical(root.path)}")
        if args.no_size:
            continue
        sizes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for item in scan(adapter, env, None):
            sizes[item.category][0] += item.size
            sizes[item.category][1] += 1
        for category in CATEGORIES:
            if category in sizes:
                size, count = sizes[category]
                mark = "" if category in DEFAULT_CATEGORIES else "  (only with --include)"
                print(f"    {category:<11} {human(size):>10} {count:>7} files{mark}")
    return 0


def cmd_backup(args, log: Log) -> int:
    env = _env(args)
    include = _categories(args.include, args.exclude)
    output = args.output or default_output_name()
    if output != "-" and Path(output).exists() and not args.force:
        raise SystemExit(f"error: {output} exists (use --force to replace it)")
    log.info(f"Backing up {', '.join(sorted(include))} from {env.home_path}")
    started = time.monotonic()
    manifest = run_backup(env, _agents(args.agent), include, output, log, args.compression, log.progress)
    if "secret" in include:
        log.warn("the archive contains credentials; store it encrypted")
    elapsed = duration(time.monotonic() - started)
    if output != "-":
        log.info(f"Wrote {output} ({human(Path(output).stat().st_size)}) in {elapsed}")
    else:
        log.info(f"Done in {elapsed}")
    return 1 if manifest["errors"] else 0


def cmd_inspect(args, log: Log) -> int:
    reader = ArchiveReader(Path(args.archive))
    m = reader.manifest
    print(f"Created:  {m['created']} by {m['tool']}")
    print(f"Source:   {m['host']} ({m['source']['os']}), home {m['source']['home']}")
    print(f"Included: {', '.join(m['include'])}")
    for agent_id, info in m["agents"].items():
        files = info["files"]
        print(f"\n{agent_id}: {len(files)} files, {human(sum(f['size'] for f in files))}")
        for name, path in info["roots"].items():
            print(f"  {name:<14} {path}")
        by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for f in files:
            by_cat[f["category"]][0] += f["size"]
            by_cat[f["category"]][1] += 1
        for category, (size, count) in sorted(by_cat.items()):
            print(f"    {category:<11} {human(size):>10} {count:>7} files")
        if args.files:
            for f in files:
                extra = f"  [scrubbed: {', '.join(f['scrubbed'])}]" if f.get("scrubbed") else ""
                print(f"      {f['category']:<10} {human(f['size']):>10}  {f['root']}/{f['path']}{extra}")
    if m.get("errors"):
        print(f"\n{len(m['errors'])} file(s) could not be backed up:")
        for e in m["errors"]:
            print(f"  {e['path']}: {e['error']}")
    reader.close()
    return 0


def cmd_verify(args, log: Log) -> int:
    reader = ArchiveReader(Path(args.archive))
    problems = reader.verify()
    reader.close()
    for p in problems:
        print(p)
    count = sum(len(a["files"]) for a in reader.manifest["agents"].values())
    print(f"{count} files checked, {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_restore(args, log: Log) -> int:
    env = _env(args)
    reader = ArchiveReader(Path(args.archive))
    manifest = reader.manifest
    try:
        explicit = [parse_mapping(m) for m in args.map_path or []]
    except ValueError as e:
        raise SystemExit(f"error: {e}") from None

    requested = _agents(args.agent) if args.agent else list(manifest["agents"])
    agents = [a for a in requested if a in manifest["agents"]]
    for a in requested:
        if a not in manifest["agents"]:
            log.warn(f"{a} is not contained in the archive")
        elif a not in ADAPTERS:
            log.warn(f"{a} is not supported by this version")
    agents = [a for a in agents if a in ADAPTERS]

    src = manifest["source"]
    log.info(f"Restoring from {src['os']} ({src['home']}) to {env.os} ({env.home_path})" + (" [dry run]" if args.dry_run else ""))
    restorer = Restorer(
        reader, env, dry_run=args.dry_run, overwrite=args.overwrite, checksum=args.checksum, log=log, progress=log.progress
    )
    started = time.monotonic()
    log.progress.begin(sum(f["size"] for a in agents for f in reader.files(a)))
    failed = conflicts = False
    missing: dict[str, tuple[str, set[str], set[str]]] = {}
    for agent_id in agents:
        report = restorer.restore_agent(agent_id, explicit)
        for dst, (src_path, children) in report.missing.items():
            entry = missing.setdefault(dst, (src_path, set(), set()))
            entry[1].update(children)
            entry[2].add(agent_id)
        counts = ", ".join(f"{n} {s}" for s, n in sorted(report.counts.items()))
        log.info(f"{agent_id}: {counts or 'nothing to do'}" + (f"; paths rewritten in {report.rewritten} files" if report.rewritten else ""))
        if report.mapper and not report.mapper.is_identity:
            for rule in report.mapper.rules:
                if not rule.identity:
                    log.debug(f"  map {rule.src} -> {rule.dst}")
        for c in report.conflicts[:10]:
            log.info(f"  conflict: {c}")
        if len(report.conflicts) > 10:
            log.info(f"  ... and {len(report.conflicts) - 10} more")
        for note in report.notes:
            log.info(f"  {note}")
        for e in report.errors:
            log.info(f"  error: {e}")
        conflicts |= bool(report.conflicts)
        failed |= bool(report.errors or (report.conflicts and not args.dry_run))
    if conflicts:
        log.info("Existing files that differ were kept; rerun with --overwrite to replace them (originals are kept as *.bak).")
    if missing:
        _report_missing(missing, log)
    log.info(f"Done in {duration(time.monotonic() - started)}")
    reader.close()
    return 1 if failed else 0


def _report_missing(missing: dict[str, tuple[str, set[str], set[str]]], log: Log, limit: int = 15) -> None:
    log.warn(
        f"sessions refer to {len(missing)} project folder(s) that do not exist on this system; "
        "agents only show sessions of existing project folders:"
    )
    items = sorted(missing.items(), key=lambda kv: (-len(kv[1][1]), kv[0].lower()))
    shown = items if log.verbose else items[:limit]
    for dst, (src_path, children, agent_ids) in shown:
        names = sorted(children, key=str.lower)
        inside = f", e.g. {', '.join(names[:4])}{', ...' if len(names) > 4 else ''}" if names else ""
        log.info(f"  {dst}  <- {src_path}  [{', '.join(sorted(agent_ids))}{inside}]")
    if len(shown) < len(items):
        log.info(f"  ... and {len(items) - len(shown)} more (use -v to list all)")
    example = items[0][1][0]
    log.info(f"If the projects live elsewhere, rerun with --map-path, e.g. --map-path '{example}=/path/to/projects'.")


# Parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aibackup", description="Backup, restore and migrate coding agent sessions.")
    parser.add_argument("--version", action="version", version=f"aibackup {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every file")
    sub = parser.add_subparsers(dest="command", required=True)

    def target_options(p):
        g = p.add_argument_group("target system (default: current user)")
        g.add_argument("--home", help="home directory to operate on, e.g. \\\\wsl$\\Ubuntu\\home\\me")
        g.add_argument("--platform", choices=OS_NAMES, help="OS of that home")
        g.add_argument("--home-path", help="path of that home as seen on its OS, e.g. /home/me")

    agent_help = f"agents, comma separated (default: all): {', '.join(ADAPTERS)}"

    p = sub.add_parser("list", help="show detected agents and data sizes")
    p.add_argument("-a", "--agent", action="append", help=agent_help)
    p.add_argument("--no-size", action="store_true", help="skip size calculation")
    target_options(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("backup", help="create a backup archive")
    p.add_argument("-a", "--agent", action="append", help=agent_help)
    p.add_argument("-o", "--output", help="archive file, '-' for stdout (default: aibackup-<host>-<time>.zip)")
    p.add_argument("--include", action="append", help=f"additional categories: {', '.join(CATEGORIES)}, all")
    p.add_argument("--exclude", action="append", help="categories to leave out")
    p.add_argument(
        "--compression",
        choices=list(COMPRESSION),
        default="fast",
        help="fast (default), best (smallest, much slower) or none",
    )
    p.add_argument("-f", "--force", action="store_true", help="replace an existing output file")
    target_options(p)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("inspect", help="show the contents of an archive")
    p.add_argument("archive")
    p.add_argument("--files", action="store_true", help="list all files")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("verify", help="check archive integrity")
    p.add_argument("archive")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("restore", help="restore an archive, optionally onto another OS")
    p.add_argument("archive")
    p.add_argument("-a", "--agent", action="append", help="agents to restore (default: all in archive)")
    p.add_argument("--map-path", action="append", metavar="FROM=TO", help="rewrite project paths with prefix FROM to TO")
    p.add_argument("-n", "--dry-run", action="store_true", help="only report what would happen")
    p.add_argument("--overwrite", action="store_true", help="replace differing files (originals are kept as *.bak)")
    p.add_argument(
        "--checksum",
        action="store_true",
        help="compare existing files by content instead of size and modification time",
    )
    target_options(p)
    p.set_defaults(func=cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, Log(args.verbose))
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
