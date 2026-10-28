import unittest

from aibackup.archive import safe_rel


class SafeRelTest(unittest.TestCase):
    def test_safe_rel(self):
        self.assertTrue(safe_rel("a/b.json"))
        for bad in ("", "../x", "a/../../x", "/etc/passwd", "C:/x", "a\\b", "a//b", "a/"):
            self.assertFalse(safe_rel(bad), bad)


if __name__ == "__main__":
    unittest.main()
