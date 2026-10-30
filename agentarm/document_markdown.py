from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urldefrag

RICH_DOCUMENT_EXTENSIONS = {".epub", ".ipynb", ".pptx"}
ARCHIVE_DOCUMENT_EXTENSIONS = {".epub", ".pptx"}

MAX_EXTRACTED_MARKDOWN_BYTES = 24 * 1024 * 1024
MAX_EPUB_SPINE_ITEMS = 10_000
MAX_NOTEBOOK_CELLS = 100_000
MAX_PRESENTATION_SLIDES = 2_000
MAX_PRESENTATION_SHAPES = 100_000
MAX_PRESENTATION_TABLE_CELLS = 250_000
MAX_PRESENTATION_CHART_POINTS = 250_000
MAX_GROUP_DEPTH = 32


class RichDocumentError(ValueError):
    pass


class _MarkdownWriter:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._bytes = 0

    def append(self, value: str | None) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            return
        fragment = ("\n\n" if self._parts else "") + normalized
        fragment_bytes = len(fragment.encode("utf-8"))
        if self._bytes + fragment_bytes > MAX_EXTRACTED_MARKDOWN_BYTES:
            raise RichDocumentError(
                "Extracted Markdown exceeds the 24 MiB safety limit"
            )
        self._parts.append(fragment)
        self._bytes += fragment_bytes

    def value(self) -> str:
        return "".join(self._parts)


def extract_rich_document(suffix: str, data: bytes) -> dict[str, Any]:
    if suffix == ".pptx":
        return _presentation_document(data)
    if suffix == ".epub":
        return _epub_document(data)
    if suffix == ".ipynb":
        return _notebook_document(data)
    raise RichDocumentError(f"Unsupported rich document type: {suffix}")


def _presentation_document(data: bytes) -> dict[str, Any]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    presentation = Presentation(io.BytesIO(data))
    slides = list(presentation.slides)
    if len(slides) > MAX_PRESENTATION_SLIDES:
        raise RichDocumentError("Presentation exceeds 2,000 slides")

    writer = _MarkdownWriter()
    counters: Counter[str] = Counter()
    for sequence, slide in enumerate(slides, start=1):
        writer.append(f"<!-- Slide {sequence} -->")
        title_shape = slide.shapes.title
        for shape in _ordered_shapes(slide.shapes):
            for fragment in _presentation_shape_fragments(
                shape,
                title_shape=title_shape,
                counters=counters,
                shape_types=MSO_SHAPE_TYPE,
                depth=0,
            ):
                writer.append(fragment)
        if slide.has_notes_slide:
            notes_frame = slide.notes_slide.notes_text_frame
            notes = notes_frame.text.strip() if notes_frame is not None else ""
            if notes:
                counters["notes"] += 1
                writer.append(f"### Speaker notes\n\n{notes}")

    return {
        "kind": "presentation",
        "markdown": writer.value(),
        "slide_count": len(slides),
        "shape_count": counters["shapes"],
        "speaker_note_count": counters["notes"],
        "table_count": counters["tables"],
        "chart_count": counters["charts"],
        "image_count": counters["images"],
    }


def _ordered_shapes(shapes: Any) -> list[Any]:
    return sorted(
        shapes,
        key=lambda shape: (
            int(getattr(shape, "top", 0) or 0),
            int(getattr(shape, "left", 0) or 0),
        ),
    )


def _presentation_shape_fragments(
    shape: Any,
    *,
    title_shape: Any,
    counters: Counter[str],
    shape_types: Any,
    depth: int,
) -> list[str]:
    if depth > MAX_GROUP_DEPTH:
        raise RichDocumentError("Presentation shape groups are nested too deeply")
    counters["shapes"] += 1
    if counters["shapes"] > MAX_PRESENTATION_SHAPES:
        raise RichDocumentError("Presentation exceeds 100,000 shapes")

    if shape.shape_type == shape_types.GROUP:
        fragments: list[str] = []
        for child in _ordered_shapes(shape.shapes):
            fragments.extend(
                _presentation_shape_fragments(
                    child,
                    title_shape=title_shape,
                    counters=counters,
                    shape_types=shape_types,
                    depth=depth + 1,
                )
            )
        return fragments

    fragments = []
    if getattr(shape, "has_table", False):
        counters["tables"] += 1
        rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
        counters["table_cells"] += sum(len(row) for row in rows)
        if counters["table_cells"] > MAX_PRESENTATION_TABLE_CELLS:
            raise RichDocumentError("Presentation exceeds 250,000 table cells")
        fragments.append(_markdown_table(rows))

    if getattr(shape, "has_chart", False):
        counters["charts"] += 1
        fragments.append(_chart_markdown(shape.chart, counters))
    elif getattr(shape, "has_text_frame", False):
        text = shape.text.strip()
        if text:
            fragments.append(f"# {text}" if shape == title_shape else text)

    if _is_picture(shape, shape_types):
        counters["images"] += 1
        description = _picture_description(shape)
        fragments.append(f"> Embedded image: {description}")
    return fragments


def _is_picture(shape: Any, shape_types: Any) -> bool:
    if shape.shape_type == shape_types.PICTURE:
        return True
    if shape.shape_type != shape_types.PLACEHOLDER:
        return False
    try:
        return shape.image is not None
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _picture_description(shape: Any) -> str:
    description = ""
    try:
        properties = shape._element._nvXxPr.cNvPr
        description = properties.attrib.get("descr", "")
    except (AttributeError, KeyError, TypeError):
        description = ""
    return re.sub(r"\s+", " ", description or shape.name or "image").strip()


def _chart_markdown(chart: Any, counters: Counter[str]) -> str:
    title = ""
    try:
        if chart.has_title:
            title = chart.chart_title.text_frame.text.strip()
        categories = [str(category.label) for category in chart.plots[0].categories]
        series = list(chart.series)
        values = [list(item.values) for item in series]
        counters["chart_points"] += sum(len(series_values) for series_values in values)
        if counters["chart_points"] > MAX_PRESENTATION_CHART_POINTS:
            raise RichDocumentError("Presentation exceeds 250,000 chart points")
        rows: list[list[Any]] = [["Category", *[item.name for item in series]]]
        for index, category in enumerate(categories):
            rows.append(
                [
                    category,
                    *[
                        series_values[index] if index < len(series_values) else None
                        for series_values in values
                    ],
                ]
            )
        heading = f"### Chart: {title}" if title else "### Chart"
        return f"{heading}\n\n{_markdown_table(rows)}"
    except RichDocumentError:
        raise
    except Exception:  # noqa: BLE001 - python-pptx chart variants differ.
        return "### Chart\n\n[Chart data could not be extracted safely.]"


def _markdown_table(rows: list[list[Any]]) -> str:
    if not rows:
        return "[Empty table]"
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = "| " + " | ".join(_markdown_cell(value) for value in normalized[0]) + " |"
    separator = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = [
        "| " + " | ".join(_markdown_cell(value) for value in row) + " |"
        for row in normalized[1:]
    ]
    return "\n".join([header, separator, *body])


def _markdown_cell(value: Any) -> str:
    return (
        str("" if value is None else value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def _epub_document(data: bytes) -> dict[str, Any]:
    from bs4 import BeautifulSoup
    from defusedxml import ElementTree as SafeElementTree
    from markdownify import markdownify

    writer = _MarkdownWriter()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        container = SafeElementTree.fromstring(
            _read_archive_member(archive, "META-INF/container.xml")
        )
        rootfile = next(
            (node for node in container.iter() if _local_name(node.tag) == "rootfile"),
            None,
        )
        if rootfile is None:
            raise RichDocumentError("EPUB container has no rootfile")
        opf_path = _safe_archive_member_name(rootfile.attrib.get("full-path", ""))
        package = SafeElementTree.fromstring(_read_archive_member(archive, opf_path))
        metadata = _epub_metadata(package)
        manifest = {}
        for node in package.iter():
            if _local_name(node.tag) != "item":
                continue
            identifier = node.attrib.get("id")
            href = node.attrib.get("href")
            if identifier and href:
                manifest[identifier] = _joined_archive_member(opf_path, href)
        spine_ids = [
            node.attrib.get("idref", "")
            for node in package.iter()
            if _local_name(node.tag) == "itemref"
        ]
        if len(spine_ids) > MAX_EPUB_SPINE_ITEMS:
            raise RichDocumentError("EPUB exceeds 10,000 spine items")

        title = metadata.get("title")
        if title:
            writer.append(f"# {title}")
        details = []
        for key in ("authors", "language", "publisher", "date", "description"):
            value = metadata.get(key)
            if isinstance(value, list):
                value = ", ".join(value)
            if value:
                details.append(f"**{key.capitalize()}:** {value}")
        writer.append("\n".join(details))

        included = 0
        for identifier in spine_ids:
            member = manifest.get(identifier)
            if not member or PurePosixPath(member).suffix.casefold() not in {
                ".htm",
                ".html",
                ".xhtml",
            }:
                continue
            markup = _decode_markup(_read_archive_member(archive, member))
            document = BeautifulSoup(markup, "html.parser")
            for unsafe_node in document.find_all(["script", "style"]):
                unsafe_node.decompose()
            converted = markdownify(
                str(document),
                heading_style="ATX",
            )
            converted = re.sub(r"\n{3,}", "\n\n", converted).strip()
            if converted:
                included += 1
                writer.append(f"<!-- EPUB item: {member} -->\n\n{converted}")

    return {
        "kind": "epub",
        "markdown": writer.value(),
        "title": metadata.get("title"),
        "metadata": metadata,
        "spine_item_count": len(spine_ids),
        "content_item_count": included,
    }


def _epub_metadata(package: Any) -> dict[str, Any]:
    values: dict[str, list[str]] = {
        "title": [],
        "authors": [],
        "language": [],
        "publisher": [],
        "date": [],
        "description": [],
        "identifier": [],
    }
    mapping = {"creator": "authors"}
    for node in package.iter():
        key = mapping.get(_local_name(node.tag), _local_name(node.tag))
        text = "".join(node.itertext()).strip()
        if key in values and text:
            values[key].append(text)
    return {
        "title": values["title"][0] if values["title"] else None,
        "authors": values["authors"],
        "language": values["language"][0] if values["language"] else None,
        "publisher": values["publisher"][0] if values["publisher"] else None,
        "date": values["date"][0] if values["date"] else None,
        "description": values["description"][0] if values["description"] else None,
        "identifier": values["identifier"][0] if values["identifier"] else None,
    }


def _read_archive_member(archive: zipfile.ZipFile, name: str) -> bytes:
    safe_name = _safe_archive_member_name(name)
    try:
        return archive.read(safe_name)
    except KeyError as exc:
        raise RichDocumentError(f"Archive member is missing: {safe_name}") from exc


def _safe_archive_member_name(value: str) -> str:
    decoded = unquote(urldefrag(str(value or ""))[0]).replace("\\", "/")
    normalized = posixpath.normpath(decoded)
    if (
        normalized in {"", ".", ".."}
        or decoded.startswith("/")
        or ".." in decoded.split("/")
        or normalized.startswith("../")
    ):
        raise RichDocumentError("Archive contains an unsafe member reference")
    return normalized


def _joined_archive_member(opf_path: str, href: str) -> str:
    parent = posixpath.dirname(opf_path)
    target = urldefrag(href)[0].replace("\\", "/")
    # Manifest hrefs may climb out of the OPF folder ("../Text/a.xhtml"). Resolve
    # them first; the safety check still rejects anything leaving the archive.
    return _safe_archive_member_name(posixpath.normpath(posixpath.join(parent, target)))


def _local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].split(":", 1)[-1]


def _decode_markup(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    declaration = data[:256].decode("ascii", errors="ignore")
    match = re.search(r"encoding=[\"']([^\"']+)", declaration, re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return data.decode("windows-1252")


def _notebook_document(data: bytes) -> dict[str, Any]:
    notebook = json.loads(data.decode("utf-8-sig"))
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise RichDocumentError("Notebook must contain a cells array")
    cells = notebook["cells"]
    if len(cells) > MAX_NOTEBOOK_CELLS:
        raise RichDocumentError("Notebook exceeds 100,000 cells")

    metadata = notebook.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    language = _notebook_language(metadata)
    writer = _MarkdownWriter()
    counts: Counter[str] = Counter()
    omitted_outputs = 0
    for sequence, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("cell_type") or "unknown")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(str(item) for item in source)
        elif not isinstance(source, str):
            source = str(source)
        counts[cell_type] += 1
        outputs = cell.get("outputs")
        if isinstance(outputs, list):
            omitted_outputs += len(outputs)
        marker = f"<!-- Notebook cell {sequence}: {cell_type} -->"
        if cell_type == "markdown":
            writer.append(f"{marker}\n\n{source}")
        elif cell_type == "code":
            writer.append(f"{marker}\n\n{_fenced_code(source, language)}")
        elif cell_type == "raw":
            writer.append(f"{marker}\n\n{_fenced_code(source, '')}")

    title = metadata.get("title")
    return {
        "kind": "notebook",
        "markdown": writer.value(),
        "title": str(title) if title is not None else None,
        "cell_count": len(cells),
        "cell_types": dict(sorted(counts.items())),
        "omitted_output_count": omitted_outputs,
        "language": language or None,
    }


def _notebook_language(metadata: dict[str, Any]) -> str:
    language_info = metadata.get("language_info")
    if isinstance(language_info, dict) and language_info.get("name"):
        value = str(language_info["name"])
    else:
        kernelspec = metadata.get("kernelspec")
        value = (
            str(kernelspec.get("language") or "")
            if isinstance(kernelspec, dict)
            else ""
        )
    return re.sub(r"[^A-Za-z0-9_+.-]", "", value)[:80]


def _fenced_code(source: str, language: str) -> str:
    longest = max(
        (len(match.group(0)) for match in re.finditer(r"`+", source)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{source.rstrip()}\n{fence}"
