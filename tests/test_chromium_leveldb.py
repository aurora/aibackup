import tempfile
import unittest
from pathlib import Path

from aibackup import chromium_leveldb as ldb
from aibackup.adapters.claude_code import merge_sidebar_state


def new_db(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "000003.log").write_bytes(b"")
    return path


class LevelDbTest(unittest.TestCase):
    def test_put_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = new_db(Path(tmp) / "db")
            big = "x" * 70000  # spans several 32 KiB log blocks
            ldb.put(db, {b"a": b"1", b"big": big.encode()})
            ldb.put(db, {b"a": b"2"})
            state, seq = ldb.load(db)
            self.assertEqual(state, {b"a": b"2", b"big": big.encode()})
            self.assertEqual(seq, 3)

    def test_local_storage_encoding(self):
        for text in ("plain", "Umlaut \u00e4", "emoji \U0001f600"):
            self.assertEqual(ldb.decode_local_storage(ldb.encode_local_storage(text)), text)
        self.assertEqual(ldb.local_storage_key("https://claude.ai", "k"), b"_https://claude.ai\x00\x01k")

    def test_lock_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = new_db(Path(tmp) / "db")
            (db / "LOCK").write_bytes(b"")
            self.assertFalse(ldb.in_use(db))


class SidebarMergeTest(unittest.TestCase):
    def test_existing_groups_and_assignments_win(self):
        src = {"customGroupsByScope": {"acc/org": {
            "groups": [{"id": "g1", "name": "Alpha"}, {"id": "g2", "name": "Beta"}],
            "assignments": {"code:local_a": "g1", "code:local_b": "g2", "code:local_c": "g1"},
        }}}
        dst = {"customGroupsByScope": {"acc/org": {
            "groups": [{"id": "other", "name": "Alpha"}],
            "assignments": {"code:local_c": "other"},
            "order": {},
        }}}
        self.assertEqual(merge_sidebar_state(src, dst), 3)  # group Beta + two assignments
        scope = dst["customGroupsByScope"]["acc/org"]
        self.assertEqual(scope["assignments"], {"code:local_c": "other", "code:local_a": "other", "code:local_b": "g2"})
        self.assertEqual([g["name"] for g in scope["groups"]], ["Alpha", "Beta"])
        self.assertEqual(merge_sidebar_state(src, dst), 0)


if __name__ == "__main__":
    unittest.main()
