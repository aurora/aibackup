import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aibackup.adapters.zed import rebuild_sidebar
from aibackup.env import Env

SIDEBAR_SQL = """
CREATE TABLE sidebar_threads (
  thread_id BLOB PRIMARY KEY, session_id TEXT, agent_id TEXT, title TEXT NOT NULL, updated_at TEXT NOT NULL,
  created_at TEXT, folder_paths TEXT, folder_paths_order TEXT, archived INTEGER DEFAULT 0,
  main_worktree_paths TEXT, main_worktree_paths_order TEXT, remote_connection TEXT,
  interacted_at TEXT, title_override TEXT
) STRICT
"""


class ZedSidebarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = self.home = Path(self._tmp.name)
        (home / "code/proj").mkdir(parents=True)
        self.env = Env("linux", home, "/home/h", {})
        zed = home / ".local/share/zed"
        (zed / "threads").mkdir(parents=True)
        conn = sqlite3.connect(zed / "threads/threads.db")
        with conn:
            conn.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, summary TEXT, updated_at TEXT, data_type TEXT, data BLOB, "
                "parent_id TEXT, folder_paths TEXT, folder_paths_order TEXT, created_at TEXT)"
            )
            rows = [
                ("t-new", "New", None, "/home/h/code/proj"),
                ("t-win", "Migrated", None, "/home/h/code/proj"),
                ("t-ok", "Fine", None, "/home/h/code/proj"),
                ("t-gone", "Gone", None, "/home/h/gone"),
                ("t-sub", "Subagent", "t-ok", "/home/h/code/proj"),
            ]
            for tid, title, parent, folders in rows:
                conn.execute(
                    "INSERT INTO threads VALUES (?, ?, '2026-01-01', 'zstd', x'00', ?, ?, '0', '2026-01-01')",
                    (tid, title, parent, folders),
                )
        conn.close()
        self.db = zed / "db/0-stable/db.sqlite"
        self.db.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(SIDEBAR_SQL)
            for tid, folders in (("t-win", "C:\\Users\\h\\code\\proj"), ("t-ok", "/home/h/code/proj")):
                conn.execute(
                    "INSERT INTO sidebar_threads (thread_id, session_id, title, updated_at, folder_paths, "
                    "folder_paths_order, main_worktree_paths, main_worktree_paths_order) "
                    "VALUES (randomblob(16), ?, ?, '2026-01-01', ?, '0', ?, '0')",
                    (tid, tid, folders, folders),
                )
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            query = "SELECT session_id, folder_paths, main_worktree_paths, archived FROM sidebar_threads"
            return {r[0]: r[1:] for r in conn.execute(query)}
        finally:
            conn.close()

    def test_adds_missing_threads_and_fixes_foreign_paths(self):
        dry = rebuild_sidebar(self.env, dry_run=True)
        self.assertEqual((dry.added, dry.fixed, dry.listed), (["New"], ["Migrated"], 1))
        self.assertEqual(dry.missing, {"/home/h/gone": 1})
        self.assertEqual(len(self.rows()), 2)

        rebuild_sidebar(self.env, dry_run=False)
        rows = self.rows()
        self.assertEqual(rows["t-new"], ("/home/h/code/proj", "/home/h/code/proj", 0))
        self.assertEqual(rows["t-win"], ("/home/h/code/proj", "/home/h/code/proj", 0))
        self.assertNotIn("t-sub", rows)
        self.assertNotIn("t-gone", rows)
        again = rebuild_sidebar(self.env, dry_run=False)
        self.assertEqual((again.added, again.fixed, again.listed), ([], [], 3))

    def test_refuses_while_zed_runs(self):
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        script = (
            f"import sqlite3,sys; c=sqlite3.connect({str(self.db)!r}); c.execute('SELECT 1').fetchall(); "
            "print(1, flush=True); sys.stdin.read()"
        )
        pipes = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "text": True}
        zed = subprocess.Popen([sys.executable, "-c", script], **pipes)
        try:
            zed.stdout.readline()
            with self.assertRaisesRegex(ValueError, "running"):
                rebuild_sidebar(self.env, dry_run=False)
        finally:
            zed.communicate("")


if __name__ == "__main__":
    unittest.main()
