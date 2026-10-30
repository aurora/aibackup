import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from aibackup.archive import ArchiveReader
from aibackup.backup import run_backup
from aibackup.env import Env
from aibackup.restore import Restorer

WIN_HOME = "C:\\Users\\h"
PROJ = WIN_HOME + "\\source\\proj"
ZOO = "AppData/Roaming/Code/User/globalStorage/zoocodeorganization.zoo-code"


class QuietLog:
    def __init__(self):
        self.warnings = []

    def info(self, msg):
        pass

    def debug(self, msg):
        pass

    def warn(self, msg):
        self.warnings.append(msg)


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    path.write_text(content, encoding="utf-8")
    return path


def jsonl(*objs) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def query(db: Path, sql: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.win = self.tmp / "win"
        self.archive = self.tmp / "backup.zip"
        self.open_conns = []
        self.populate_windows_home()
        self.src_env = Env("windows", self.win, WIN_HOME, {})

    def tearDown(self):
        for c in self.open_conns:
            c.close()
        self._tmp.cleanup()

    def populate_windows_home(self):
        h = self.win
        # Claude Code
        write(
            h / ".claude/projects/C--Users-h-source-proj/s1.jsonl",
            jsonl({"type": "user", "cwd": PROJ, "message": {"content": f"see {PROJ}\\a.py"}}, {"type": "summary"}),
        )
        write(h / ".claude/history.jsonl", jsonl({"display": "hi", "timestamp": 1, "project": PROJ}))
        write(h / ".claude/settings.json", {"theme": "dark"})
        write(h / ".claude/.credentials.json", {"token": "SECRET"})
        write(h / ".claude/file-history/s1/abc@v1", "old file content")
        write(h / ".claude/debug/log.txt", "debug")
        write(h / ".claude.json", {"primaryApiKey": "SECRET"})

        # Codex: database in WAL mode with an open connection and uncheckpointed data
        write(h / ".codex/auth.json", {"token": "SECRET"})
        rollout_rel = "sessions/2026/10/27/rollout-1.jsonl"
        write(h / ".codex" / rollout_rel, jsonl({"type": "session_meta", "payload": {"id": "1", "cwd": PROJ}}))
        conn = sqlite3.connect(h / ".codex/state_5.sqlite")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)")
        conn.execute(
            "INSERT INTO threads VALUES ('1', ?, ?)",
            (PROJ, WIN_HOME + "\\.codex\\" + rollout_rel.replace("/", "\\")),
        )
        conn.commit()
        self.open_conns.append(conn)

        # OpenCode with credentials inside the database
        oc = h / ".local/share/opencode"
        oc.mkdir(parents=True)
        conn = sqlite3.connect(oc / "opencode.db")
        conn.executescript(
            """
            CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT);
            CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);
            CREATE TABLE account (id TEXT, access_token TEXT);
            INSERT INTO project VALUES ('p', 'C:/Users/h/source/proj');
            INSERT INTO session VALUES ('s', 'C:/Users/h/source/proj');
            INSERT INTO account VALUES ('a', 'SECRET');
            """
        )
        conn.commit()
        conn.close()
        write(oc / "auth.json", {"token": "SECRET"})
        write(oc / "bin/rg.exe", "binary")

        # Zed
        zed = h / "AppData/Local/Zed"
        (zed / "threads").mkdir(parents=True)
        conn = sqlite3.connect(zed / "threads/threads.db")
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, folder_paths TEXT, data BLOB)")
        conn.execute("INSERT INTO threads VALUES ('t', ?, x'00')", (PROJ,))
        conn.commit()
        conn.close()
        write(zed / "node/node.exe", "binary")
        write(h / "AppData/Roaming/Zed/settings.json", {"agent": {}})

        # Zoo Code
        zoo = h / ZOO
        entry = {"id": "t1", "ts": 1, "task": "x", "workspace": "c:\\Users\\h\\source\\proj"}
        write(zoo / "tasks/_index.json", {"version": 1, "updatedAt": 1, "entries": [entry]})
        write(zoo / "tasks/t1/history_item.json", entry)
        write(zoo / "tasks/t1/api_conversation_history.json", [{"role": "user", "content": "x"}])
        write(zoo / "tasks/t1/checkpoints/.git/config", "[core]\n\tworktree = c:\\\\Users\\\\h\\\\source\\\\proj\n")
        write(zoo / "settings/custom_modes.yaml", "customModes: []\n")
        write(zoo / "settings/mcp_settings.json", {"mcpServers": {"x": {"env": {"KEY": "SECRET"}}}})

    def backup(self, include=frozenset({"data", "config"}), compression="fast"):
        log = QuietLog()
        agents = ["claude-code", "codex", "opencode", "zed", "zoo"]
        manifest = run_backup(self.src_env, agents, set(include), str(self.archive), log, compression)
        self.assertEqual(manifest["errors"], [])
        return manifest

    def restore(self, env, *, maps=(), overwrite=False, dry_run=False, checksum=False):
        reader = ArchiveReader(self.archive)
        try:
            restorer = Restorer(reader, env, dry_run=dry_run, overwrite=overwrite, checksum=checksum, log=QuietLog())
            return {a: restorer.restore_agent(a, list(maps)) for a in reader.manifest["agents"]}
        finally:
            reader.close()

    # -------------------------------------------------------------------------

    def test_backup_selects_categories_and_keeps_secrets_out(self):
        self.backup()
        with zipfile.ZipFile(self.archive) as z:
            names = set(z.namelist())
            blob = b"".join(z.read(n) for n in names)
        self.assertIn("agents/claude-code/config/projects/C--Users-h-source-proj/s1.jsonl", names)
        self.assertIn("agents/codex/home/state_5.sqlite", names)
        self.assertNotIn("agents/codex/home/state_5.sqlite-wal", names)
        self.assertIn("agents/zed/data/threads/threads.db", names)
        self.assertIn("agents/zed/config/settings.json", names)
        self.assertIn("agents/zoo/code/tasks/t1/history_item.json", names)
        for excluded in ("file-history", "debug", "bin/rg.exe", "node/node.exe", "checkpoints", ".claude.json"):
            self.assertFalse(any(excluded in n for n in names), excluded)
        self.assertNotIn(b"SECRET", blob)

        reader = ArchiveReader(self.archive)
        self.assertEqual(reader.verify(), [])
        reader.close()

    def test_sqlite_snapshot_contains_uncheckpointed_wal_data(self):
        self.assertTrue((self.win / ".codex/state_5.sqlite-wal").stat().st_size > 0)
        self.backup()
        out = self.tmp / "out"
        with zipfile.ZipFile(self.archive) as z:
            z.extract("agents/codex/home/state_5.sqlite", out)
        db = out / "agents/codex/home/state_5.sqlite"
        self.assertEqual(query(db, "SELECT id FROM threads"), [("1",)])
        self.assertEqual(query(db, "PRAGMA journal_mode"), [("delete",)])

    def test_include_all(self):
        self.backup(include={"data", "config", "checkpoint", "secret", "cache"})
        with zipfile.ZipFile(self.archive) as z:
            names = set(z.namelist())
        self.assertIn("agents/claude-code/home/.claude.json", names)
        self.assertIn("agents/zoo/code/tasks/t1/checkpoints/.git/config", names)
        self.assertIn("agents/zed/data/node/node.exe", names)

    def test_migrate_windows_to_linux(self):
        self.backup(include={"data", "config", "checkpoint"})
        lin = self.tmp / "lin"
        env = Env("linux", lin, "/home/h", {})
        reports = self.restore(env, maps=[(WIN_HOME + "\\source", "/home/h/code")])
        for agent, report in reports.items():
            self.assertEqual(report.errors, [], agent)
            self.assertEqual(report.conflicts, [], agent)

        # Claude: directory renamed, cwd rewritten, message content untouched
        session = lin / ".claude/projects/-home-h-code-proj/s1.jsonl"
        first = json.loads(session.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["cwd"], "/home/h/code/proj")
        self.assertIn(PROJ, first["message"]["content"])
        history = json.loads((lin / ".claude/history.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(history["project"], "/home/h/code/proj")

        # Codex: rollout + database (including path into the data root)
        rollout = lin / ".codex/sessions/2026/10/27/rollout-1.jsonl"
        self.assertEqual(json.loads(rollout.read_text(encoding="utf-8"))["payload"]["cwd"], "/home/h/code/proj")
        self.assertEqual(
            query(lin / ".codex/state_5.sqlite", "SELECT cwd, rollout_path FROM threads"),
            [("/home/h/code/proj", "/home/h/.codex/sessions/2026/10/27/rollout-1.jsonl")],
        )

        # OpenCode: paths rewritten, credentials scrubbed
        db = lin / ".local/share/opencode/opencode.db"
        self.assertEqual(query(db, "SELECT worktree FROM project"), [("/home/h/code/proj",)])
        self.assertEqual(query(db, "SELECT directory FROM session"), [("/home/h/code/proj",)])
        self.assertEqual(query(db, "SELECT count(*) FROM account"), [(0,)])

        # Zed: Linux locations
        self.assertEqual(query(lin / ".local/share/zed/threads/threads.db", "SELECT folder_paths FROM threads"), [("/home/h/code/proj",)])
        self.assertTrue((lin / ".config/zed/settings.json").exists())

        # Zoo: VS Code user dir on Linux, workspace + checkpoint worktree rewritten
        zoo = lin / ".config/Code/User/globalStorage/zoocodeorganization.zoo-code"
        self.assertEqual(json.loads((zoo / "tasks/t1/history_item.json").read_text())["workspace"], "/home/h/code/proj")
        self.assertEqual(json.loads((zoo / "tasks/_index.json").read_text())["entries"][0]["workspace"], "/home/h/code/proj")
        self.assertIn("worktree = /home/h/code/proj", (zoo / "tasks/t1/checkpoints/.git/config").read_text())

        # Second run: nothing left to do
        again = self.restore(env, maps=[(WIN_HOME + "\\source", "/home/h/code")])
        for agent, report in again.items():
            self.assertEqual(set(report.counts), {"unchanged"}, agent)

    def test_cache_is_not_restored_across_platforms(self):
        self.backup(include={"data", "config", "cache"})
        lin = self.tmp / "lin"
        reports = self.restore(Env("linux", lin, "/home/h", {}))
        self.assertFalse((lin / ".local/share/zed/node/node.exe").exists())
        self.assertGreater(reports["zed"].counts["skipped (cache)"], 0)
        self.assertTrue((lin / ".local/share/zed/threads/threads.db").exists())

        win2 = self.tmp / "win2"
        self.restore(Env("windows", win2, WIN_HOME, {}))
        self.assertTrue((win2 / "AppData/Local/Zed/node/node.exe").exists())

    def test_reports_missing_project_folders(self):
        self.backup()
        lin = self.tmp / "lin"
        env = Env("linux", lin, "/home/h", {})
        reports = self.restore(env)
        for agent in ("claude-code", "codex", "opencode", "zed", "zoo"):
            self.assertEqual(reports[agent].missing, {"/home/h/source": (WIN_HOME + "\\source", {"proj"})}, agent)

        lin2 = self.tmp / "lin2"
        (lin2 / "code/proj/.git").mkdir(parents=True)
        env2 = Env("linux", lin2, "/home/h", {})
        reports = self.restore(env2, maps=[(WIN_HOME + "\\source", "/home/h/code")])
        for agent, report in reports.items():
            self.assertEqual(report.missing, {}, agent)

    def test_missing_folders_inside_repos(self):
        sessions = self.win / ".claude/projects/C--Users-h-source-proj"
        write(sessions / "s2.jsonl", jsonl({"type": "user", "cwd": PROJ + "\\bin\\Debug"}))
        worktree = PROJ + "\\.claude\\worktrees\\wt"
        write(sessions.with_name("C--Users-h-source-proj--claude-worktrees-wt") / "s3.jsonl", jsonl({"type": "user", "cwd": worktree}))
        write(sessions.with_name("C--Users-h-source-proj--claude-worktrees-ok") / "s4.jsonl", jsonl({"type": "user", "cwd": PROJ + "\\.claude\\worktrees\\ok\\src"}))
        self.backup()
        lin = self.tmp / "lin"
        (lin / "source/proj/.git").mkdir(parents=True)
        (lin / "source/proj/.claude/worktrees/ok").mkdir(parents=True)
        missing = self.restore(Env("linux", lin, "/home/h", {}))["claude-code"].missing
        # Build output inside an existing repo or worktree is noise; a vanished worktree is not.
        self.assertEqual(list(missing), ["/home/h/source/proj/.claude/worktrees/wt"])

    def test_same_platform_restore_is_byte_identical(self):
        self.backup()
        target = self.tmp / "win2"
        env = Env("windows", target, WIN_HOME, {})
        self.restore(env)
        src = self.win / ".claude/projects/C--Users-h-source-proj/s1.jsonl"
        dst = target / ".claude/projects/C--Users-h-source-proj/s1.jsonl"
        self.assertEqual(src.read_bytes(), dst.read_bytes())
        self.assertEqual(int(src.stat().st_mtime), int(dst.stat().st_mtime))

    def test_conflicts_merge_and_overwrite(self):
        self.backup()
        target = self.tmp / "win2"
        env = Env("windows", target, WIN_HOME, {})
        settings = write(target / ".claude/settings.json", {"theme": "light"})
        index = write(
            target / ZOO / "tasks/_index.json",
            {"version": 1, "updatedAt": 5, "entries": [{"id": "t9", "ts": 5, "workspace": "x"}]},
        )

        dry = self.restore(env, dry_run=True)
        self.assertIn(str(settings), dry["claude-code"].conflicts)
        self.assertFalse((target / ".codex").exists())

        reports = self.restore(env)
        self.assertIn(str(settings), reports["claude-code"].conflicts)
        self.assertEqual(json.loads(settings.read_text())["theme"], "light")
        self.assertEqual(reports["zoo"].counts["merged"], 1)
        self.assertEqual({e["id"] for e in json.loads(index.read_text())["entries"]}, {"t1", "t9"})
        self.assertEqual(len(list(index.parent.glob("_index.json.aibackup-*.bak"))), 1)

        self.restore(env, overwrite=True)
        self.assertEqual(json.loads(settings.read_text())["theme"], "dark")
        self.assertEqual(len(list(settings.parent.glob("settings.json.aibackup-*.bak"))), 1)

    def test_compression_modes(self):
        sizes = {}
        for mode, method in (("none", zipfile.ZIP_STORED), ("fast", zipfile.ZIP_DEFLATED)):
            self.backup(compression=mode)
            with zipfile.ZipFile(self.archive) as z:
                self.assertEqual({i.compress_type for i in z.infolist()}, {method}, mode)
            sizes[mode] = self.archive.stat().st_size
        self.assertLess(sizes["fast"], sizes["none"])

    def test_quick_check_and_checksum(self):
        self.backup()
        target = self.tmp / "win2"
        env = Env("windows", target, WIN_HOME, {})
        self.restore(env)
        settings = target / ".claude/settings.json"
        st = settings.stat()

        # Same size and mtime: the quick check trusts it, --checksum doesn't.
        settings.write_text(settings.read_text().replace("dark", "DARK"))
        os.utime(settings, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.restore(env)["claude-code"].conflicts, [])
        self.assertIn(str(settings), self.restore(env, checksum=True)["claude-code"].conflicts)

        # A newer mtime is always detected.
        os.utime(settings, (st.st_mtime + 10, st.st_mtime + 10))
        self.assertIn(str(settings), self.restore(env)["claude-code"].conflicts)

    def test_command_code_wsl_to_linux(self):
        wsl = self.tmp / "wsl"
        cc = wsl / ".commandcode"
        proj = "/mnt/c/Users/h/source/MyProject"
        session = cc / "projects/mnt-c-users-h-source-my-project/s1.jsonl"
        write(
            session,
            jsonl(
                {"type": "session", "id": "s1", "cwd": proj},
                {"role": "assistant", "content": [{"type": "tool", "input": {"cwd": proj}}]},
            ),
        )
        write(session.with_name("s1.meta.json"), {"title": "t"})
        write(session.with_name("s1.checkpoints.jsonl"), jsonl({"type": "snapshot"}))
        write(cc / "history.jsonl", jsonl({"p": "hi", "t": "1"}))
        write(cc / "config.json", {"model": "x"})
        write(cc / "auth.json", {"token": "SECRET"})
        write(cc / "file-history/s1/abc@v1", "old")
        write(cc / "cache/model-catalog/v1/c.json", {})

        run_backup(Env("linux", wsl, "/home/h", {}), ["command-code"], {"data", "config"}, str(self.archive), QuietLog())
        with zipfile.ZipFile(self.archive) as z:
            names = {n.split("/", 3)[3] for n in z.namelist() if n.startswith("agents/")}
        self.assertEqual(
            names,
            {
                "projects/mnt-c-users-h-source-my-project/s1.jsonl",
                "projects/mnt-c-users-h-source-my-project/s1.meta.json",
                "projects/mnt-c-users-h-source-my-project/s1.checkpoints.jsonl",
                "history.jsonl",
                "config.json",
            },
        )

        lin = self.tmp / "lin"
        reports = self.restore(Env("linux", lin, "/home/user", {}), maps=[("/mnt/c/Users/h/source", "/home/user/code")])
        self.assertEqual(reports["command-code"].errors, [])
        restored = lin / ".commandcode/projects/home-user-code-my-project/s1.jsonl"
        header, message = [json.loads(x) for x in restored.read_text().splitlines()]
        self.assertEqual(header["cwd"], "/home/user/code/MyProject")
        self.assertEqual(message["content"][0]["input"]["cwd"], proj)
        self.assertTrue(restored.with_name("s1.meta.json").exists())

    def test_overwrite_sqlite_moves_stale_wal_away(self):
        self.backup()
        target = self.tmp / "win2"
        env = Env("windows", target, WIN_HOME, {})
        db = write(target / ".codex/state_5.sqlite", "not a db")
        wal = write(target / ".codex/state_5.sqlite-wal", "stale")
        self.restore(env, overwrite=True)
        self.assertFalse(wal.exists())
        self.assertEqual(query(db, "SELECT id FROM threads"), [("1",)])


if __name__ == "__main__":
    unittest.main()
