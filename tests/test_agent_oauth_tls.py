"""A real TLS+OAuth SDK round trip with a synthetic, explicitly trusted fixture CA."""

import asyncio
import datetime
import ipaddress
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import uvicorn
from bs4 import BeautifulSoup
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from agentarm.mcp_server import create_agent
from agentarm.oauth import EXEC_SCOPE, READ_SCOPE, WRITE_SCOPE, OwnerOAuthProvider
from agentarm.workspace import AgentPolicy
from folderlens import http_app


class MemoryTokens:
    def __init__(self):
        self.tokens = None
        self.client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, value):
        self.tokens = value

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, value):
        self.client = value


class OAuthTLSRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_discovers_registers_authenticates_and_calls_tools_over_verified_tls(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name(
                [
                    x509.NameAttribute(
                        NameOID.COMMON_NAME, "FolderLens local integration fixture"
                    )
                ]
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(
                    x509.SubjectAlternativeName(
                        [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                    ),
                    critical=False,
                )
                .add_extension(
                    x509.BasicConstraints(ca=True, path_length=None), critical=True
                )
                .sign(key, hashes.SHA256())
            )
            cert_path, key_path = root / "fixture.pem", root / "fixture.key"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
            callback_uri = "https://client.example/callback"
            policy = AgentPolicy(root, allow_write=True, allow_exec=True)
            provider = OwnerOAuthProvider(
                policy, origin, [callback_uri], root / "private-auth"
            )
            pins = {}
            provider.notify_owner = lambda request_id, pin, client, scopes: pins.update(
                {request_id: pin}
            )
            server = create_agent(policy, oauth_provider=provider)
            runner = uvicorn.Server(
                uvicorn.Config(
                    http_app(server, public_origin=origin, oauth_provider=provider),
                    host="127.0.0.1",
                    ssl_certfile=str(cert_path),
                    ssl_keyfile=str(key_path),
                    log_level="error",
                    access_log=False,
                )
            )
            thread = threading.Thread(
                target=runner.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not runner.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("TLS fixture server failed to start")
                    await asyncio.sleep(0.02)
                verified = ssl.create_default_context(cafile=str(cert_path))
                authorization_result = None

                async def synthetic_owner_approval(url):
                    nonlocal authorization_result
                    # Only synthetic fixture consent is automated. This never opens
                    # or signs in to ChatGPT, Windows authentication or a real account.
                    async with httpx2.AsyncClient(
                        verify=verified, trust_env=False, follow_redirects=False
                    ) as browser:
                        start = await browser.get(url)
                        self.assertEqual(start.status_code, 302, start.text)
                        consent_url = start.headers["location"]
                        request_id = urlsplit(consent_url).path.rsplit("/", 1)[1]
                        page = await browser.get(consent_url)
                        self.assertEqual(page.status_code, 200, page.text)
                        csrf = BeautifulSoup(page.text, "html.parser").find(
                            "input", {"name": "csrf"}
                        )["value"]
                        approved = await browser.post(
                            consent_url,
                            headers={"Origin": origin},
                            data={
                                "pin": pins[request_id],
                                "csrf": csrf,
                                "decision": "approve",
                            },
                        )
                        self.assertEqual(approved.status_code, 303, approved.text)
                        params = parse_qs(urlsplit(approved.headers["location"]).query)
                        authorization_result = AuthorizationCodeResult(
                            code=params["code"][0],
                            state=params["state"][0],
                            iss=params["iss"][0],
                        )

                async def callback():
                    self.assertIsNotNone(authorization_result)
                    return authorization_result

                storage = MemoryTokens()
                auth = OAuthClientProvider(
                    server_url=origin + "/mcp",
                    client_metadata=OAuthClientMetadata(
                        redirect_uris=[callback_uri],
                        client_name="Synthetic verified-TLS client",
                        token_endpoint_auth_method="none",
                        scope=f"{READ_SCOPE} {WRITE_SCOPE} {EXEC_SCOPE}",
                    ),
                    storage=storage,
                    redirect_handler=synthetic_owner_approval,
                    callback_handler=callback,
                )
                async with (
                    httpx2.AsyncClient(
                        auth=auth, verify=verified, trust_env=False, timeout=15
                    ) as http_client,
                    streamable_http_client(
                        origin + "/mcp", http_client=http_client
                    ) as streams,
                    ClientSession(streams[0], streams[1]) as client,
                ):
                    await client.initialize()
                    result = await client.call_tool(
                        "write_file",
                        {
                            "path": "oauth.txt",
                            "content": "verified TLS + OAuth",
                            "base_sha256": None,
                            "preview": False,
                        },
                    )
                    self.assertFalse(result.is_error, str(result))
                    read = await client.call_tool("read_file", {"path": "oauth.txt"})
                    self.assertIn("verified TLS + OAuth", read.content[0].text)
                    config = await client.call_tool("get_config", {})
                    body = config.structured_content or json.loads(
                        config.content[0].text
                    )
                    self.assertEqual(
                        set(body["oauth_scopes"]), {READ_SCOPE, WRITE_SCOPE}
                    )
                    self.assertIsNotNone(storage.tokens)
                    process = await client.call_tool(
                        "start_process",
                        {
                            "command": [
                                sys.executable,
                                "-u",
                                "-c",
                                "print('OAuth terminal works')",
                            ]
                        },
                    )
                    self.assertFalse(process.is_error, str(process))
                    started = process.structured_content or json.loads(
                        process.content[0].text
                    )
                    deadline = time.monotonic() + 10
                    output = ""
                    offset = 0
                    while time.monotonic() < deadline:
                        result = await client.call_tool(
                            "read_process_output",
                            {"session_id": started["session_id"], "offset": offset},
                        )
                        self.assertFalse(result.is_error, str(result))
                        page = result.structured_content or json.loads(
                            result.content[0].text
                        )
                        output += page["output"]
                        offset = page["next_offset"]
                        if page["status"] == "completed":
                            self.assertEqual(page["exit_code"], 0)
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("OAuth terminal command did not complete")
                    self.assertIn("OAuth terminal works", output)
                    self.assertEqual(
                        set(storage.tokens.scope.split()),
                        {READ_SCOPE, WRITE_SCOPE, EXEC_SCOPE},
                    )
            finally:
                runner.should_exit = True
                await asyncio.to_thread(thread.join, 5)
                listener.close()
                provider.close()
                self.assertFalse(
                    thread.is_alive(), "TLS fixture server leaked its thread"
                )
