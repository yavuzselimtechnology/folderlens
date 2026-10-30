"""Build a public source download from reviewed Git blobs, never the working tree."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

PUBLIC_FILES = (
    "folderlens.py",
    "run.py",
    "start.ps1",
    "requirements.txt",
    "requirements-agent.txt",
    "CLIENT_README.md",
    "QUICKSTART.en.md",
    "AGENT_GUIDE.md",
    "index.html",
    "agent.html",
    "agent.en.html",
    "styles.css",
    "app.js",
    "git-reader.js",
    "agentarm/__init__.py",
    "agentarm/config.py",
    "agentarm/device_catalog.py",
    "agentarm/device_config.py",
    "agentarm/directory_listing.py",
    "agentarm/directory_tools.py",
    "agentarm/document_markdown.py",
    "agentarm/document_tools.py",
    "agentarm/git_tools.py",
    "agentarm/helpers.py",
    "agentarm/http_security.py",
    "agentarm/image_tools.py",
    "agentarm/legacy_excel_runtime.py",
    "agentarm/legacy_excel.lock.json",
    "agentarm/java/NativeXls.java",
    "agentarm/mcp_server.py",
    "agentarm/multiple_files.py",
    "agentarm/oauth.py",
    "agentarm/oauth_http.py",
    "agentarm/paths.py",
    "agentarm/pdf_tools.py",
    "agentarm/pdf_worker.py",
    "agentarm/processes.py",
    "agentarm/process_worker.py",
    "agentarm/relay.py",
    "agentarm/remote_calls.py",
    "agentarm/search_history.py",
    "agentarm/shutdown.py",
    "agentarm/snapshots.py",
    "agentarm/system_processes.py",
    "agentarm/terminal_output.py",
    "agentarm/tool_history.py",
    "agentarm/url_reader.py",
    "agentarm/web.py",
    "agentarm/word_authoring.py",
    "agentarm/workbook_tools.py",
    "agentarm/workbook_worker.py",
    "agentarm/workspace.py",
)


def build(project: Path, revision: str, output: Path) -> dict:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(project), *args])

    commit = (
        git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
        .decode()
        .strip()
    )
    prefix = git("rev-parse", "--show-prefix").decode().strip()
    contents = {}
    for name in PUBLIC_FILES:
        entry = (
            git("ls-tree", "--full-tree", commit, "--", prefix + name).decode().strip()
        )
        if not entry:
            raise ValueError(f"Required public source is missing: {name}")
        mode, kind, blob = entry.split("\t", 1)[0].split()
        if mode != "100644" or kind != "blob":
            raise ValueError(f"Expected regular non-executable source: {name}")
        target = "README.md" if name == "CLIENT_README.md" else name
        contents[target] = git("cat-file", "blob", blob)
    manifest = {
        "format": "folderlens.agent.source.v1",
        "source_commit": commit,
        "files": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(contents.items())
        },
    }
    metadata = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    contents["RELEASE.json"] = metadata
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in sorted(contents.items()):
            # The ZIP format starts at 1980; a fixed timestamp makes repeat builds identical.
            info = zipfile.ZipInfo(
                "FolderLens/" + name, date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    for name, data in {
        "folderlens-agent.zip": payload,
        "folderlens-agent.zip.sha256": (digest + "  folderlens-agent.zip\n").encode(),
        "folderlens-agent.json": metadata,
    }.items():
        with tempfile.NamedTemporaryFile(dir=output, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
        try:
            os.replace(temporary, output / name)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "source_commit": commit,
        "files": len(contents),
        "bytes": len(payload),
        "sha256": digest,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parent / "downloads"
    )
    args = parser.parse_args()
    print(
        json.dumps(build(Path(__file__).resolve().parent, args.commit, args.output_dir))
    )


if __name__ == "__main__":
    main()
