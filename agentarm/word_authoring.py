"""A bounded Markdown subset rendered as native Word paragraphs, runs and tables."""

import io

import markdown
from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.shared import Inches

ALLOWED_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "strong",
    "em",
    "b",
    "i",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "blockquote",
    "hr",
    "a",
    "br",
}


def _inline(paragraph, node, bold=False, italic=False, code=False):
    if isinstance(node, NavigableString):
        run = paragraph.add_run(str(node))
        run.bold, run.italic = True if bold else None, True if italic else None
        if code:
            run.font.name = "Consolas"
        return
    if not isinstance(node, Tag):
        return
    if node.name == "br":
        paragraph.add_run().add_break()
        return
    for child in node.children:
        _inline(
            paragraph,
            child,
            bold or node.name in {"b", "strong"},
            italic or node.name in {"i", "em"},
            code or node.name == "code",
        )
    if node.name == "a" and node.get("href"):
        paragraph.add_run(f" ({node['href']})")


def markdown_docx(content: str) -> bytes:
    soup = BeautifulSoup(
        markdown.markdown(content, extensions=["tables", "fenced_code", "sane_lists"]),
        "html.parser",
    )
    for node in soup.find_all(True):
        if node.name not in ALLOWED_TAGS:
            raise ValueError(
                f"Unsupported Word Markdown element: {node.name}; use headings, lists, tables and inline text"
            )
        if sum(1 for _ in node.parents) > 64:
            raise ValueError("Word Markdown exceeds the 64-level markup nesting budget")
        if node.get("rowspan", "1") != "1" or node.get("colspan", "1") != "1":
            raise ValueError("Word Markdown tables require unmerged rectangular cells")
    document = Document()

    def block(node, list_depth=0):
        if isinstance(node, NavigableString):
            if str(node).strip():
                document.add_paragraph(str(node))
            return
        if node.name in {"ul", "ol"}:
            for item in node.find_all("li", recursive=False):
                style = "List Number" if node.name == "ol" else "List Bullet"
                if 0 < list_depth < 3:
                    style += f" {list_depth + 1}"
                paragraph = document.add_paragraph(style=style)
                if list_depth >= 3:
                    paragraph.paragraph_format.left_indent = Inches(0.25 * list_depth)
                for child in item.children:
                    if isinstance(child, Tag) and child.name in {"ul", "ol"}:
                        block(child, list_depth + 1)
                        paragraph = None
                    else:
                        if (
                            paragraph is None
                            and isinstance(child, NavigableString)
                            and not str(child).strip()
                        ):
                            continue
                        if paragraph is None:
                            paragraph = document.add_paragraph(style="List Continue")
                            paragraph.paragraph_format.left_indent = Inches(
                                0.25 * list_depth
                            )
                        if (
                            isinstance(child, Tag)
                            and child.name == "p"
                            and paragraph.text
                        ):
                            paragraph.add_run().add_break()
                        _inline(paragraph, child)
            return
        if node.name == "table":
            rows = node.find_all("tr")
            cells = [row.find_all(["th", "td"], recursive=False) for row in rows]
            columns = max((len(row) for row in cells), default=0)
            if not columns or any(len(row) != columns for row in cells):
                raise ValueError(
                    "Word Markdown table must have rectangular nonempty rows"
                )
            table = document.add_table(rows=len(rows), cols=columns, style="Table Grid")
            for row_index, row in enumerate(cells):
                for column_index, cell in enumerate(row):
                    paragraph = table.cell(row_index, column_index).paragraphs[0]
                    for child in cell.children:
                        _inline(paragraph, child, bold=cell.name == "th")
            return
        if node.name == "blockquote":
            for child in node.children:
                if isinstance(child, Tag) and child.name == "p":
                    _inline(document.add_paragraph(style="Quote"), child)
                else:
                    block(child, list_depth)
            return
        if node.name == "pre":
            run = document.add_paragraph(style="No Spacing").add_run(node.get_text())
            run.font.name = "Consolas"
            return
        if node.name == "hr":
            document.add_paragraph("—")
            return
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            paragraph = document.add_paragraph(style=f"Heading {node.name[1]}")
        else:
            paragraph = document.add_paragraph()
        _inline(paragraph, node)

    for node in soup.children:
        block(node)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()
