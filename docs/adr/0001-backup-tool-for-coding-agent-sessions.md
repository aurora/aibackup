# ADR 0001: Backup Tool for Coding Agent Sessions

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** Harald Lapp

## Context

I use several coding agents side by side. Their sessions (chat histories, tool calls,
project memory, todos) are stored locally in the home directory, each in its own
format and location. There is no common export and hardly any built-in backup.

Goals:

1. **Disaster recovery:** Sessions must be recoverable after a disk failure.
2. **Platform migration:** Sessions must be movable between Windows, Linux and macOS
   and remain usable in the respective agent on the target system.

### Inventory (Windows machine, as of 2026-10-27)

| Agent | Location (Windows) | Format | Size | Notes |
|---|---|---|---|---|
| Claude Code | `~/.claude/`, `~/.claude.json` | JSONL per session under `projects/<encoded-path>/`, `history.jsonl`, `file-history/` | ~960 MB (`projects/`: 884 MB) | Directory name = absolute project path with `[^A-Za-z0-9]` → `-` (verified for 35 of 36 projects); every line contains `"cwd"`; `~/.claude.json` contains `primaryApiKey`/`oauthAccount` |
| Codex CLI | `~/.codex/` | SQLite (`state_5.sqlite`: `threads.cwd`, `threads.rollout_path`), rollouts as JSONL under `sessions/` | ~540 MB (mostly `plugins/`, `.tmp/`) | currently no sessions stored locally; `auth.json` |
| Command Code | `~/.commandcode/` (typically in WSL, e.g. `\\wsl$\<distro>\home\<user>\.commandcode`) | per session `projects/<slug>/<id>.jsonl`, `.meta.json`, `.checkpoints.jsonl`; `history.jsonl`, `plans/`, `file-history/` | ~27 MB | Directory name = `@sindresorhus/slugify(cwd)` (lowercase, CamelCase → `-`, lossy); only newer sessions have a header line `{"type":"session","cwd":…}`; tokens in `auth.json`, `mcp-tokens.json` |
| OpenCode | `~/.local/share/opencode/` (on Windows too), `~/.config/opencode/` | `opencode.db` (SQLite, WAL, ~2 GB), legacy JSON under `storage/` | ~2.3 GB | Paths in `project.worktree`, `session.directory`; **tokens in DB tables** `account`, `control_account`, `credential`, `session_share` and in `auth.json` |
| Zed Agent | `%LOCALAPPDATA%\Zed\threads\threads.db`, `…\conversations\*.zed.json`, `%APPDATA%\Zed\` | SQLite (`threads.folder_paths`, `data` zstd-compressed) + JSON | ~34 MB | The data directory also holds Node, language servers and extensions |
| Zoo Code (VS Code) | `%APPDATA%\Code\User\globalStorage\zoocodeorganization.zoo-code\` | JSON per task under `tasks/<id>/`, `tasks/_index.json` | ~1.4 GB | Roo Code fork; `workspace` in `history_item.json`/`_index.json`; `checkpoints/` = shadow Git repo with `worktree = <absolute path>`; API keys live in VS Code SecretStorage (OS keychain, not portable) |
| Roo Code (VS Code) | `…\globalStorage\rooveterinaryinc.roo-cline\` | same as Zoo Code | ~8 GB | same format, hence the same adapter |
| VS Code Copilot Chat / Cursor | `workspaceStorage\<hash>\…`, `state.vscdb` | JSON, SQLite (key/value) | – | `<hash>` is derived from the workspace URI |

The Linux/macOS locations are derived from the tools' documentation and behavior
(e.g. Zed: `~/.local/share/zed/` or `~/Library/Application Support/Zed/`;
VS Code: `~/.config/Code/User/` or `~/Library/Application Support/Code/User/`)
and still need to be verified on those systems.

### Core problems

- **Heterogeneous formats:** file trees, JSON/JSONL, SQLite (partly WAL, open while in use), LMDB.
- **Consistency:** a file copy of an open SQLite database plus `-wal`/`-shm` can
  produce an inconsistent state.
- **Absolute paths:** sessions are linked to projects via paths (directory names,
  JSON fields, DB columns, Git config, hashes).
- **Secrets:** tokens are stored in files *and* in the agents' databases.
- **Volume:** several GB, much of it caches, binaries and checkpoints.

## Decision drivers

- Runs identically on Windows, Linux and macOS (and in WSL)
- No dependencies besides Python
- The backup archive is self-describing and readable without the tool
- Restore is safe (no silent overwrites) and transparent (dry run)
- New agents can be added with little effort
- New files introduced by future agent versions are not silently lost

## Considered options

### Language / runtime

| Option | Pros | Cons |
|---|---|---|
| **Python 3 (standard library only)** | available everywhere; `sqlite3`, `zipfile`, `json`, `hashlib` built in; fast to develop | runtime must be installed; no crypto in the standard library |
| Go / Rust (single binary) | no runtime needed | more effort; one build per platform |
| Shell (bash + PowerShell) | nothing to install | two implementations; SQLite snapshots and path rewriting hardly feasible |
| restic / Borg / rsync | mature, incremental, encrypted | unaware of SQLite consistency, path migration and per-agent secret filtering |

### Archive format

| Option | Pros | Cons |
|---|---|---|
| **ZIP (Deflate, ZIP64)** | opens natively on every OS; random access; standard library | somewhat worse compression |
| tar.gz | good compression, streaming | no random access; inconvenient on Windows |
| tar.zst | best compression | standard library support only from Python 3.14 |

### File selection

| Option | Pros | Cons |
|---|---|---|
| Complete directories | nothing is lost | caches, binaries (Zed: Node, language servers) and tokens end up in the backup; gigabytes of ballast |
| Allowlist (known session files only) | minimal | new files in future agent versions are silently missing |
| **Categories with a denylist** | unknown files are included; ballast and secrets are excluded on purpose; extensible up to a full backup via flags | rules need maintenance |

## Decision

We build a **Python CLI tool `aibackup`** (Python ≥ 3.10, standard library only)
with **one adapter per agent**, a **ZIP archive with a manifest** and a
**category for every file**.

### 1. What is backed up: categories

Each adapter assigns files to a category via glob rules (first matching rule wins;
directory rules `dir/*` cut off whole subtrees). Unmatched files get the data root's
default category, usually `data`. This way files introduced by a new agent version
are backed up as well.

| Category | Contents (examples) | Default |
|---|---|---|
| `data` | sessions, history, memory, todos, task index | ✅ |
| `config` | `settings.json`, `AGENTS.md`/`CLAUDE.md`, skills, custom modes, Zed config | ✅ |
| `checkpoint` | Claude/Command Code `file-history/`, Zoo/Roo `tasks/*/checkpoints/`, OpenCode `snapshot/` | ❌ `--include checkpoint` |
| `secret` | `.credentials.json`, `auth.json`, `mcp-tokens.json`, `~/.claude.json`, `mcp_settings.json` | ❌ `--include secret` |
| `cache` | caches, logs, binaries, telemetry, plugin marketplaces, Zed runtimes | ❌ `--include cache` |

`--include all` backs up the directories completely (SQLite still as snapshots).

Checkpoints are excluded by default because they are snapshots of *project files*
(which live in the project repository anyway), make up most of the volume and only
serve to rewind within a session.

### 2. Adapters

| Adapter | Data roots |
|---|---|
| `claude-code` | `~/.claude/` (or `$CLAUDE_CONFIG_DIR`), `~/.claude.json`; Claude Desktop's user data dir (Windows `%APPDATA%\Claude` or the Microsoft Store package dir, Linux `~/.config/Claude`, macOS `~/Library/Application Support/Claude`): `claude-code-sessions/`, `local-agent-mode-sessions/`, `Local Storage/`, `IndexedDB/` as `data`, `claude_desktop_config.json` as `secret`, everything else as `cache` |
| `codex` | `~/.codex/` (or `$CODEX_HOME`) |
| `command-code` | `~/.commandcode/`; when it runs in WSL, the WSL home is addressed via `--home`/`--platform linux`/`--home-path`; desktop app user data dir `Command Code` (Linux `~/.config`; Windows `%APPDATA%` and macOS `~/Library/Application Support` assumed from Electron's defaults, unverified): `app-state.json` as `data`, everything else (including the rebuildable `thread-index.db`) as `cache` |
| `opencode` | `$XDG_DATA_HOME/opencode`, `$XDG_CONFIG_HOME/opencode` (default `~/.local/share`, `~/.config`, on every OS); OpenCode Desktop's user data dir `ai.opencode.desktop` (Windows `%APPDATA%`, Linux `~/.config`, macOS `~/Library/Application Support`): `drafts.sqlite` and legacy `*.dat` as `data`, `opencode.settings` as `config`, everything else as `cache` |
| `zed` | Windows `%LOCALAPPDATA%\Zed` + `%APPDATA%\Zed`; Linux `~/.local/share/zed` + `~/.config/zed`; macOS `~/Library/Application Support/Zed` + `~/.config/zed` |
| `zoo`, `roo` | `globalStorage/<extension-id>` in VS Code, VS Code Insiders, VSCodium, Cursor, Windsurf |

VS Code Copilot Chat and Cursor's own chats are **not supported for now**
(linked via workspace hash, stored in `state.vscdb`).

### 3. Consistent SQLite snapshots

SQLite files (detected by their header) are snapshotted read-only via the online
backup API (`sqlite3.Connection.backup()`). WAL contents are included, `-wal`/`-shm`
are not copied, and the agent may keep running.

### 4. Secrets

- Secret files are excluded by default (category `secret`).
- Tables holding tokens (OpenCode: `account`, `control_account`, `credential`,
  `session_share`) are emptied **in the snapshot** (`secure_delete`) unless `secret`
  is included. If the snapshot of such a database fails, it is skipped instead of
  being copied raw.
- No built-in encryption. The archive can be written to stdout and encrypted with
  `age`/`gpg`. For unattended runs, public-key encryption (`age -r`) is preferred:
  the machine only holds the public key, which cannot decrypt the backup.

  ```sh
  aibackup backup -o - | age -r age1… > backup.zip.age
  aibackup backup -o - | gpg --batch --pinentry-mode loopback --passphrase-file FILE -c > backup.zip.gpg
  ```

  Built-in ZIP passwords were rejected: the standard library cannot write them,
  legacy ZipCrypto is broken, and even AES ZIP leaves file names (which contain
  project names) readable.

### 5. Archive format

```
aibackup-<host>-<YYYYMMDD-HHMMSS>.zip
├── manifest.json
└── agents/<adapter>/<root>/<relative/path>
```

- Paths inside the archive are relative to the data root and use POSIX separators.
- `manifest.json`: format/tool version, source OS, home path, host name, data roots
  per agent and, per file, category, kind (`file`/`sqlite`), size, mtime, SHA-256.
- Compression is Deflate level 1 by default (`--compression fast`); measured on
  real data it is about twice as fast as level 6 for an archive 10–25 % larger.
  `best` (level 9) and `none` are available.
- `aibackup verify` decompresses every entry and checks it against the manifest.

### 6. Platform migration via path mapping

On restore a path mapping is built; the longest prefix wins:

1. explicit mappings: `--map-path 'C:\Users\me\source=/home/me/code'`
2. implicit: each data root source → target (e.g. `C:\…\.codex` → `/home/me/.codex`)
3. implicit: home → home

Windows paths are compared case-insensitively and with both separators; the result
uses the target's spelling.

Only fields an agent uses to link sessions to projects are rewritten. Paths inside
message contents (tool calls, outputs) stay unchanged; they are history.

| Adapter | Rewritten |
|---|---|
| `claude-code` | directory names under `projects/` (encoding based on the mapped path), top-level `cwd` in session JSONL, `project` in `history.jsonl`, project keys in `~/.claude.json` (trust); every absolute path in Desktop's session records (many version-dependent fields) |
| `command-code` | directory names under `projects/` (slug based on the mapped path), `cwd` in the session header line; in the desktop app's `app-state.json` project paths, chat `projectPath`/`worktreePath`, expanded/removed projects and trusted roots |
| `codex` | `cwd` in rollouts; `threads.cwd`, `threads.rollout_path`, `local_thread_catalog.cwd`, `automation_runs.source_cwd` |
| `opencode` | `project.worktree`, `session.directory`, `project_directory.directory`, `workspace.directory`; `directory`/`worktree` in `storage/`; every absolute path in the JSON values of Desktop's `drafts.sqlite` `state` table |
| `zed` | `threads.folder_paths` (not: zstd-compressed `data` blobs); `sidebar_threads.folder_paths`/`main_worktree_paths` in the workspace database when the archive includes it (category `cache`) |
| `zoo`, `roo` | `workspace` in `history_item.json`/`_index.json`; `worktree` in the checkpoint Git config |

Deliberately not rewritten: Codex `config.toml`.

After restoring, sessions whose mapped project folder does not exist on the target
are reported together with the source path, because agents only list sessions of
existing folders. Folders inside an existing Git repository (build output) are not
reported, except vanished Claude Code worktrees (`.claude/worktrees/<name>`).

The `cache` category is not restored onto another OS: it holds platform-specific
binaries.

### 7. Safe restore

- `--dry-run` reports what would be created and where conflicts exist.
- Existing files whose size and mtime match the archive are treated as unchanged
  (like rsync; restored files get their original mtime). `--checksum` compares
  contents instead.
- Existing files with different content are reported as conflicts and left alone.
  `history.jsonl` (Claude Code, Codex, Command Code) and `tasks/_index.json`
  (Zoo/Roo) are merged. With `--overwrite` the original is first moved to
  `<name>.aibackup-<timestamp>.bak`, for SQLite including `-wal`/`-shm`.
- Files are replaced atomically (`os.replace`) and mtimes are restored (agents sort
  sessions by them).
- `--overwrite` refuses to replace a SQLite database another process has open (probed
  via SQLite's file locks, without opening it): a running agent or its background
  server would keep writing to the moved-away file. It also refuses to replace a file
  whose rewritten paths point to project folders that don't exist on the target,
  unless `--allow-missing` is given; agents hide such sessions and OpenCode archives
  the projects, which looks like data loss.
- Archive paths are validated (no `..`, no absolute paths).
- Directories that are only consistent as a whole (LevelDB databases: Claude Desktop's
  `Local Storage`, `IndexedDB`) are never mixed with archived files. If one exists, the
  archived copy goes to `<dir>.aibackup-<timestamp>` and the adapter may merge from it:
  Claude Desktop's sidebar groups and the assignment of local sessions to them
  (Local Storage key `dframe-store`, not synced to the account) are added to the
  existing database by appending a write batch to its log (`chromium_leveldb.py`,
  standard library only). This requires Claude Desktop to be quit; it is checked via
  the database lock.
- Zed's agent sidebar lists threads from `sidebar_threads` in the workspace database,
  which Zed backfilled from `threads.db` only once. `desktop-index -a zed` adds rows for
  top-level threads missing there and replaces paths of rows that point to
  non-existent folders with the (already mapped) paths from `threads.db`. The
  workspace database stays in the `cache` category (it is mostly editor state).
- The Command Code desktop app lists projects and chats from `app-state.json`. If it
  exists on the target, missing projects, chats and trusted roots are merged into it
  (entries the user removed or deleted stay out). Trusted roots are only taken from
  the archive, never added by the tool. Files of a desktop app that is running are
  not restored at all (`Adapter.busy`), because the app rewrites its state on exit.
- OpenCode Desktop's sidebar only shows the projects listed in `usedProjectIDs` of
  its UI state (`drafts.sqlite`, table `state`). An adapter may declare SQLite files
  it can merge (`sqlite_merges`); for this one, missing state entries and listed
  projects are added to an existing file instead of reporting a conflict. Per-window
  state is skipped. Requires OpenCode Desktop to be quit (checked via Electron's
  `SingletonLock`/`lockfile`).
- `aibackup desktop-index` rebuilds these lists for archives without them: Claude
  Desktop's session list from Claude Code transcripts (the same records as Desktop's
  own "Import Claude Code CLI Sessions"), and OpenCode Desktop's project list from the
  projects with sessions in `opencode.db`.
- `--home`, `--platform` and `--home-path` redirect the target, e.g. directly into a
  WSL home: `--home '\\wsl$\Ubuntu\home\me' --platform linux --home-path /home/me`.

### 8. CLI

```
aibackup list    [-a AGENTS] [--no-size]
aibackup backup  [-a AGENTS] [-o FILE|-] [--include checkpoint,secret,cache|all] [--exclude …] [--compression fast|best|none]
aibackup inspect FILE [--files]
aibackup verify  FILE
aibackup restore FILE [-a AGENTS] [--map-path FROM=TO]… [--dry-run] [--overwrite] [--allow-missing] [--checksum]
aibackup desktop-index [-a claude-code,opencode,command-code,zed] [--dry-run] [--org ORG]
```

Backup and restore show a progress line when stderr is a terminal.
Installation via `pipx install .` / `uv tool install .` or as a `zipapp` (`aibackup.pyz`).

## Consequences

**Positive**

- One command backs up all agent sessions consistently, even while agents are running.
- Default backups are lean (sessions + configuration); a full backup is one flag away.
- Secrets do not end up in the backup by accident, not even via SQLite tables.
- The archive can be read with any ZIP program.

**Negative / risks**

- **Formats are not stable** (cf. `state_5.sqlite`). Adapters need maintenance and
  tests with realistic sample data. Path rewriting checks tables/columns before
  accessing them and skips anything unknown.
- **Migration is limited** to the linking fields. An agent may still treat a
  migrated session differently (e.g. Zed, if paths inside the `data` blob matter).
- **Zoo/Roo task history:** whether newer versions build the list from
  `tasks/_index.json` alone or also need the global state in `state.vscdb` is unverified.
- **Desktop app internals:** Claude Desktop's session records and sidebar state and
  OpenCode Desktop's UI state are internal to the apps; they may change with any
  update.
- **No incremental backups** in v1.
- **Python must be installed.**

## Open points

1. Verify the data locations on Linux and macOS for each adapter.
2. Zoo/Roo: back up/restore the global state (`state.vscdb`, key `<Publisher>.<extension>`)?
3. Adapters for VS Code Copilot Chat and Cursor (recompute the workspace hash).
4. Scheduled backups (Task Scheduler / systemd timer / launchd) and retention rules.
