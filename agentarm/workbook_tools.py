"""Workspace-authorized Excel operations delegated to a fixed resource-limited parser."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from .helpers import run_helper
from .snapshots import _validate_archive
from .workspace import Workspace, digest


def _request(
    workspace: Workspace, path: str, data: bytes | None, mode: str, **arguments
) -> dict:
    suffix = Path(path).suffix.lower()
    if suffix not in {".xls", ".xlsx", ".xlsm"}:
        raise ValueError("Use an .xls, .xlsx or .xlsm workbook")
    if data is not None and suffix != ".xls":
        _validate_archive(data, "Workbook")
    return {
        "mode": mode,
        "suffix": suffix,
        "data": base64.b64encode(data).decode("ascii") if data is not None else None,
        "max_file_bytes": workspace.policy.max_file_bytes,
        "max_output_chars": workspace.policy.max_output_chars,
        **arguments,
    }


def read_workbook(
    workspace: Workspace,
    path: str,
    sheet: str | int,
    cell_range: str = "A1:J50",
    expected_sha256: str | None = None,
) -> dict:
    data = workspace.bytes(path)
    source_hash = digest(data)
    if expected_sha256 is not None and expected_sha256 != source_hash:
        raise ValueError("SHA-256 conflict: workbook changed between windows")
    result = run_helper(
        workspace,
        "workbook_worker.py",
        _request(workspace, path, data, "read", sheet=sheet, cell_range=cell_range),
        workspace.policy.max_output_chars * 6 + 16384,
    )
    return {"path": path, "sha256": source_hash, **result}


def list_sheets(
    workspace: Workspace,
    path: str,
    offset: int = 0,
    limit: int = 100,
    expected_sha256: str | None = None,
) -> dict:
    data = workspace.bytes(path)
    source_hash = digest(data)
    if expected_sha256 is not None and expected_sha256 != source_hash:
        raise ValueError("SHA-256 conflict: workbook changed between windows")
    result = run_helper(
        workspace,
        "workbook_worker.py",
        _request(workspace, path, data, "sheets", offset=offset, limit=limit),
        workspace.policy.max_output_chars * 6 + 16384,
    )
    return {"path": path, "sha256": source_hash, **result}


def write_workbook(
    workspace: Workspace,
    path: str,
    sheet: str,
    start_cell: str,
    rows: list[list],
    base_sha256: str | None,
) -> dict:
    workspace.writable()
    if (
        len(json.dumps(rows, ensure_ascii=False, allow_nan=False))
        > workspace.policy.max_output_chars
    ):
        raise ValueError("Cell payload exceeds budget")
    with workspace.lock:
        data = workspace._current(path, base_sha256)
        request = _request(
            workspace,
            path,
            data,
            "write",
            sheet=sheet,
            start_cell=start_cell,
            rows=rows,
        )
        result = run_helper(
            workspace,
            "workbook_worker.py",
            request,
            workspace.policy.max_file_bytes * 4 // 3 + 16384,
        )
        output = base64.b64decode(result.pop("data"), validate=True)
        return {**workspace.write_bytes(path, output, base_sha256), **result}
