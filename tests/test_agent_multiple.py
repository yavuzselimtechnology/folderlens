import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from agentarm.image_tools import MAX_IMAGE_BYTES, read_image
from agentarm.mcp_server import create_agent
from agentarm.multiple_files import read_multiple_files
from agentarm.workspace import AgentPolicy, Workspace, digest


class MultipleFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root))

    async def test_mcp_mixed_text_images_failures_and_identity(self):
        (self.root / "notes.txt").write_text("Türkçe notes", encoding="utf-8")
        (self.root / ".env").write_text("private fixture")
        names = []
        for suffix, format_name in (
            ("png", "PNG"),
            ("jpg", "JPEG"),
            ("webp", "WEBP"),
            ("gif", "GIF"),
        ):
            name = "image." + suffix
            Image.new("RGB", (3, 2), "red").save(self.root / name, format=format_name)
            names.append(name)
        paths = ["notes.txt", names[0], "missing.txt", *names[1:], ".env"]
        response = await create_agent(self.workspace.policy).call_tool(
            "read_multiple_files", {"paths": paths}
        )
        self.assertFalse(response.is_error)
        records = json.loads(response.content[0].text)["files"]
        self.assertEqual([x["path"] for x in records], paths)
        self.assertEqual(records[0]["content"], "Türkçe notes")
        self.assertEqual(records[2]["error"], "ValueError")
        self.assertIn("error", records[-1])
        self.assertNotIn("private fixture", response.content[0].text)
        self.assertEqual(len(response.content), 5)
        for record in records:
            if record.get("representation") != "image":
                continue
            content = response.content[record["content_index"]]
            original = (self.root / record["path"]).read_bytes()
            self.assertEqual(content.type, "image")
            self.assertEqual(content.mime_type, record["mime_type"])
            self.assertEqual(base64.b64decode(content.data), original)
            self.assertEqual(record["sha256"], digest(original))
            self.assertEqual((record["width"], record["height"]), (3, 2))
        self.assertEqual(response.structured_content["files"], records)

    async def test_aggregate_images_share_one_byte_budget(self):
        image = Image.frombytes(
            "RGB", (1024, 1024), bytes(range(256)) * (1024 * 1024 * 3 // 256)
        )
        image.save(self.root / "noise.png", compress_level=0)
        size = (self.root / "noise.png").stat().st_size
        self.assertLess(size, MAX_IMAGE_BYTES)
        self.assertGreater(size * 2, MAX_IMAGE_BYTES)
        response = read_multiple_files(self.workspace, ["noise.png", "noise.png"], 100)
        records = json.loads(response.content[0].text)["files"]
        self.assertEqual(records[0]["bytes"], size)
        self.assertEqual(records[1]["error"], "ValueError")
        self.assertEqual(len(response.content), 2)
        with self.assertRaisesRegex(ValueError, "byte budget"):
            read_image(self.workspace, "noise.png", byte_budget=size - 1)

    async def test_text_json_budget_truncates_with_lossless_cursor_and_reserves_errors(
        self,
    ):
        source = "\x01" * 256 + "Türkçe"
        (self.root / "escaped.txt").write_text(source, encoding="utf-8")
        workspace = Workspace(AgentPolicy(self.root, max_output_chars=600))
        response = read_multiple_files(workspace, ["escaped.txt", "missing.txt"], 256)
        self.assertLessEqual(len(response.content[0].text), 600)
        records = json.loads(response.content[0].text)["files"]
        first = records[0]
        self.assertGreater(len(first["content"]), 0)
        self.assertLess(len(first["content"]), 256)
        self.assertEqual(first["next_offset"], len(first["content"]))
        rest = self.workspace.read_file("escaped.txt", first["next_offset"])["content"]
        self.assertEqual(first["content"] + rest, source)
        self.assertEqual(records[1]["error"], "ValueError")

    async def test_corrupt_disguised_and_bomb_images_are_explicit_failures(self):
        (self.root / "corrupt.png").write_bytes(b"not a valid image")
        Image.new("RGB", (4, 4), "blue").save(self.root / "disguised.png", format="BMP")
        Image.new("RGB", (12, 12), "blue").save(self.root / "large.png")
        with patch("PIL.Image.MAX_IMAGE_PIXELS", 100):
            response = read_multiple_files(
                self.workspace, ["corrupt.png", "disguised.png", "large.png"], 100
            )
        self.assertEqual(len(response.content), 1)
        self.assertTrue(
            all("error" in x for x in json.loads(response.content[0].text)["files"])
        )

    async def test_text_only_contract_and_invalid_windows(self):
        (self.root / "text.txt").write_text("abcdefgh", encoding="utf-8")
        response = await create_agent(self.workspace.policy).call_tool(
            "read_multiple_files", {"paths": ["text.txt"], "length": 3}
        )
        self.assertFalse(response.is_error)
        record = json.loads(response.content[0].text)["files"][0]
        self.assertEqual(record["content"], "abc")
        self.assertEqual(record["next_offset"], 3)
        for paths, length in (([], 1), (["x"] * 17, 1), (["x"], 0), (["x"], True)):
            with (
                self.subTest(paths=len(paths), length=length),
                self.assertRaises(ValueError),
            ):
                read_multiple_files(self.workspace, paths, length)

    async def test_real_stdio_client_receives_native_images_and_structured_metadata(
        self,
    ):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        (self.root / "notes.txt").write_text("wire fixture", encoding="utf-8")
        Image.new("RGB", (3, 2), "green").save(self.root / "wire.png")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                str(Path(__file__).resolve().parents[1] / "folderlens.py"),
                "--root",
                str(self.root),
            ],
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "read_multiple_files", {"paths": ["notes.txt", "wire.png"]}
            )
            self.assertFalse(result.is_error)
            records = json.loads(result.content[0].text)["files"]
            self.assertEqual(result.structured_content["files"], records)
            self.assertEqual(records[0]["content"], "wire fixture")
            self.assertEqual(records[1]["content_index"], 1)
            self.assertEqual(result.content[1].type, "image")
            self.assertEqual(
                base64.b64decode(result.content[1].data),
                (self.root / "wire.png").read_bytes(),
            )
