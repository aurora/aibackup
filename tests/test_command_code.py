import unittest

from aibackup.adapters.command_code import CommandCode, slugify
from aibackup.pathmap import PathMapper


class CommandCodeTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("/mnt/c/Users/h/source/MyProject"), "mnt-c-users-h-source-my-project")
        self.assertEqual(slugify("/home/h/Acme.Web.Api"), "home-h-acme-web-api")
        self.assertEqual(slugify("/home/h/web3app"), "home-h-web3app")
        self.assertEqual(slugify("/home/h/Grüße"), "home-h-gruesse")

    def test_rename_project_dirs(self):
        m = PathMapper("linux", "linux", [("/mnt/c/Users/h/source", "/home/h/code"), ("/home/old", "/home/h")])
        a = CommandCode()
        self.assertEqual(
            a.restore_path("home", "projects/mnt-c-users-h-source-my-project/s.jsonl", m),
            "projects/home-h-code-my-project/s.jsonl",
        )
        self.assertEqual(a.restore_path("home", "projects/home-old-x/s.jsonl", m), "projects/home-h-x/s.jsonl")
        self.assertEqual(a.restore_path("home", "projects/home-older/s.jsonl", m), "projects/home-older/s.jsonl")


if __name__ == "__main__":
    unittest.main()
