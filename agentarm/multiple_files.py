"""Mixed text/image MCP reads with per-file failures and bounded aggregate output."""

import json
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from .image_tools import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, read_image


def read_multiple_files(workspace, paths: list[str], length: int) -> CallToolResult:
    if (
        not 1 <= len(paths) <= 16
        or type(length) is not int
        or not 1 <= length <= workspace.policy.max_output_chars // len(paths)
    ):
        raise ValueError(
            "Use 1..16 paths and a combined character window within the output budget"
        )
    # Reserve one explicit outcome for every input, even when earlier files fill the budget.
    records = [
        {"path": path[:1024], "error": "response_budget_exceeded"} for path in paths
    ]

    def serialized() -> str:
        return json.dumps({"files": records}, ensure_ascii=False)

    if len(serialized()) > workspace.policy.max_output_chars:
        raise ValueError(
            "File labels exceed response budget; request fewer/shorter paths"
        )
    images = []
    image_bytes = 0
    for index, path in enumerate(paths):
        try:
            if Path(path).suffix.casefold() in IMAGE_SUFFIXES:
                record, picture = read_image(
                    workspace, path, MAX_IMAGE_BYTES - image_bytes
                )
                record["content_index"] = 1 + len(images)
                records[index] = record
                if len(serialized()) > workspace.policy.max_output_chars:
                    records[index] = {
                        "path": path[:1024],
                        "error": "response_budget_exceeded",
                    }
                    continue
                images.append(picture)
                image_bytes += record["bytes"]
            else:
                record = workspace.read_file(path, length=length)
                records[index] = record
                if len(serialized()) > workspace.policy.max_output_chars:
                    content = record["content"]
                    low, high = 0, len(content)
                    while low < high:
                        count = (low + high + 1) // 2
                        record.update(content=content[:count], next_offset=count)
                        if len(serialized()) <= workspace.policy.max_output_chars:
                            low = count
                        else:
                            high = count - 1
                    record.update(content=content[:low], next_offset=low)
                    if (
                        low == 0
                        or len(serialized()) > workspace.policy.max_output_chars
                    ):
                        records[index] = {
                            "path": path[:1024],
                            "error": "response_budget_exceeded",
                        }
        except (OSError, ValueError) as exc:
            error = (
                "FileNotFoundError"
                if isinstance(exc, FileNotFoundError)
                else "PermissionError"
                if isinstance(exc, PermissionError)
                else "OSError"
                if isinstance(exc, OSError)
                else "ValueError"
            )
            records[index] = {"path": path[:1024], "error": error}
    return CallToolResult(
        content=[TextContent(type="text", text=serialized()), *images],
        structuredContent={"files": records},
    )
