import tempfile
import unittest
from pathlib import Path

from agentarm.workspace import AgentPolicy, Workspace


class LineReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root))

    def test_line_paging_tail_endings_and_hash_conflict(self):
        target = self.root / "source.txt"
        target.write_bytes("ilk\r\nikinci\rson\nfinal".encode("utf-16"))
        first = self.workspace.read_lines("source.txt", line_count=2)
        self.assertEqual(first["content"], "ilk\r\nikinci\r")
        self.assertEqual(first["total_lines"], 4)
        self.assertEqual(first["next_offset"], 2)
        tail = self.workspace.read_lines("source.txt", -2)
        self.assertEqual(tail["content"], "son\nfinal")
        self.assertIsNone(tail["next_offset"])
        self.assertEqual(self.workspace.read_lines("source.txt", 100)["content"], "")
        self.assertEqual(self.workspace.read_lines("source.txt", -100)["offset"], 0)
        target.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.workspace.read_lines("source.txt", 2, expected_sha256=first["sha256"])

    def test_budget_empty_file_and_protected_paths(self):
        (self.root / "lines.txt").write_bytes(b"abcd\n" * 4)
        workspace = Workspace(AgentPolicy(self.root, max_output_chars=10))
        page = workspace.read_lines("lines.txt")
        self.assertEqual(page["line_count"], 2)
        self.assertEqual(page["next_offset"], 2)
        (self.root / "empty.txt").write_text("")
        self.assertEqual(self.workspace.read_lines("empty.txt")["total_lines"], 0)
        (self.root / "long.txt").write_text("x" * 11)
        with self.assertRaisesRegex(ValueError, "character windows"):
            workspace.read_lines("long.txt")
        with self.assertRaises(PermissionError):
            self.workspace.read_lines(".env")
        with self.assertRaises(ValueError):
            self.workspace.read_lines("lines.txt", line_count=0)
        with self.assertRaisesRegex(ValueError, "document tools"):
            self.workspace.read_lines("word.docx")


if __name__ == "__main__":
    unittest.main()
