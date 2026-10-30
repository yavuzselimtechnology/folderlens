"""Bounded serialized search-result history; independent from the discovery cursor."""

import itertools
import json
from collections import deque

SEARCH_HISTORY_BYTES = 1024 * 1024


class SearchHistory:
    def __init__(self, max_bytes: int = SEARCH_HISTORY_BYTES):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Search history byte budget must be a positive integer")
        self.max_bytes = max_bytes
        self.records = deque()
        self.bytes = 0
        self.start = 0
        self.end = 0

    def append(self, rows: list[dict]) -> None:
        for row in rows:
            encoded = json.dumps(row, ensure_ascii=False).encode("utf-8")
            self.records.append(encoded)
            self.bytes += len(encoded)
            self.end += 1
            while self.bytes > self.max_bytes:
                self.bytes -= len(self.records.popleft())
                self.start += 1

    def metadata(self) -> dict:
        return {
            "history_start": self.start,
            "history_end": self.end,
            "history_bytes": self.bytes,
            "history_limit_bytes": self.max_bytes,
        }

    def page(self, offset: int, limit: int, character_budget: int) -> dict:
        requested = max(0, self.end + offset) if offset < 0 else offset
        start = max(self.start, min(requested, self.end))
        rows = []
        for raw in itertools.islice(
            self.records, start - self.start, start - self.start + limit
        ):
            row = json.loads(raw)
            cost = len(json.dumps(row, ensure_ascii=False))
            if cost > character_budget:
                if not rows:
                    raise ValueError(
                        "Search result exceeds output budget; request a narrower search"
                    )
                break
            rows.append(row)
            character_budget -= cost
        end = start + len(rows)
        return {
            "results": rows,
            "offset": start,
            "next_offset": end,
            "has_more": end < self.end,
            "requested_results_dropped": requested < self.start,
            "delivery": "history",
        }
