"""Rich PDF authoring and visual reading through bounded, owned helper processes."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from .helpers import run_helper
from .workspace import Workspace, digest

DEFAULT_CSS = """
body { font: 11pt Arial, sans-serif; color: #172b3a; line-height: 1.5; }
h1,h2,h3 { line-height: 1.2; break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th,td { border: 1px solid #bac8ce; padding: 7px 10px; vertical-align: top; }
th { background: #eaf1f4; text-align: left; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: #f0f4f5; }
img,svg { max-width: 100%; }
a { color: #186b87; }
"""


def _prepare_html(
    workspace: Workspace, content: str, source_format: str, css: str
) -> str:
    import markdown
    from bs4 import BeautifulSoup

    if len(content) + len(css) > workspace.policy.max_output_chars:
        raise ValueError("PDF markup and CSS exceed the character budget")
    if source_format not in {"markdown", "html"}:
        raise ValueError("source_format must be markdown or html")
    body = (
        markdown.markdown(content, extensions=["tables", "fenced_code", "sane_lists"])
        if source_format == "markdown"
        else content
    )
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.find_all(
        ["script", "iframe", "object", "embed", "base", "meta", "link"]
    ):
        node.decompose()
    total_assets = 0
    for node in soup.find_all("img"):
        source = node.get("src", "")
        if source.startswith("data:"):
            # Inline data is already bounded by the markup input budget.
            continue
        if not source or ":" in source or source.startswith("/"):
            raise ValueError(
                "PDF images must use eligible relative workspace paths or inline data"
            )
        data = workspace.bytes(source)
        total_assets += len(data)
        if (
            total_assets > workspace.policy.max_file_bytes
            or len(data) > 4 * 1024 * 1024
        ):
            raise ValueError("PDF image assets exceed the bounded file/image budget")
        if Path(source).suffix.lower() == ".svg":
            from defusedxml.ElementTree import fromstring

            fromstring(data)
            mime = "image/svg+xml"
        else:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                image.verify()
                if image.width * image.height > 16_000_000:
                    raise ValueError("PDF image exceeds the 16 megapixel decode budget")
                mime = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "GIF": "image/gif",
                    "WEBP": "image/webp",
                }.get(image.format)
            if mime is None:
                raise ValueError("Use PNG, JPEG, GIF, WebP or SVG PDF image assets")
        node["src"] = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        node.attrs.pop("srcset", None)
    policy = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'"
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        f"<style>{DEFAULT_CSS}\n{css}</style></head><body>{soup}</body></html>"
    )


def _worker(workspace: Workspace, request: dict, max_output: int) -> dict:
    return run_helper(workspace, "pdf_worker.py", request, max_output)


def write_pdf(
    workspace: Workspace,
    path: str,
    content: str,
    base_sha256: str | None,
    source_format: str = "markdown",
    css: str = "",
    page_size: str = "A4",
    landscape: bool = False,
) -> dict:
    workspace.writable()
    if Path(path).suffix.lower() != ".pdf" or page_size not in {"A4", "Letter"}:
        raise ValueError("Use a .pdf path and A4/Letter page size")
    workspace._current(path, base_sha256)
    html = _prepare_html(workspace, content, source_format, css)
    result = _worker(
        workspace,
        {
            "mode": "create",
            "html": html,
            "page_size": page_size,
            "landscape": landscape,
        },
        workspace.policy.max_file_bytes * 4 // 3 + 16384,
    )
    data = base64.b64decode(result.pop("pdf"), validate=True)
    return {**workspace.write_bytes(path, data, base_sha256), **result}


def render_pdf_page(
    workspace: Workspace, path: str, page_index: int = 0, scale: float = 1
) -> dict:
    data = workspace.bytes(path)
    result = _worker(
        workspace,
        {
            "mode": "preview",
            "pdf": base64.b64encode(data).decode("ascii"),
            "page_index": page_index,
            "scale": scale,
        },
        4 * 1024 * 1024 * 4 // 3 + 16384,
    )
    return {**result, "path": path, "sha256": digest(data)}


def read_pdf(
    workspace: Workspace,
    path: str,
    page_index: int = 0,
    mode: str = "text",
    offset: int = 0,
    length: int = 16000,
    image_index: int | None = None,
    expected_sha256: str | None = None,
) -> dict:
    if (
        mode not in {"text", "images"}
        or type(page_index) is not int
        or page_index < 0
        or type(offset) is not int
        or offset < 0
        or type(length) is not int
        or not 1 <= length <= workspace.policy.max_output_chars
        or (
            image_index is not None
            and (type(image_index) is not int or image_index < 0)
        )
        or (mode == "text" and image_index is not None)
    ):
        raise ValueError(
            "Use text/images mode, nonnegative integer page/offset/image index and a valid character window"
        )
    data = workspace.bytes(path)
    source_hash = digest(data)
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise ValueError("SHA-256 conflict: PDF changed between windows")
    return _worker(
        workspace,
        {
            "mode": "inspect",
            "pdf": base64.b64encode(data).decode("ascii"),
            "page_index": page_index,
            "read_mode": mode,
            "offset": offset,
            "length": length,
            "image_index": image_index,
            "max_output_chars": workspace.policy.max_output_chars,
            "identity": {"path": path, "sha256": source_hash},
        },
        4 * 1024 * 1024 * 4 // 3 + workspace.policy.max_output_chars * 6 + 16384,
    )
