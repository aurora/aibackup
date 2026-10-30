import json
import os
import tempfile
import unittest
from pathlib import Path

from aibackup.desktop_index import rebuild
from aibackup.env import Env


def jsonl(*objs) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


class DesktopIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.env = Env("linux", self.home, "/home/h", {})
        (self.home / "code/proj").mkdir(parents=True)
        projects = self.home / ".claude/projects"
        (projects / "-home-h-code-proj").mkdir(parents=True)
        (projects / "-home-h-gone").mkdir(parents=True)
        (projects / "-home-h-code-proj/s1.jsonl").write_text(
            jsonl(
                {"type": "user", "cwd": "/home/h/code/proj", "timestamp": "2026-10-01T10:00:00Z",
                 "message": {"content": "first   prompt\nhere"}},
                {"type": "custom-title", "customTitle": "My session"},
            )
        )
        (projects / "-home-h-code-proj/s2.jsonl").write_text(
            jsonl({"type": "user", "cwd": "/home/h/code/proj", "message": {"content": "only a prompt"}})
        )
        (projects / "-home-h-gone/s3.jsonl").write_text(jsonl({"type": "user", "cwd": "/home/h/gone"}))
        desktop = self.home / ".config/Claude"
        desktop.mkdir(parents=True)
        (desktop / "config.json").write_text(json.dumps({"lastKnownAccountUuid": "acct"}))
        self.storage = desktop / "claude-code-sessions/acct/org"
        self.storage.mkdir(parents=True)
        (self.storage / "local_s2.json").write_text("{}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        result = rebuild(self.env, dry_run=True)
        self.assertEqual(result.created, [("/home/h/code/proj", "My session")])
        self.assertFalse((self.storage / "local_s1.json").exists())

    def test_creates_records_like_desktops_import(self):
        result = rebuild(self.env, dry_run=False)
        self.assertEqual(result.existing, 1)
        self.assertEqual(result.missing, {"/home/h/gone": 1})
        record = json.loads((self.storage / "local_s1.json").read_text())
        self.assertEqual(record["sessionId"], "local_s1")
        self.assertEqual(record["cliSessionId"], "s1")
        self.assertEqual(record["cwd"], "/home/h/code/proj")
        self.assertEqual(record["title"], "My session")
        self.assertEqual(record["createdAt"], 1790848800000)
        self.assertEqual((self.storage / "local_s2.json").read_text(), "{}")
        self.assertEqual(rebuild(self.env, dry_run=False).created, [])

    def test_requires_a_known_account(self):
        os.remove(self.home / ".config/Claude/config.json")
        with self.assertRaisesRegex(ValueError, "sign in"):
            rebuild(self.env, dry_run=True)


if __name__ == "__main__":
    unittest.main()
