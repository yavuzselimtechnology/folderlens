from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tomllib

from agentarm.git_tools import inspect_git
from agentarm.mcp_server import create_agent
from agentarm.processes import Processes
from agentarm.workspace import AgentPolicy, Workspace, digest
from folderlens import client_config, http_app, parser


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))

    def test_read_pagination_exact_hash_and_conflict(self):
        data = "Türkçe\r\nsecond line\n".encode()
        (self.root / "readme.txt").write_bytes(data)
        first = self.workspace.read_file("readme.txt", length=4)
        second = self.workspace.read_file("readme.txt", first["next_offset"])
        self.assertEqual(first["content"] + second["content"], data.decode())
        self.assertEqual(first["sha256"], digest(data))
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.workspace.write_file("readme.txt", "changed", None)
        self.assertEqual((self.root / "readme.txt").read_bytes(), data)
        preview = self.workspace.edit_block(
            "readme.txt", "second", "last", digest(data)
        )
        self.assertIn("last", preview["diff"])
        self.assertEqual((self.root / "readme.txt").read_bytes(), data)
        result = self.workspace.edit_block(
            "readme.txt", "second", "last", digest(data), preview=False
        )
        self.workspace.undo_change(result["undo_id"])
        self.assertEqual((self.root / "readme.txt").read_bytes(), data)

    def test_encoding_and_match_count(self):
        data = "a Türkçe\r\na".encode("utf-16")
        (self.root / "utf.txt").write_bytes(data)
        with self.assertRaisesRegex(ValueError, "count"):
            self.workspace.edit_block("utf.txt", "a", "b", digest(data))
        self.workspace.edit_block("utf.txt", "a", "b", digest(data), 2, False)
        self.assertEqual(
            (self.root / "utf.txt").read_bytes().decode("utf-16"), "b Türkçe\r\nb"
        )

    def test_write_undo_delete_and_external_change(self):
        result = self.workspace.write_file("new.txt", "new", None)
        (self.root / "new.txt").write_text("external")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.workspace.undo_change(result["undo_id"])
        removed = self.workspace.delete_file("new.txt", digest(b"external"))
        self.assertFalse((self.root / "new.txt").exists())
        self.workspace.undo_change(removed["undo_id"])
        self.assertEqual((self.root / "new.txt").read_bytes(), b"external")

    def test_create_copy_move_and_limits(self):
        self.workspace.create_directory("nested/dir")
        result = self.workspace.write_file("a.txt", "a", None)
        self.workspace.transfer_file("a.txt", "nested/dir/b.txt", result["sha256"])
        moved = self.workspace.transfer_file(
            "a.txt", "c.txt", result["sha256"], move=True
        )
        self.assertFalse((self.root / "a.txt").exists())
        self.workspace.undo_change(moved["source_undo_id"])
        self.workspace.undo_change(moved["undo_id"])
        self.assertTrue((self.root / "a.txt").exists())
        self.assertFalse((self.root / "c.txt").exists())
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.workspace.transfer_file("a.txt", "nested/dir/b.txt", result["sha256"])
        bounded = Workspace(
            AgentPolicy(self.root, allow_write=True, max_undo_records=1)
        )
        one = bounded.write_file("one.txt", "1", None)
        bounded.write_file("two.txt", "2", None)
        self.assertNotIn(one["undo_id"], bounded.undo)

    def test_readonly_traversal_secret_generated_and_links(self):
        readonly = Workspace(AgentPolicy(self.root))
        with self.assertRaises(PermissionError):
            readonly.write_file("new.txt", "bad", None)
        for path in [
            "../outside.txt",
            "/absolute",
            "a/../b",
            "a\\b",
            "CON.txt",
            ".env",
            "secrets.json",
            "nested/key.pem",
            "node_modules/a.js",
            "node_modules",
            ".git/config",
            ".folderlens/state",
            "a.txt:stream",
        ]:
            with (
                self.subTest(path=path),
                self.assertRaises((PermissionError, ValueError)),
            ):
                self.workspace.path(path)
        (self.root / "target.txt").write_text("private")
        try:
            (self.root / "link.txt").symlink_to(self.root / "target.txt")
        except OSError:
            # Windows junctions do not require developer mode/admin permission.
            if os.name == "nt":
                junction = self.root / "junction"
                import _winapi

                _winapi.CreateJunction(str(self.root), str(junction))
                self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
                with self.assertRaises(PermissionError):
                    self.workspace.path("junction/target.txt")
        else:
            with self.assertRaises(PermissionError):
                self.workspace.path("link.txt")

    def test_search_pages_exclusions_limits_and_expiry(self):
        for i in range(80):
            (self.root / f"file{i}.txt").write_text(f"needle {i}\n", encoding="utf-8")
        (self.root / ".env").write_text("needle secret")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/private.js").write_text("needle generated")
        search = self.workspace.start_search("NEEDLE")
        results = list(search["results"])
        while search["has_more"]:
            search = {
                **search,
                **self.workspace.more_search(search["search_id"], limit=7),
            }
            results.extend(search["results"])
        self.assertEqual(len(results), 80)
        self.assertEqual(search["status"], "completed")
        self.assertTrue(all(item["line"] == 1 for item in results))
        key = search["search_id"]
        self.workspace.searches[key]["touched"] -= 301
        self.workspace.cleanup_searches()
        self.assertNotIn(key, self.workspace.searches)
        bounded = Workspace(AgentPolicy(self.root, max_scan_files=2))
        limited = bounded.start_search("does not exist")
        self.assertEqual(limited["status"], "scan_limit")
        self.assertEqual(limited["scanned"], 2)

    def test_directory_pages_and_file_budget(self):
        for i in range(6):
            (self.root / f"{i}.txt").write_text("a")
        self.assertEqual(len(self.workspace.list_directory(limit=2)["entries"]), 2)
        self.assertEqual(
            self.workspace.list_directory(offset=4, limit=2)["next_offset"], None
        )
        bounded = Workspace(AgentPolicy(self.root, max_file_bytes=1))
        (self.root / "big.txt").write_text("ab")
        with self.assertRaisesRegex(ValueError, "budget"):
            bounded.read_file("big.txt")

    def test_office_pdf_roundtrip_and_cell_clear(self):
        result = self.workspace.write_workbook(
            "book.xlsx", "Data", "B2", [["Türkçe", "=1+2"], [None, 5]], None
        )
        read = self.workspace.read_workbook("book.xlsx", "Data", "B2:C3")
        self.assertEqual(read["rows"][0][1]["value"], "=1+2")
        self.workspace.write_workbook(
            "book.xlsx", "Data", "C3", [[None]], result["sha256"]
        )
        self.assertIsNone(
            self.workspace.read_workbook("book.xlsx", "Data", "C3:C3")["rows"][0][0][
                "value"
            ]
        )
        for suffix in ["docx", "pdf"]:
            with self.subTest(suffix=suffix):
                result = self.workspace.write_document(
                    f"document.{suffix}", ["Türkçe içerik", "Second paragraph"], None
                )
                document = self.workspace.read_file(f"document.{suffix}")
                self.assertIn("Türkçe", document["content"])
                self.assertEqual(result["sha256"], document["sha256"])
                self.workspace.undo_change(result["undo_id"])
                self.assertFalse((self.root / f"document.{suffix}").exists())

    def test_git_inspection_without_exec_or_external_diff(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "a.txt").write_text("original")
        subprocess.run(["git", "add", "a.txt"], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=self.root,
            check=True,
        )
        readonly = Workspace(AgentPolicy(self.root))
        (self.root / "a.txt").write_text("changed")
        self.assertIn("a.txt", inspect_git(readonly)["output"])
        self.assertIn("fixture", inspect_git(readonly, "log")["output"])
        self.assertIn("+changed", inspect_git(readonly, "diff", path="a.txt")["output"])
        with self.assertRaises(ValueError):
            inspect_git(readonly, "show", revision="--help", path="a.txt")
        with self.assertRaises(ValueError):
            inspect_git(readonly, "show")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.processes = Processes(
            Workspace(AgentPolicy(self.root, allow_exec=True)), max_output_bytes=128
        )
        self.addCleanup(self.processes.close)

    def wait(self, key, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.processes.read(key)
            if result["status"] == "completed":
                return result
            time.sleep(0.02)
        self.fail("process did not complete within the test deadline")

    def test_execution_optin_and_command_validation(self):
        readonly = Processes(Workspace(AgentPolicy(self.root)))
        with self.assertRaises(PermissionError):
            readonly.start([sys.executable, "-c", "print('no')"])
        for command in [[], ["bad\0name"]]:
            with self.assertRaises(ValueError):
                self.processes.start(command)

    def test_repl_output_offsets_input_and_secret_environment(self):
        with patch.dict(os.environ, {"TEST_API_KEY": "must-not-inherit"}):
            result = self.processes.start(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import os; print(os.getenv('TEST_API_KEY')); print(input())",
                ]
            )
        key = result["session_id"]
        self.processes.interact(key, "hello\n")
        output = self.wait(key)
        self.assertEqual(output["exit_code"], 0)
        self.assertIn("None", output["output"])
        self.assertIn("hello", output["output"])
        self.assertEqual(self.processes.read(key, output["next_offset"])["output"], "")

    def test_timeout_termination_and_output_ring(self):
        result = self.processes.start([sys.executable, "-u", "-c", "print('x'*2000)"])
        output = self.wait(result["session_id"])
        self.assertLessEqual(len(output["output"].encode()), 128)
        self.assertGreater(output["dropped_bytes"], 0)
        result = self.processes.start(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout_seconds=1
        )
        output = self.wait(result["session_id"])
        self.assertTrue(output["timed_out"])
        self.assertIsNotNone(output["exit_code"])
        result = self.processes.start(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        output = self.processes.stop(result["session_id"])
        self.assertEqual(output["status"], "completed")


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = create_agent(AgentPolicy(self.root))

    async def test_schema_annotations_errors_and_private_audit(self):
        tools = await self.server.list_tools()
        indexed = {tool.name: tool for tool in tools}
        reference_names = {
            "create_directory",
            "edit_block",
            "force_terminate",
            "get_config",
            "get_file_info",
            "get_more_search_results",
            "get_recent_tool_calls",
            "get_tool_result",
            "get_usage_stats",
            "interact_with_process",
            "kill_process",
            "list_devices",
            "list_directory",
            "list_processes",
            "list_remote_tool_calls",
            "list_searches",
            "list_sessions",
            "move_file",
            "read_file",
            "read_multiple_files",
            "read_process_output",
            "set_config_value",
            "shutdown",
            "start_process",
            "start_search",
            "stop_search",
            "who_am_i",
            "write_file",
            "write_pdf",
        }
        self.assertEqual(len(indexed), 50)
        self.assertTrue(reference_names.issubset(indexed))
        self.assertTrue(indexed["read_file"].annotations.read_only_hint)
        self.assertTrue(indexed["write_file"].annotations.destructive_hint)
        self.assertIn("base_sha256", indexed["write_file"].input_schema["required"])
        from mcp.server.mcpserver.exceptions import ToolError

        with self.assertRaises(ToolError):
            await self.server.call_tool(
                "write_file",
                {
                    "path": "x.txt",
                    "content": "private content",
                    "base_sha256": None,
                    "preview": False,
                },
            )
        result = await self.server.call_tool("get_recent_tool_calls", {})
        serialized = str(result)
        self.assertNotIn("private content", serialized)
        self.assertIn("write_file", serialized)

    async def test_image_is_native_mcp_content(self):
        from PIL import Image

        Image.new("RGB", (2, 2), "red").save(self.root / "image.png")
        result = await self.server.call_tool("read_image", {"path": "image.png"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].type, "image")

    async def test_pdf_preview_is_native_mcp_image_with_source_identity(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=100)
        writer.write(self.root / "preview.pdf")
        writer.close()
        result = await self.server.call_tool("render_pdf_page", {"path": "preview.pdf"})
        self.assertFalse(result.is_error, str(result))
        self.assertEqual(result.content[0].type, "text")
        self.assertIn("sha256", result.content[0].text)
        self.assertEqual(result.content[1].type, "image")

    async def test_stdio_real_client_end_to_end(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(Path(__file__).resolve().parents[1] / "folderlens.py"),
                "--root",
                str(self.root),
                "--allow-write",
                "--allow-exec",
            ],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            initialized = await client.initialize()
            self.assertEqual(initialized.server_info.name, "FolderLens Agent")
            tools = await client.list_tools()
            self.assertGreaterEqual(len(tools.tools), 29)
            result = await client.call_tool(
                "write_file",
                {
                    "path": "hello.txt",
                    "content": "hello MCP",
                    "base_sha256": None,
                    "preview": False,
                },
            )
            self.assertFalse(result.is_error, str(result))

            result = await client.call_tool("read_file", {"path": "hello.txt"})
            self.assertIn("hello MCP", str(result))
            result = await client.call_tool(
                "start_process",
                {"command": [sys.executable, "-u", "-c", "print('MCP command works')"]},
            )
            self.assertFalse(result.is_error, str(result))

            started = json.loads(result.content[0].text)
            deadline = time.monotonic() + 10
            output = ""
            offset = 0
            while time.monotonic() < deadline:
                result = await client.call_tool(
                    "read_process_output",
                    {"session_id": started["session_id"], "offset": offset},
                )
                self.assertFalse(result.is_error, str(result))
                page = json.loads(result.content[0].text)
                output += page["output"]
                offset = page["next_offset"]
                if page["status"] == "completed":
                    self.assertEqual(page["exit_code"], 0)
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("MCP terminal command did not complete")
            self.assertIn("MCP command works", output)

    async def test_http_authorization_host_origin_and_client(self):
        from starlette.testclient import TestClient

        app = http_app(self.server, "t" * 32)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"},
            },
        }
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.assertEqual(client.post("/mcp", json=payload).status_code, 401)
            headers = {
                "Authorization": "Bearer " + "t" * 32,
                "Accept": "application/json, text/event-stream",
            }
            valid = client.post("/mcp", json=payload, headers=headers)
            self.assertEqual(valid.status_code, 200, valid.text)
            self.assertIn("no-store", valid.headers["cache-control"])
            evil_host = client.post(
                "/mcp", json=payload, headers={**headers, "Host": "evil.invalid"}
            )
            self.assertNotEqual(evil_host.status_code, 200)
            evil_origin = client.post(
                "/mcp",
                json=payload,
                headers={**headers, "Origin": "https://evil.invalid"},
            )
            self.assertNotEqual(evil_origin.status_code, 200)
        with self.assertRaises(ValueError):
            http_app(self.server, "weak")
        with self.assertRaises(ValueError):
            http_app(self.server, "t" * 32, "http://bad.invalid/path")


class LauncherTests(unittest.TestCase):
    def test_config_is_parseable_and_does_not_contain_a_token(self):
        args = parser().parse_args(
            [
                "--root",
                str(Path(__file__).resolve().parents[1]),
                "--allow-write",
                "--print-config",
                "toml",
            ]
        )
        config = tomllib.loads(client_config(args))["mcp_servers"]["folderlens"]
        self.assertIn("--allow-write", config["args"])
        self.assertNotIn("--allow-exec", config["args"])
        args.transport = "http"
        config = tomllib.loads(client_config(args))["mcp_servers"]["folderlens"]
        self.assertEqual(config["bearer_token_env_var"], "FOLDERLENS_TOKEN")


if __name__ == "__main__":
    unittest.main()
