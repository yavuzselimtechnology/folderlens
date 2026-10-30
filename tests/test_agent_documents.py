import io
import tempfile
import unittest
from pathlib import Path

from agentarm.document_tools import edit_pdf, edit_word
from agentarm.workspace import AgentPolicy, Workspace, digest


class DocumentEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))

    def test_word_replaces_across_runs_preserving_styles_tables_headers(self):
        from docx import Document

        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run("Before ").bold = True
        paragraph.add_run("a name").italic = True
        paragraph.add_run(" after a name.")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "a name"
        document.sections[0].header.paragraphs[0].text = "Header a name"
        document.save(self.root / "source.docx")
        before = (self.root / "source.docx").read_bytes()
        hashed = digest(before)
        preview = edit_word(
            self.workspace, "source.docx", "a name", "Türkçe", hashed, 4
        )
        self.assertEqual(preview["replacements"], 4)
        self.assertEqual((self.root / "source.docx").read_bytes(), before)
        changed = edit_word(
            self.workspace, "source.docx", "a name", "Türkçe", hashed, 4, False
        )
        loaded = Document(self.root / "source.docx")
        self.assertEqual(loaded.paragraphs[0].text, "Before Türkçe after Türkçe.")
        self.assertTrue(loaded.paragraphs[0].runs[0].bold)
        self.assertTrue(loaded.paragraphs[0].runs[1].italic)
        self.assertEqual(loaded.tables[0].cell(0, 0).text, "Türkçe")
        self.assertEqual(loaded.sections[0].header.paragraphs[0].text, "Header Türkçe")
        self.workspace.undo_change(changed["undo_id"])
        self.assertEqual((self.root / "source.docx").read_bytes(), before)

    def test_word_cross_run_repeated_match_and_count_failure(self):
        from docx import Document

        document = Document()
        paragraph = document.add_paragraph()
        for text in ["ab", "c abc a", "bc"]:
            paragraph.add_run(text)
        document.save(self.root / "source.docx")
        before = (self.root / "source.docx").read_bytes()
        with self.assertRaises(ValueError):
            edit_word(
                self.workspace,
                "source.docx",
                "abc",
                "changed",
                digest(before),
                2,
                False,
            )
        self.assertEqual((self.root / "source.docx").read_bytes(), before)
        edit_word(
            self.workspace,
            "source.docx",
            "abc",
            "longer text",
            digest(before),
            3,
            False,
        )
        self.assertEqual(
            Document(self.root / "source.docx").paragraphs[0].text,
            "longer text longer text longer text",
        )

    def pdf(self, name, widths):
        from pypdf import PdfWriter

        writer = PdfWriter()
        for width in widths:
            writer.add_blank_page(width=width, height=300)
        output = io.BytesIO()
        writer.write(output)
        writer.close()
        data = output.getvalue()
        (self.root / name).write_bytes(data)
        return digest(data)

    def test_word_does_not_join_words_across_tabs_or_line_breaks(self):
        from docx import Document

        document = Document()
        document.add_paragraph("a\tb a\nb abc")
        document.save(self.root / "separators.docx")
        data = (self.root / "separators.docx").read_bytes()
        edit_word(self.workspace, "separators.docx", "ab", "X", digest(data), 1, False)
        self.assertEqual(
            Document(self.root / "separators.docx").paragraphs[0].text, "a\tb a\nb Xc"
        )

    def test_pdf_insert_rotate_delete_undo_and_conflicts(self):
        from pypdf import PdfReader

        hashed = self.pdf("target.pdf", [100, 200])
        self.pdf("source.pdf", [400, 500])
        inserted = edit_pdf(
            self.workspace, "target.pdf", "insert", [1, 0], hashed, "source.pdf", 1
        )
        reader = PdfReader(self.root / "target.pdf")
        self.assertEqual(
            [float(page.mediabox.width) for page in reader.pages], [100, 500, 400, 200]
        )
        rotated = edit_pdf(
            self.workspace, "target.pdf", "rotate", [1], inserted["sha256"], rotation=90
        )
        self.assertEqual(PdfReader(self.root / "target.pdf").pages[1].rotation, 90)
        deleted = edit_pdf(
            self.workspace, "target.pdf", "delete", [0, 3], rotated["sha256"]
        )
        self.assertEqual(deleted["page_count"], 2)
        self.workspace.undo_change(deleted["undo_id"])
        self.assertEqual(len(PdfReader(self.root / "target.pdf").pages), 4)
        with self.assertRaisesRegex(ValueError, "conflict"):
            edit_pdf(self.workspace, "target.pdf", "delete", [0], hashed)

    def test_pdf_invalid_indexes_and_no_empty_document(self):
        hashed = self.pdf("target.pdf", [100, 200])
        for pages in [[0, 1], [2], [-1], [0, 0]]:
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                edit_pdf(self.workspace, "target.pdf", "delete", pages, hashed)
        with self.assertRaises(ValueError):
            edit_pdf(self.workspace, "target.pdf", "rotate", [0], hashed, rotation=23)
