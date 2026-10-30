import base64
import io
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentarm.pdf_tools import _prepare_html, render_pdf_page, write_pdf
from agentarm.workspace import AgentPolicy, Workspace


class RichPDFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))

    def test_markup_csp_script_removal_and_protected_assets(self):
        html = _prepare_html(
            self.workspace,
            '<script>bad()</script><iframe src="https://example.com"></iframe><h1>Good</h1>',
            "html",
            "",
        )
        self.assertNotIn("bad()", html)
        self.assertNotIn("iframe", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn("Good", html)
        for asset in [
            "../secret.png",
            "https://example.com/a.png",
            "/absolute.png",
            ".env",
        ]:
            with (
                self.subTest(asset=asset),
                self.assertRaises((ValueError, PermissionError)),
            ):
                _prepare_html(self.workspace, f'<img src="{asset}">', "html", "")

    def test_rich_pdf_table_svg_local_image_page_break_and_visual_preview(self):
        from PIL import Image
        from pypdf import PdfReader

        pixels = random.Random(0).randbytes(256 * 128 * 3)
        Image.frombytes("RGB", (256, 128), pixels).save(self.root / "figure.png")
        self.assertGreater((self.root / "figure.png").stat().st_size, 65536)
        content = """# Türkçe rapor

**Başlık:** doğrulanmış PDF çıktısı.

| Araç | Sonuç |
| --- | --- |
| FolderLens | Başarılı |

![Yerel görsel](figure.png)

<svg width="200" height="60" xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="180" height="40" fill="#145a78"/></svg>

<div style="page-break-before: always;"></div>

## İkinci sayfa

Görsel önizleme ve sayfa düzeni korunur.
"""
        result = write_pdf(self.workspace, "rich.pdf", content, None)
        self.assertEqual(result["page_count"], 2)
        self.assertFalse(result["network_enabled"])
        self.assertFalse(result["javascript_enabled"])
        reader = PdfReader(self.root / "rich.pdf")
        self.assertIn("Türkçe rapor", reader.pages[0].extract_text())
        self.assertIn("FolderLens", reader.pages[0].extract_text())
        self.assertIn("İkinci sayfa", reader.pages[1].extract_text())
        preview = render_pdf_page(self.workspace, "rich.pdf", 0, 2)
        self.assertEqual(preview["sha256"], result["sha256"])
        self.assertEqual(preview["page_count"], 2)
        self.assertLessEqual(preview["width"] * preview["height"], 4_000_000)
        with Image.open(io.BytesIO(base64.b64decode(preview["image"]))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertGreater(image.width, 500)
        # Authoring's internal helper must not grant arbitrary command access.
        self.assertFalse(self.workspace.policy.allow_exec)
        self.workspace.undo_change(result["undo_id"])
        self.assertFalse((self.root / "rich.pdf").exists())

    def test_readonly_denial_and_invalid_preview_index(self):
        from pypdf import PdfWriter

        readonly = Workspace(AgentPolicy(self.root))
        with self.assertRaises(PermissionError):
            write_pdf(readonly, "no.pdf", "No write", None)
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(self.root / "one.pdf")
        writer.close()
        with self.assertRaisesRegex(ValueError, "Page index"):
            render_pdf_page(readonly, "one.pdf", 9)

    def test_owner_browser_channel_rejects_arbitrary_executable_names(self):
        with (
            patch.dict(os.environ, {"FOLDERLENS_PDF_BROWSER_CHANNEL": "../browser"}),
            self.assertRaisesRegex(ValueError, "must be chrome or msedge"),
        ):
            write_pdf(self.workspace, "invalid.pdf", "No file should be written", None)
        self.assertFalse((self.root / "invalid.pdf").exists())
