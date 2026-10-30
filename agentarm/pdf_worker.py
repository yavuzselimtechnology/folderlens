"""Fixed-purpose isolated PDF worker. Input/output are bounded JSON over pipes."""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import sys
import tempfile
from pathlib import Path

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PAGE_PIXELS = 4_000_000
MAX_PAGES = 2000


def inspect_pdf(request: dict) -> dict:
    from pypdf import PdfReader

    logger = logging.getLogger("pypdf")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    data = base64.b64decode(request["pdf"], validate=True)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds file budget")
    book = PdfReader(io.BytesIO(data), strict=True)
    try:
        if book.is_encrypted:
            raise ValueError(
                "Encrypted PDF requires a separate owner decryption workflow"
            )
        count = len(book.pages)
        index = request["page_index"]
        if not 0 < count <= MAX_PAGES or not 0 <= index < count:
            raise ValueError("Invalid PDF page index or page count exceeds 2000")
        page = book.pages[index]
        result = {
            **request["identity"],
            "page_index": index,
            "page_count": count,
            "next_page_index": index + 1 if index + 1 < count else None,
            "engine": "pypdf",
        }
        budget = request["max_output_chars"]
        if request["read_mode"] == "text":
            text = page.extract_text() or ""
            start = min(request["offset"], len(text))
            result.update(
                representation="pdf_page_text",
                ocr=False,
                layout_preserved=False,
                offset=start,
                total_chars=len(text),
            )

            def window(length):
                end = start + length
                result.update(
                    content=text[start:end],
                    next_offset=end if end < len(text) else None,
                )
                return len(json.dumps(result, ensure_ascii=False)) <= budget

            low, high = 0, min(request["length"], len(text) - start)
            while low < high:
                middle = (low + high + 1) // 2
                if window(middle):
                    low = middle
                else:
                    high = middle - 1
            if not window(low) or (low == 0 and start < len(text)):
                raise ValueError(
                    "PDF text metadata exceeds output budget; use a larger local budget"
                )
            return result
        images = page.images
        keys = images.keys()
        result.update(representation="pdf_embedded_images", total_images=len(keys))
        selected = request["image_index"]
        if selected is None:
            start = min(request["offset"], len(keys))
            result.update(images=[], offset=start, next_offset=None)
            if len(json.dumps(result, ensure_ascii=False)) > min(
                budget, request["length"]
            ):
                raise ValueError(
                    "PDF image catalog metadata exceeds window; use a larger length"
                )
            for image_index in range(start, len(keys)):
                result["images"].append(
                    {"image_index": image_index, "object_key": keys[image_index]}
                )
                result["next_offset"] = (
                    image_index + 1 if image_index + 1 < len(keys) else None
                )
                if len(json.dumps(result, ensure_ascii=False)) > min(
                    budget, request["length"]
                ):
                    result["images"].pop()
                    result["next_offset"] = image_index
                    if not result["images"]:
                        raise ValueError(
                            "PDF image catalog entry exceeds window; use a larger length or select image_index"
                        )
                    break
            return result
        if not 0 <= selected < len(keys):
            raise ValueError("Invalid embedded image index")
        image = images[selected]
        if image.image is None:
            raise ValueError("Embedded image cannot be decoded")
        import hashlib

        from PIL import Image

        picture = image.image
        try:
            if picture.width * picture.height > MAX_PAGE_PIXELS:
                raise ValueError("Embedded image exceeds the 4 megapixel decode budget")
            encoded = image.data
            with Image.open(io.BytesIO(encoded)) as decoded:
                mime = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "GIF": "image/gif",
                    "WEBP": "image/webp",
                }.get(decoded.format)
            converted = mime is None
            if converted:
                output = io.BytesIO()
                picture.save(output, "PNG")
                encoded, mime = output.getvalue(), "image/png"
            if len(encoded) > MAX_IMAGE_BYTES:
                raise ValueError("Embedded image exceeds the 4 MiB output budget")
            result.update(
                image_index=selected,
                object_key=keys[selected],
                image_name=image.name[:1024],
                width=picture.width,
                height=picture.height,
                bytes=len(encoded),
                image_sha256=hashlib.sha256(encoded).hexdigest(),
                content_index=1,
                extraction="pypdf image extraction; PDF streams may be decoded/repacked",
                converted_to_png=converted,
            )
            if len(json.dumps(result, ensure_ascii=False)) > budget:
                raise ValueError("Embedded image metadata exceeds output budget")
            return {
                **result,
                "image": base64.b64encode(encoded).decode("ascii"),
                "mime_type": mime,
            }
        finally:
            picture.close()
    finally:
        book.close()


def create_pdf(request: dict) -> dict:
    from playwright.sync_api import Error, sync_playwright
    from pypdf import PdfReader

    runtime = Path(request["runtime_dir"])
    with sync_playwright() as playwright:
        channel = None
        if not Path(playwright.chromium.executable_path).is_file() and os.name == "nt":
            channel = "msedge"
        try:
            context = playwright.chromium.launch_persistent_context(
                runtime / "profile",
                channel=channel,
                headless=True,
                java_script_enabled=False,
                service_workers="block",
                offline=True,
                accept_downloads=False,
                chromium_sandbox=True,
            )
        except Error as exc:
            raise ValueError(
                "PDF browser unavailable; run python -m playwright install chromium or install Edge"
            ) from exc
        try:
            context.route("**/*", lambda route: route.abort())
            context.set_default_timeout(30000)
            page = context.new_page()
            page.set_content(request["html"], wait_until="load", timeout=30000)
            if not page.locator("img").evaluate_all(
                "imgs => imgs.every(i => i.complete && i.naturalWidth > 0)"
            ):
                raise ValueError("One or more PDF images could not be decoded")
            data = page.pdf(
                format=request["page_size"],
                landscape=request["landscape"],
                print_background=True,
                tagged=True,
                outline=True,
                margin={
                    "top": "16mm",
                    "right": "16mm",
                    "bottom": "16mm",
                    "left": "16mm",
                },
            )
            if len(data) > MAX_PDF_BYTES:
                raise ValueError("Rendered PDF exceeds the 16 MiB budget")
            count = len(PdfReader(io.BytesIO(data)).pages)
            if not 1 <= count <= MAX_PAGES:
                raise ValueError("Rendered PDF exceeds the page budget")
            return {
                "pdf": base64.b64encode(data).decode("ascii"),
                "page_count": count,
                "renderer": channel or "chromium",
                "network_enabled": False,
                "javascript_enabled": False,
            }
        finally:
            context.close()


def preview_pdf(request: dict) -> dict:
    # PDFium is not thread-safe; each worker process handles exactly one request.
    import pypdfium2 as pdfium

    data = base64.b64decode(request["pdf"], validate=True)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the preview budget")
    index = request["page_index"]
    scale = request["scale"]
    if (
        type(index) is not int
        or index < 0
        or not math.isfinite(scale)
        or not 0.1 <= scale <= 4
    ):
        raise ValueError("Use a nonnegative page index and scale 0.1..4")
    with pdfium.PdfDocument(data) as document:
        if len(document) > MAX_PAGES or index >= len(document):
            raise ValueError("Page index or document page count exceeds the budget")
        page = document[index]
        try:
            width, height = page.get_size()
            if width <= 0 or height <= 0 or not math.isfinite(width * height):
                raise ValueError("Invalid PDF page dimensions")
            scale = min(scale, math.sqrt(MAX_PAGE_PIXELS / (width * height)))
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil()
                output = io.BytesIO()
                try:
                    with image.convert("RGB") as rgb:
                        rgb.save(output, "JPEG", quality=85)
                finally:
                    image.close()
                encoded = output.getvalue()
                if len(encoded) > MAX_IMAGE_BYTES:
                    raise ValueError(
                        "Preview exceeds the image budget; use a lower scale"
                    )
                return {
                    "image": base64.b64encode(encoded).decode("ascii"),
                    "mime_type": "image/jpeg",
                    "width": bitmap.width,
                    "height": bitmap.height,
                    "scale": scale,
                    "page_index": index,
                    "page_count": len(document),
                }
            finally:
                bitmap.close()
        finally:
            page.close()


def main() -> int:
    memory_guard = None
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("PDF worker input exceeds the 32 MiB budget")
        request = json.loads(raw)
        runtime = Path(request["runtime_dir"]).resolve(strict=True)
        # All browser profiles, downloads and helper temporary artifacts stay in
        # the parent-owned temporary directory, even on timeout/forced termination.
        for name in ("TMP", "TEMP", "TMPDIR"):
            os.environ[name] = str(runtime)
        tempfile.tempdir = None
        if request["mode"] == "inspect":
            from workbook_worker import limit_memory

            memory_guard = limit_memory()
            result = inspect_pdf(request)
        elif request["mode"] == "create":
            result = create_pdf(request)
        elif request["mode"] == "preview":
            result = preview_pdf(request)
        else:
            raise ValueError("Unsupported PDF helper operation")
    except Exception as exc:  # noqa: BLE001 - untrusted PDF/browser failures remain in the worker.
        result = {
            "error": str(exc)[:1000]
            if isinstance(exc, ValueError)
            else f"PDF operation failed ({type(exc).__name__}); verify the document and renderer installation"
        }
    finally:
        if memory_guard is not None:
            memory_guard.Close()
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
