"""LF-delimited terminal windows; byte cursors remain available for unfinished lines."""

import io


def line_window(
    raw: bytes,
    dropped_lines: int,
    prefix_dropped: bool,
    byte_start: int,
    offset: int,
    length: int,
    budget: int,
    ended: bool,
) -> dict:
    count = raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n"))
    end = dropped_lines + count
    requested = max(0, end + offset) if offset < 0 else offset
    start = max(dropped_lines, min(requested, end))
    rows = []
    used = 0
    start_byte = end_byte = len(raw)
    stream = io.BytesIO(raw)
    for index, line in enumerate(stream):
        if index < start - dropped_lines:
            continue
        if len(rows) == length:
            break
        text = line.decode("utf-8", "replace")
        if used + len(text) > budget:
            if not rows:
                raise ValueError(
                    "Terminal line exceeds character budget; use unit=bytes"
                )
            break
        if not rows:
            start_byte = stream.tell() - len(line)
        rows.append(text)
        used += len(text)
        end_byte = stream.tell()
    incomplete = (
        bool(rows)
        and start + len(rows) == end
        and not raw.endswith(b"\n")
        and not ended
    )
    return {
        "unit": "lines",
        "output": "".join(rows),
        "offset": start,
        "next_offset": start + len(rows) - int(incomplete),
        "line_count": len(rows),
        "available_start": dropped_lines,
        "available_end": end,
        "byte_offset": byte_start + start_byte,
        "next_byte_offset": byte_start + end_byte,
        "available_byte_start": byte_start,
        "available_byte_end": byte_start + len(raw),
        "requested_data_dropped": requested < dropped_lines
        or (start == dropped_lines and prefix_dropped),
        "first_line_prefix_dropped": start == dropped_lines and prefix_dropped,
        "last_line_incomplete": incomplete,
        "encoding": "UTF-8 with replacement; LF-delimited lines, CRLF preserved; no terminal emulation",
    }
