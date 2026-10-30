import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from agentarm.mcp_server import create_agent
from agentarm.pdf_tools import read_pdf
from agentarm.workspace import AgentPolicy, Workspace, digest


class PDFReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root))
        self.jpeg = io.BytesIO()
        Image.new("RGB", (5, 3), "blue").save(self.jpeg, "JPEG")
        target = self.root / "sample.pdf"
        canvas = Canvas(str(target))
        canvas.drawString(40, 750, "Page one cafe: " + "abcdefghij" * 20)
        canvas.drawImage(ImageReader(Image.new("RGB", (4, 2), "red")), 40, 600, 40, 20)
        canvas.drawImage(ImageReader(io.BytesIO(self.jpeg.getvalue())), 40, 500, 50, 30)
        canvas.showPage()
        canvas.drawString(40, 750, "Second page")
        canvas.save()
        self.original = target.read_bytes()

    def test_page_text_char_windows_hash_and_page_navigation(self):
        first = read_pdf(self.workspace, "sample.pdf", length=20)
        self.assertEqual(first["page_count"], 2)
        self.assertEqual(first["next_page_index"], 1)
        parts = [first["content"]]
        offset = first["next_offset"]
        while offset is not None:
            page = read_pdf(
                self.workspace,
                "sample.pdf",
                offset=offset,
                length=80,
                expected_sha256=first["sha256"],
            )
            parts.append(page["content"])
            offset = page["next_offset"]
        with PdfReader(io.BytesIO(self.original)) as reader:
            self.assertEqual("".join(parts), reader.pages[0].extract_text())
        second = read_pdf(self.workspace, "sample.pdf", page_index=1)
        self.assertIn("Second page", second["content"])
        self.assertIsNone(second["next_page_index"])
        self.assertFalse(second["ocr"])
        self.assertEqual((self.root / "sample.pdf").read_bytes(), self.original)

    def test_embedded_catalog_pages_and_extracted_pixels_jpeg_bytes(self):
        catalog = read_pdf(self.workspace, "sample.pdf", mode="images", length=400)
        entries = list(catalog["images"])
        while catalog["next_offset"] is not None:
            catalog = read_pdf(
                self.workspace,
                "sample.pdf",
                mode="images",
                length=400,
                offset=catalog["next_offset"],
            )
            entries.extend(catalog["images"])
        self.assertEqual([x["image_index"] for x in entries], [0, 1])
        found_jpeg = False
        for entry in entries:
            image = read_pdf(
                self.workspace,
                "sample.pdf",
                mode="images",
                image_index=entry["image_index"],
            )
            encoded = base64.b64decode(image["image"])
            self.assertEqual(image["image_sha256"], digest(encoded))
            with Image.open(io.BytesIO(encoded)) as extracted:
                self.assertIn(extracted.size, {(4, 2), (5, 3)})
                if image["mime_type"] == "image/jpeg":
                    found_jpeg = True
                    self.assertEqual(encoded, self.jpeg.getvalue())
                else:
                    self.assertEqual(
                        extracted.convert("RGB").getpixel((0, 0)), (255, 0, 0)
                    )
        self.assertTrue(found_jpeg)
        empty = read_pdf(self.workspace, "sample.pdf", page_index=1, mode="images")
        self.assertEqual(empty["images"], [])
        self.assertEqual(empty["total_images"], 0)

    def test_conflict_validation_encryption_and_corruption_fail_closed(self):
        with patch("agentarm.pdf_tools._worker") as worker:
            with self.assertRaisesRegex(ValueError, "conflict"):
                read_pdf(self.workspace, "sample.pdf", expected_sha256="0" * 64)
            worker.assert_not_called()
        for args in (
            {"page_index": -1},
            {"page_index": True},
            {"offset": -1},
            {"length": 0},
            {"image_index": 0},
            {"mode": "bad"},
            {"mode": "images", "image_index": -1},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                read_pdf(self.workspace, "sample.pdf", **args)
        for args in (
            {"page_index": 2},
            {"mode": "images", "image_index": 2},
            {"mode": "images", "length": 1},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                read_pdf(self.workspace, "sample.pdf", **args)
        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        writer.encrypt("fixture password")
        writer.write(self.root / "encrypted.pdf")
        writer.close()
        with self.assertRaises(ValueError):
            read_pdf(self.workspace, "encrypted.pdf")
        (self.root / "corrupt.pdf").write_bytes(b"%PDF-1.7 not a complete file")
        with self.assertRaises(ValueError):
            read_pdf(self.workspace, "corrupt.pdf")

    def test_character_budget_caps_text_and_native_mcp_image(self):
        bounded = Workspace(AgentPolicy(self.root, max_output_chars=450))
        text = read_pdf(bounded, "sample.pdf", length=450)
        self.assertLessEqual(len(json.dumps(text, ensure_ascii=False)), 450)
        self.assertIsNotNone(text["next_offset"])

        async def run():
            agent = create_agent(self.workspace.policy)
            result = await agent.call_tool(
                "read_pdf", {"path": "sample.pdf", "mode": "images", "image_index": 0}
            )
            self.assertFalse(result.is_error)
            metadata = json.loads(result.content[0].text)
            self.assertEqual(result.content[1].type, "image")
            self.assertEqual(
                digest(base64.b64decode(result.content[1].data)),
                metadata["image_sha256"],
            )
            self.assertEqual(result.structured_content, metadata)

        asyncio.run(run())

    def test_embedded_pixel_budget_rejects_oversized_image(self):
        canvas = Canvas(str(self.root / "oversized.pdf"))
        canvas.drawImage(ImageReader(Image.new("RGB", (2001, 2000), "red")), 0, 0)
        canvas.save()
        with self.assertRaisesRegex(ValueError, "megapixel"):
            read_pdf(self.workspace, "oversized.pdf", mode="images", image_index=0)

    def test_jpeg2000_is_explicitly_converted_to_supported_png(self):
        from PIL import features
        from pypdf.generic import (
            DecodedStreamObject,
            DictionaryObject,
            EncodedStreamObject,
            NameObject,
            NumberObject,
        )

        if not features.check_codec("jpg_2000"):
            self.skipTest("Pillow JPEG2000 codec unavailable")
        source = io.BytesIO()
        Image.new("RGB", (4, 3), "green").save(source, "JPEG2000")
        writer = PdfWriter()
        page = writer.add_blank_page(100, 100)
        stream = EncodedStreamObject()
        stream._data = source.getvalue()
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(4),
                NameObject("/Height"): NumberObject(3),
                NameObject("/BitsPerComponent"): NumberObject(8),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/Filter"): NameObject("/JPXDecode"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/Im0"): writer._add_object(stream)}
                )
            }
        )
        content = DecodedStreamObject()
        content.set_data(b"q 10 0 0 10 0 0 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(content)
        writer.write(self.root / "jpx.pdf")
        writer.close()
        result = read_pdf(self.workspace, "jpx.pdf", mode="images", image_index=0)
        self.assertTrue(result["converted_to_png"])
        self.assertEqual(result["mime_type"], "image/png")
        with Image.open(io.BytesIO(base64.b64decode(result["image"]))) as image:
            self.assertEqual(image.size, (4, 3))
            self.assertEqual(image.getpixel((0, 0)), (0, 128, 0))
