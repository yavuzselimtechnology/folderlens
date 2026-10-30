import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentarm.mcp_server import create_agent
from agentarm.workspace import AgentPolicy, Workspace


class DirectoryListingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "src" / "nested").mkdir(parents=True)
        (self.root / "src" / "nested" / "üç.txt").write_text("third")
        (self.root / "src" / "second.txt").write_text("second")
        (self.root / "first.txt").write_text("first")
        self.workspace = Workspace(AgentPolicy(self.root))

    def test_depth_pages_have_no_omission_or_duplicates(self):
        shallow = self.workspace.list_directory()
        self.assertEqual({x["path"] for x in shallow["entries"]}, {"src", "first.txt"})
        second = self.workspace.list_directory(depth=2)
        self.assertEqual(
            {x["path"] for x in second["entries"]},
            {"src", "first.txt", "src/second.txt", "src/nested"},
        )
        complete = self.workspace.list_directory(depth=3)
        pages, offset = [], 0
        while offset is not None:
            page = self.workspace.list_directory(depth=3, limit=1, offset=offset)
            pages.extend(page["entries"])
            offset = page["next_offset"]
        self.assertEqual(pages, complete["entries"])
        self.assertEqual(len(pages), 5)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(complete["entries"][-1]["depth"], 3)
        self.assertFalse(self.workspace.list_directory(offset=999, depth=3)["entries"])

    def test_nested_secrets_generated_and_private_paths_are_excluded(self):
        (self.root / "src" / ".env").write_text("secret")
        (self.root / "src" / "node_modules").mkdir()
        (self.root / "src" / "node_modules" / "hidden.txt").write_text("generated")
        private = self.root / "private"
        private.mkdir()
        (private / "hidden.txt").write_text("protected")
        workspace = Workspace(AgentPolicy(self.root, protected_paths=(private,)))
        result = workspace.list_directory(depth=100)
        self.assertEqual(len(result["entries"]), 5)
        self.assertFalse(
            any("hidden" in x["path"] or ".env" in x["path"] for x in result["entries"])
        )

    def test_scan_time_and_output_limits_report_incomplete_results(self):
        bounded = Workspace(AgentPolicy(self.root, max_scan_files=2))
        result = bounded.list_directory(depth=3)
        self.assertEqual(result["status"], "scan_limit")
        self.assertEqual(result["scanned"], 2)
        self.assertIsNone(result["next_offset"])
        with patch("agentarm.directory_listing.time.monotonic", side_effect=[0, 3]):
            timed = self.workspace.list_directory(depth=3)
        self.assertEqual(timed["status"], "time_limit")
        self.assertFalse(timed["entries"])
        for i in range(8):
            (self.root / (str(i) + "long_name" * 10 + ".txt")).write_text("x")
        bounded = Workspace(AgentPolicy(self.root, max_output_chars=550))
        offset, paths = 0, []
        while offset is not None:
            result = bounded.list_directory(depth=3, offset=offset)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 550)
            paths.extend(x["path"] for x in result["entries"])
            offset = result["next_offset"]
        self.assertEqual(len(paths), 13)
        self.assertEqual(len(set(paths)), 13)

    def test_unavailable_nested_directory_is_explicit_and_root_errors_propagate(self):
        scandir = os.scandir

        def unavailable(path):
            if Path(path).name == "src":
                raise PermissionError("fixture denied")
            return scandir(path)

        with patch("agentarm.directory_listing.os.scandir", side_effect=unavailable):
            result = self.workspace.list_directory(depth=3)
            errors = [x for x in result["entries"] if x["kind"] == "error"]
            self.assertEqual(
                errors,
                [
                    {
                        "path": "src",
                        "kind": "error",
                        "error": "directory_unavailable",
                        "depth": 1,
                    }
                ],
            )
            with self.assertRaises(PermissionError):
                self.workspace.list_directory("src", depth=2)

    def test_invalid_parameters_and_file_root_are_rejected(self):
        for args in (
            {"depth": 0},
            {"depth": True},
            {"depth": 1.5},
            {"offset": -1},
            {"offset": True},
            {"limit": 501},
            {"limit": False},
            {"path": "first.txt"},
            {"path": "../"},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.workspace.list_directory(**args)

    def test_mcp_schema_and_call_expose_depth(self):
        async def run():
            agent = create_agent(self.workspace.policy)
            tools = await agent.list_tools()
            tool = next(x for x in tools if x.name == "list_directory")
            self.assertEqual(tool.input_schema["properties"]["depth"]["default"], 1)
            result = await agent.call_tool("list_directory", {"depth": 3})
            self.assertFalse(result.is_error)
            payload = json.loads(result.content[0].text)
            self.assertEqual(payload["status"], "completed")
            self.assertIn("src/nested/üç.txt", {x["path"] for x in payload["entries"]})

        asyncio.run(run())
