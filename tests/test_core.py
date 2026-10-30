from __future__ import annotations

import hashlib
import io
import json
import unittest
import zipfile
from itertools import pairwise

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from agentarm.config import Settings
from agentarm.paths import (
    is_ignored_path,
    is_sensitive_path,
    safe_relative_path,
    validate_share_id,
)
from agentarm.snapshots import (
    DOCUMENT_LINKS_PAGE_SIZE,
    GIT_COMMITS_PAGE_SIZE,
    MAX_DOCUMENT_LINKS_HTML_BYTES,
    TEXT_CHUNK_BYTES,
    TEXT_CHUNK_THRESHOLD_BYTES,
    SnapshotError,
    SnapshotStore,
    _extract_document,
    _json_bytes,
    _json_size,
    _text_chunks,
)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()
        self.store = SnapshotStore(self.settings)

    def publish(self, uploads: list[tuple[str, bytes]]) -> tuple[dict, dict, dict]:
        result = self.store.publish(
            {
                "project_name": "Exact Project",
                "directories": ["src", "docs"],
                "git": {
                    "detected": True,
                    "head": "ref: refs/heads/main",
                    "branch": "refs/heads/main",
                    "refs": [
                        {"sha": "b" * 40, "name": "refs/tags/v1"},
                        {"name": "refs/heads/main", "sha": "a" * 40},
                    ],
                    "history": [
                        {
                            "sequence": 2,
                            "old_sha": "0" * 40,
                            "new_sha": "a" * 40,
                            "actor": "Yavuz Selim",
                            "email": "test@example.test",
                            "timestamp": 1_785_364_800,
                            "timezone": "+0300",
                            "message": "commit: Preserve punctuation, CASE & Türkçe.",
                            "source": "logs/HEAD",
                        },
                        {"sequence": 1, "message": "clone: from origin"},
                    ],
                    "commit_graph_complete": True,
                    "commit_graph_source": "reachable Git objects",
                    "commits": [
                        {
                            "sha": "a" * 40,
                            "tree": "c" * 40,
                            "parents": ["b" * 40],
                            "author": {
                                "name": "Yavuz Selim",
                                "email": "test@example.test",
                                "timestamp": 1_785_364_800,
                                "timezone": "+0300",
                            },
                            "committer": {
                                "name": "Yavuz Selim",
                                "email": "test@example.test",
                                "timestamp": 1_785_364_800,
                                "timezone": "+0300",
                            },
                            "subject": "Preserve the whole commit",
                            "message": "Preserve the whole commit\n\nBody, CASE & Türkçe.\n",
                            "references": ["refs/heads/main"],
                        }
                    ],
                    "tracked_paths": ["src/z.py", "README.md", "src/a.py"],
                },
            },
            uploads,
        )
        git = self.store.json_document(result["share_id"], "git.json")
        project = self.store.json_document(result["share_id"], "project-full.json")
        return result, git, project

    def test_exact_text_order_tree_and_git_manifest(self) -> None:
        exact = "Line 1\r\nİkinci satır: A,B; C!\nCaseSensitive = True\n"
        raw = exact.encode("utf-8")
        result, git, project = self.publish(
            [("src/z.py", b"z = 1\n"), ("README.md", raw), ("src/a.py", b"a = 1\n")]
        )

        self.assertRegex(result["share_id"], r"^[A-Za-z0-9_-]{12,32}$")
        self.assertEqual(
            [item["path"] for item in project["documents"]],
            ["README.md", "src/a.py", "src/z.py"],
        )
        readme = project["documents"][0]
        self.assertEqual(readme["content"], exact)
        self.assertEqual(readme["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(readme["source_bytes"], len(raw))
        self.assertEqual(
            [item["name"] for item in git["repository"]["refs"]],
            ["refs/heads/main", "refs/tags/v1"],
        )
        self.assertEqual(
            [item["sequence"] for item in git["repository"]["history"]],
            [1, 2],
        )
        self.assertEqual(
            git["repository"]["tracked_paths"],
            ["README.md", "src/a.py", "src/z.py"],
        )
        self.assertTrue(git["repository"]["commit_graph"]["complete"])
        self.assertEqual(git["repository"]["commit_graph"]["commit_count"], 1)
        commit_page = self.store.git_commit_page(result["share_id"], 1)
        self.assertEqual(
            commit_page["commits"][0]["message"],
            ("Preserve the whole commit\n\nBody, CASE & Türkçe.\n"),
        )
        self.assertEqual(
            git["repository"]["history"][1]["source"],
            "logs/HEAD",
        )
        tree = {item["path"]: item for item in project["directory_tree"]}
        self.assertEqual(tree["."]["directories"], ["docs", "src"])
        self.assertEqual(tree["src"]["files"], ["src/a.py", "src/z.py"])
        index = self.store.json_document(result["share_id"], "project.json")
        self.assertEqual(index["format"], "agentarm.project-index.v1")
        context = self.store.json_document(result["share_id"], "context.json")
        self.assertEqual(context["format"], "agentarm.ai-context.v1")
        self.assertEqual(context["workspace"]["documents"], 3)
        self.assertEqual(context["workspace"]["source_bytes"], len(raw) + 12)
        self.assertIn(
            "all 3 document sequences",
            context["completion_contract"]["complete_only_after"],
        )
        self.assertEqual(
            git["ai_context"]["context_url"],
            f"/AgentArm/share/{result['share_id']}/context.json",
        )
        self.assertNotIn("content", index["documents"][0])
        self.assertEqual(
            index["summary"]["document_url_template"],
            (
                f"/AgentArm/share/{result['share_id']}/project/documents/"
                "{sequence}.json"
            ),
        )
        self.assertEqual(
            index["summary"]["document_links_url"],
            f"/AgentArm/share/{result['share_id']}/document-links.html",
        )
        html = self.store.share_html(result["share_id"])
        self.assertLess(html.index(">AI Context<"), html.index(">Git<"))
        self.assertLess(html.index(">Git<"), html.index(">Project<"))
        self.assertIn(
            f'href="/AgentArm/share/{result["share_id"]}/document-links.html"',
            html,
        )
        links = self.store.document_links_html(result["share_id"])
        first_url = project["documents"][0]["document_url"]
        last_url = project["documents"][-1]["document_url"]
        self.assertIn(f'<a href="{first_url}">', links)
        self.assertIn(f'<a href="{last_url}">', links)
        self.assertLess(links.index(first_url), links.index(last_url))
        self.assertIn("Fetch JSON", html)
        self.assertFalse(hasattr(self.store, "root"))
        self.assertGreater(self.store.memory_bytes(), 0)

    def test_complete_git_graph_is_paged_with_real_links(self) -> None:
        commits = []
        for sequence in range(GIT_COMMITS_PAGE_SIZE + 1):
            sha = f"{sequence + 1:040x}"
            commits.append(
                {
                    "sha": sha,
                    "tree": "f" * 40,
                    "parents": [f"{sequence:040x}"] if sequence else [],
                    "author": None,
                    "committer": None,
                    "subject": f"Commit {sequence + 1}",
                    "message": f"Commit {sequence + 1}\n",
                    "references": [],
                }
            )
        result = self.store.publish(
            {
                "project_name": "Full Git",
                "directories": [],
                "git": {
                    "detected": True,
                    "commit_graph_complete": True,
                    "commit_graph_source": "reachable Git objects",
                    "commits": commits,
                },
            },
            [("README.md", b"git\n")],
        )
        first = self.store.git_commit_page(result["share_id"], 1)
        second = self.store.git_commit_page(result["share_id"], 2)
        self.assertEqual(len(first["commits"]), GIT_COMMITS_PAGE_SIZE)
        self.assertEqual(len(second["commits"]), 1)
        self.assertEqual(second["commit_count"], GIT_COMMITS_PAGE_SIZE + 1)
        links = self.store.git_commit_links_html(result["share_id"])
        self.assertIn(f"/AgentArm/share/{result['share_id']}/git/commits/1.json", links)
        self.assertIn(f"/AgentArm/share/{result['share_id']}/git/commits/2.json", links)
        with self.assertRaises(FileNotFoundError):
            self.store.git_commit_page(result["share_id"], 3)

    def test_large_unicode_text_is_split_losslessly_into_bounded_chunks(self) -> None:
        exact = "İçerik A,B; CASE sensitive.\r\n" * (TEXT_CHUNK_THRESHOLD_BYTES // 20)
        raw = exact.encode("utf-8")
        result, _, project = self.publish([("large.txt", raw)])
        document = project["documents"][0]
        self.assertTrue(document["chunked"])
        self.assertNotIn("content", document)
        chunks = [
            self.store.document_chunk(result["share_id"], 1, sequence)["chunk"]
            for sequence in range(1, document["chunk_count"] + 1)
        ]
        self.assertEqual("".join(chunk["content"] for chunk in chunks), exact)
        self.assertTrue(
            all(chunk["utf8_bytes"] <= TEXT_CHUNK_BYTES for chunk in chunks)
        )
        for chunk in chunks:
            self.assertEqual(
                chunk["sha256"],
                hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest(),
            )
        links = self.store.document_chunk_links_html(result["share_id"], 1)
        self.assertEqual(links.count("/chunks/"), document["chunk_count"] * 2)

    def test_json_size_matches_wire_without_large_string_copies(self) -> None:
        for value in (
            "",
            [],
            {},
            {"a": [True, False, None, -2, 0.25]},
            {"long": '\x00\n\r\t"\\\u0130\U0001f600' * TEXT_CHUNK_BYTES},
        ):
            self.assertEqual(_json_size(value), len(_json_bytes(value)))

    def test_chunk_lines_encodings_and_content_identity(self) -> None:
        for text in ("α\r\nβ\n\rfinal", "😀" * 40, "\n\n", "word " * 40):
            chunks = _text_chunks(text, 16)
            self.assertEqual("".join(c["content"] for c in chunks), text)
            for left, right in pairwise(chunks):
                self.assertEqual(left["utf8_end"], right["utf8_start"])
                self.assertEqual(
                    left["line_end"] + int(not left["ends_mid_line"]),
                    right["line_start"],
                )
                self.assertFalse(
                    left["content"].endswith("\r") and right["content"].startswith("\n")
                )
        for encoding in ("utf-8-sig", "utf-16", "utf-32"):
            exact = "İ α 😀\r\n" * TEXT_CHUNK_BYTES
            raw = exact.encode(encoding)
            _, _, project = self.publish([("encoded.txt", raw)])
            item = project["documents"][0]
            self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(
                item["content_utf8_sha256"], hashlib.sha256(exact.encode()).hexdigest()
            )
            self.assertEqual(item["encoding"], encoding)
        ambiguous = _extract_document(1, "legacy.txt", b"T\xfcrk\xe7e")
        self.assertEqual(ambiguous["kind"], "extraction_error")
        self.assertIn("Ambiguous text encoding", ambiguous["error"])

    def test_large_structured_and_markdown_documents_are_reconstructable(self) -> None:
        word = Document()
        for i in range(800):
            word.add_paragraph(f"Paragraph {i}: İ α <script> & whitespace.  ")
        buffer = io.BytesIO()
        word.save(buffer)
        notebook = json.dumps(
            {"cells": [{"cell_type": "markdown", "source": ["# Exact\n" * 6000]}]}
        ).encode()
        for path, raw in (("large.docx", buffer.getvalue()), ("large.ipynb", notebook)):
            original = _extract_document(1, path, raw)
            result, _, project = self.publish([(path, raw)])
            item = project["documents"][0]
            self.assertTrue(item["chunked"])
            text = "".join(
                self.store.document_chunk(result["share_id"], 1, i)["chunk"]["content"]
                for i in range(1, item["chunk_count"] + 1)
            )
            field = item["chunk_content_field"]
            if field == "document_json":
                self.assertEqual(json.loads(text), original)
            else:
                self.assertEqual(text, original[field])
            self.assertLess(len(json.dumps(item)), TEXT_CHUNK_BYTES)

    def test_entry_html_does_not_expand_with_manifest(self) -> None:
        result, _, _ = self.publish(
            [(f"file-{i:05d}.txt", b"hello") for i in range(2000)]
        )
        page = self.store.share_html(result["share_id"])
        self.assertNotIn("file-01999.txt", page)
        self.assertLess(len(page.encode()), TEXT_CHUNK_BYTES)
        self.assertIn("2000", page)

    def test_upload_streams_are_consumed_once_in_path_order(self) -> None:
        events = []

        class TrackingStream(io.BytesIO):
            def __init__(self, name: str, value: bytes):
                super().__init__(value)
                self.name = name

            def read(self, size: int = -1) -> bytes:
                events.append((self.name, size))
                return super().read(size)

        self.store.publish(
            {"project_name": "Streamed", "directories": [], "git": None},
            [
                ("b.txt", TrackingStream("b", b"second\n")),
                ("a.txt", TrackingStream("a", b"first\n")),
            ],
        )
        self.assertEqual(
            events,
            [
                ("a", self.settings.max_file_bytes + 1),
                ("b", self.settings.max_file_bytes + 1),
            ],
        )

    def test_document_link_manifest_is_bounded_and_paginates_real_anchors(
        self,
    ) -> None:
        path_padding = "'" * 980
        uploads = [
            (f"{sequence:04d}-{path_padding}.txt", b"")
            for sequence in range(1, DOCUMENT_LINKS_PAGE_SIZE + 2)
        ]
        result, _, _ = self.publish(uploads)
        page_one = self.store.document_links_html(result["share_id"])
        page_two = self.store.document_links_html(result["share_id"], 2)
        document_prefix = f"/AgentArm/share/{result['share_id']}/project/documents/"
        self.assertEqual(
            page_one.count(f'<a href="{document_prefix}'),
            DOCUMENT_LINKS_PAGE_SIZE,
        )
        self.assertEqual(page_two.count(f'<a href="{document_prefix}'), 1)
        self.assertIn(
            f'href="/AgentArm/share/{result["share_id"]}/document-links/2.html"',
            page_one,
        )
        self.assertIn(
            f'href="/AgentArm/share/{result["share_id"]}/document-links.html"',
            page_two,
        )
        self.assertLess(len(page_one.encode("utf-8")), MAX_DOCUMENT_LINKS_HTML_BYTES)
        self.assertNotIn(path_padding, page_one)
        self.assertIn("&#x27;", page_one)
        with self.assertRaises(FileNotFoundError):
            self.store.document_links_html(result["share_id"], 3)

    def test_dotfiles_are_supported_without_server_storage(self) -> None:
        result, _, project = self.publish(
            [
                (".gitignore", b".venv/\n__pycache__/\n"),
                (".dockerignore", b"dist/\n"),
            ]
        )
        self.assertEqual(
            [item["path"] for item in project["documents"]],
            [".dockerignore", ".gitignore"],
        )
        self.assertIn("bridge_token", result)
        self.assertIn("write_token", result)
        self.store.authorize_bridge(result["share_id"], result["bridge_token"])
        self.store.authorize_write(result["share_id"], result["write_token"])
        with self.assertRaises(SnapshotError):
            self.store.authorize_write(result["share_id"], "wrong")

    def test_refresh_cannot_restore_a_disconnected_or_replaced_workspace(self) -> None:
        class InterleavedRead(io.BytesIO):
            def __init__(self, store, created, action):
                super().__init__(b"stale")
                self.store, self.created, self.action = store, created, action

            def read(self, size: int = -1) -> bytes:
                share_id, token = self.created["share_id"], self.created["bridge_token"]
                if self.action == "disconnect":
                    self.store.forget(share_id, token)
                else:
                    self.store.refresh(
                        share_id,
                        token,
                        {"project_name": "newest"},
                        [("a.txt", b"newest")],
                    )
                return super().read(size)

        for action in ("disconnect", "replace"):
            with self.subTest(action=action):
                created, _, _ = self.publish([("a.txt", b"original")])
                with self.assertRaises((FileNotFoundError, SnapshotError)):
                    self.store.refresh(
                        created["share_id"],
                        created["bridge_token"],
                        {"project_name": "stale"},
                        [("a.txt", InterleavedRead(self.store, created, action))],
                    )
                if action == "disconnect":
                    with self.assertRaises(FileNotFoundError):
                        self.store.document(created["share_id"], 1)
                else:
                    self.assertEqual(
                        self.store.document(created["share_id"], 1)["document"][
                            "content"
                        ],
                        "newest",
                    )

    def test_refresh_keeps_identity_and_write_capability(self) -> None:
        result, _, _ = self.publish([("README.md", b"first\n")])
        refreshed = self.store.refresh(
            result["share_id"],
            result["bridge_token"],
            {
                "project_name": "Exact Project",
                "directories": [],
                "git": None,
            },
            [("README.md", b"second\n")],
        )
        self.assertEqual(refreshed["share_id"], result["share_id"])
        self.assertNotIn("write_token", refreshed)
        self.store.authorize_write(result["share_id"], result["write_token"])
        document = self.store.document(result["share_id"], 1)
        self.assertEqual(document["document"]["content"], "second\n")

    def test_word_and_excel_are_structured_without_losing_order(self) -> None:
        document = Document()
        document.add_paragraph("First paragraph — exact.")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Header"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "α"
        table.cell(1, 1).text = "12,5"
        word_buffer = io.BytesIO()
        document.save(word_buffer)

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Data"
        sheet["A1"] = "Label"
        sheet["B2"] = 42
        sheet["C3"] = "=B2*2"
        excel_buffer = io.BytesIO()
        workbook.save(excel_buffer)

        _, _, project = self.publish(
            [
                ("docs/report.docx", word_buffer.getvalue()),
                ("docs/data.xlsx", excel_buffer.getvalue()),
            ]
        )
        items = {item["path"]: item for item in project["documents"]}
        self.assertEqual(
            items["docs/report.docx"]["blocks"][0]["text"], "First paragraph — exact."
        )
        self.assertEqual(
            items["docs/report.docx"]["blocks"][1]["rows"],
            [["Header", "Value"], ["α", "12,5"]],
        )
        cells = {
            cell["coordinate"]: cell["value"]
            for row in items["docs/data.xlsx"]["sheets"][0]["rows"]
            for cell in row["cells"]
        }
        self.assertEqual(cells, {"A1": "Label", "B2": 42, "C3": "=B2*2"})

    def test_presentation_epub_and_notebook_are_markdown_ready(self) -> None:
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "Greenhouse Review"
        text_box = slide.shapes.add_textbox(
            Inches(1), Inches(1.4), Inches(4), Inches(1)
        )
        text_box.text = "Humidity stayed inside the target range."
        table = slide.shapes.add_table(
            2, 2, Inches(1), Inches(2.5), Inches(4), Inches(1)
        ).table
        table.cell(0, 0).text = "Metric"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "VPD"
        table.cell(1, 1).text = "0.85 | kPa"
        chart_data = CategoryChartData()
        chart_data.categories = ["Day", "Night"]
        chart_data.add_series("Temperature", (24.5, 18.2))
        slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED,
            Inches(5.2),
            Inches(1.5),
            Inches(3),
            Inches(2.5),
            chart_data,
        )
        slide.notes_slide.notes_text_frame.text = "Check the east-zone sensor."
        presentation_buffer = io.BytesIO()
        presentation.save(presentation_buffer)

        epub_buffer = io.BytesIO()
        with zipfile.ZipFile(epub_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "META-INF/container.xml",
                """<?xml version="1.0"?>
                <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                  <rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>
                </container>""",
            )
            archive.writestr(
                "OEBPS/content.opf",
                """<?xml version="1.0"?>
                <package xmlns="http://www.idpf.org/2007/opf"
                  xmlns:dc="http://purl.org/dc/elements/1.1/">
                  <metadata><dc:title>Climate Notes</dc:title>
                    <dc:creator>Yavuz Selim</dc:creator>
                    <dc:language>en</dc:language></metadata>
                  <manifest><item id="chapter" href="chapter.xhtml"/></manifest>
                  <spine><itemref idref="chapter"/></spine>
                </package>""",
            )
            archive.writestr(
                "OEBPS/chapter.xhtml",
                """<html><body><h1>Ventilation</h1>
                <ul><li>Open the ridge vents</li></ul>
                <script>never publish this script</script></body></html>""",
            )

        notebook = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {
                "title": "Climate model",
                "language_info": {"name": "python"},
            },
            "cells": [
                {
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": ["# Analysis\n", "Exact Turkish: sıcaklık"],
                },
                {
                    "cell_type": "code",
                    "metadata": {},
                    "source": ["print('VPD')\n"],
                    "outputs": [{"output_type": "stream", "text": ["secret-output"]}],
                    "execution_count": 1,
                },
            ],
        }

        result, _, project = self.publish(
            [
                ("docs/review.pptx", presentation_buffer.getvalue()),
                ("docs/notes.epub", epub_buffer.getvalue()),
                ("analysis/model.ipynb", json.dumps(notebook).encode("utf-8")),
            ]
        )
        items = {item["path"]: item for item in project["documents"]}

        slides = items["docs/review.pptx"]
        self.assertEqual(slides["kind"], "presentation")
        self.assertEqual(slides["slide_count"], 1)
        self.assertEqual(slides["speaker_note_count"], 1)
        self.assertIn("# Greenhouse Review", slides["markdown"])
        self.assertIn("| VPD | 0.85 \\| kPa |", slides["markdown"])
        self.assertIn("### Chart", slides["markdown"])
        self.assertIn("Check the east-zone sensor.", slides["markdown"])

        epub = items["docs/notes.epub"]
        self.assertEqual(epub["kind"], "epub")
        self.assertEqual(epub["title"], "Climate Notes")
        self.assertEqual(epub["content_item_count"], 1)
        self.assertIn("# Ventilation", epub["markdown"])
        self.assertIn("* Open the ridge vents", epub["markdown"])
        self.assertNotIn("never publish this script", epub["markdown"])

        converted_notebook = items["analysis/model.ipynb"]
        self.assertEqual(converted_notebook["kind"], "notebook")
        self.assertEqual(converted_notebook["cell_count"], 2)
        self.assertEqual(converted_notebook["omitted_output_count"], 1)
        self.assertIn("Exact Turkish: sıcaklık", converted_notebook["markdown"])
        self.assertIn("```python", converted_notebook["markdown"])
        self.assertNotIn("secret-output", converted_notebook["markdown"])

        index = self.store.json_document(result["share_id"], "project.json")
        index_items = {item["path"]: item for item in index["documents"]}
        self.assertNotIn("markdown", index_items["docs/review.pptx"])
        self.assertEqual(index_items["docs/review.pptx"]["slide_count"], 1)
        self.assertEqual(index_items["analysis/model.ipynb"]["cell_count"], 2)

    def test_rich_document_archives_fail_closed(self) -> None:
        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("oversized.xml", b"0" * (2 * 1024 * 1024))
        _, _, project = self.publish([("docs/unsafe.pptx", unsafe.getvalue())])
        document = project["documents"][0]
        self.assertEqual(document["kind"], "extraction_error")
        self.assertIn("unsafe compression ratio", document["error"])

        malicious = io.BytesIO()
        with zipfile.ZipFile(malicious, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "META-INF/container.xml",
                """<?xml version="1.0"?>
                <!DOCTYPE container [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
                <container><rootfile full-path="&xxe;"/></container>""",
            )
        _, _, project = self.publish([("docs/malicious.epub", malicious.getvalue())])
        document = project["documents"][0]
        self.assertEqual(document["kind"], "extraction_error")
        self.assertIn("EntitiesForbidden", document["error"])

        traversal = io.BytesIO()
        with zipfile.ZipFile(traversal, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "META-INF/container.xml",
                "<container><rootfile "
                'full-path="OEBPS/%2e%2e/content.opf"/></container>',
            )
            archive.writestr("content.opf", "<package/>")
        _, _, project = self.publish([("docs/traversal.epub", traversal.getvalue())])
        document = project["documents"][0]
        self.assertEqual(document["kind"], "extraction_error")
        self.assertIn("unsafe member reference", document["error"])

    def test_epub_manifest_may_reference_parent_of_the_opf_folder(self) -> None:
        epub = io.BytesIO()
        with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "META-INF/container.xml",
                '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                "</rootfiles></container>",
            )
            archive.writestr(
                "OEBPS/content.opf",
                "<package><manifest>"
                '<item id="a" href="../Text/a%20b.xhtml"/>'
                '<item id="bad" href="../../outside.xhtml"/>'
                "</manifest></package>",
            )
            archive.writestr(
                "Text/a b.xhtml", "<html><body><h1>Parent</h1></body></html>"
            )
        _, _, project = self.publish([("docs/parent.epub", epub.getvalue())])
        document = project["documents"][0]
        self.assertEqual(document["kind"], "extraction_error")
        self.assertIn("unsafe member reference", document["error"])

        epub = io.BytesIO()
        with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "META-INF/container.xml",
                '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/>'
                "</rootfiles></container>",
            )
            archive.writestr(
                "OEBPS/content.opf",
                "<package><manifest>"
                '<item id="a" href="../Text/a%20b.xhtml"/></manifest>'
                '<spine><itemref idref="a"/></spine></package>',
            )
            archive.writestr(
                "Text/a b.xhtml", "<html><body><h1>Parent</h1></body></html>"
            )
        _, _, project = self.publish([("docs/parent.epub", epub.getvalue())])
        document = project["documents"][0]
        self.assertEqual(document["kind"], "epub")
        self.assertIn("# Parent", document["markdown"])

    def test_git_manifest_tolerates_unrepresentable_names_and_non_finite_numbers(
        self,
    ) -> None:
        result = self.store.publish(
            {
                "project_name": "Git edge cases",
                "git": {
                    "detected": True,
                    "tracked_paths": ["ok.py", "~$lock.docx", "a:b.txt", ".env"],
                    "history": [
                        {"sequence": float("inf"), "message": "inf"},
                        {"sequence": 1, "message": "one"},
                    ],
                    "commits": [
                        {
                            "sha": "a" * 40,
                            "author": {"name": "A", "timestamp": float("nan")},
                        }
                    ],
                },
            },
            [("ok.py", b"print(1)\n")],
        )
        git = self.store.json_document(result["share_id"], "git.json")["repository"]
        self.assertEqual(git["tracked_paths"], ["ok.py"])
        self.assertEqual(len(git["history"]), 2)
        commit = self.store.git_commit_page(result["share_id"], 1)["commits"][0]
        self.assertIsNone(commit["author"]["timestamp"])

    def test_protected_generated_and_unsupported_paths_fail_closed(self) -> None:
        for path in (
            ".env",
            "demo.env",
            "demo.env/config.txt",
            "secrets/private.pem",
            ".venv/config.txt",
            "logs/runtime.json",
            "archive.zip",
        ):
            with self.subTest(path=path), self.assertRaises(SnapshotError):
                self.publish([(path, b"secret")])

    def test_path_policy(self) -> None:
        self.assertEqual(safe_relative_path("src/main.py"), "src/main.py")
        self.assertEqual(
            safe_relative_path("Cafe\u0301/main.py"),
            "Cafe\u0301/main.py",
        )
        self.assertTrue(is_sensitive_path("config/demo.env"))
        self.assertFalse(is_sensitive_path(".env.example"))
        self.assertTrue(is_ignored_path(".pytest_cache/results.json"))
        for path in (
            ".",
            "src//main.py",
            "src/./main.py",
            "src/main.py/",
            "../secret",
            "/etc/passwd",
            "a\\b",
            "CON.txt",
            "src/a:b",
            "src/file. ",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_relative_path(path)
        for share_id in ("short", "../escape", "contains space"):
            with self.subTest(share_id=share_id), self.assertRaises(ValueError):
                validate_share_id(share_id)


if __name__ == "__main__":
    unittest.main()
