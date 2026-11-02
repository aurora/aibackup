import json
import os
import tempfile
import unittest
from pathlib import Path

from aibackup.adapters.command_code import find_project_dir, merge_app_state, rebuild_project_list, slugify
from aibackup.archive import ArchiveReader
from aibackup.backup import run_backup
from aibackup.env import Env
from aibackup.restore import Restorer


class QuietLog:
    def info(self, msg):
        pass

    def debug(self, msg):
        pass

    def warn(self, msg):
        pass


def state(projects=(), threads=(), trusted=(), removed=None, deleted=None) -> dict:
    return {
        "appState": {
            "projects": [{"path": p, "registeredAt": 1} for p in projects],
            "threads": [{"id": i, "projectPath": p, "title": i} for i, p in threads],
            "removedProjects": removed or {},
            "deletedThreads": deleted or {},
        },
        "trustedProjectRoots": list(trusted),
        "accountCache": {"user": "someone"},
    }


def write_json(path: Path, obj, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class CommandCodeDesktopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_merge_respects_existing_and_removed_entries(self):
        current = state(
            projects=["/p/a"], threads=[("t1", "/p/a")], trusted=["/p/a"], removed={"/p/gone": 5}, deleted={"t3": 5}
        )
        archived = state(
            projects=["/p/a", "/p/b", "/p/gone"],
            threads=[("t1", "/p/a"), ("t2", "/p/b"), ("t3", "/p/b"), ("t4", "/p/gone")],
            trusted=["/p/b", "/p/gone"],
        )
        self.assertEqual(merge_app_state(current, archived, 9), 3)  # project b, thread t2, trust b
        app = current["appState"]
        self.assertEqual([p["path"] for p in app["projects"]], ["/p/a", "/p/b"])
        self.assertEqual([t["id"] for t in app["threads"]], ["t1", "t2"])
        self.assertEqual(current["trustedProjectRoots"], ["/p/a", "/p/b"])
        self.assertEqual(current["accountCache"], {"user": "someone"})
        self.assertEqual(merge_app_state(current, archived, 10), 0)

    def test_restore_maps_paths_and_merges_into_existing_state(self):
        win, lin = self.tmp / "win", self.tmp / "lin"
        proj = "C:\\Users\\h\\source\\proj"
        write_json(
            win / "AppData/Roaming/Command Code/app-state.json",
            state(projects=[proj], threads=[("t1", proj)], trusted=[proj]),
        )
        write_json(win / "AppData/Roaming/Command Code/Cache/data_0", "cache")
        archive = self.tmp / "b.zip"
        win_env = Env("windows", win, "C:\\Users\\h", {})
        run_backup(win_env, ["command-code"], {"data", "config"}, str(archive), QuietLog())

        live = write_json(lin / ".config/Command Code/app-state.json", state(projects=["/home/h/other"]), 1e9)
        reader = ArchiveReader(archive)
        try:
            env = Env("linux", lin, "/home/h", {})
            report = Restorer(reader, env, dry_run=False, overwrite=False, log=QuietLog()).restore_agent(
                "command-code", [("C:\\Users\\h\\source", "/home/h/code")]
            )
        finally:
            reader.close()
        self.assertEqual(report.errors, [])
        self.assertEqual(report.counts["merged"], 1)
        merged = json.loads(live.read_text(encoding="utf-8"))
        self.assertEqual([p["path"] for p in merged["appState"]["projects"]], ["/home/h/other", "/home/h/code/proj"])
        self.assertEqual(merged["appState"]["threads"][0]["projectPath"], "/home/h/code/proj")
        self.assertEqual(merged["trustedProjectRoots"], ["/home/h/code/proj"])
        self.assertFalse((lin / ".config/Command Code/Cache").exists())

    def test_restore_refuses_while_the_app_runs(self):
        home = self.tmp / "home"
        write_json(home / ".config/Command Code/app-state.json", state(projects=["/x"]))
        archive = self.tmp / "b.zip"
        env = Env("linux", home, "/home/h", {})
        run_backup(env, ["command-code"], {"data"}, str(archive), QuietLog())
        live = write_json(home / ".config/Command Code/app-state.json", state(projects=["/y"]), 1e9)
        (live.parent / "SingletonLock").symlink_to("host-1")
        reader = ArchiveReader(archive)
        try:
            restorer = Restorer(reader, env, dry_run=False, overwrite=True, log=QuietLog())
            report = restorer.restore_agent("command-code", [])
        finally:
            reader.close()
        self.assertEqual(report.counts["refused"], 1)
        self.assertIn("running", report.errors[0])
        self.assertEqual(json.loads(live.read_text())["appState"]["projects"][0]["path"], "/y")

    def test_find_project_dir_by_slug(self):
        target = self.tmp / "src" / "My.Project"
        target.mkdir(parents=True)
        (self.tmp / "src" / "My.Projects").mkdir()
        self.assertEqual(find_project_dir(slugify(str(target)), self.tmp), target)
        self.assertIsNone(find_project_dir(slugify(str(self.tmp / "src" / "gone")), self.tmp))

    def test_rebuild_registers_projects_without_trusting_them(self):
        home = self.tmp / "home"
        (home / "code/new").mkdir(parents=True)
        (home / "code/old").mkdir(parents=True)
        projects = home / ".commandcode/projects"
        header = {"type": "session", "version": 3, "id": "s1", "cwd": str(home / "code/new")}
        write_json(projects / slugify(str(home / "code/new")) / "s1.jsonl", header)
        write_json(projects / slugify(str(home / "code/old")) / "s2.jsonl", {"id": "m1", "sessionId": "s2"})
        write_json(projects / slugify(str(home / "gone")) / "s3.jsonl", {"id": "m2"})
        live = write_json(home / ".config/Command Code/app-state.json", state())
        env = Env("linux", home, str(home), {})

        result = rebuild_project_list(env, dry_run=False)
        self.assertEqual(sorted(result.added), [str(home / "code/new"), str(home / "code/old")])
        self.assertEqual(result.missing, {slugify(str(home / "gone")): 1})
        saved = json.loads(live.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["appState"]["projects"]), 2)
        self.assertEqual(saved["trustedProjectRoots"], [])
        self.assertEqual(rebuild_project_list(env, dry_run=False).added, [])


if __name__ == "__main__":
    unittest.main()
