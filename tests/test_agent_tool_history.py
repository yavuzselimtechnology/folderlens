import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from agentarm.mcp_server import create_agent
from agentarm.oauth import EXEC_SCOPE, READ_SCOPE, WRITE_SCOPE
from agentarm.tool_history import (
    CAPTURE_CHARS,
    MAX_RECORD_BYTES,
    RETENTION_SECONDS,
    ToolHistory,
    capture,
    parse_since,
)
from agentarm.workspace import AgentPolicy
from folderlens import client_config, main, parser


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "history"
        self.store = ToolHistory(self.directory, "content")
        self.addCleanup(self.store.close)

    def append(
        self,
        index=0,
        scopes=None,
        timestamp=None,
        tool="read_file",
        success=True,
    ):
        self.store.append(
            {
                "tool": tool,
                "success": success,
                "duration_ms": 1,
                "time": time.time() if timestamp is None else timestamp,
            },
            scopes or [READ_SCOPE],
            {"index": index},
            {"value": index},
        )

    def test_since_tool_success_filters_compose_with_pagination(self):
        baseline = time.time() - 10
        self.append(1, timestamp=baseline, tool="write_file", success=True)
        self.append(2, timestamp=baseline + 1, tool="read_file", success=False)
        self.append(3, timestamp=baseline + 2, tool="read_file", success=True)
        self.append(4, timestamp=baseline + 3, tool="read_file", success=False)
        latest = self.store.recent(
            1,
            {READ_SCOPE},
            64000,
            since=baseline + 0.5,
            tool_name="read_file",
            success=False,
        )
        self.assertEqual(
            json.loads(latest["calls"][0]["result"]["json_prefix"])["value"], 4
        )
        self.assertTrue(latest["has_more"])
        older = self.store.recent(
            20,
            {READ_SCOPE},
            64000,
            before_id=latest["next_before_id"],
            since=baseline + 0.5,
            tool_name="read_file",
            success=False,
        )
        self.assertEqual(
            json.loads(older["calls"][0]["result"]["json_prefix"])["value"], 2
        )
        self.assertFalse(older["has_more"])

    def test_history_filter_validation_rejects_ambiguous_values(self):
        self.assertEqual(parse_since("2026-11-01T12:30:00Z"), 1793536200.0)
        self.assertEqual(parse_since("2026-11-01T15:30:00+03:00"), 1793536200.0)
        for value in ("2026-11-01T12:30:00", "not-a-date", ""):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "timezone-aware"),
            ):
                parse_since(value)
        with self.assertRaisesRegex(ValueError, "tool_name"):
            self.store.recent(20, {READ_SCOPE}, 64000, tool_name="")
        with self.assertRaisesRegex(ValueError, "success"):
            self.store.recent(20, {READ_SCOPE}, 64000, success=1)

    def test_restart_recovers_content_in_order_and_supports_older_pages(self):
        for index in range(5):
            self.append(index)
        self.store.close()
        self.store = ToolHistory(self.directory, "content")
        self.addCleanup(self.store.close)
        latest = self.store.recent(2, {READ_SCOPE}, 64000)
        self.assertEqual(
            [
                json.loads(row["result"]["json_prefix"])["value"]
                for row in latest["calls"]
            ],
            [3, 4],
        )
        self.assertTrue(latest["has_more"])
        older = self.store.recent(20, {READ_SCOPE}, 64000, latest["next_before_id"])
        self.assertEqual(
            [
                json.loads(row["result"]["json_prefix"])["value"]
                for row in older["calls"]
            ],
            [0, 1, 2],
        )
        self.assertFalse(older["has_more"])

    def test_scope_filter_does_not_expose_args_or_outputs_from_privileged_calls(self):
        self.append(1)
        self.append(2, [READ_SCOPE, WRITE_SCOPE])
        self.append(3, [READ_SCOPE, EXEC_SCOPE])
        read = self.store.recent(20, {READ_SCOPE}, 64000)
        self.assertEqual(len(read["calls"]), 1)
        full = self.store.recent(20, {READ_SCOPE, WRITE_SCOPE, EXEC_SCOPE}, 64000)
        self.assertEqual(len(full["calls"]), 3)

    def test_metadata_mode_hides_previously_captured_content(self):
        self.append()
        self.store.close()
        self.store = ToolHistory(self.directory, "metadata")
        self.addCleanup(self.store.close)
        self.append(1)
        rows = self.store.recent(20, {READ_SCOPE}, 64000)["calls"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("arguments", row)
            self.assertNotIn("result", row)

    def test_record_count_byte_budget_ttl_and_monotonic_ids(self):
        with patch("agentarm.tool_history.MAX_RECORDS", 3):
            for index in range(5):
                self.append(index)
        rows = self.store.recent(20, {READ_SCOPE}, 64000)["calls"]
        self.assertEqual(len(rows), 3)
        highest = rows[-1]["id"]
        with patch("agentarm.tool_history.MAX_STORED_BYTES", 1):
            self.append()
        self.assertEqual(self.store.recent(20, {READ_SCOPE}, 64000)["calls"], [])
        self.append(timestamp=time.time() - RETENTION_SECONDS - 1)
        self.assertEqual(self.store.recent(20, {READ_SCOPE}, 64000)["calls"], [])
        self.append()
        self.assertGreater(
            self.store.recent(20, {READ_SCOPE}, 64000)["calls"][0]["id"], highest
        )
        pages = self.store.connection.execute("PRAGMA max_page_count").fetchone()[0]
        self.assertEqual(pages, 4096)

    def test_capture_and_response_budgets_and_native_media_exclusion(self):
        rich = CallToolResult(
            content=[
                TextContent(type="text", text="visible"),
                ImageContent(
                    type="image", data="private-native-image", mimeType="image/png"
                ),
            ]
        )
        value = capture(rich)
        self.assertNotIn("private-native-image", value["json_prefix"])
        self.assertIn("payload_omitted", value["json_prefix"])
        clipped = capture({"long": '"\\' * CAPTURE_CHARS})
        self.assertTrue(clipped["truncated"])
        self.assertLessEqual(len(clipped["json_prefix"]), CAPTURE_CHARS)
        metadata = {"tool": "read_file", "success": True, "time": time.time()}
        self.store.append(
            metadata, [READ_SCOPE], '"\\' * CAPTURE_CHARS, '"\\' * CAPTURE_CHARS
        )
        size = self.store.connection.execute("SELECT size FROM calls").fetchone()[0]
        self.assertLessEqual(size, MAX_RECORD_BYTES)
        for index in range(4):
            self.append(index)
        page = self.store.recent(20, {READ_SCOPE}, 1200)
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=True)), 1200)
        self.assertTrue(page["has_more"])
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.store.recent(20, {READ_SCOPE}, 20)

    def test_unrelated_directory_and_unsigned_database_are_rejected(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        owned = other / "owner.txt"
        owned.write_text("unchanged")
        with self.assertRaisesRegex(ValueError, "dedicated"):
            ToolHistory(other, "content")
        self.assertEqual(owned.read_text(), "unchanged")
        empty_db_dir = Path(self.temp.name) / "foreign"
        empty_db_dir.mkdir()
        foreign = sqlite3.connect(empty_db_dir / "history.sqlite3")
        foreign.execute("CREATE TABLE owner_data(value)")
        foreign.commit()
        foreign.close()
        before = (empty_db_dir / "history.sqlite3").read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            ToolHistory(empty_db_dir, "metadata")
        self.assertEqual((empty_db_dir / "history.sqlite3").read_bytes(), before)

    def test_cli_configuration_preserves_opt_in_without_loading_dependencies(self):
        args = parser().parse_args(
            ["--root", self.temp.name, "--history", "content", "--print-config", "json"]
        )
        config = json.loads(client_config(args))
        self.assertIn("--history", config["mcpServers"]["folderlens"]["args"])
        self.assertEqual(parser().parse_args([]).history, "memory")
        process = subprocess.run(
            [
                sys.executable,
                "-S",
                str(Path(__file__).resolve().parents[1] / "folderlens.py"),
                "--root",
                self.temp.name,
                "--history",
                "content",
                "--print-config",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn(
            "content", json.loads(process.stdout)["mcpServers"]["folderlens"]["args"]
        )
        self.assertFalse((Path(self.temp.name) / ".folderlens").exists())

    def test_cli_doctor_checks_persistent_storage_and_closes_it(self):
        output = io.StringIO()
        with (
            patch("folderlens.__file__", str(Path(self.temp.name) / "folderlens.py")),
            redirect_stdout(output),
        ):
            status = main(
                ["--root", self.temp.name, "--history", "metadata", "--doctor"]
            )
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        databases = list(
            (Path(self.temp.name) / ".folderlens/history").rglob("history.sqlite3")
        )
        self.assertEqual(len(databases), 1)
        # An exclusive transaction proves the CLI did not retain a transaction lock.
        connection = sqlite3.connect(databases[0], timeout=0)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()
        finally:
            connection.close()


class HistoryMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ToolHistory(self.root / "private-history", "content")
        self.addCleanup(self.store.close)
        self.server = create_agent(
            AgentPolicy(self.root, allow_write=True), history_store=self.store
        )

    async def call(self, name, args):
        result = await self.server.call_tool(name, args)
        return result.structured_content or json.loads(result.content[0].text)

    async def test_real_tool_result_is_persisted_and_history_does_not_record_itself(
        self,
    ):
        await self.call(
            "write_file",
            {
                "path": "file.txt",
                "content": "retained content",
                "base_sha256": None,
                "preview": False,
            },
        )
        page = await self.call("get_recent_tool_calls", {})
        self.assertEqual(len(page["calls"]), 1)
        self.assertIn("retained content", page["calls"][0]["arguments"]["json_prefix"])
        again = await self.call("get_recent_tool_calls", {})
        self.assertEqual(page, again)
        await self.call("set_config_value", {"key": "allow_write", "value": False})
        page = await self.call("get_recent_tool_calls", {})
        self.assertEqual(page["calls"], [])

    async def test_history_failure_does_not_make_a_completed_write_look_failed(self):
        with patch.object(
            self.store, "append", side_effect=sqlite3.OperationalError("disk full")
        ):
            changed = await self.call(
                "write_file",
                {
                    "path": "file.txt",
                    "content": "one write",
                    "base_sha256": None,
                    "preview": False,
                },
            )
        self.assertEqual((self.root / "file.txt").read_text(), "one write")
        self.assertIn("undo_id", changed)
        stats = await self.call("get_usage_stats", {})
        self.assertEqual(stats["history_last_error"], "OperationalError")

    async def test_private_history_cannot_be_read_through_file_tools(self):
        from mcp.server.mcpserver.exceptions import ToolError

        with self.assertRaises(ToolError):
            await self.call("read_file", {"path": "private-history/history.sqlite3"})

    async def test_mcp_history_respects_current_oauth_scope(self):
        await self.call(
            "write_file",
            {
                "path": "file.txt",
                "content": "write-scope-only",
                "base_sha256": None,
                "preview": False,
            },
        )
        await self.call("read_file", {"path": "file.txt"})
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=SimpleNamespace(scopes=[READ_SCOPE]),
        ):
            page = await self.call("get_recent_tool_calls", {})
        self.assertEqual([row["tool"] for row in page["calls"]], ["read_file"])

    async def test_default_history_stays_in_memory_and_omits_content(self):
        server = create_agent(AgentPolicy(self.root))
        (self.root / "file.txt").write_text("do not retain content")
        await server.call_tool("read_file", {"path": "file.txt"})
        response = await server.call_tool("get_recent_tool_calls", {})
        self.assertNotIn("do not retain content", str(response))
        self.assertNotIn("arguments", str(response))
        self.assertFalse((self.root / ".folderlens").exists())

    async def test_mcp_filters_persistent_and_memory_history(self):
        tools = {item.name: item for item in await self.server.list_tools()}
        properties = tools["get_recent_tool_calls"].input_schema["properties"]
        self.assertEqual(
            {"limit", "before_id", "since", "tool_name", "success"},
            set(properties),
        )
        self.assertIn({"type": "boolean"}, properties["success"]["anyOf"])
        before = datetime.now(timezone.utc).isoformat()
        with self.assertRaises(ToolError):
            await self.server.call_tool("read_file", {"path": "missing.txt"})
        await self.call(
            "write_file",
            {
                "path": "file.txt",
                "content": "history filters",
                "base_sha256": None,
                "preview": False,
            },
        )
        await self.call("read_file", {"path": "file.txt"})
        failures = await self.call(
            "get_recent_tool_calls",
            {"since": before, "tool_name": "read_file", "success": False},
        )
        self.assertEqual([row["success"] for row in failures["calls"]], [False])
        reads = await self.call(
            "get_recent_tool_calls",
            {"since": before, "tool_name": "read_file", "success": True},
        )
        self.assertEqual([row["tool"] for row in reads["calls"]], ["read_file"])

        memory = create_agent(AgentPolicy(self.root))
        with self.assertRaises(ToolError):
            await memory.call_tool("read_file", {"path": "missing-memory.txt"})
        page = await memory.call_tool(
            "get_recent_tool_calls",
            {"since": before, "tool_name": "read_file", "success": False},
        )
        value = page.structured_content or json.loads(page.content[0].text)
        self.assertEqual([row["success"] for row in value["calls"]], [False])

    async def test_mcp_rejects_naive_since_and_non_boolean_success(self):
        for arguments in (
            {"since": "2026-11-01T12:30:00"},
            {"tool_name": ""},
            {"success": 1},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                await self.server.call_tool("get_recent_tool_calls", arguments)


if __name__ == "__main__":
    unittest.main()
