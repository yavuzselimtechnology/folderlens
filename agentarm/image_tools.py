"""Original image bytes with shared format, integrity and response-budget checks."""

import base64
import warnings
from io import BytesIO

from mcp.types import ImageContent
from PIL import Image

from .workspace import digest

MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def read_image(
    workspace, path: str, byte_budget: int = MAX_IMAGE_BYTES
) -> tuple[dict, ImageContent]:
    data = workspace.bytes(path)
    if len(data) > min(MAX_IMAGE_BYTES, byte_budget):
        raise ValueError(
            "Image exceeds remaining image byte budget (4 MiB per response)"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as picture:
                picture.verify()
                mime = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "GIF": "image/gif",
                    "WEBP": "image/webp",
                }.get(picture.format)
                width, height = picture.size
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError("Image dimensions exceed Pillow's safe pixel budget") from exc
    if mime is None:
        raise ValueError("Use PNG, JPEG, GIF or WebP")
    return (
        {
            "path": path,
            "sha256": digest(data),
            "representation": "image",
            "bytes": len(data),
            "mime_type": mime,
            "width": width,
            "height": height,
        },
        ImageContent(
            type="image", mimeType=mime, data=base64.b64encode(data).decode("ascii")
        ),
    )
