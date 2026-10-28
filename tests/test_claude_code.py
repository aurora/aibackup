import unittest

from aibackup.adapters.claude_code import ClaudeCode, encode_project_dir
from aibackup.pathmap import PathMapper


class ClaudeProjectDirTest(unittest.TestCase):
    def test_encoding(self):
        self.assertEqual(encode_project_dir("C:\\Users\\h\\my.proj"), "C--Users-h-my-proj")
        self.assertEqual(encode_project_dir("/home/h/x"), "-home-h-x")

    def test_rename_project_dirs(self):
        m = PathMapper("windows", "linux", [("C:\\Users\\h\\source", "/home/h/code"), ("C:\\Users\\h", "/home/h")])
        a = ClaudeCode()
        self.assertEqual(
            a.restore_path("config", "projects/C--Users-h-source-p-q/s.jsonl", m), "projects/-home-h-code-p-q/s.jsonl"
        )
        self.assertEqual(a.restore_path("config", "projects/C--Users-h/s.jsonl", m), "projects/-home-h/s.jsonl")
        # "C--Users-hans" must not match the rule for C:\Users\h
        self.assertEqual(a.restore_path("config", "projects/C--Users-hans/s.jsonl", m), "projects/C--Users-hans/s.jsonl")


if __name__ == "__main__":
    unittest.main()
