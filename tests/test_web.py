from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from agentarm.config import Settings
from agentarm.web import create_app


class WebIntegrationTests(unittest.TestCase):
    def test_invalid_json_encoding_returns_client_error(self) -> None:
        response = self.client.post(
            "/AgentArm/api/snapshots/TemporaryWorkspace123/bridge/poll",
            content=b"\x7b\x22bridge_token\x22:\x22\xff\x22\x7d",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)

    def test_malformed_changes_return_client_error(self) -> None:
        workspace = self.create_snapshot().json()
        for operation in ([], {}, "invalid"):
            with self.subTest(operation=operation):
                response = self.client.post(
                    workspace["changes_url"],
                    headers={"Authorization": f"Bearer {workspace['write_token']}"},
                    json={
                        "format": "agentarm.changes.v1",
                        "title": "Invalid",
                        "operations": [{"op": operation, "path": "README.md"}],
                    },
                )
                self.assertEqual(response.status_code, 422)

    def setUp(self) -> None:
        self.settings = Settings(uploads_per_hour=20)
        self.client_context = TestClient(create_app(self.settings))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def create_snapshot(self, path: str = "README.md", content: bytes = b"# Demo\n"):
        metadata = {
            "project_name": "Browser Project",
            "directories": ["src"],
            "git": {
                "detected": True,
                "head": "ref: refs/heads/main",
                "branch": "refs/heads/main",
                "refs": [{"name": "refs/heads/main", "sha": "a" * 40}],
                "history": [],
                "tracked_paths": [path],
            },
        }
        return self.client.post(
            "/AgentArm/api/snapshots",
            data={
                "metadata": json.dumps(metadata),
                "paths": path,
            },
            files={
                "files": (
                    path.rsplit("/", 1)[-1],
                    content,
                    "application/octet-stream",
                )
            },
        )

    def test_root_keeps_passwordless_picker_and_agent_download_entry(self) -> None:
        response = self.client.get("/AgentArm/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose Folder", response.text)
        self.assertIn('href="/folderlens/agent/"', response.text)
        self.assertIn("Download FolderLens Agent", response.text)
        setup = self.client.get("/AgentArm/agent/")
        self.assertEqual(setup.status_code, 200)
        self.assertIn('<html lang="tr">', setup.text)
        self.assertIn("get_config", setup.text)
        self.assertEqual(setup.headers["x-frame-options"], "DENY")
        self.assertEqual(setup.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/AgentArm/agent").content, setup.content)
        self.assertEqual(
            self.client.get(
                "/AgentArm/agent/.folderlens/tunnel/runtime.key"
            ).status_code,
            404,
        )
        self.assertIn('id="folderStage"', response.text)
        self.assertIn('id="idlePet"', response.text)
        self.assertIn('class="idle-pet" aria-hidden="true"', response.text)
        self.assertIn('id="idlePetPaws"', response.text)
        self.assertIn('class="idle-pet-paws" aria-hidden="true"', response.text)
        self.assertIn('class="idle-pet-face"', response.text)
        self.assertIn('id="excludedDetails"', response.text)
        self.assertIn("not published in the AI view", response.text)
        # nose and mouth were intentionally removed from the idle pet
        self.assertNotIn("idle-pet-nose", response.text)
        self.assertNotIn("idle-pet-smile", response.text)
        self.assertIn('focusable="false"', response.text)
        lowered = response.text.casefold()
        for obsolete in ("password", "parola", "login", "oauth", "runner"):
            self.assertNotIn(obsolete, lowered)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["cache-control"], "no-store")
        local_root = self.client.get("/", follow_redirects=False)
        self.assertEqual(local_root.status_code, 307)
        self.assertEqual(local_root.headers["location"], "/AgentArm/")
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)
        policy = self.client.get("/AgentArm/api/policy").json()
        self.assertEqual(policy["max_files"], self.settings.max_files)
        self.assertEqual(policy["max_git_commits"], self.settings.max_git_commits)

    def test_agent_guide_languages_versions_and_friendly_aliases(self) -> None:
        pages = {}
        for lang in ("tr", "en"):
            response = self.client.get("/folderlens/agent/", params={"lang": lang})
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.text, "html.parser")
            self.assertEqual(soup.html["lang"], lang)
            self.assertEqual(
                soup.select_one('[aria-current="page"]').text, lang.upper()
            )
            self.assertEqual(len(soup.select("h1")), 1)
            anchors = {a["href"][1:] for a in soup.select('a[href^="#"]')}
            sections = {s["id"] for s in soup.select("section[id]")}
            self.assertEqual(anchors, sections)
            self.assertEqual(len(sections), 10)
            self.assertNotIn("<!--agent-dependencies-->", response.text)
            self.assertIn("mcp==2.3.0", response.text)
            self.assertEqual(
                response.content,
                self.client.get("/AgentArm/agent/", params={"lang": lang}).content,
            )
            pages[lang] = [p.text for p in soup.select("pre code")]
        self.assertEqual(pages["tr"], pages["en"])
        self.assertEqual(
            self.client.get("/folderlens/agent/?lang=other").status_code, 422
        )
        self.assertEqual(
            self.client.get("/folderlens/").content,
            self.client.get("/AgentArm/").content,
        )

    def test_download_allowlist_missing_package_headers_and_range(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("agentarm.web.ROOT", Path(temporary)),
        ):
            root = Path(temporary)
            self.assertEqual(
                self.client.get(
                    "/folderlens/downloads/folderlens-agent.zip"
                ).status_code,
                503,
            )
            (root / "downloads").mkdir()
            path = root / "downloads/folderlens-agent.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("FolderLens/README.md", "download fixture")
            response = self.client.get("/folderlens/downloads/folderlens-agent.zip")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, path.read_bytes())
            self.assertEqual(response.headers["content-type"], "application/zip")
            self.assertIn("attachment;", response.headers["content-disposition"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                self.client.head("/folderlens/downloads/folderlens-agent.zip").content,
                b"",
            )
            partial = self.client.get(
                "/AgentArm/downloads/folderlens-agent.zip",
                headers={"Range": "bytes=0-3"},
            )
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, path.read_bytes()[:4])
            for name in ("runtime.key", "other.zip", "../runtime.key"):
                self.assertEqual(
                    self.client.get("/folderlens/downloads/" + name).status_code, 404
                )

    def test_idle_pet_assets_are_event_driven_and_motion_safe(self) -> None:
        script = self.client.get("/AgentArm/assets/app.js")
        git_reader = self.client.get("/AgentArm/assets/git-reader.js")
        styles = self.client.get("/AgentArm/assets/styles.css")
        self.assertEqual(script.status_code, 200)
        self.assertEqual(git_reader.status_code, 200)
        self.assertEqual(styles.status_code, 200)
        self.assertIn("window.AgentArmGit", git_reader.text)
        self.assertIn("DecompressionStream", git_reader.text)
        self.assertIn('window.addEventListener("pointermove"', script.text)
        self.assertIn("passive: true", script.text)
        self.assertIn("new AbortController()", script.text)
        self.assertIn("prefers-reduced-motion: reduce", script.text)
        self.assertNotIn("setInterval(", script.text)
        self.assertIn(".folder-stage.pet-visible .idle-pet", styles.text)
        self.assertIn(".folder-stage.pet-visible .idle-pet-paws", styles.text)
        self.assertIn("@keyframes idle-pet-blink", styles.text)
        self.assertIn("@keyframes idle-pet-gaze", styles.text)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles.text)
        self.assertIn(".idle-pet-paws { display: none; }", styles.text)

    def test_browser_imports_readable_subset_instead_of_rejecting_large_project(
        self,
    ) -> None:
        script = self.client.get("/AgentArm/assets/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertNotIn(
            "This project exceeds the published workspace policy", script.text
        )
        self.assertIn("function importPriority(path)", script.text)
        self.assertIn("unreadable_files", script.text)
        self.assertIn("left local:", script.text)
        self.assertIn('".epub", ".ipynb", ".pdf", ".pptx"', script.text)
        self.assertIn("code.textContent = path", script.text)
        self.assertIn("const excludedPathLimit = 2000", script.text)
        self.assertIn(
            "importPriority(left.path) - importPriority(right.path)", script.text
        )

    def test_excluded_path_details_remain_in_owner_browser(self) -> None:
        private_path = ".env.production"
        metadata = {
            "project_name": "Privacy Boundary",
            "directories": [],
            "excluded": {
                "ignored_directories": 0,
                "protected_files": 1,
                "unsupported_files": 0,
                "unreadable_files": 0,
                "oversized_files": 0,
                "file_limit_files": 0,
                "upload_budget_files": 0,
            },
            "excludedPaths": {
                "paths": {"protected_files": [private_path]},
            },
        }
        created = self.client.post(
            "/AgentArm/api/snapshots",
            data={"metadata": json.dumps(metadata), "paths": "README.md"},
            files={"files": ("README.md", b"# Safe\n", "text/plain")},
        )
        self.assertEqual(created.status_code, 201, created.text)
        result = created.json()
        project = self.client.get(result["project_full_url"])
        self.assertEqual(project.status_code, 200)
        self.assertEqual(project.json()["summary"]["excluded"]["protected_files"], 1)
        self.assertNotIn(private_path, project.text)
        self.assertNotIn("excludedPaths", project.text)

    def test_public_snapshot_html_and_json_are_fetchable_without_session(self) -> None:
        created = self.create_snapshot(content="Exact Türkçe.\r\nSecond line.".encode())
        self.assertEqual(created.status_code, 201, created.text)
        result = created.json()

        page = self.client.get(result["view_url"])
        context = self.client.get(result["context_url"])
        document_links = self.client.get(result["document_links_url"])
        git = self.client.get(result["git_url"])
        git_commit_links = self.client.get(result["git_commit_links_url"])
        project = self.client.get(result["project_url"])
        project_full = self.client.get(result["project_full_url"])
        discovery = self.client.get(result["changes_url"])
        document = self.client.get(project.json()["documents"][0]["document_url"])
        self.assertEqual(page.status_code, 200)
        self.assertLess(page.text.index(">AI Context<"), page.text.index(">Git<"))
        self.assertLess(page.text.index(">Git<"), page.text.index(">Project<"))
        self.assertIn("Fetch Index JSON", page.text)
        self.assertNotIn("Exact Türkçe.", page.text)
        self.assertEqual(context.json()["format"], "agentarm.ai-context.v1")
        self.assertEqual(context.json()["workspace"]["documents"], 1)
        self.assertIn("native web", context.json()["transport"]["preferred"])
        self.assertIn("Could not resolve host", context.text)
        self.assertEqual(document_links.status_code, 200)
        self.assertIn(
            f'<a href="{project.json()["documents"][0]["document_url"]}">',
            document_links.text,
        )
        self.assertIn("Follow these real", document_links.text)
        self.assertEqual(git.json()["format"], "agentarm.git.v1")
        self.assertEqual(git_commit_links.status_code, 200)
        self.assertIn(
            f"/AgentArm/share/{result['share_id']}/git/commits/1.json",
            git_commit_links.text,
        )
        git_commit_page = self.client.get(
            f"/AgentArm/share/{result['share_id']}/git/commits/1.json"
        )
        self.assertEqual(git_commit_page.status_code, 200)
        self.assertEqual(git_commit_page.json()["commits"], [])
        self.assertEqual(project.json()["format"], "agentarm.project-index.v1")
        self.assertEqual(project_full.json()["format"], "agentarm.project.v1")
        self.assertEqual(git.json()["ai_context"]["context_url"], result["context_url"])
        self.assertEqual(
            project.json()["ai_context"]["context_url"], result["context_url"]
        )
        self.assertEqual(
            document.json()["ai_context"]["context_url"], result["context_url"]
        )
        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(
            discovery.json(),
            {
                "format": "agentarm.workspace-access.v1",
                "project": "Browser Project",
                "generated_at": project.json()["generated_at"],
                "read": {
                    "method": "GET",
                    "context_url": f"http://testserver{result['context_url']}",
                    "view_url": f"http://testserver{result['view_url']}",
                    "git_url": f"http://testserver{result['git_url']}",
                    "git_commit_links_url": (
                        f"http://testserver{result['git_commit_links_url']}"
                    ),
                    "project_index_url": f"http://testserver{result['project_url']}",
                    "document_links_url": (
                        f"http://testserver{result['document_links_url']}"
                    ),
                    "project_full_url": (
                        f"http://testserver{result['project_full_url']}"
                    ),
                    "project_document_url_template": (
                        "http://testserver/AgentArm/share/"
                        f"{result['share_id']}/project/documents/{{sequence}}.json"
                    ),
                },
                "write": {
                    "method": "POST",
                    "endpoint": f"http://testserver{result['changes_url']}",
                    "authorization": (
                        "Bearer temporary capability from the folder owner"
                    ),
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
            },
        )
        self.assertNotIn(result["write_token"], discovery.text)
        expected_link = (
            f"</AgentArm/share/{result['share_id']}/context.json>; "
            'rel="describedby"; type="application/json"'
        )
        self.assertEqual(page.headers["link"], expected_link)
        self.assertEqual(document_links.headers["link"], expected_link)
        self.assertEqual(project.headers["link"], expected_link)
        self.assertEqual(document.headers["link"], expected_link)
        self.assertNotIn("content", project.json()["documents"][0])
        self.assertEqual(
            document.json()["document"]["content"],
            "Exact Türkçe.\r\nSecond line.",
        )
        self.assertEqual(project.headers["cache-control"], "no-store")
        self.assertEqual(
            project.headers["content-type"], "application/json; charset=utf-8"
        )
        self.assertIn("temporary memory", page.text)
        self.assertIn("fetch_only", project_full.text)
        self.assertNotIn(result["write_token"], context.text)

    def test_images_are_available_as_unchanged_original_bytes(self) -> None:
        original = b"\x89PNG\r\n\x1a\nagentarm-original-pixels"
        created = self.create_snapshot("assets/figure #1.png", original)
        self.assertEqual(created.status_code, 201, created.text)
        result = created.json()
        project = self.client.get(result["project_url"]).json()
        item = project["documents"][0]
        self.assertEqual(item["kind"], "image")
        self.assertEqual(item["content_type"], "image/png")
        self.assertIn("media_url", item)
        self.assertTrue(item["media_url"].endswith("/figure%20%231.png"))
        document = self.client.get(item["document_url"])
        self.assertNotIn("content", document.json()["document"])
        self.assertIn("no lossy conversion", document.text)
        media = self.client.get(item["media_url"])
        self.assertEqual(media.status_code, 200)
        self.assertEqual(media.content, original)
        self.assertEqual(media.headers["content-type"], "image/png")
        self.assertEqual(media.headers["content-length"], str(len(original)))
        self.assertIn("sandbox", media.headers["content-security-policy"])
        self.assertEqual(media.headers["cache-control"], "no-store")
        links = self.client.get(result["document_links_url"])
        self.assertIn(f'href="{item["media_url"]}"', links.text)
        missing = item["media_url"].rsplit("/", 1)[0] + "/wrong.png"
        self.assertEqual(self.client.get(missing).status_code, 404)

    def test_extraction_failures_are_visible_to_folder_owner(self) -> None:
        response = self.create_snapshot("legacy.txt", b"\xff")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["extraction_errors"], 1)
        page = self.client.get("/AgentArm/")
        self.assertIn('id="loadProgress"', page.text)
        self.assertIn("Anyone with the view link", page.text)

    def test_reader_escapes_source_and_links_next_chunk(self) -> None:
        result = self.create_snapshot(
            "code.txt", b"<script>alert(1)</script>\r\n" * 2000
        ).json()
        item = self.client.get(result["project_url"]).json()["documents"][0]
        url = item["chunk_url_template"].format(chunk=1).replace(".json", ".html")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertIn('rel="next"', response.text)
        self.assertIn("Lines 1", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.get(url.replace("/1.html", "/0.html")).status_code, 404
        )
        missing = "/AgentArm/share/" + "a" * 24 + "/project-full.json"
        self.assertEqual(self.client.get(missing).status_code, 404)

    def test_large_text_chunk_links_reconstruct_exact_content(self) -> None:
        exact = "İçerik — exact whitespace.\r\n" * 90_000
        created = self.create_snapshot("docs/large.txt", exact.encode("utf-8"))
        self.assertEqual(created.status_code, 201, created.text)
        result = created.json()
        project = self.client.get(result["project_url"]).json()
        item = project["documents"][0]
        self.assertTrue(item["chunked"])
        manifest = self.client.get(item["document_url"]).json()["document"]
        self.assertNotIn("content", manifest)
        chunk_links = self.client.get(item["chunk_links_url"])
        self.assertEqual(chunk_links.status_code, 200)
        contents = []
        for sequence in range(1, item["chunk_count"] + 1):
            url = item["chunk_url_template"].format(chunk=sequence)
            self.assertIn(f'href="{url}"', chunk_links.text)
            chunk = self.client.get(url)
            self.assertEqual(chunk.status_code, 200)
            contents.append(chunk.json()["chunk"]["content"])
        self.assertEqual("".join(contents), exact)

    def test_origin_path_and_legacy_surfaces_fail_closed(self) -> None:
        rejected = self.client.post(
            "/AgentArm/api/snapshots",
            headers={"Origin": "https://attacker.example"},
            data={"metadata": "{}", "paths": "README.md"},
            files={"files": ("README.md", b"x", "text/plain")},
        )
        self.assertEqual(rejected.status_code, 403)
        secret = self.create_snapshot(".env", b"SECRET=value")
        self.assertEqual(secret.status_code, 422)
        dotfile = self.create_snapshot(".gitignore", b".venv/\n")
        self.assertEqual(dotfile.status_code, 201, dotfile.text)
        result = dotfile.json()
        unauthorized = self.client.post(
            result["changes_url"],
            json={
                "format": "agentarm.changes.v1",
                "title": "No token",
                "operations": [
                    {
                        "op": "write",
                        "path": "README.md",
                        "base_sha256": None,
                        "content": "x",
                    }
                ],
            },
        )
        self.assertEqual(unauthorized.status_code, 401)
        for path in (
            "/AgentArm/login",
            "/AgentArm/api/session",
            "/AgentArm/mcp",
            "/AgentArm/actions/openapi.json",
            "/AgentArm/authorize",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_temporary_change_capability_reaches_live_browser_bridge(self) -> None:
        created = self.create_snapshot()
        self.assertEqual(created.status_code, 201, created.text)
        workspace = created.json()
        change = {
            "format": "agentarm.changes.v1",
            "title": "Bridge integration test",
            "operations": [
                {
                    "op": "write",
                    "path": "README.md",
                    "base_sha256": "b" * 64,
                    "content": "# Updated\n",
                }
            ],
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                self.client.post,
                workspace["changes_url"],
                headers={"Authorization": f"Bearer {workspace['write_token']}"},
                json=change,
            )
            polled = self.client.post(
                (f"/AgentArm/api/snapshots/{workspace['share_id']}/bridge/poll"),
                json={"bridge_token": workspace["bridge_token"]},
            )
            self.assertEqual(polled.status_code, 200, polled.text)
            command = polled.json()["command"]
            self.assertEqual(command["payload"], change)
            answered = self.client.post(
                (f"/AgentArm/api/snapshots/{workspace['share_id']}/bridge/respond"),
                json={
                    "bridge_token": workspace["bridge_token"],
                    "request_id": command["id"],
                    "result": {"state": "applied", "operation_count": 1},
                },
            )
            self.assertEqual(answered.status_code, 200, answered.text)
            result = pending.result(timeout=2)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["state"], "applied")

    def test_disconnect_forgets_temporary_workspace(self) -> None:
        created = self.create_snapshot().json()
        disconnected = self.client.post(
            (f"/AgentArm/api/snapshots/{created['share_id']}/bridge/disconnect"),
            json={"bridge_token": created["bridge_token"]},
        )
        self.assertEqual(disconnected.status_code, 200, disconnected.text)
        self.assertEqual(self.client.get(created["view_url"]).status_code, 404)
        self.assertEqual(self.client.get(created["context_url"]).status_code, 404)
        self.assertEqual(
            self.client.get(created["document_links_url"]).status_code,
            404,
        )
        self.assertEqual(self.client.get(created["changes_url"]).status_code, 404)
        self.assertEqual(
            self.client.get("/AgentArm/healthz").json()["temporary_workspaces"],
            0,
        )

    def test_health_reports_only_public_snapshot_count(self) -> None:
        self.assertEqual(self.create_snapshot().status_code, 201)
        health = self.client.get("/AgentArm/healthz")
        payload = health.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "agentarm")
        self.assertEqual(payload["temporary_workspaces"], 1)
        self.assertGreater(payload["memory_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
