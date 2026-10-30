"""Checked edits to existing document containers, sharing Workspace writes/undo."""

from __future__ import annotations

import difflib
import io
import itertools
import json
import zipfile
from pathlib import Path

from .snapshots import _validate_archive
from .workspace import Workspace, digest, text_line_window

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _parse_word_xml(data: bytes):
    from lxml import etree

    try:
        root = etree.fromstring(
            data,
            parser=etree.XMLParser(
                resolve_entities=False, no_network=True, load_dtd=False, recover=False
            ),
        )
    except etree.XMLSyntaxError as exc:
        raise ValueError("Word part contains invalid XML") from exc
    if root.getroottree().docinfo.doctype:
        raise ValueError("Word XML with a DOCTYPE is not supported")
    return root


def _word_part(source, part: str):
    if (
        not part.startswith("word/")
        or not part.endswith(".xml")
        or "\\" in part
        or ":" in part
        or any(piece in {"", ".", ".."} for piece in part.split("/"))
    ):
        raise ValueError("Choose a regular word/*.xml part listed by read_word")
    names = source.namelist()
    if len(names) != len(set(names)):
        raise ValueError("Word archive contains duplicate member names")
    if part not in names:
        raise ValueError("Word XML part not found")
    return _parse_word_xml(source.read(part))


def read_word(
    workspace: Workspace,
    path: str,
    mode: str = "outline",
    part: str = "word/document.xml",
    offset: int = 0,
    limit: int = 100,
    expected_sha256: str | None = None,
) -> dict:
    """Page a text-bearing body outline or the exact normalized XML used by edits."""
    from lxml import etree

    if Path(path).suffix.lower() != ".docx" or mode not in {
        "outline",
        "xml",
        "xml_chars",
        "parts",
    }:
        raise ValueError("Use a .docx with outline/xml/xml_chars/parts mode")
    data = workspace.bytes(path)
    source_hash = digest(data)
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise ValueError("SHA-256 conflict: Word file changed between windows")
    _validate_archive(data, "Word document")
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        result = {"path": path, "sha256": source_hash, "part": part, "mode": mode}
        if mode == "parts":
            if offset < 0 or not 1 <= limit <= 2000:
                raise ValueError("Use a parts offset >= 0 and limit 1..2000")
            names = sorted(
                name
                for name in source.namelist()
                if name.startswith("word/") and name.endswith(".xml")
            )
            entries = []
            budget = workspace.policy.max_output_chars
            for name in names[offset : offset + limit]:
                cost = len(json.dumps(name, ensure_ascii=False))
                if cost > budget:
                    if not entries:
                        raise ValueError("Word part name exceeds the output budget")
                    break
                entries.append(name)
                budget -= cost
            end = offset + len(entries)
            return {
                **result,
                "parts": entries,
                "total_parts": len(names),
                "next_offset": end if end < len(names) else None,
            }
        root = _word_part(source, part)
        if mode in {"xml", "xml_chars"}:
            text = etree.tostring(root, encoding="unicode", pretty_print=True)
            if mode == "xml_chars":
                if offset < 0 or not 1 <= limit <= workspace.policy.max_output_chars:
                    raise ValueError(
                        "Use a character offset >= 0 and limit within output budget"
                    )
                end = min(len(text), offset + limit)
                return {
                    **result,
                    "offset": offset,
                    "content": text[offset:end],
                    "total_chars": len(text),
                    "next_offset": end if end < len(text) else None,
                }
            try:
                window = text_line_window(
                    text, offset, limit, workspace.policy.max_output_chars
                )
            except ValueError as exc:
                raise ValueError(
                    "Invalid XML line window or long line; use xml_chars for character paging"
                ) from exc
            return {
                **result,
                **window,
            }
        if not 0 <= offset or not 1 <= limit <= 2000:
            raise ValueError("Use an outline offset >= 0 and limit 1..2000")
        if root.tag != WORD_NS + "document":
            raise ValueError(
                "Outline mode requires the main document part; use xml for other parts"
            )
        body = root.find(WORD_NS + "body")
        if body is None:
            raise ValueError("Word document has no body")
        entries = []
        position = min(offset, len(body))
        budget = workspace.policy.max_output_chars - len(
            json.dumps(result, ensure_ascii=False)
        )
        for index in range(position, min(len(body), position + limit)):
            child = body[index]
            text = "\n".join(
                "".join(node.text or "" for node in paragraph.iter(WORD_NS + "t"))
                for paragraph in child.iter(WORD_NS + "p")
            )
            style = child.find(WORD_NS + "pPr/" + WORD_NS + "pStyle")
            image_attribute = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
            images = list(
                itertools.islice(
                    (
                        node.get(image_attribute)
                        for node in child.iter()
                        if node.get(image_attribute)
                    ),
                    21,
                )
            )
            entry = {
                "body_index": index,
                "type": etree.QName(child).localname
                if isinstance(child.tag, str)
                else "comment",
                "text": text[:2000],
                "text_truncated": len(text) > 2000,
                "style": style.get(WORD_NS + "val") if style is not None else None,
                "image_refs": images[:20],
                "image_refs_truncated": len(images) > 20,
            }
            cost = len(json.dumps(entry, ensure_ascii=False))
            if cost > budget:
                if not entries:
                    raise ValueError(
                        "Outline entry exceeds the output budget; use xml windows"
                    )
                break
            entries.append(entry)
            position = index + 1
            budget -= cost
        return {
            **result,
            "entries": entries,
            "total_blocks": len(body),
            "next_offset": position if position < len(body) else None,
        }


def edit_word_xml(
    workspace: Workspace,
    path: str,
    part: str,
    old_string: str,
    new_string: str,
    base_sha256: str,
    expected_replacements: int = 1,
    preview: bool = True,
) -> dict:
    """Replace exact fragments from read_word's normalized XML, checking structure before writing."""
    from lxml import etree

    if (
        Path(path).suffix.lower() != ".docx"
        or not old_string
        or not 1 <= expected_replacements <= 1000
        or len(old_string) + len(new_string) > workspace.policy.max_output_chars
    ):
        raise ValueError("Use .docx, a bounded nonempty XML fragment and count 1..1000")
    if not preview:
        workspace.writable()
    with workspace.lock:
        original = workspace._current(path, base_sha256)
        if original is None:
            raise FileNotFoundError(path)
        _validate_archive(original, "Word document")
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original)) as source:
            root = _word_part(source, part)
            text = etree.tostring(root, encoding="unicode", pretty_print=True)
            count = text.count(old_string)
            if count != expected_replacements:
                raise ValueError(
                    f"Replacement count mismatch: found {count}, expected {expected_replacements}"
                )
            changed = text.replace(old_string, new_string)
            updated = _parse_word_xml(changed.encode("utf-8"))
            if root.tag != updated.tag:
                raise ValueError("The Word XML part root must remain unchanged")
            if (
                root.tag == WORD_NS + "document"
                and len(updated.findall(WORD_NS + "body")) != 1
            ):
                raise ValueError("The main Word document must contain exactly one body")
            if preview:
                pieces = []
                remaining = workspace.policy.max_output_chars
                truncated = False
                for line in difflib.unified_diff(
                    text.splitlines(True),
                    changed.splitlines(True),
                    fromfile=part,
                    tofile=part,
                ):
                    pieces.append(line[:remaining])
                    remaining -= min(remaining, len(line))
                    if not remaining:
                        truncated = True
                        break
                return {
                    "preview": True,
                    "part": part,
                    "replacements": count,
                    "base_sha256": base_sha256,
                    "diff": "".join(pieces),
                    "diff_truncated": truncated,
                }
            replacement = etree.tostring(
                updated, encoding="UTF-8", xml_declaration=True, standalone=True
            )
            if len(replacement) > workspace.policy.max_file_bytes:
                raise ValueError("Edited XML part exceeds the file budget")
            with zipfile.ZipFile(output, "w") as target:
                for member in source.infolist():
                    target.writestr(
                        member,
                        replacement if member.filename == part else source.read(member),
                    )
                    if output.tell() > workspace.policy.max_file_bytes:
                        raise ValueError("Edited document exceeds the file budget")
        return {
            **workspace.write_bytes(path, output.getvalue(), base_sha256),
            "part": part,
            "replacements": count,
        }


def _text_segments(paragraph):
    """Do not join text across explicit Word separators or nested paragraphs."""
    nodes = []
    boundaries = {
        WORD_NS + name
        for name in ("tab", "br", "cr", "fldChar", "instrText", "drawing")
    }
    for node in paragraph.iter():
        if next(node.iterancestors(WORD_NS + "p"), None) is not paragraph:
            continue
        if node.tag in boundaries:
            if nodes:
                yield nodes
                nodes = []
        elif node.tag == WORD_NS + "t":
            nodes.append(node)
    if nodes:
        yield nodes


def _replace_text_nodes(nodes, old_string: str, new_string: str) -> int:
    text = "".join(node.text or "" for node in nodes)
    occurrences = []
    cursor = 0
    while (index := text.find(old_string, cursor)) >= 0:
        occurrences.append(index)
        if len(occurrences) > 1000:
            raise ValueError("More than 1000 replacements; narrow the search")
        cursor = index + len(old_string)
    # Later replacements cannot change the offsets of earlier matches.
    for start in reversed(occurrences):
        end = start + len(old_string)
        offset = 0
        first = True
        for node in nodes:
            value = node.text or ""
            node_start, node_end = offset, offset + len(value)
            offset = node_end
            if node_end <= start or node_start >= end:
                continue
            prefix = value[: max(0, start - node_start)]
            suffix = value[max(0, end - node_start) :] if end < node_end else ""
            node.text = prefix + (new_string if first else "") + suffix
            node.set(XML_SPACE, "preserve")
            first = False
    return len(occurrences)


def edit_word(
    workspace: Workspace,
    path: str,
    old_string: str,
    new_string: str,
    base_sha256: str,
    expected_replacements: int = 1,
    preview: bool = True,
) -> dict:
    """Replace text across runs within paragraphs, including tables/headers/footnotes.

    Inserted text takes the formatting of its first replaced text node. Paragraph
    boundaries, field instructions, tabs and explicit breaks are not searchable text.
    """
    from lxml import etree

    if (
        Path(path).suffix.lower() != ".docx"
        or not old_string
        or not 1 <= expected_replacements <= 1000
        or len(old_string) + len(new_string) > workspace.policy.max_output_chars
    ):
        raise ValueError(
            "Use .docx, nonempty search text, a bounded replacement and count 1..1000"
        )
    if not preview:
        workspace.writable()
    with workspace.lock:
        original = workspace._current(path, base_sha256)
        if original is None:
            raise FileNotFoundError(path)
        _validate_archive(original, "Word document")
        output = io.BytesIO()
        count = 0
        changed_parts = []
        with (
            zipfile.ZipFile(io.BytesIO(original)) as source,
            zipfile.ZipFile(output, "w") as target,
        ):
            for member in source.infolist():
                data = source.read(member)
                if member.filename.startswith("word/") and member.filename.endswith(
                    ".xml"
                ):
                    root = _parse_word_xml(data)
                    changed = False
                    for paragraph in root.iter(WORD_NS + "p"):
                        for nodes in _text_segments(paragraph):
                            replacements = _replace_text_nodes(
                                nodes, old_string, new_string
                            )
                            count += replacements
                            changed = changed or bool(replacements)
                            if count > 1000:
                                raise ValueError(
                                    "More than 1000 replacements; narrow the search"
                                )
                    if changed:
                        changed_parts.append(member.filename)
                        data = etree.tostring(
                            root,
                            encoding="UTF-8",
                            xml_declaration=True,
                            standalone=True,
                        )
                target.writestr(member, data)
                if output.tell() > workspace.policy.max_file_bytes:
                    raise ValueError("Edited document exceeds file budget")
        if count != expected_replacements:
            raise ValueError(
                f"Replacement count mismatch: found {count}, expected {expected_replacements}"
            )
        if preview:
            return {
                "preview": True,
                "replacements": count,
                "parts": changed_parts,
                "base_sha256": base_sha256,
                "formatting": "Replacement inherits first matched text-node formatting",
            }
        return {
            **workspace.write_bytes(path, output.getvalue(), base_sha256),
            "replacements": count,
            "parts": changed_parts,
        }


def edit_pdf(
    workspace: Workspace,
    path: str,
    action: str,
    page_indexes: list[int],
    base_sha256: str,
    source_path: str | None = None,
    insert_at: int = 0,
    rotation: int = 90,
) -> dict:
    """Modify zero-based PDF pages; source order is explicit for inserted pages."""
    from pypdf import PdfReader, PdfWriter

    workspace.writable()
    if Path(path).suffix.lower() != ".pdf" or action not in {
        "delete",
        "insert",
        "rotate",
    }:
        raise ValueError("Use a .pdf and delete/insert/rotate action")
    if (
        not page_indexes
        or len(page_indexes) > 1000
        or len(set(page_indexes)) != len(page_indexes)
    ):
        raise ValueError("Use 1..1000 unique zero-based page indexes")
    if any(type(index) is not int or index < 0 for index in page_indexes):
        raise ValueError("Page indexes must be nonnegative integers")
    if rotation % 90:
        raise ValueError("PDF rotation must be a multiple of 90 degrees")
    with workspace.lock:
        original = workspace._current(path, base_sha256)
        if original is None:
            raise FileNotFoundError(path)
        reader = PdfReader(io.BytesIO(original))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported")
        if len(reader.pages) > 2000:
            raise ValueError("PDF page budget is 2000")
        source = reader
        if action == "insert":
            if source_path is None or not 0 <= insert_at <= len(reader.pages):
                raise ValueError(
                    "Insert requires source_path and a valid insertion index"
                )
            source = PdfReader(io.BytesIO(workspace.bytes(source_path)))
            if source.is_encrypted or len(source.pages) > 2000:
                raise ValueError(
                    "Source PDF must be unencrypted and within the page budget"
                )
            if len(reader.pages) + len(page_indexes) > 2000:
                raise ValueError("Result exceeds the PDF page budget")
        if max(page_indexes) >= len(source.pages):
            raise ValueError("Page index out of range")
        if action == "delete" and len(page_indexes) == len(reader.pages):
            raise ValueError("Cannot delete every page")
        writer = PdfWriter(clone_from=reader)
        try:
            if action == "delete":
                for index in sorted(page_indexes, reverse=True):
                    del writer.pages[index]
            elif action == "rotate":
                for index in page_indexes:
                    writer.pages[index].rotate(rotation)
            else:
                for sequence, index in enumerate(page_indexes):
                    writer.insert_page(source.pages[index], insert_at + sequence)
            output = io.BytesIO()
            writer.write(output)
            return {
                **workspace.write_bytes(path, output.getvalue(), base_sha256),
                "page_count": len(writer.pages),
                "action": action,
            }
        finally:
            writer.close()
