from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.datastructures import FormData, UploadFile

from .config import Settings
from .paths import is_ignored_path, is_sensitive_path, safe_relative_path
from .relay import EphemeralRelay, RelayError
from .snapshots import (
    SnapshotError,
    SnapshotStore,
    is_editable_text_path,
)

ROOT = Path(__file__).resolve().parents[1]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = SnapshotStore(settings)
    # Keep one memory-intensive extraction in flight while HTTP stays responsive.
    extraction_lock = asyncio.Lock()
    relay = EphemeralRelay(
        request_timeout=settings.bridge_request_timeout,
        idle_ttl=settings.bridge_idle_seconds,
    )
    upload_windows: dict[str, deque[float]] = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cleanup_task = asyncio.create_task(_cleanup_loop(store, relay))
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="FolderLens",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        return response

    @app.get("/AgentArm/assets/styles.css", include_in_schema=False)
    async def stylesheet() -> FileResponse:
        return FileResponse(ROOT / "styles.css", media_type="text/css")

    @app.get("/AgentArm/assets/app.js", include_in_schema=False)
    async def javascript() -> FileResponse:
        return FileResponse(ROOT / "app.js", media_type="text/javascript")

    @app.get("/AgentArm/assets/git-reader.js", include_in_schema=False)
    async def git_reader_javascript() -> FileResponse:
        return FileResponse(ROOT / "git-reader.js", media_type="text/javascript")

    @app.get("/AgentArm", include_in_schema=False)
    @app.get("/AgentArm/", include_in_schema=False)
    @app.get("/folderlens", include_in_schema=False)
    @app.get("/folderlens/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(ROOT / "index.html")

    @app.get("/AgentArm/agent", include_in_schema=False)
    @app.get("/AgentArm/agent/", include_in_schema=False)
    @app.get("/folderlens/agent", include_in_schema=False)
    @app.get("/folderlens/agent/", include_in_schema=False)
    async def agent_setup(lang: Literal["tr", "en"] = "tr") -> HTMLResponse:
        page = ROOT / ("agent.en.html" if lang == "en" else "agent.html")
        requirements = "\n\n".join(
            name + "\n" + (ROOT / name).read_text(encoding="utf-8").strip()
            for name in ("requirements.txt", "requirements-agent.txt")
        )
        return HTMLResponse(
            page.read_text(encoding="utf-8").replace(
                "<!--agent-dependencies-->", escape(requirements)
            )
        )

    @app.get("/AgentArm/downloads/{filename}", include_in_schema=False)
    @app.get("/folderlens/downloads/{filename}", include_in_schema=False)
    @app.head("/AgentArm/downloads/{filename}", include_in_schema=False)
    @app.head("/folderlens/downloads/{filename}", include_in_schema=False)
    async def agent_download(filename: str) -> FileResponse:
        types = {
            "folderlens-agent.zip": "application/zip",
            "folderlens-agent.zip.sha256": "text/plain",
            "folderlens-agent.json": "application/json",
        }
        if filename not in types:
            raise HTTPException(404, "Unknown download")
        path = ROOT / "downloads" / filename
        if not path.is_file() or path.is_symlink():
            raise HTTPException(503, "Download is not available for this release")
        return FileResponse(path, media_type=types[filename], filename=filename)

    @app.get("/", include_in_schema=False)
    async def local_root() -> RedirectResponse:
        return RedirectResponse("/AgentArm/", status_code=307)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/AgentArm/api/policy")
    async def policy() -> dict[str, int]:
        return {
            "max_file_bytes": settings.max_file_bytes,
            "max_upload_bytes": settings.max_upload_bytes,
            "max_files": settings.max_files,
            "max_git_commits": settings.max_git_commits,
        }

    @app.post("/AgentArm/api/snapshots", status_code=201)
    async def create_snapshot(request: Request) -> JSONResponse:
        _check_origin(request)
        _check_rate_limit(request, upload_windows, settings.uploads_per_hour)
        form, parsed, uploads = await _read_snapshot_form(request, settings)
        try:
            async with extraction_lock:
                result = await run_in_threadpool(
                    store.publish,
                    parsed,
                    [(path, upload.file) for path, upload in uploads],
                )
        except (SnapshotError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            await form.close()
        share_id = result["share_id"]
        relay.register(share_id, result["bridge_token"])
        result.update(_share_urls(share_id))
        return JSONResponse(result, status_code=201)

    @app.post("/AgentArm/api/snapshots/{share_id}/refresh")
    async def refresh_snapshot(share_id: str, request: Request) -> JSONResponse:
        _check_origin(request)
        bridge_token = request.headers.get("x-agentarm-bridge", "")
        form, parsed, uploads = await _read_snapshot_form(request, settings)
        try:
            async with extraction_lock:
                result = await run_in_threadpool(
                    store.refresh,
                    share_id,
                    bridge_token,
                    parsed,
                    [(path, upload.file) for path, upload in uploads],
                )
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        finally:
            await form.close()
        result.update(_share_urls(share_id))
        return JSONResponse(result)

    @app.post("/AgentArm/api/snapshots/{share_id}/bridge/poll")
    async def poll_bridge(share_id: str, request: Request) -> JSONResponse:
        payload = await _json_body(request)
        try:
            result = await relay.poll(share_id, str(payload.get("bridge_token", "")))
        except (RelayError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        return JSONResponse(result)

    @app.post("/AgentArm/api/snapshots/{share_id}/bridge/respond")
    async def respond_bridge(share_id: str, request: Request) -> JSONResponse:
        payload = await _json_body(request)
        try:
            await relay.respond(
                share_id,
                str(payload.get("bridge_token", "")),
                str(payload.get("request_id", "")),
                payload.get("result"),
                str(payload["error"]) if payload.get("error") else None,
            )
        except (RelayError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/AgentArm/api/snapshots/{share_id}/bridge/disconnect")
    async def disconnect_bridge(share_id: str, request: Request) -> JSONResponse:
        payload = await _json_body(request)
        try:
            bridge_token = str(payload.get("bridge_token", ""))
            await relay.disconnect(share_id, bridge_token)
            store.forget(share_id, bridge_token)
        except (RelayError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/AgentArm/api/snapshots/{share_id}/changes")
    async def apply_changes(share_id: str, request: Request) -> JSONResponse:
        token = _bearer_token(request)
        try:
            store.authorize_write(share_id, token)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Workspace not found or expired") from exc
        except (SnapshotError, ValueError) as exc:
            raise HTTPException(403, str(exc)) from exc
        try:
            payload = _validated_changes(await _json_body(request), settings)
            result = await relay.request(share_id, payload)
        except (SnapshotError, RelayError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse(result)

    @app.get("/AgentArm/api/snapshots/{share_id}/changes")
    async def discover_workspace(share_id: str, request: Request) -> JSONResponse:
        try:
            metadata = store.json_document(share_id, "meta.json")
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Workspace not found or expired") from exc
        urls = _absolute_share_urls(request, share_id)
        return _json_response(
            {
                "format": "agentarm.workspace-access.v1",
                "project": metadata["project"],
                "generated_at": metadata["created_at"],
                "read": {
                    "method": "GET",
                    "context_url": urls["context_url"],
                    "view_url": urls["view_url"],
                    "git_url": urls["git_url"],
                    "git_commit_links_url": urls["git_commit_links_url"],
                    "project_index_url": urls["project_url"],
                    "document_links_url": urls["document_links_url"],
                    "project_full_url": urls["project_full_url"],
                    "project_document_url_template": (
                        f"{str(request.base_url).rstrip('/')}/AgentArm/share/"
                        f"{share_id}/project/documents/{{sequence}}.json"
                    ),
                },
                "write": {
                    "method": "POST",
                    "endpoint": urls["changes_url"],
                    "authorization": "Bearer temporary capability from the folder owner",
                    "content_type": "application/json",
                    "change_format": "agentarm.changes.v1",
                },
                "instructions": [
                    (
                        "START: Read read.context_url with the AI platform's native "
                        "web or URL-fetch tool."
                    ),
                    (
                        "Do not switch to isolated shell networking when native web "
                        "fetch is available."
                    ),
                    "Read Git with GET read.git_url.",
                    (
                        "Open read.git_commit_links_url and fetch every real commit "
                        "page link before claiming complete Git history coverage."
                    ),
                    (
                        "Read read.project_index_url first, then follow its ordered "
                        "document_url entries for large projects."
                    ),
                    (
                        "Open read.document_links_url and follow its real HTML anchors "
                        "in sequence; do not construct blocked derived URLs."
                    ),
                    (
                        "For every document with chunked=true, open its "
                        "chunk_links_url and fetch every chunk in order."
                    ),
                    (
                        "Use read.project_full_url only when the client can accept "
                        "the complete workspace in one response."
                    ),
                    "Do not POST an empty object to read this workspace.",
                    (
                        "POST write.change_format only when a temporary Bearer "
                        "capability was supplied and the local FolderLens page remains open."
                    ),
                ],
            }
        )

    @app.get("/AgentArm/share/{share_id}/", include_in_schema=False)
    async def share_page(share_id: str) -> HTMLResponse:
        try:
            body = store.share_html(share_id)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        response = HTMLResponse(body)
        _set_context_link(response, share_id)
        return response

    @app.get("/AgentArm/share/{share_id}/context.json", include_in_schema=False)
    async def ai_context_json(share_id: str) -> JSONResponse:
        return _snapshot_json(store, share_id, "context.json")

    @app.get(
        "/AgentArm/share/{share_id}/document-links.html",
        include_in_schema=False,
    )
    async def document_links(share_id: str) -> HTMLResponse:
        return _document_links_response(store, share_id, 1)

    @app.get(
        "/AgentArm/share/{share_id}/document-links/{page}.html",
        include_in_schema=False,
    )
    async def document_links_page(share_id: str, page: int) -> HTMLResponse:
        return _document_links_response(store, share_id, page)

    @app.get("/AgentArm/share/{share_id}/git.json", include_in_schema=False)
    async def git_json(share_id: str) -> JSONResponse:
        return _snapshot_json(store, share_id, "git.json")

    @app.get(
        "/AgentArm/share/{share_id}/git-commit-links.html",
        include_in_schema=False,
    )
    async def git_commit_links(share_id: str) -> HTMLResponse:
        try:
            body = store.git_commit_links_html(share_id)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Git commit links not found") from exc
        response = HTMLResponse(body)
        _set_context_link(response, share_id)
        return response

    @app.get(
        "/AgentArm/share/{share_id}/git/commits/{page}.json",
        include_in_schema=False,
    )
    async def git_commit_page(share_id: str, page: int) -> JSONResponse:
        try:
            body = store.git_commit_page(share_id, page)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Git commit page not found") from exc
        response = _json_response(body)
        _set_context_link(response, share_id)
        return response

    @app.get("/AgentArm/share/{share_id}/project.json", include_in_schema=False)
    async def project_json(share_id: str) -> JSONResponse:
        return _snapshot_json(store, share_id, "project.json")

    @app.get("/AgentArm/share/{share_id}/project-full.json", include_in_schema=False)
    async def project_full_json(share_id: str) -> StreamingResponse:
        try:
            body = store.iter_project_json(share_id)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        response = StreamingResponse(
            body,
            media_type="application/json; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": "inline",
            },
        )
        _set_context_link(response, share_id)
        return response

    @app.get(
        "/AgentArm/share/{share_id}/project/documents/{sequence}.json",
        include_in_schema=False,
    )
    async def project_document(share_id: str, sequence: int) -> JSONResponse:
        try:
            body = store.document(share_id, sequence)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Document not found") from exc
        response = _json_response(body)
        _set_context_link(response, share_id)
        return response

    @app.get(
        "/AgentArm/share/{share_id}/project/documents/{sequence}/chunk-links.html",
        include_in_schema=False,
    )
    async def project_document_chunk_links(
        share_id: str, sequence: int
    ) -> HTMLResponse:
        try:
            body = store.document_chunk_links_html(share_id, sequence)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Document chunk links not found") from exc
        response = HTMLResponse(body)
        _set_context_link(response, share_id)
        return response

    @app.get(
        "/AgentArm/share/{share_id}/project/documents/{sequence}/chunks/"
        "{chunk_sequence}.json",
        include_in_schema=False,
    )
    async def project_document_chunk(
        share_id: str, sequence: int, chunk_sequence: int
    ) -> JSONResponse:
        try:
            body = store.document_chunk(share_id, sequence, chunk_sequence)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Document chunk not found") from exc
        response = _json_response(body)
        _set_context_link(response, share_id)
        return response

    @app.get(
        "/AgentArm/share/{share_id}/project/documents/{sequence}/chunks/{chunk_sequence}.html",
        include_in_schema=False,
    )
    async def project_document_chunk_html(
        share_id: str, sequence: int, chunk_sequence: int
    ) -> HTMLResponse:
        try:
            body = store.document_chunk_html(share_id, sequence, chunk_sequence)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Document chunk not found") from exc
        response = HTMLResponse(body)
        _set_context_link(response, share_id)
        return response

    @app.get(
        "/AgentArm/share/{share_id}/project/media/{sequence}/{filename:path}",
        include_in_schema=False,
    )
    async def project_media(share_id: str, sequence: int, filename: str) -> Response:
        try:
            asset = store.media(share_id, sequence)
        except (FileNotFoundError, SnapshotError, ValueError) as exc:
            raise HTTPException(404, "Image not found") from exc
        if filename != Path(asset.path).name:
            raise HTTPException(404, "Image not found")
        response = Response(
            asset.data,
            media_type=asset.content_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    "inline; filename*=UTF-8''" + quote(filename, safe="")
                ),
                "Content-Length": str(len(asset.data)),
                "Content-Security-Policy": (
                    "sandbox; default-src 'none'; img-src data:; "
                    "style-src 'unsafe-inline'"
                ),
            },
        )
        if Path(asset.path).suffix.casefold() == ".svgz":
            response.headers["Content-Encoding"] = "gzip"
        _set_context_link(response, share_id)
        return response

    @app.get("/AgentArm/healthz", include_in_schema=False)
    async def health() -> dict[str, int | str]:
        return {
            "status": "ok",
            "service": "agentarm",
            "temporary_workspaces": store.count(),
            "memory_bytes": store.memory_bytes(),
        }

    return app


async def _cleanup_loop(store: SnapshotStore, relay: EphemeralRelay) -> None:
    while True:
        await asyncio.sleep(60)
        for share_id in store.cleanup():
            relay.forget(share_id)
        relay.cleanup()


def _snapshot_json(store: SnapshotStore, share_id: str, name: str) -> JSONResponse:
    try:
        body = store.json_document(share_id, name)
    except (FileNotFoundError, SnapshotError, ValueError) as exc:
        raise HTTPException(404, "Snapshot not found") from exc
    response = _json_response(body)
    if name != "context.json":
        _set_context_link(response, share_id)
    return response


def _document_links_response(
    store: SnapshotStore,
    share_id: str,
    page: int,
) -> HTMLResponse:
    try:
        body = store.document_links_html(share_id, page)
    except (FileNotFoundError, SnapshotError, ValueError) as exc:
        raise HTTPException(404, "Document link page not found") from exc
    response = HTMLResponse(body)
    _set_context_link(response, share_id)
    return response


def _json_response(value: Any) -> JSONResponse:
    return JSONResponse(
        value,
        media_type="application/json; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": "inline",
        },
    )


def _set_context_link(response: Response, share_id: str) -> None:
    response.headers["Link"] = (
        f"</AgentArm/share/{share_id}/context.json>; "
        'rel="describedby"; type="application/json"'
    )


async def _read_snapshot_form(
    request: Request, settings: Settings
) -> tuple[FormData, dict[str, Any], list[tuple[str, UploadFile]]]:
    form = await request.form(
        max_files=settings.max_files,
        max_fields=settings.max_files + 10,
        max_part_size=settings.max_file_bytes,
    )
    try:
        metadata = form.get("metadata")
        paths = form.getlist("paths")
        files = form.getlist("files")
        if not isinstance(metadata, str):
            raise HTTPException(422, "Snapshot metadata is required")
        if not all(isinstance(path, str) for path in paths) or not all(
            isinstance(upload, UploadFile) for upload in files
        ):
            raise HTTPException(422, "Invalid snapshot upload")
        if len(files) != len(paths):
            raise HTTPException(422, "Each uploaded document needs one path")
        if len(files) > settings.max_files:
            raise HTTPException(413, "Too many documents")
        try:
            parsed = json.loads(metadata)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "Invalid snapshot metadata") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(422, "Snapshot metadata must be an object")
        total = 0
        uploads: list[tuple[str, UploadFile]] = []
        for path, upload in zip(paths, files, strict=True):
            size = upload.size
            if size is None:
                raise HTTPException(422, f"Document size is unavailable: {path}")
            if size > settings.max_file_bytes:
                raise HTTPException(413, f"Document exceeds size limit: {path}")
            total += size
            if total > settings.max_upload_bytes:
                raise HTTPException(413, "Snapshot exceeds upload size limit")
            uploads.append((path, upload))
        return form, parsed, uploads
    except BaseException:
        await form.close()
        raise


def _share_urls(share_id: str) -> dict[str, str]:
    return {
        "context_url": f"/AgentArm/share/{share_id}/context.json",
        "view_url": f"/AgentArm/share/{share_id}/",
        "git_url": f"/AgentArm/share/{share_id}/git.json",
        "git_commit_links_url": (f"/AgentArm/share/{share_id}/git-commit-links.html"),
        "project_url": f"/AgentArm/share/{share_id}/project.json",
        "document_links_url": f"/AgentArm/share/{share_id}/document-links.html",
        "project_full_url": f"/AgentArm/share/{share_id}/project-full.json",
        "changes_url": f"/AgentArm/api/snapshots/{share_id}/changes",
    }


def _absolute_share_urls(request: Request, share_id: str) -> dict[str, str]:
    origin = str(request.base_url).rstrip("/")
    return {name: f"{origin}{path}" for name, path in _share_urls(share_id).items()}


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "A JSON request body is required") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "JSON request body must be an object")
    return payload


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token:
        raise HTTPException(401, "Temporary change capability required")
    return token


def _validated_changes(value: dict[str, Any], settings: Settings) -> dict[str, Any]:
    if value.get("format") != "agentarm.changes.v1":
        raise SnapshotError("Change format must be agentarm.changes.v1")
    title = str(value.get("title") or "").strip()
    if not title or len(title) > 200:
        raise SnapshotError("Change title must be 1-200 characters")
    operations = value.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise SnapshotError("Changes must contain 1-100 operations")
    total_content = 0
    validated = []
    seen: set[str] = set()
    for item in operations:
        if not isinstance(item, dict):
            raise SnapshotError("Each change operation must be an object")
        operation = item.get("op")
        if not isinstance(operation, str) or operation not in {"write", "delete"}:
            raise SnapshotError("Change operation must be write or delete")
        path = safe_relative_path(str(item.get("path") or ""))
        if path in seen:
            raise SnapshotError(f"Duplicate change path: {path}")
        if is_sensitive_path(path) or is_ignored_path(path):
            raise SnapshotError(f"Protected path cannot be changed: {path}")
        if not is_editable_text_path(path):
            raise SnapshotError(f"Only supported text documents can change: {path}")
        base = item.get("base_sha256")
        if base is not None and (
            not isinstance(base, str)
            or len(base) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in base)
        ):
            raise SnapshotError(f"Invalid base_sha256: {path}")
        normalized: dict[str, Any] = {
            "op": operation,
            "path": path,
            "base_sha256": base.casefold() if isinstance(base, str) else None,
        }
        if operation == "write":
            content = item.get("content")
            if not isinstance(content, str):
                raise SnapshotError(f"Write content must be text: {path}")
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > settings.max_file_bytes:
                raise SnapshotError(f"Write exceeds per-file limit: {path}")
            total_content += content_bytes
            if total_content > 16 * 1024 * 1024:
                raise SnapshotError("Change content exceeds the 16 MiB limit")
            normalized["content"] = content
        validated.append(normalized)
        seen.add(path)
    return {
        "format": "agentarm.changes.v1",
        "title": title,
        "operations": validated,
    }


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin.rstrip("/") != expected.rstrip("/"):
        raise HTTPException(403, "Cross-origin publishing is not allowed")


def _check_rate_limit(
    request: Request,
    windows: dict[str, deque[float]],
    limit: int,
) -> None:
    source = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = windows[source]
    while window and window[0] < now - 3600:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(429, "Snapshot publishing rate limit reached")
    window.append(now)
    if len(windows) > 10_000:
        stale = [
            key
            for key, values in windows.items()
            if not values or values[-1] < now - 3600
        ]
        for key in stale[:2_000]:
            windows.pop(key, None)
