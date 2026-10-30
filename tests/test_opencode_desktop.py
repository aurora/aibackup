import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aibackup.adapters.opencode import desktop_running, merge_desktop_state, rebuild_project_list
from aibackup.archive import ArchiveReader
from aibackup.backup import run_backup
from aibackup.env import Env
from aibackup.restore import Restorer

STATE_SQL = (
    "CREATE TABLE state (name TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
    "updated_at INTEGER NOT NULL, PRIMARY KEY (name, key))"
)
GLOBAL = "opencode.global.dat"


class QuietLog:
    def info(self, msg):
        pass

    def debug(self, msg):
        pass

    def warn(self, msg):
        pass


def make_state(path: Path, entries: dict[tuple[str, str], object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(STATE_SQL)
        for (name, key), value in entries.items():
            conn.execute("INSERT INTO state VALUES (?, ?, ?, 1)", (name, key, json.dumps(value)))
    conn.close()
    return path


def read_state(path: Path) -> dict[tuple[str, str], object]:
    conn = sqlite3.connect(path)
    try:
        return {(n, k): json.loads(v) for n, k, v in conn.execute("SELECT name, key, value FROM state")}
    finally:
        conn.close()


def server(used: list[str]) -> dict:
    return {"list": [], "hidden": {}, "lastProjectID": {"local": used[0]}, "usedProjectIDs": {"local": used}}


class OpenCodeDesktopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_merge_keeps_existing_entries_and_appends_projects(self):
        archived = make_state(
            self.tmp / "a.sqlite",
            {
                ("opencode.global.dat", "server"): server(["p1", "p2"]),
                ("opencode.global.dat", "layout"): {"width": 300},
                ("opencode.window.w1.dat", "tabs"): [{"directory": "/x"}],
            },
        )
        live = make_state(
            self.tmp / "l.sqlite",
            {(GLOBAL, "server"): server(["default", "p2"]), (GLOBAL, "layout"): {"width": 1}},
        )
        self.assertEqual(merge_desktop_state(live, archived, 5), 1)
        state = read_state(live)
        self.assertEqual(state[("opencode.global.dat", "server")]["usedProjectIDs"]["local"], ["default", "p2", "p1"])
        self.assertEqual(state[("opencode.global.dat", "layout")], {"width": 1})
        self.assertNotIn(("opencode.window.w1.dat", "tabs"), state)
        self.assertEqual(merge_desktop_state(live, archived, 6), 0)

    def test_restore_windows_to_linux_merges_sidebar_and_maps_paths(self):
        win, lin = self.tmp / "win", self.tmp / "lin"
        make_state(
            win / "AppData/Roaming/ai.opencode.desktop/drafts.sqlite",
            {
                ("opencode.global.dat", "server"): server(["p1"]),
                ("opencode.global.dat", "recent"): ["C:\\Users\\h\\source\\proj"],
            },
        )
        (win / "AppData/Roaming/ai.opencode.desktop/Cache").mkdir()
        (win / "AppData/Roaming/ai.opencode.desktop/Cache/data_0").write_text("cache")
        archive = self.tmp / "b.zip"
        win_env = Env("windows", win, "C:\\Users\\h", {})
        run_backup(win_env, ["opencode"], {"data", "config"}, str(archive), QuietLog())

        live = make_state(lin / ".config/ai.opencode.desktop/drafts.sqlite", {(GLOBAL, "server"): server(["d"])})
        reader = ArchiveReader(archive)
        try:
            lin_env = Env("linux", lin, "/home/h", {})
            restorer = Restorer(reader, lin_env, dry_run=False, overwrite=False, log=QuietLog())
            report = restorer.restore_agent("opencode", [("C:\\Users\\h\\source", "/home/h/code")])
        finally:
            reader.close()
        self.assertEqual(report.errors, [])
        self.assertEqual(report.counts["merged"], 1)
        state = read_state(live)
        self.assertEqual(state[("opencode.global.dat", "server")]["usedProjectIDs"]["local"], ["d", "p1"])
        self.assertEqual(state[("opencode.global.dat", "recent")], ["/home/h/code/proj"])
        self.assertFalse((lin / ".config/ai.opencode.desktop/Cache").exists())

    def test_merge_refuses_while_desktop_runs(self):
        desktop = self.tmp / "desktop"
        desktop.mkdir()
        self.assertFalse(desktop_running(desktop))
        (desktop / "SingletonLock").symlink_to("host-123")
        self.assertTrue(desktop_running(desktop))

    def test_rebuild_adds_projects_with_sessions_and_existing_folders(self):
        home = self.tmp / "home"
        (home / "code/a").mkdir(parents=True)
        (home / "code/b").mkdir(parents=True)
        db = home / ".local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        with conn:
            conn.executescript(
                """
                CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, time_archived INTEGER);
                CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, time_updated INTEGER);
                INSERT INTO project VALUES ('a', '/home/h/code/a', NULL), ('b', '/home/h/code/b', NULL),
                    ('gone', '/home/h/gone', NULL), ('old', '/home/h/code/b', 1), ('empty', '/home/h/code/a', NULL),
                    ('global', '/', NULL);
                INSERT INTO session VALUES ('s1', 'a', 1), ('s2', 'b', 3), ('s3', 'gone', 2), ('s4', 'old', 4),
                    ('s5', 'global', 5);
                """
            )
        conn.close()
        state = make_state(home / ".config/ai.opencode.desktop/drafts.sqlite", {(GLOBAL, "server"): server(["a"])})
        env = Env("linux", home, "/home/h", {})

        dry = rebuild_project_list(env, dry_run=True)
        self.assertEqual((dry.added, dry.listed, dry.missing), (["/home/h/code/b"], 1, {"/home/h/gone": 1}))
        self.assertEqual(read_state(state)[("opencode.global.dat", "server")]["usedProjectIDs"]["local"], ["a"])

        rebuild_project_list(env, dry_run=False)
        self.assertEqual(read_state(state)[("opencode.global.dat", "server")]["usedProjectIDs"]["local"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
