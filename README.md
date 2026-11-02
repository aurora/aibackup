# aibackup

Backs up the sessions of local coding agents into a ZIP archive and restores them,
optionally onto another operating system (Windows ⇄ Linux ⇄ macOS).
Requires only Python ≥ 3.10, no other packages.

Supported: Claude Code (including Claude Desktop's session list), Codex CLI, Command Code, OpenCode, Zed Agent, Zoo Code, Roo Code
(in VS Code, VS Code Insiders, VSCodium, Cursor, Windsurf).
Background and design decisions: [ADR 0001](docs/adr/0001-backup-tool-for-coding-agent-sessions.md).

## Installation

```sh
pipx install .            # or: uv tool install .
```

Without installing:

```sh
python -m zipapp src -m "aibackup.cli:main" -o aibackup.pyz
python aibackup.pyz --help
```

## Usage

```sh
aibackup list                              # detected agents, paths and sizes per category
aibackup backup                            # -> aibackup-<host>-<time>.zip
aibackup backup -a claude-code,zoo -o sessions.zip
aibackup verify sessions.zip
aibackup inspect sessions.zip --files
aibackup restore sessions.zip --dry-run
aibackup restore sessions.zip
aibackup desktop-index                     # rebuild the desktop apps' session/project lists
```

### What is backed up?

Not the complete directories. Every file belongs to a category:

| Category | Examples | Default |
|---|---|---|
| `data` | sessions, history, memory, task index, SQLite databases | yes |
| `config` | settings, `AGENTS.md`/`CLAUDE.md`, skills, custom modes | yes |
| `checkpoint` | Claude/Command Code `file-history/`, Zoo/Roo checkpoints, OpenCode `snapshot/` | `--include checkpoint` |
| `secret` | `.credentials.json`, `auth.json`, `~/.claude.json`, MCP settings | `--include secret` |
| `cache` | logs, caches, binaries, Zed runtimes | `--include cache` |

`--include all` backs up everything, `--exclude config` leaves a category out.
Unknown files count as `data` (as `cache` for Zed, whose data directory mostly
consists of runtimes). `aibackup list` shows the size of each category.

SQLite databases are snapshotted via the SQLite backup API, which is consistent even
while the agent is running. Tokens in OpenCode's database are removed from the
snapshot unless `secret` is included.

Compression defaults to `--compression fast` (Deflate level 1); `best` produces a
somewhat smaller archive at a much lower speed, `none` stores files uncompressed.

### Encryption

The archive itself is not encrypted. Write it to stdout and pipe it into `age` or `gpg`:

```sh
aibackup backup -o - | age -r age1… > sessions.zip.age
```

For scheduled or service runs, prefer `age -r` with a public key: the machine then
holds nothing that can decrypt the backup. If you need a passphrase, `gpg` can read
it from a file without prompting:

```sh
aibackup backup -o - | gpg --batch --pinentry-mode loopback --passphrase-file /etc/aibackup/pass -c > sessions.zip.gpg
```

Decrypt the archive before running `restore`, `verify` or `inspect`.

### Moving to another operating system

Data directories and the home directory are mapped to the target system
automatically. If projects live elsewhere on the target, specify that with
`--map-path` (longest prefix wins):

```sh
aibackup restore sessions.zip --map-path 'C:\Users\me\source=/home/me/code'
```

Agents only show the sessions of the project that is currently open. If mapped
sessions point to folders that don't exist on the target, `restore` warns and names
the source path to pass to `--map-path`, so check with `--dry-run` first.

When restoring onto another operating system, the `cache` category is skipped
because it holds platform-specific binaries.

Only the fields an agent uses to link sessions to projects are rewritten (e.g.
Claude Code project directories, `cwd` and `projects` in `~/.claude.json`, OpenCode
`session.directory`, Zed `folder_paths`, Zoo/Roo `workspace`). Paths inside the chat history stay unchanged.

An agent running in WSL (e.g. Command Code) can be backed up from Windows by
pointing to the WSL home:

```sh
aibackup backup -a command-code --home '\\wsl$\Ubuntu\home\me' --platform linux --home-path /home/me
```

Restore directly into a WSL home:

```sh
aibackup restore sessions.zip --home '\\wsl$\Ubuntu\home\me' --platform linux --home-path /home/me
```

### Claude Desktop

From Claude Desktop, its list of Code sessions (`claude-code-sessions`), local chats
(`local-agent-mode-sessions`) and the storage of its page (`Local Storage`, `IndexedDB`)
are backed up. The latter records which local session belongs to which sidebar group;
claude.ai does not sync that. These databases are only restored as a whole: if one
already exists on the target, the archived copy is put next to it (`*.aibackup-<time>`)
and the sidebar groups are merged into the existing database. Quit Claude Desktop first.

### OpenCode Desktop

OpenCode Desktop only lists the projects it remembers in its UI state
(`ai.opencode.desktop/drafts.sqlite`), even though the sessions themselves live in
`opencode.db`. That file is backed up; if it already exists on the target, the remembered
projects are merged into it. Quit OpenCode Desktop first.

### Command Code desktop app

The desktop app lists projects, chats and trusted project folders in its state file
(`Command Code/app-state.json`); the sessions live in `~/.commandcode`. The state file is
backed up; if it already exists on the target, missing projects, chats and trusted folders
are merged into it. A project's chats appear once the project is opened in the app. Quit
the app first: a running app rewrites the file on exit, so restore refuses to touch it.

### Archives without the desktop apps' lists

For archives created before this version, `aibackup desktop-index` rebuilds Claude
Desktop's session list from the transcripts in `~/.claude/projects` and adds every
OpenCode or Command Code project with sessions to the app's sidebar (quit the apps
first; `--dry-run` shows what would happen, `-a` limits it to one app). Command Code
folders added this way are not marked as trusted; the app asks when a project is
first opened.

### Conflicts

- Files whose size and modification time match the archive are skipped.
  `--checksum` compares contents instead.
- `history.jsonl` (Claude Code, Codex, Command Code) and `tasks/_index.json` (Zoo/Roo) are merged.
- Other differing files are left untouched and reported as conflicts.
  `--overwrite` replaces them; the original is kept as `*.aibackup-<time>.bak`.
- Databases cannot be merged: if an agent has already been used on the target system,
  `--overwrite` replaces its database. Close the agent first, including servers it
  started in the background; databases that are still open are not replaced.
- `--overwrite` does not replace a file whose project paths would point to folders that
  don't exist on this system (agents hide such sessions; OpenCode even archives the
  projects). Fix the mapping with `--map-path`, or pass `--allow-missing`.

## Development

```sh
python -m unittest discover -s tests      # with PYTHONPATH=src or after `pip install -e .`
```

A new agent is a class in `src/aibackup/adapters/`: data roots per OS, category
rules and optional hooks for rewriting paths and merging files.
See [AGENTS.md](AGENTS.md) for conventions.

## License

[MIT](LICENSE)
