import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document

from agentarm.document_tools import edit_word_xml, read_word
from agentarm.workspace import AgentPolicy, Workspace, digest


class WordXMLTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))
        document = Document()
        document.add_heading("Başlık", 1)
        document.add_paragraph("Türkçe & XML")
        document.add_table(rows=1, cols=2).cell(0, 0).text = "Cell content"
        document.sections[0].header.paragraphs[0].text = "Header text"
        document.save(self.root / "source.docx")
        self.before = (self.root / "source.docx").read_bytes()
        self.sha = digest(self.before)

    def test_outline_styles_tables_and_complete_xml_paging(self):
        first = read_word(self.workspace, "source.docx", limit=1)
        self.assertEqual(first["entries"][0]["text"], "Başlık")
        self.assertEqual(first["entries"][0]["style"], "Heading1")
        second = read_word(self.workspace, "source.docx", offset=first["next_offset"])
        table = next(row for row in second["entries"] if row["type"] == "tbl")
        self.assertEqual(table["text"].splitlines()[0], "Cell content")
        self.assertEqual(second["entries"][0]["body_index"], 1)
        first = read_word(self.workspace, "source.docx", "xml", limit=4)
        pieces = [first["content"]]
        while first["next_offset"] is not None:
            first = read_word(
                self.workspace,
                "source.docx",
                "xml",
                offset=first["next_offset"],
                limit=4,
                expected_sha256=self.sha,
            )
            pieces.append(first["content"])
        xml = "".join(pieces)
        self.assertIn('<w:pStyle w:val="Heading1"/>', xml)
        self.assertIn("Türkçe &amp; XML", xml)
        self.assertTrue(
            read_word(self.workspace, "source.docx", "xml", offset=-2)[
                "content"
            ].endswith("</w:document>\n")
        )
        parts = read_word(self.workspace, "source.docx", "parts", limit=1)
        collected = list(parts["parts"])
        while parts["next_offset"] is not None:
            parts = read_word(
                self.workspace,
                "source.docx",
                "parts",
                offset=parts["next_offset"],
                limit=1,
            )
            collected.extend(parts["parts"])
        self.assertIn("word/header1.xml", collected)
        self.assertEqual(len(collected), len(set(collected)))

    def test_xml_preview_style_change_package_preservation_and_undo(self):
        arguments = (
            self.workspace,
            "source.docx",
            "word/document.xml",
            '<w:pStyle w:val="Heading1"/>',
            '<w:pStyle w:val="Heading2"/>',
            self.sha,
        )
        preview = edit_word_xml(*arguments)
        self.assertIn("Heading2", preview["diff"])
        self.assertTrue(preview["preview"])
        self.assertEqual((self.root / "source.docx").read_bytes(), self.before)
        changed = edit_word_xml(*arguments, preview=False)
        self.assertEqual(
            Document(self.root / "source.docx").paragraphs[0].style.name, "Heading 2"
        )
        with (
            zipfile.ZipFile(io.BytesIO(self.before)) as before,
            zipfile.ZipFile(self.root / "source.docx") as after,
        ):
            self.assertEqual(before.namelist(), after.namelist())
            for member in before.namelist():
                if member != "word/document.xml":
                    self.assertEqual(before.read(member), after.read(member))
        self.workspace.undo_change(changed["undo_id"])
        self.assertEqual((self.root / "source.docx").read_bytes(), self.before)

    def test_header_edits_hash_count_and_readonly_preview(self):
        readonly = Workspace(AgentPolicy(self.root))
        arguments = (
            readonly,
            "source.docx",
            "word/header1.xml",
            "Header text",
            "New header",
            self.sha,
        )
        self.assertTrue(edit_word_xml(*arguments)["preview"])
        with self.assertRaises(PermissionError):
            edit_word_xml(*arguments, preview=False)
        with self.assertRaisesRegex(ValueError, "count mismatch"):
            edit_word_xml(*arguments, expected_replacements=2)
        with self.assertRaisesRegex(ValueError, "conflict"):
            read_word(self.workspace, "source.docx", expected_sha256="0" * 64)
        changed = edit_word_xml(self.workspace, *arguments[1:], preview=False)
        self.assertEqual(
            Document(self.root / "source.docx").sections[0].header.paragraphs[0].text,
            "New header",
        )
        with self.assertRaisesRegex(ValueError, "conflict"):
            edit_word_xml(*arguments)
        self.workspace.undo_change(changed["undo_id"])

    def test_invalid_xml_entities_part_paths_and_required_body_rejected(self):
        for old, new in (
            ("<w:t>Başlık</w:t>", "<w:t>Unclosed"),
            ("<w:body>", "<w:other>"),
            ("<w:body>", ""),
        ):
            with self.subTest(new=new), self.assertRaises(ValueError):
                edit_word_xml(
                    self.workspace,
                    "source.docx",
                    "word/document.xml",
                    old,
                    new,
                    self.sha,
                    preview=False,
                )
            self.assertEqual((self.root / "source.docx").read_bytes(), self.before)
        for part in (
            "../word/document.xml",
            "word/../document.xml",
            "word/missing.xml",
            "word\\document.xml",
        ):
            with self.assertRaises(ValueError):
                read_word(self.workspace, "source.docx", "xml", part=part)
        hostile = b'<!DOCTYPE document [<!ENTITY private SYSTEM "file:///private">]><document>&private;</document>'
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", hostile)
        (self.root / "hostile.docx").write_bytes(output.getvalue())
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            read_word(self.workspace, "hostile.docx", "xml")

    def test_well_formed_xml_cannot_change_root_or_remove_body(self):
        from lxml import etree

        xml = read_word(self.workspace, "source.docx", "xml", limit=2000)["content"]
        for change_root in (True, False):
            root = etree.fromstring(xml.encode("utf-8"))
            if change_root:
                root.tag = "{urn:other}document"
            else:
                root.remove(
                    root.find(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body"
                    )
                )
            changed = etree.tostring(root, encoding="unicode")
            with self.assertRaisesRegex(ValueError, "root|body"):
                edit_word_xml(
                    self.workspace,
                    "source.docx",
                    "word/document.xml",
                    xml,
                    changed,
                    self.sha,
                    preview=False,
                )
            self.assertEqual((self.root / "source.docx").read_bytes(), self.before)

    def test_outline_reports_image_reference_and_budget_continuation(self):
        from PIL import Image

        document = Document(self.root / "source.docx")
        picture = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(picture, format="PNG")
        picture.seek(0)
        document.add_picture(picture)
        document.save(self.root / "source.docx")
        outline = read_word(self.workspace, "source.docx")
        image = next(row for row in outline["entries"] if row["image_refs"])
        self.assertEqual(len(image["image_refs"]), 1)
        bounded = Workspace(AgentPolicy(self.root, max_output_chars=500))
        first = read_word(bounded, "source.docx")
        self.assertIsNotNone(first["next_offset"])
        second = read_word(bounded, "source.docx", offset=first["next_offset"])
        self.assertGreater(
            second["entries"][0]["body_index"], first["entries"][0]["body_index"]
        )

    def test_long_xml_lines_have_lossless_character_paging(self):
        document = Document()
        document.add_paragraph("lengthy " * 100)
        document.save(self.root / "source.docx")
        bounded = Workspace(AgentPolicy(self.root, max_output_chars=200))
        with self.assertRaisesRegex(ValueError, "xml_chars"):
            read_word(bounded, "source.docx", "xml")
        page = read_word(bounded, "source.docx", "xml_chars", limit=200)
        pieces = [page["content"]]
        while page["next_offset"] is not None:
            page = read_word(
                bounded,
                "source.docx",
                "xml_chars",
                offset=page["next_offset"],
                limit=200,
                expected_sha256=page["sha256"],
            )
            pieces.append(page["content"])
        complete = read_word(self.workspace, "source.docx", "xml_chars", limit=16000)
        self.assertEqual("".join(pieces), complete["content"])


if __name__ == "__main__":
    unittest.main()
