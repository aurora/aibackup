import json
import unittest

from aibackup.adapters.base import Root, rewrite_jsonl, rules
from aibackup.pathmap import PathMapper


class RewriteJsonlTest(unittest.TestCase):
    mapper = PathMapper("windows", "linux", [("C:\\Users\\h", "/home/h")])

    def run_rewrite(self, *lines: str) -> list[str]:
        out = rewrite_jsonl("\n".join(lines).encode(), (("cwd",),), self.mapper)
        return out.decode().split("\n") if out is not None else None

    def test_single_key_is_replaced_textually(self):
        line = '{"a": 1,  "cwd" : "C:\\\\Users\\\\h\\\\p", "msg":"\\"cwd\\":\\"C:\\\\Users\\\\h\\""}'
        (out,) = self.run_rewrite(line)
        self.assertEqual(json.loads(out)["cwd"], "/home/h/p")
        # untouched parts keep their exact formatting, escaped text is not a key
        self.assertTrue(out.startswith('{"a": 1,  "cwd" : "/home/h/p"'))
        self.assertEqual(json.loads(out)["msg"], json.loads(line)["msg"])

    def test_multiple_keys_only_change_the_key_path(self):
        line = json.dumps({"cwd": "C:\\Users\\h\\p", "input": {"cwd": "C:\\Users\\h\\q"}})
        (out,) = self.run_rewrite(line)
        obj = json.loads(out)
        self.assertEqual(obj["cwd"], "/home/h/p")
        self.assertEqual(obj["input"]["cwd"], "C:\\Users\\h\\q")

    def test_single_nested_key_is_not_touched(self):
        line = json.dumps({"type": "tool", "input": {"cwd": "C:\\Users\\h\\q"}, "x": ["{"]})
        self.assertIsNone(self.run_rewrite(line))

    def test_unchanged_returns_none(self):
        self.assertIsNone(self.run_rewrite('{"cwd":"D:\\\\x"}', '{"type":"summary"}', ""))


class ClassifyTest(unittest.TestCase):
    root = Root(
        "r",
        None,
        rules(("checkpoint", "tasks/*/checkpoints/*"), ("cache", "cache/*", "*.log"), ("config", "settings/*")),
    )

    def test_files(self):
        self.assertEqual(self.root.classify("tasks/1/ui.json"), "data")
        self.assertEqual(self.root.classify("tasks/1/checkpoints/.git/HEAD"), "checkpoint")
        self.assertEqual(self.root.classify("cache/a/b"), "cache")
        self.assertEqual(self.root.classify("x/y.log"), "cache")
        self.assertEqual(self.root.classify("foo.aibackup-20260101-000000.bak"), "cache")

    def test_dir_pruning(self):
        self.assertIsNone(self.root.classify_dir("tasks"))
        self.assertIsNone(self.root.classify_dir("tasks/1"))
        self.assertEqual(self.root.classify_dir("tasks/1/checkpoints"), "checkpoint")
        self.assertEqual(self.root.classify_dir("cache"), "cache")
        # "*.log" could match inside any directory listed after it
        self.assertIsNone(self.root.classify_dir("settings"))


if __name__ == "__main__":
    unittest.main()
