"""Fixed Excel parser/writer. No application automation, network, macro or formula execution."""

from __future__ import annotations

import base64
import datetime
import io
import json
import math
import os
import struct
import subprocess
import sys
import warnings
import zipfile

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MEMORY_BYTES = 512 * 1024 * 1024


def limit_memory():
    if os.name == "nt":
        import win32api
        import win32job

        job = win32job.CreateJobObject(None, "")
        try:
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            )
            info["ProcessMemoryLimit"] = MEMORY_BYTES
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, info
            )
            win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
            return job
        except Exception:
            job.Close()
            raise
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    return None


def sheet_name(names: list[str], selected: str | int) -> str:
    if isinstance(selected, str) and selected in names:
        return selected
    if type(selected) is int or (isinstance(selected, str) and selected.isdecimal()):
        index = int(selected)
        if 0 <= index < len(names):
            return names[index]
    raise ValueError(
        "Worksheet not found; list_workbook_sheets shows names and zero-based indices"
    )


def bounds(cell_range: str, suffix: str):
    from openpyxl.utils.cell import range_boundaries

    first_col, first_row, last_col, last_row = range_boundaries(cell_range)
    max_row, max_col = (65536, 256) if suffix == ".xls" else (1048576, 16384)
    if (
        not all((first_col, first_row, last_col, last_row))
        or first_col < 1
        or first_row < 1
        or last_col < first_col
        or last_row < first_row
        or last_row > max_row
        or last_col > max_col
        or (last_col - first_col + 1) * (last_row - first_row + 1) > 10000
    ):
        raise ValueError(
            "Use a rectangular range of at most 10000 cells within Excel format limits"
        )
    return first_col, first_row, last_col, last_row


def normalized(value):
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Workbook contains a nonfinite number")
    return value


def metadata(names: list[str], request: dict) -> dict:
    offset, limit = request.get("offset", 0), request.get("limit", 100)
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 500
    ):
        raise ValueError("Use a worksheet offset >= 0 and limit 1..500")
    end = min(len(names), offset + limit)
    return {
        "sheets": [
            {"index": index, "name": names[index]} for index in range(offset, end)
        ],
        "total_sheets": len(names),
        "next_offset": end if end < len(names) else None,
    }


def read_legacy(data: bytes, request: dict) -> dict:
    import xlrd
    from openpyxl.utils.cell import get_column_letter

    with xlrd.open_workbook(
        file_contents=data, logfile=io.StringIO(), on_demand=True, ragged_rows=True
    ) as book:
        names = book.sheet_names()
        if request["mode"] == "sheets":
            return metadata(names, request)
        first_col, first_row, last_col, last_row = bounds(
            request.get("cell_range", "A1:J50"), ".xls"
        )
        name = sheet_name(names, request["sheet"])
        sheet = book.sheet_by_name(name)
        rows = []
        for row in range(first_row - 1, last_row):
            cells = []
            for col in range(first_col - 1, last_col):
                value, kind = None, "n"
                if row < sheet.nrows and col < sheet.row_len(row):
                    cell = sheet.cell(row, col)
                    value = cell.value
                    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                        value = None
                    elif cell.ctype == xlrd.XL_CELL_DATE:
                        value, kind = (
                            xlrd.xldate_as_datetime(value, book.datemode).isoformat(),
                            "d",
                        )
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value, kind = bool(value), "b"
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        value, kind = (
                            xlrd.error_text_from_code.get(value, "#UNKNOWN!"),
                            "e",
                        )
                    elif cell.ctype == xlrd.XL_CELL_TEXT:
                        kind = "s"
                cells.append(
                    {
                        "cell": f"{get_column_letter(col + 1)}{row + 1}",
                        "value": normalized(value),
                        "type": kind,
                    }
                )
            rows.append(cells)
        return {
            "sheet": name,
            "sheet_index": names.index(name),
            "rows": rows,
            "formula_representation": "cached_values_only",
            "date_mode": book.datemode,
        }


def read_modern(data: bytes, request: dict) -> dict:
    from openpyxl import load_workbook
    from openpyxl.utils.cell import get_column_letter

    book = load_workbook(
        io.BytesIO(data), read_only=True, data_only=False, keep_links=False
    )
    try:
        names = [sheet.title for sheet in book.worksheets]
        if request["mode"] == "sheets":
            return metadata(names, request)
        first_col, first_row, last_col, last_row = bounds(
            request.get("cell_range", "A1:J50"), request["suffix"]
        )
        name = sheet_name(names, request["sheet"])
        rows = [
            [
                {
                    "cell": f"{get_column_letter(col)}{row}",
                    "value": normalized(cell.value),
                    "type": cell.data_type,
                }
                for col, cell in enumerate(cells, first_col)
            ]
            for row, cells in enumerate(
                book[name].iter_rows(
                    min_row=first_row,
                    max_row=last_row,
                    min_col=first_col,
                    max_col=last_col,
                ),
                first_row,
            )
        ]
        return {
            "sheet": name,
            "sheet_index": names.index(name),
            "rows": rows,
            "formula_representation": "formulas_not_evaluated",
        }
    finally:
        book.close()


def validate_write(request: dict) -> tuple[int, int]:
    from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

    suffix, rows = request["suffix"], request["rows"]
    if (
        not rows
        or not all(isinstance(row, list) for row in rows)
        or not 1 <= sum(map(len, rows)) <= 10000
    ):
        raise ValueError("Write 1..10000 cells")
    if (
        len(json.dumps(rows, ensure_ascii=False, allow_nan=False))
        > request["max_output_chars"]
    ):
        raise ValueError("Cell payload exceeds the character budget")
    for row in rows:
        if any(
            value is not None and type(value) not in {str, int, float, bool}
            for value in row
        ):
            raise ValueError(
                "Cell values must be JSON strings, finite numbers, booleans or null"
            )
    start_row, start_col = coordinate_to_tuple(request["start_cell"].upper())
    last_column = get_column_letter(start_col + max(map(len, rows)) - 1)
    bounds(f"{request['start_cell']}:{last_column}{start_row + len(rows) - 1}", suffix)
    if not isinstance(request["sheet"], str) or not request["sheet"]:
        raise ValueError("Writing requires a worksheet name")
    return start_row, start_col


def write_legacy(data: bytes | None, request: dict) -> dict:
    if os.name != "nt":
        raise ValueError(
            "Native XLS writing currently requires Windows; XLS reading is cross-platform"
        )
    from legacy_excel_runtime import _environment, ready

    start_row, start_col = validate_write(request)
    runtime = ready()
    payload = io.BytesIO()
    payload.write(b"FLXLS1")
    payload.write(
        struct.pack(">ii", request["max_file_bytes"], -1 if data is None else len(data))
    )
    if data is not None:
        payload.write(data)

    def text(value: str) -> None:
        encoded = value.encode("utf-8")
        payload.write(struct.pack(">i", len(encoded)))
        payload.write(encoded)

    text(request["sheet"])
    payload.write(
        struct.pack(">iii", start_row - 1, start_col - 1, len(request["rows"]))
    )
    for row in request["rows"]:
        payload.write(struct.pack(">i", len(row)))
        for value in row:
            if value is None:
                payload.write(b"\x00")
            elif isinstance(value, str):
                payload.write(b"\x01")
                text(value)
            elif type(value) is bool:
                payload.write(b"\x03" + bytes([value]))
            else:
                payload.write(b"\x02" + struct.pack(">d", value))
    completed = subprocess.run(
        [
            runtime["java"],
            "-Xmx128m",
            "-XX:MaxMetaspaceSize=96m",
            "-XX:ReservedCodeCacheSize=32m",
            "-XX:+UseSerialGC",
            "-XX:-CreateCoredumpOnCrash",
            "-cp",
            runtime["classpath"],
            "NativeXls",
        ],
        input=payload.getvalue(),
        capture_output=True,
        check=False,
        timeout=30,
        cwd=request["runtime_dir"],
        env=_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    result = completed.stdout
    if completed.returncode or not result.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        raise ValueError(
            "Native XLS writer failed; verify workbook features and format limits"
        )
    if len(result) > request["max_file_bytes"]:
        raise ValueError("Edited workbook exceeds file budget")
    return {
        "data": base64.b64encode(result).decode("ascii"),
        "format": "BIFF8",
        "formulas_evaluated": False,
    }


def write_modern(data: bytes | None, request: dict) -> dict:
    from openpyxl import Workbook, load_workbook

    start_row, start_col = validate_write(request)
    suffix, rows = request["suffix"], request["rows"]
    if suffix not in {".xlsx", ".xlsm"}:
        raise ValueError("Use .xls, .xlsx or .xlsm")
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        book = (
            load_workbook(io.BytesIO(data), keep_vba=suffix == ".xlsm", keep_links=True)
            if data is not None
            else Workbook()
        )
        if suffix == ".xlsm" and data is None:
            # Let openpyxl retain a real package manifest and select the XLSM
            # content type without inventing a VBA project.
            seed = io.BytesIO()
            book.save(seed)
            book.close()
            book = load_workbook(io.BytesIO(seed.getvalue()), keep_vba=True)
        try:
            sheet = (
                book[request["sheet"]]
                if request["sheet"] in book.sheetnames
                else book.create_sheet(request["sheet"])
            )
            if data is None and len(book.sheetnames) > 1:
                del book["Sheet"]
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    sheet.cell(start_row + i, start_col + j).value = value
            output = io.BytesIO()
            book.save(output)
        finally:
            book.close()
            if book.vba_archive is not None:
                book.vba_archive.close()
    result = output.getvalue()
    if len(result) > request["max_file_bytes"]:
        raise ValueError("Edited workbook exceeds file budget")
    if data is not None:
        with (
            zipfile.ZipFile(io.BytesIO(data)) as before,
            zipfile.ZipFile(io.BytesIO(result)) as after,
        ):
            removed = (
                set(before.namelist())
                - set(after.namelist())
                - {"xl/sharedStrings.xml", "xl/calcChain.xml"}
            )
            if removed:
                raise ValueError(
                    "Editing would remove workbook features; original was kept unchanged"
                )
            if any(name.startswith("_xmlsignatures/") for name in before.namelist()):
                raise ValueError(
                    "Editing a digitally signed workbook requires an explicit signing workflow"
                )
            for name in before.namelist():
                if name.endswith(".bin") and before.read(name) != after.read(name):
                    raise ValueError(
                        "Editing would alter a binary workbook component; original was kept unchanged"
                    )
    return {
        "data": base64.b64encode(result).decode("ascii"),
        "vba_preservation_enabled": suffix == ".xlsm",
    }


def operate(request: dict) -> dict:
    data = (
        base64.b64decode(request["data"], validate=True)
        if request.get("data") is not None
        else None
    )
    if data is not None and len(data) > request["max_file_bytes"]:
        raise ValueError("Workbook exceeds file budget")
    if request["mode"] == "write":
        return (
            write_legacy(data, request)
            if request["suffix"] == ".xls"
            else write_modern(data, request)
        )
    if request["mode"] not in {"read", "sheets"} or data is None:
        raise ValueError("Use read/sheets/write workbook operation")
    result = (
        read_legacy(data, request)
        if request["suffix"] == ".xls"
        else read_modern(data, request)
    )
    if len(json.dumps(result, ensure_ascii=False)) > request["max_output_chars"]:
        raise ValueError(
            "Result too large; request fewer sheets or a smaller cell range"
        )
    return result


def main() -> int:
    memory_guard = None
    try:
        memory_guard = limit_memory()
        raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("Workbook helper input exceeds 32 MiB")
        request = json.loads(raw)
        result = operate(request)
    except Exception as exc:  # noqa: BLE001 - binary parser failures remain in the worker.
        result = {
            "error": str(exc)[:1000]
            if isinstance(exc, ValueError)
            else f"Workbook operation failed ({type(exc).__name__}); verify the file and supported features"
        }
    finally:
        if memory_guard is not None:
            memory_guard.Close()
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))
    sys.stdout.flush()
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
