from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    max_file_bytes: int = 128 * 1024 * 1024
    max_upload_bytes: int = 384 * 1024 * 1024
    max_files: int = 25_000
    max_git_commits: int = 100_000
    max_snapshots: int = 10
    max_memory_bytes: int = 512 * 1024 * 1024
    retention_seconds: int = 2 * 60 * 60
    uploads_per_hour: int = 20
    bridge_request_timeout: float = 90.0
    bridge_idle_seconds: float = 45.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            max_file_bytes=int(
                os.getenv("AGENTARM_MAX_FILE_BYTES", str(128 * 1024 * 1024))
            ),
            max_upload_bytes=int(
                os.getenv("AGENTARM_MAX_UPLOAD_BYTES", str(384 * 1024 * 1024))
            ),
            max_files=int(os.getenv("AGENTARM_MAX_FILES", "25000")),
            max_git_commits=int(os.getenv("AGENTARM_MAX_GIT_COMMITS", "100000")),
            max_snapshots=int(os.getenv("AGENTARM_MAX_SNAPSHOTS", "10")),
            max_memory_bytes=int(
                os.getenv("AGENTARM_MAX_MEMORY_BYTES", str(512 * 1024 * 1024))
            ),
            retention_seconds=int(
                os.getenv("AGENTARM_RETENTION_SECONDS", str(2 * 60 * 60))
            ),
            uploads_per_hour=int(os.getenv("AGENTARM_UPLOADS_PER_HOUR", "20")),
            bridge_request_timeout=float(
                os.getenv("AGENTARM_BRIDGE_REQUEST_TIMEOUT", "90")
            ),
            bridge_idle_seconds=float(os.getenv("AGENTARM_BRIDGE_IDLE_SECONDS", "45")),
        )
