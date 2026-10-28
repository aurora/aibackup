# AGENTS.md

Guidelines for humans and coding agents working on `aibackup`.

## Project

`aibackup` is a CLI that backs up, restores and migrates coding agent sessions
(Claude Code, Codex, Command Code, OpenCode, Zed, Zoo/Roo Code) across Windows,
Linux and macOS. The design is recorded in
[ADR 0001](docs/adr/0001-backup-tool-for-coding-agent-sessions.md); read it before
changing behavior.

## Hard constraints

- **Python ≥ 3.10, standard library only.** Do not add runtime dependencies. If a
  feature truly needs one, write an ADR first and make it an optional extra.
- **Cross-platform.** Every change must work on Windows, Linux, macOS and in WSL.
  Use `pathlib`, pass `encoding="utf-8"` explicitly, never assume `/` as separator
  for paths that come from agent data, and replace files with `os.replace`.
- **Never modify the user's agent data during development.** Backups only read.
  For restore experiments use a temporary target (`--home <tmp> --platform …
  --home-path …`). Do not delete or rewrite anything under real agent directories.
- **No built-in encryption.** Archives are encrypted by piping to `age`/`gpg`
  (see ADR 0001, section 4).
- **Secrets stay out by default.** New credential files belong to the `secret`
  category; tables with tokens go into the adapter's `scrub` mapping.
- **English only** for code, comments, docs, ADRs and commit messages.

## Layout

```
src/aibackup/
├── cli.py              # argument parsing and output only, no business logic
├── env.py              # target system: OS, home, base directories
├── pathmap.py          # prefix-based path mapping between systems
├── scan.py             # discovery and classification of files
├── backup.py           # writes archives
├── restore.py          # restores archives (quick check, conflicts, merge, rewrite)
├── archive.py          # ZIP container, manifest, verification
├── sqlite_snapshot.py  # consistent SQLite snapshots and column rewriting
├── progress.py         # progress line and formatting helpers
└── adapters/           # one module per agent, registered in adapters/__init__.py
tests/                  # unittest, synthetic data only
docs/adr/               # architecture decision records
```

## Design principles

- **Modular.** Agent-specific knowledge lives only in its adapter. Shared logic
  (scanning, archiving, restoring, path mapping) must not contain agent names.
  An adapter declares data roots per OS, category rules and optional hooks
  (`restore_path`, `rewrite_file`, `rewrite_sqlite`, `merge`, `scrub`).
- **Small, focused modules and functions.** One responsibility each. Prefer plain
  functions and dataclasses over class hierarchies.
- **Clean code.** Descriptive names, type hints on public functions, no dead code,
  no speculative abstractions. Keep the existing style (formatted like `black`
  with a line length of 120).
- **Comments explain why, not what.** Document non-obvious constraints, format
  quirks of agents and tradeoffs; don't restate the code.
- **Be defensive about agent formats.** They change without notice. Check that
  tables, columns and keys exist before touching them and skip anything unknown
  instead of failing. One broken file must not abort a backup or restore.
- **Rewrite only linking fields.** On migration, change only the fields an agent
  uses to link sessions to projects; never search-and-replace paths in message
  contents or binary data.
- **Stream large data.** Don't load big files or databases into memory; avoid
  per-file system calls the directory listing already answers.

## Adding or changing an adapter

1. Inspect the agent's real data **read-only** and verify locations per OS.
2. Add a module under `src/aibackup/adapters/` and register it in `__init__.py`.
3. Classify files into `data`, `config`, `checkpoint`, `secret`, `cache`; unknown
   files should fall into the default category so new files aren't lost.
4. Implement path rewriting for migration and merging where it makes sense.
5. Add tests (see below) and update the inventory, adapter and rewrite tables in
   ADR 0001 as well as the README.

## Tests

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

- Every change in behavior needs a test; bug fixes get a regression test.
- Tests use **synthetic data only**: neutral names such as `MyProject`,
  `/home/user` or `C:\Users\h\source\proj`. Never copy real project names, paths,
  session contents or credentials from a machine into tests, docs or examples.
- Round-trip tests (`tests/test_roundtrip.py`) build a fake home, back it up and
  restore it, ideally across platforms (e.g. Windows → Linux).
- Validate with real data only via read-only commands (`list`, `backup` to a temp
  file, `verify`) or a restore into a temporary home.

## Documentation

- Architectural decisions (new dependencies, archive format changes, new
  categories, security-relevant behavior) need a new ADR in `docs/adr/`
  (`NNNN-short-title.md`) or an update of the affected ADR.
- Keep README, ADR and `--help` texts in sync with the behavior.
- Bump `FORMAT_VERSION` in `src/aibackup/__init__.py` when the archive layout or
  manifest changes incompatibly.

## Commits

- **Small commits**, one logical change each (e.g. an adapter, a fix, a refactoring);
  don't mix refactorings with behavior changes.
- The test suite passes on every commit.
- Message: imperative subject, capitalized, max. ~50 characters, no trailing period;
  add a body (wrapped at 72) only when it explains *why*.
- Don't commit archives, `*.pyz`, `__pycache__` or anything from real agent directories.
