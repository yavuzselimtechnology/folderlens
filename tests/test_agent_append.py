import asyncio
import codecs
import json
import tempfile
import unittest
from pathlib import Path

from agentarm.mcp_server import create_agent
from agentarm.workspace import AgentPolicy, Workspace, digest


class AppendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))

    def test_chunk_hash_chain_retry_and_reverse_undo(self):
        first = self.workspace.write_file("chunks.txt", "ilk\r\n", None)
        second = self.workspace.write_file(
            "chunks.txt", "iki中文", first["sha256"], mode="append"
        )
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.workspace.write_file(
                "chunks.txt", "iki中文", first["sha256"], mode="append"
            )
        third = self.workspace.write_file(
            "chunks.txt", "\nson", second["sha256"], mode="append"
        )
        self.assertEqual(
            (self.root / "chunks.txt").read_bytes(), "ilk\r\niki中文\nson".encode()
        )
        self.workspace.undo_change(third["undo_id"])
        self.workspace.undo_change(second["undo_id"])
        self.assertEqual((self.root / "chunks.txt").read_bytes(), b"ilk\r\n")
        self.workspace.undo_change(first["undo_id"])
        self.assertFalse((self.root / "chunks.txt").exists())

    def test_append_preserves_utf_bom_byte_order_and_original_bytes(self):
        for codec, bom in (
            ("utf-8", b""),
            ("utf-8", codecs.BOM_UTF8),
            ("utf-16-le", codecs.BOM_UTF16_LE),
            ("utf-16-be", codecs.BOM_UTF16_BE),
            ("utf-32-le", codecs.BOM_UTF32_LE),
            ("utf-32-be", codecs.BOM_UTF32_BE),
        ):
            with self.subTest(codec=codec, bom=bom):
                original = bom + "Türkçe\r\n".encode(codec)
                (self.root / "encoded.txt").write_bytes(original)
                result = self.workspace.write_file(
                    "encoded.txt", "中文😀", digest(original), mode="append"
                )
                expected = original + "中文😀".encode(codec)
                self.assertEqual((self.root / "encoded.txt").read_bytes(), expected)
                self.assertEqual(result["sha256"], digest(expected))

    def test_readonly_preview_is_exact_and_does_not_apply(self):
        before = codecs.BOM_UTF16_BE + "ilk\r\n".encode("utf-16-be")
        (self.root / "preview.txt").write_bytes(before)
        readonly = Workspace(AgentPolicy(self.root))
        result = readonly.write_file(
            "preview.txt", "yeni\n", digest(before), preview=True, mode="append"
        )
        self.assertIn("+yeni", result["diff"])
        self.assertEqual(
            result["sha256"], digest(before + "yeni\n".encode("utf-16-be"))
        )
        self.assertEqual((self.root / "preview.txt").read_bytes(), before)
        self.assertFalse(readonly.undo)
        with self.assertRaises(PermissionError):
            readonly.write_file("preview.txt", "denied", digest(before), mode="append")

    def test_invalid_binary_document_and_file_budget_fail_without_changes(self):
        for path, before in (
            ("binary.txt", b"a\x00b"),
            ("old.txt", b"\xff"),
            ("document.pdf", b"%PDF fixture"),
        ):
            (self.root / path).write_bytes(before)
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workspace.write_file(path, "new", digest(before), mode="append")
            self.assertEqual((self.root / path).read_bytes(), before)
        bounded = Workspace(AgentPolicy(self.root, allow_write=True, max_file_bytes=5))
        created = bounded.write_file("budget.txt", "1234", None, mode="append")
        with self.assertRaisesRegex(ValueError, "budget"):
            bounded.write_file("budget.txt", "56", created["sha256"], mode="append")
        self.assertEqual((self.root / "budget.txt").read_bytes(), b"1234")
        with self.assertRaises(ValueError):
            self.workspace.write_file("bad.txt", "bad", None, mode="invalid")
        self.assertFalse((self.root / "bad.txt").exists())

    def test_mcp_append_and_readonly_preview_schema(self):
        async def run():
            agent = create_agent(self.workspace.policy)
            tools = {x.name: x for x in await agent.list_tools()}
            self.assertEqual(
                tools["write_file"].input_schema["properties"]["mode"]["enum"],
                ["rewrite", "append"],
            )
            result = await agent.call_tool(
                "write_file",
                {
                    "path": "mcp.txt",
                    "content": "first",
                    "base_sha256": None,
                    "preview": False,
                },
            )
            self.assertFalse(result.is_error)
            source_hash = json.loads(result.content[0].text)["sha256"]
            readonly = create_agent(AgentPolicy(self.root))
            preview = await readonly.call_tool(
                "preview_changes",
                {
                    "path": "mcp.txt",
                    "content": "last",
                    "base_sha256": source_hash,
                    "mode": "append",
                },
            )
            self.assertFalse(preview.is_error)
            applied = await agent.call_tool(
                "write_file",
                {
                    "path": "mcp.txt",
                    "content": "last",
                    "base_sha256": source_hash,
                    "mode": "append",
                    "preview": False,
                },
            )
            self.assertFalse(applied.is_error)
            self.assertEqual((self.root / "mcp.txt").read_text(), "firstlast")

        asyncio.run(run())
