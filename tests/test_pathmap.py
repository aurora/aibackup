import unittest

from aibackup.pathmap import PathMapper


class PathMapperTest(unittest.TestCase):
    def win_to_linux(self):
        return PathMapper(
            "windows",
            "linux",
            [("C:\\Users\\h\\source", "/home/h/code"), ("C:\\Users\\h", "/home/h")],
        )

    def test_longest_prefix_wins(self):
        m = self.win_to_linux()
        self.assertEqual(m.map("C:\\Users\\h\\source\\proj"), "/home/h/code/proj")
        self.assertEqual(m.map("C:\\Users\\h\\other"), "/home/h/other")
        self.assertEqual(m.map("C:\\Users\\h"), "/home/h")

    def test_windows_matching_is_case_and_separator_insensitive(self):
        m = self.win_to_linux()
        self.assertEqual(m.map("c:/users/H/Source/Proj"), "/home/h/code/Proj")
        self.assertEqual(m.map("c:\\users\\h\\source\\"), "/home/h/code")

    def test_unmatched_and_partial_component_are_unchanged(self):
        m = self.win_to_linux()
        self.assertEqual(m.map("D:\\work"), "D:\\work")
        self.assertEqual(m.map("C:\\Users\\hans"), "C:\\Users\\hans")

    def test_linux_to_windows(self):
        m = PathMapper("linux", "windows", [("/home/h", "C:\\Users\\h")])
        self.assertEqual(m.map("/home/h/a/b"), "C:\\Users\\h\\a\\b")
        self.assertEqual(m.map("/home/H/a"), "/home/H/a")  # POSIX is case sensitive

    def test_identity_keeps_original_spelling(self):
        m = PathMapper("windows", "windows", [("C:\\Users\\h", "C:\\Users\\h")])
        self.assertTrue(m.is_identity)
        self.assertEqual(m.map("C:/Users/h/x"), "C:/Users/h/x")

    def test_first_added_rule_wins_for_same_prefix(self):
        m = PathMapper("linux", "linux", [("/a", "/b"), ("/a/", "/c")])
        self.assertEqual(m.map("/a/x"), "/b/x")


if __name__ == "__main__":
    unittest.main()
