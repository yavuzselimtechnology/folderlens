import base64
import hashlib
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from starlette.testclient import TestClient

from agentarm.mcp_server import create_agent
from agentarm.oauth import EXEC_SCOPE, READ_SCOPE, WRITE_SCOPE, OwnerOAuthProvider
from agentarm.shutdown import ShutdownControl
from agentarm.workspace import AgentPolicy, Workspace
from folderlens import http_app, main


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "private-auth"
        self.policy = AgentPolicy(self.root, allow_write=True, allow_exec=True)
        self.callback = "https://client.example/callback"
        self.provider = self.make_provider(self.policy)
        self.pins = {}
        self.provider.notify_owner = lambda key, pin, client, scopes: self.pins.update(
            {key: pin}
        )
        self.stopped = []
        self.shutdown_control = ShutdownControl()
        self.shutdown_control.bind(lambda: self.stopped.append(True))
        server = create_agent(
            self.policy,
            oauth_provider=self.provider,
            shutdown_control=self.shutdown_control,
        )
        self.client = TestClient(
            http_app(
                server,
                public_origin=self.provider.origin,
                oauth_provider=self.provider,
                shutdown_control=self.shutdown_control,
            ),
            base_url=self.provider.origin,
            follow_redirects=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def make_provider(self, policy, **kwargs):
        provider = OwnerOAuthProvider(
            policy, "https://agent.example", [self.callback], self.state, **kwargs
        )
        self.addCleanup(provider.close)
        return provider

    def register(self, scopes=None, confidential=False, auth_method=None):
        scopes = scopes or [READ_SCOPE, WRITE_SCOPE, EXEC_SCOPE]
        result = self.client.post(
            "/register",
            json={
                "redirect_uris": [self.callback],
                "client_name": "Synthetic test client",
                "scope": " ".join(scopes),
                "token_endpoint_auth_method": auth_method
                or ("client_secret_post" if confidential else "none"),
            },
        )
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def begin(self, client, scopes=None, resource=None):
        verifier = "test-code-verifier-for-pkce-with-enough-entropy-length-123456789"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        result = self.client.get(
            "/authorize",
            params={
                "client_id": client["client_id"],
                "redirect_uri": self.callback,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": " ".join(scopes or [READ_SCOPE, WRITE_SCOPE, EXEC_SCOPE]),
                "resource": resource or self.provider.resource,
                "state": "round-trip & state",
            },
        )
        self.assertEqual(result.status_code, 302, result.text)
        location = result.headers["location"]
        request_id = urlsplit(location).path.rsplit("/", 1)[1]
        return location, request_id, verifier

    def approve(self, location, request_id, pin=None, origin=None, csrf_override=None):
        form = self.client.get(location)
        self.assertEqual(form.status_code, 200, form.text)
        self.assertNotIn(str(self.root), form.text)
        self.assertNotIn(self.pins[request_id], form.text)
        csrf = BeautifulSoup(form.text, "html.parser").find("input", {"name": "csrf"})[
            "value"
        ]
        return self.client.post(
            location,
            data={
                "pin": pin if pin is not None else self.pins[request_id],
                "csrf": csrf_override or csrf,
                "decision": "approve",
            },
            headers={"Origin": origin or self.provider.origin},
        )

    def exchange(self, client, code, verifier, resource=None):
        form = {
            "client_id": client["client_id"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.callback,
            "code_verifier": verifier,
            "resource": resource or self.provider.resource,
        }
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        return self.client.post("/token", data=form)

    def token(self, scopes=None, confidential=False):
        client = self.register(scopes, confidential)
        location, request_id, verifier = self.begin(client, scopes)
        approved = self.approve(location, request_id)
        self.assertEqual(approved.status_code, 303, approved.text)
        query = parse_qs(urlsplit(approved.headers["location"]).query)
        self.assertEqual(query["iss"], [self.provider.issuer])
        self.assertEqual(query["state"], ["round-trip & state"])
        result = self.exchange(client, query["code"][0], verifier)
        self.assertEqual(result.status_code, 200, result.text)
        return client, result.json()

    def rpc(self, token, method="tools/call", params=None):
        return self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params or {"name": "get_config", "arguments": {}},
            },
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-03-26",
            },
        )

    def mint_existing(self, client, scopes=None):
        location, request_id, verifier = self.begin(client, scopes)
        approved = self.approve(location, request_id)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        token = self.exchange(client, code, verifier)
        self.assertEqual(token.status_code, 200, token.text)
        return token.json()

    def remote_rpc(self, token, name, arguments):
        response = self.rpc(token, params={"name": name, "arguments": arguments})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def test_remote_calls_enforce_target_scopes_and_owner_and_deduplicate_write(self):
        client, full = self.token()
        read = self.mint_existing(client, [READ_SCOPE])
        args = {
            "tool_name": "write_file",
            "request_id": "oauth-once",
            "arguments": {
                "path": "once.txt",
                "content": "exactly once",
                "base_sha256": None,
                "preview": False,
            },
        }
        denied = self.remote_rpc(read["access_token"], "start_tool_call", args)
        self.assertTrue(denied["isError"])
        self.assertIn("mcp/www_authenticate", denied["_meta"])
        started = self.remote_rpc(full["access_token"], "start_tool_call", args)
        call_id = started["structuredContent"]["call_id"]
        completed = self.remote_rpc(
            full["access_token"],
            "get_tool_result",
            {"call_id": call_id, "wait_ms": 2000},
        )
        self.assertEqual(completed["structuredContent"]["call"]["status"], "completed")
        self.assertEqual((self.root / "once.txt").read_text(), "exactly once")
        duplicate = self.remote_rpc(full["access_token"], "start_tool_call", args)
        self.assertTrue(duplicate["structuredContent"]["deduplicated"])
        self.assertEqual(duplicate["structuredContent"]["call_id"], call_id)
        self.assertTrue(
            self.remote_rpc(
                read["access_token"], "get_tool_result", {"call_id": call_id}
            )["isError"]
        )
        _, other = self.token()
        self.assertTrue(
            self.remote_rpc(
                other["access_token"], "get_tool_result", {"call_id": call_id}
            )["isError"]
        )

    def test_queued_remote_call_rechecks_revoked_token_before_side_effect(self):
        _, first_token = self.token()
        client, revoked = self.token()
        observer = self.mint_existing(client)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = Workspace.write_file

        def gated(workspace, path, *args, **kwargs):
            if path == "first.txt":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Fixture release was not received")
            return original(workspace, path, *args, **kwargs)

        def start(token, key, path):
            return self.remote_rpc(
                token["access_token"],
                "start_tool_call",
                {
                    "tool_name": "write_file",
                    "request_id": key,
                    "arguments": {
                        "path": path,
                        "content": "fixture",
                        "base_sha256": None,
                        "preview": False,
                    },
                },
            )["structuredContent"]["call_id"]

        with patch.object(Workspace, "write_file", gated):
            first = start(first_token, "first-job", "first.txt")
            self.assertTrue(entered.wait(2))
            blocked = start(revoked, "queued-job", "must-not-exist.txt")
            response = self.client.post(
                "/revoke",
                data={
                    "client_id": client["client_id"],
                    "token": revoked["access_token"],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            release.set()
            self.remote_rpc(
                first_token["access_token"],
                "get_tool_result",
                {"call_id": first, "wait_ms": 2000},
            )
            finished = self.remote_rpc(
                observer["access_token"],
                "get_tool_result",
                {"call_id": blocked, "wait_ms": 2000},
            )
        self.assertTrue(finished["isError"])
        self.assertEqual(finished["structuredContent"]["call"]["status"], "failed")
        self.assertNotIn("mcp/www_authenticate", finished.get("_meta", {}))
        self.assertIn(
            "mcp/www_authenticate", finished["structuredContent"]["result_meta"]
        )
        self.assertFalse((self.root / "must-not-exist.txt").exists())

    def test_shutdown_requires_exec_correct_device_and_blocks_new_tool_access(self):
        for scopes in ([READ_SCOPE], [READ_SCOPE, WRITE_SCOPE]):
            _, token = self.token(scopes)
            denied = self.rpc(
                token["access_token"],
                params={"name": "shutdown", "arguments": {"device_id": "folderlens"}},
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(self.stopped, [])
            config = self.remote_rpc(token["access_token"], "get_config", {})
            state = config.get("structuredContent") or json.loads(
                config["content"][0]["text"]
            )
            self.assertFalse(state["remote_shutdown_available"])
        _, full = self.token()
        wrong = self.remote_rpc(
            full["access_token"], "shutdown", {"device_id": "wrong-device"}
        )
        self.assertTrue(wrong["isError"])
        self.assertEqual(self.stopped, [])
        accepted = self.remote_rpc(
            full["access_token"], "shutdown", {"device_id": "folderlens"}
        )
        payload = accepted.get("structuredContent") or json.loads(
            accepted["content"][0]["text"]
        )
        self.assertTrue(payload["shutdown_requested"])
        self.assertEqual(self.stopped, [True])
        blocked = self.remote_rpc(full["access_token"], "get_config", {})
        self.assertTrue(blocked["isError"])
        self.assertIn("shutting down", blocked["content"][0]["text"])

    def test_discovery_public_client_pkce_and_single_use_code(self):
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
        self.assertIn("S256", metadata["code_challenge_methods_supported"])
        self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])
        missing = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertIn("resource_metadata", missing.headers["www-authenticate"])
        advertised = re.search(
            r'resource_metadata="([^"]+)"', missing.headers["www-authenticate"]
        )[1]
        resource_metadata = self.client.get(advertised)
        self.assertEqual(resource_metadata.status_code, 200, resource_metadata.text)
        self.assertEqual(resource_metadata.json()["resource"], self.provider.resource)
        client = self.register()
        location, request_id, verifier = self.begin(client)
        approved = self.approve(location, request_id)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        self.assertEqual(self.exchange(client, code, "wrong-verifier").status_code, 400)
        wrong_resource = self.exchange(
            client, code, verifier, "https://other.example/mcp"
        )
        self.assertEqual(wrong_resource.json()["error"], "invalid_target")
        minted = self.exchange(client, code, verifier)
        self.assertEqual(minted.status_code, 200, minted.text)
        self.assertEqual(self.exchange(client, code, verifier).status_code, 400)
        call = self.rpc(minted.json()["access_token"])
        self.assertEqual(call.status_code, 200, call.text)
        self.assertFalse(call.json()["result"].get("isError", False), call.text)

    def test_write_scope_without_exec_cannot_terminate_system_process(self):
        _, tokens = self.token([READ_SCOPE, WRITE_SCOPE])
        response = self.rpc(
            tokens["access_token"],
            params={
                "name": "kill_process",
                "arguments": {"pid": 2147483647, "process_key": "denied fixture"},
            },
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn(EXEC_SCOPE, response.headers["www-authenticate"])

    def test_read_scope_denies_write_exec_and_private_auth_state(self):
        _, tokens = self.token([READ_SCOPE])
        token = tokens["access_token"]
        preview = self.rpc(
            token,
            params={
                "name": "preview_changes",
                "arguments": {
                    "path": "planned.txt",
                    "content": "preview only",
                    "base_sha256": None,
                },
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertFalse(preview.json()["result"].get("isError", False), preview.text)
        self.assertFalse((self.root / "planned.txt").exists())
        for name, args in [
            (
                "write_file",
                {
                    "path": "denied.txt",
                    "content": "no",
                    "base_sha256": None,
                    "preview": False,
                },
            ),
            ("start_process", {"command": ["never-run"]}),
            ("list_sessions", {}),
            ("kill_process", {"pid": 2147483647, "process_key": "denied fixture"}),
        ]:
            result = self.rpc(token, params={"name": name, "arguments": args})
            self.assertEqual(result.status_code, 403, result.text)
            self.assertIn("insufficient_scope", result.headers["www-authenticate"])
        state = self.rpc(
            token,
            params={
                "name": "read_file",
                "arguments": {"path": "private-auth/oauth.sqlite3"},
            },
        )
        self.assertTrue(state.json()["result"]["isError"])
        self.assertNotIn("client_secret", state.text)
        self.assertFalse((self.root / "denied.txt").exists())

    def test_refresh_rotates_pair_and_revoke_invalidates_both_tokens(self):
        client, tokens = self.token(confidential=True)
        form = {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "resource": self.provider.resource,
        }
        result = self.client.post("/token", data=form)
        self.assertEqual(result.status_code, 200, result.text)
        fresh = result.json()
        self.assertNotEqual(fresh["refresh_token"], tokens["refresh_token"])
        self.assertEqual(self.client.post("/token", data=form).status_code, 400)
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 401)
        revoked = self.client.post(
            "/revoke",
            data={
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "token": fresh["refresh_token"],
            },
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.rpc(fresh["access_token"]).status_code, 401)

    def test_redirect_allowlist_csrf_origin_pin_attempts_and_host(self):
        refused = self.client.post(
            "/register", json={"redirect_uris": ["https://attacker.example/callback"]}
        )
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(
            self.client.get(
                "/.well-known/oauth-authorization-server",
                headers={"Host": "attacker.example"},
            ).status_code,
            421,
        )
        client = self.register()
        location, request_id, _ = self.begin(client)
        self.assertEqual(
            self.approve(
                location, request_id, origin="https://attacker.example"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.approve(
                location, request_id, csrf_override="not-the-browser-token"
            ).status_code,
            403,
        )
        for _ in range(self.provider.limits.max_pin_attempts):
            self.assertEqual(
                self.approve(location, request_id, pin="wrong").status_code, 403
            )
        self.assertEqual(self.client.get(location).status_code, 410)
        oversized = self.client.post("/register", content=b"x" * 65537)
        self.assertEqual(oversized.status_code, 413)

    def test_authorization_error_echoes_issuer_and_state(self):
        client = self.register()
        location, _, _ = self.begin(client, resource="https://other.example/mcp")
        query = parse_qs(urlsplit(location).query)
        self.assertEqual(query["error"], ["invalid_target"])
        self.assertEqual(query["iss"], [self.provider.issuer])
        self.assertEqual(query["state"], ["round-trip & state"])

    def test_restart_preserves_approved_client_tokens_and_root_change_revokes(self):
        client, tokens = self.token()
        # Close the active app before opening the same private state for restart.
        self.client.__exit__(None, None, None)
        self.client = None
        restarted = self.make_provider(self.policy)
        import asyncio

        self.assertIsNotNone(asyncio.run(restarted.get_client(client["client_id"])))
        self.assertIsNotNone(
            asyncio.run(restarted.load_access_token(tokens["access_token"]))
        )
        serialized = (self.state / "oauth.sqlite3").read_bytes()
        self.assertNotIn(tokens["access_token"].encode(), serialized)
        self.assertNotIn(tokens["refresh_token"].encode(), serialized)
        restarted.close()
        other = self.root / "different-workspace"
        other.mkdir()
        changed = self.make_provider(
            AgentPolicy(other, allow_write=True, allow_exec=True)
        )
        self.assertIsNone(
            asyncio.run(changed.load_access_token(tokens["access_token"]))
        )
        self.assertIsNotNone(asyncio.run(changed.get_client(client["client_id"])))

    def test_basic_client_authentication_without_redundant_form_client_id(self):
        client = self.register(auth_method="client_secret_basic")
        location, request_id, verifier = self.begin(client)
        self.assertRegex(self.pins[request_id], r"^[A-Za-z0-9_-]{32}$")
        approved = self.approve(location, request_id)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        encoded = base64.b64encode(
            (client["client_id"] + ":" + client["client_secret"]).encode()
        ).decode()
        token = self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.callback,
                "code_verifier": verifier,
                "resource": self.provider.resource,
            },
            headers={"Authorization": "Basic " + encoded},
        )
        self.assertEqual(token.status_code, 200, token.text)
        result = self.client.post(
            "/revoke",
            data={"token": token.json()["access_token"]},
            headers={"Authorization": "Basic " + encoded},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.rpc(token.json()["access_token"]).status_code, 401)

    def test_revoke_cannot_target_another_client_and_public_revoke_needs_no_secret(
        self,
    ):
        first, tokens = self.token([READ_SCOPE])
        second, other = self.token([READ_SCOPE])
        refused_effect = self.client.post(
            "/revoke",
            data={"client_id": second["client_id"], "token": tokens["access_token"]},
        )
        self.assertEqual(refused_effect.status_code, 200, refused_effect.text)
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 200)
        revoked = self.client.post(
            "/revoke",
            data={"client_id": first["client_id"], "token": tokens["refresh_token"]},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 401)
        self.assertEqual(self.rpc(other["access_token"]).status_code, 200)

    def test_expired_authorization_code_and_access_token_are_rejected(self):
        import time
        from unittest.mock import patch

        client = self.register([READ_SCOPE])
        location, request_id, verifier = self.begin(client, [READ_SCOPE])
        approved = self.approve(location, request_id)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        with patch(
            "agentarm.oauth.time.time",
            return_value=time.time() + self.provider.limits.code_seconds + 1,
        ):
            self.assertEqual(self.exchange(client, code, verifier).status_code, 400)
        _, tokens = self.token([READ_SCOPE])
        with patch(
            "agentarm.oauth.time.time",
            return_value=time.time() + self.provider.limits.access_seconds + 1,
        ):
            self.assertEqual(self.rpc(tokens["access_token"]).status_code, 401)

    def test_parallel_policy_binding_cannot_read_or_refresh_another_root_token(self):
        import asyncio

        client, tokens = self.token([READ_SCOPE])
        other_root = self.root / "other-root"
        other_root.mkdir()
        other = self.make_provider(
            AgentPolicy(other_root, allow_write=True, allow_exec=True), rebind=False
        )
        self.assertIsNone(asyncio.run(other.load_access_token(tokens["access_token"])))
        self.assertIsNone(
            asyncio.run(
                other.load_refresh_token(
                    asyncio.run(other.get_client(client["client_id"])),
                    tokens["refresh_token"],
                )
            )
        )
        # An inspection-only provider must neither accept nor revoke the real instance's token.
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 200)
        with self.assertRaisesRegex(ValueError, "must match"):
            create_agent(AgentPolicy(other_root), oauth_provider=self.provider)

    def test_local_client_listing_does_not_rebind_and_targeted_revoke_is_immediate(
        self,
    ):
        from contextlib import redirect_stdout

        client, tokens = self.token([READ_SCOPE])
        arguments = [
            "--auth",
            "oauth",
            "--transport",
            "http",
            "--root",
            str(self.root),
            "--public-origin",
            self.provider.origin,
            "--oauth-redirect-uri",
            self.callback,
            "--oauth-state-dir",
            str(self.state),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([*arguments, "--oauth-list-clients"]), 0)
        self.assertNotIn("client_secret", output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())["clients"][0]["client_id"],
            client["client_id"],
        )
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 200)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                main([*arguments, "--oauth-revoke-client", client["client_id"]]), 0
            )
        self.assertEqual(self.rpc(tokens["access_token"]).status_code, 401)


class OAuthStateSafetyTests(unittest.TestCase):
    def test_never_restrict_an_unrelated_nonempty_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "user-file.txt").write_text("keep")
            with self.assertRaisesRegex(ValueError, "dedicated directory"):
                OwnerOAuthProvider(
                    AgentPolicy(root),
                    "https://agent.example",
                    ["https://client.example/callback"],
                    root,
                )
            self.assertEqual((root / "user-file.txt").read_text(), "keep")

    def test_foreign_database_is_rejected_without_altering_its_bytes_or_permissions(
        self,
    ):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "oauth.sqlite3"
            connection = sqlite3.connect(data)
            connection.execute("CREATE TABLE user_data (value TEXT)")
            connection.execute("INSERT INTO user_data VALUES ('keep unchanged')")
            connection.commit()
            connection.close()
            before = data.read_bytes()
            mode = data.stat().st_mode
            with self.assertRaisesRegex(ValueError, "not a supported"):
                OwnerOAuthProvider(
                    AgentPolicy(root),
                    "https://agent.example",
                    ["https://client.example/callback"],
                    root,
                )
            self.assertEqual(data.read_bytes(), before)
            self.assertEqual(data.stat().st_mode, mode)
