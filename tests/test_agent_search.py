import tempfile
import time
import unittest
from pathlib import Path

from agentarm.workspace import AgentPolicy, Workspace, digest


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root))

    def collect(self, result):
        key = result["search_id"]
        rows = list(result["results"])
        while result["has_more"]:
            result = self.workspace.more_search(key, limit=7)
            rows.extend(result["results"])
        return rows, result

    def test_regex_context_hash_and_filename_glob(self):
        source = self.root / "weather.py"
        source.write_text("before\nTEMP=23\nafter\ntemp=24\n", encoding="utf-8")
        (self.root / "ignore.txt").write_text("TEMP=999")
        rows, state = self.collect(
            self.workspace.start_search(
                r"^temp=\d{2}$",
                pattern_mode="regex",
                file_pattern="*.py",
                context_lines=1,
            )
        )
        self.assertEqual([row["line"] for row in rows], [2, 4])
        self.assertEqual(rows[0]["sha256"], digest(source.read_bytes()))
        self.assertEqual(
            [line["text"] for line in rows[0]["context"]],
            ["before", "TEMP=23", "after"],
        )
        self.assertEqual(state["status"], "completed")
        files, _ = self.collect(
            self.workspace.start_search(
                "*.py", search_type="files", pattern_mode="glob"
            )
        )
        self.assertEqual(files, [{"path": "weather.py"}])
        case, _ = self.collect(
            self.workspace.start_search(
                r"^temp=\d+$", pattern_mode="regex", case_sensitive=True
            )
        )
        self.assertEqual([row["line"] for row in case], [4])

    def test_all_matches_in_one_file_resume_without_overflow(self):
        (self.root / "many.txt").write_text("needle\n" * 350)
        result = self.workspace.start_search("needle")
        rows, state = self.collect(result)
        self.assertEqual([row["line"] for row in rows], list(range(1, 351)))
        self.assertEqual(state["skipped_files_or_match_overflows"], 0)
        self.assertEqual(state["matched"], 350)
        self.assertIsNone(self.workspace.searches[result["search_id"]]["current"])

    def test_early_stop_limits_content_and_files_and_releases_snapshot(self):
        for index in range(5):
            (self.root / f"file{index}.txt").write_text("match\n" * 12)
        first = self.workspace.start_search("match", max_results=3)
        rows, state = self.collect(first)
        self.assertEqual(len(rows), 3)
        self.assertEqual(state["status"], "result_limit")
        self.assertFalse(state["has_more"])
        self.assertIsNone(self.workspace.searches[first["search_id"]]["current"])
        files, state = self.collect(
            self.workspace.start_search("file", search_type="files", max_results=2)
        )
        self.assertEqual(len(files), 2)
        self.assertEqual(state["status"], "result_limit")

    def test_backtracking_timeout_invalid_patterns_and_literal_default(self):
        (self.root / "hostile.txt").write_text("a" * 100_000 + "!")
        started = time.monotonic()
        rows, state = self.collect(
            self.workspace.start_search(r"(a+)+$", pattern_mode="regex")
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(rows, [])
        self.assertEqual(state["status"], "regex_timeout")
        self.assertEqual(state["skipped_files_or_match_overflows"], 1)
        for arguments in (
            {"pattern": "[", "pattern_mode": "regex"},
            {"pattern": "x", "context_lines": 11},
            {"pattern": "x", "max_results": 0},
            {"pattern": "x", "pattern_mode": "glob"},
        ):
            with self.assertRaises(ValueError):
                self.workspace.start_search(**arguments)
        (self.root / "literal.txt").write_text("[abc]")
        rows, _ = self.collect(self.workspace.start_search("[abc]"))
        self.assertEqual([row["path"] for row in rows], ["literal.txt"])

    def test_single_file_snapshot_stays_consistent_between_pages(self):
        source = self.root / "changing.txt"
        source.write_text("old\n" * 120)
        result = self.workspace.start_search("old")
        old_hash = result["results"][0]["sha256"]
        source.write_text("new\n")
        rows, _ = self.collect(result)
        self.assertEqual(len(rows), 120)
        self.assertTrue(all(row["sha256"] == old_hash for row in rows))

    def test_oversized_single_result_reports_error_instead_of_infinite_paging(self):
        (self.root / "large-context.txt").write_text("needle " + "x" * 500)
        workspace = Workspace(AgentPolicy(self.root, max_output_chars=100))
        with self.assertRaisesRegex(ValueError, "output budget"):
            workspace.start_search("needle")


if __name__ == "__main__":
    unittest.main()
