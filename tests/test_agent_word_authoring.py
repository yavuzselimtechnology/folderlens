import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from docx import Document

from agentarm.mcp_server import create_agent
from agentarm.workspace import AgentPolicy, Workspace


class WordAuthoringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_write=True))

    def test_markdown_native_styles_inline_table_unicode_and_undo(self):
        source = """# Türkçe başlık

## Alt başlık

**Kalın** ve *eğik* ile `kod`.

- Birinci
- İkinci
    - İç öğe

1. Sıralı
2. Devam

| Başlık | Değer |
| --- | --- |
| 中文 | **42** |

> Alıntı

```python
print('Türkçe')
```

[Bağlantı](https://example.com)
"""
        result = self.workspace.write_document(
            "styled.docx", [source], None, "markdown"
        )
        document = Document(self.root / "styled.docx")
        self.assertEqual(document.paragraphs[0].style.name, "Heading 1")
        self.assertEqual(document.paragraphs[0].text, "Türkçe başlık")
        self.assertEqual(document.paragraphs[1].style.name, "Heading 2")
        self.assertTrue(
            next(run for run in document.paragraphs[2].runs if run.text == "Kalın").bold
        )
        self.assertTrue(
            next(
                run for run in document.paragraphs[2].runs if run.text == "eğik"
            ).italic
        )
        self.assertEqual(
            next(
                run for run in document.paragraphs[2].runs if run.text == "kod"
            ).font.name,
            "Consolas",
        )
        styles = {p.style.name for p in document.paragraphs}
        self.assertTrue(
            {
                "List Bullet",
                "List Bullet 2",
                "List Number",
                "Quote",
                "No Spacing",
            }.issubset(styles)
        )
        self.assertEqual(document.tables[0].cell(1, 0).text, "中文")
        self.assertTrue(document.tables[0].cell(0, 0).paragraphs[0].runs[0].bold)
        self.assertIn("https://example.com", document.paragraphs[-1].text)
        with zipfile.ZipFile(self.root / "styled.docx") as archive:
            self.assertNotIn(
                b'TargetMode="External"', archive.read("word/_rels/document.xml.rels")
            )
        self.workspace.undo_change(result["undo_id"])
        self.assertFalse((self.root / "styled.docx").exists())

    def test_authority_conflict_budget_and_unsupported_elements_are_unchanged(self):
        readonly = Workspace(AgentPolicy(self.root))
        with self.assertRaises(PermissionError):
            readonly.write_document("new.docx", ["# title"], None, "markdown")
        created = self.workspace.write_document(
            "plain.docx", ["# literal heading"], None
        )
        before = (self.root / "plain.docx").read_bytes()
        self.assertEqual(
            Document(self.root / "plain.docx").paragraphs[0].style.name, "Normal"
        )
        with patch("agentarm.word_authoring.markdown_docx") as formatter:
            with self.assertRaisesRegex(ValueError, "conflict"):
                self.workspace.write_document(
                    "plain.docx", ["# title"], "0" * 64, "markdown"
                )
            formatter.assert_not_called()
        for content in (
            "![image](photo.png)",
            "<script>bad()</script>",
            "<blockquote>" * 65 + "deep" + "</blockquote>" * 65,
        ):
            with self.subTest(content=content[:20]), self.assertRaises(ValueError):
                self.workspace.write_document(
                    "plain.docx", [content], created["sha256"], "markdown"
                )
            self.assertEqual((self.root / "plain.docx").read_bytes(), before)
        bounded = Workspace(
            AgentPolicy(self.root, allow_write=True, max_output_chars=4)
        )
        with self.assertRaises(ValueError):
            bounded.write_document("too.docx", ["aa", "bb"], None, "markdown")
        with self.assertRaises(ValueError):
            self.workspace.write_document("wrong.pdf", ["# title"], None, "markdown")

    def test_mcp_source_format_schema_and_hash_protected_replacement(self):
        async def run():
            agent = create_agent(self.workspace.policy)
            tools = {tool.name: tool for tool in await agent.list_tools()}
            self.assertEqual(
                tools["write_document"].input_schema["properties"]["source_format"][
                    "enum"
                ],
                ["plain", "markdown"],
            )
            created = await agent.call_tool(
                "write_document",
                {
                    "path": "mcp.docx",
                    "paragraphs": ["# First"],
                    "base_sha256": None,
                    "source_format": "markdown",
                },
            )
            self.assertFalse(created.is_error)
            first = json.loads(created.content[0].text)
            before = (self.root / "mcp.docx").read_bytes()
            changed = await agent.call_tool(
                "write_document",
                {
                    "path": "mcp.docx",
                    "paragraphs": ["## Second"],
                    "base_sha256": first["sha256"],
                    "source_format": "markdown",
                },
            )
            self.assertFalse(changed.is_error)
            self.assertEqual(
                Document(self.root / "mcp.docx").paragraphs[0].style.name, "Heading 2"
            )
            result = await agent.call_tool(
                "undo_change",
                {"undo_id": json.loads(changed.content[0].text)["undo_id"]},
            )
            self.assertFalse(result.is_error)
            self.assertEqual((self.root / "mcp.docx").read_bytes(), before)

        asyncio.run(run())

    def test_all_heading_levels_and_nested_list_continuation_order(self):
        headings = ["#" * level + f" Level {level}" for level in range(1, 7)]
        content = (
            "\n\n".join(headings)
            + "\n\n<ul><li>before<ul><li>child</li></ul>\n<p>after</p></li></ul>"
        )
        self.workspace.write_document("levels.docx", [content], None, "markdown")
        document = Document(self.root / "levels.docx")
        self.assertEqual(
            [p.style.name for p in document.paragraphs[:6]],
            [f"Heading {level}" for level in range(1, 7)],
        )
        self.assertEqual(
            [p.text for p in document.paragraphs[6:]], ["before", "child", "after"]
        )
        self.assertEqual(document.paragraphs[-1].style.name, "List Continue")
