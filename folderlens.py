"""Terminal launcher for FolderLens Agent. STDIO stdout is reserved for MCP."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="FolderLens local MCP agent for ChatGPT and compatible clients"
    )
    result.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Authorized workspace (default: current directory)",
    )
    result.add_argument(
        "--allow-write",
        action="store_true",
        help="Enable checked file changes and document authoring",
    )
    result.add_argument(
        "--allow-exec",
        action="store_true",
        help="Enable commands with full OS user privileges (NOT sandboxed)",
    )
    result.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    result.add_argument(
        "--auth",
        choices=["bearer", "oauth"],
        default="bearer",
        help="HTTP authentication method",
    )
    result.add_argument(
        "--oauth-redirect-uri",
        action="append",
        default=[],
        help="Exact client callback URI allowed for OAuth (repeatable)",
    )
    result.add_argument(
        "--oauth-state-dir",
        type=Path,
        help="Dedicated private authorization-state directory",
    )
    result.add_argument(
        "--oauth-list-clients",
        action="store_true",
        help="List approved OAuth connections locally, then exit",
    )
    result.add_argument(
        "--oauth-revoke-client",
        help="Revoke one approved OAuth client locally, then exit",
    )
    result.add_argument(
        "--name", default="folderlens", help="Unique MCP client entry/device alias"
    )
    result.add_argument(
        "--history",
        choices=["memory", "metadata", "content"],
        default="memory",
        help="Opt-in private persistent history; content stores potentially sensitive arguments/results",
    )
    result.add_argument(
        "--devices",
        type=Path,
        help="Owner-authored JSON device catalog; never contains raw credentials",
    )
    result.add_argument(
        "--allow-url-host",
        action="append",
        default=[],
        help="Allow an exact HTTPS read hostname (repeatable); no private network access",
    )
    result.add_argument(
        "--port",
        type=int,
        default=8766,
        help="Loopback HTTP port; existing browser server stays on 8765",
    )
    result.add_argument(
        "--public-origin",
        help="Explicit HTTPS origin for an owner-managed tunnel/reverse proxy",
    )
    result.add_argument(
        "--print-config",
        choices=["toml", "json"],
        help="Print client configuration; does not alter application settings",
    )
    result.add_argument(
        "--setup-xls",
        action="store_true",
        help="Install and verify the private Apache POI legacy XLS writer, then exit",
    )
    result.add_argument(
        "--doctor",
        action="store_true",
        help="Verify dependencies, workspace and tool registration without starting a server",
    )
    return result


def client_config(args: argparse.Namespace) -> str:
    root = args.root.expanduser().resolve(strict=True)
    source = Path(__file__).resolve()
    venv = (
        source.parent
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    python = str(venv if venv.is_file() else Path(sys.executable).resolve())
    command_args = [str(source), "--root", str(root), "--name", args.name]
    if args.devices is not None:
        from agentarm.device_config import load_devices

        configured = load_devices(args.devices, args.name)
        command_args.extend(["--devices", str(configured.source)])
    if args.history != "memory":
        command_args.extend(["--history", args.history])
    if args.allow_write:
        command_args.append("--allow-write")
    if args.allow_exec:
        command_args.append("--allow-exec")
    for host in args.allow_url_host:
        command_args.extend(["--allow-url-host", host])
    config = {"command": python, "args": command_args}
    if args.transport == "http":
        if args.auth == "oauth":
            from agentarm.http_security import canonical_origin

            if not args.public_origin:
                raise ValueError(
                    "OAuth needs --public-origin with the externally reachable HTTPS origin"
                )
            config = {
                "url": canonical_origin(args.public_origin) + "/mcp",
                "auth": "oauth",
            }
        else:
            config = {
                "url": f"http://127.0.0.1:{args.port}/mcp",
                "bearer_token_env_var": "FOLDERLENS_TOKEN",
            }
    if args.print_config == "json":
        return json.dumps(
            {"mcpServers": {args.name: config}}, ensure_ascii=False, indent=2
        )
    lines = [f"[mcp_servers.{args.name}]"]
    lines.extend(
        f"{key} = {json.dumps(value, ensure_ascii=False)}"
        for key, value in config.items()
    )
    lines.extend(
        [
            "startup_timeout_sec = 30",
            "tool_timeout_sec = 90",
            'default_tools_approval_mode = "writes"',
        ]
    )
    return "\n".join(lines)


class BearerGuard:
    """Authenticate every HTTP request, without cookies, query tokens or CORS."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, b"Bearer " + self.token):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b'{"error":"Unauthorized"}'}
                )
                return

        async def safe_send(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ],
                }
            await send(message)

        await self.app(scope, receive, safe_send)


def http_app(
    server,
    token: str = "",
    public_origin: str | None = None,
    oauth_provider=None,
    shutdown_control=None,
):
    from mcp.server.transport_security import TransportSecuritySettings

    if oauth_provider is None and (
        len(token) < 32
        or token.strip() != token
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
    ):
        raise ValueError(
            "FOLDERLENS_TOKEN must be at least 32 printable non-space ASCII characters"
        )
    hosts = ["127.0.0.1", "localhost", "127.0.0.1:*", "localhost:*"]
    origins = [
        "http://127.0.0.1",
        "http://localhost",
        "http://127.0.0.1:*",
        "http://localhost:*",
    ]
    if public_origin:
        from agentarm.http_security import canonical_origin

        public_origin = canonical_origin(public_origin)
        parsed = urlsplit(public_origin)
        hosts.append(parsed.netloc)
        origins.append(public_origin.rstrip("/"))
    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=1024 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    if oauth_provider is not None:
        from agentarm.oauth_http import OAuthBoundary

        app = OAuthBoundary(app, oauth_provider)
    else:
        app = BearerGuard(app, token)
    return shutdown_control.wrap(app) if shutdown_control is not None else app


def _bootstrap(argv: list[str]) -> None:
    root = Path(__file__).resolve().parent
    venv = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if os.getenv("FOLDERLENS_BOOTSTRAPPED") == "1":
        raise RuntimeError(
            "Agent dependencies failed to import after setup; inspect the Python installation"
        )
    print("Preparing FolderLens Agent's isolated environment...", file=sys.stderr)
    if not venv.exists():
        subprocess.run(
            [sys.executable, "-m", "venv", str(root / ".venv")],
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
    subprocess.run(
        [
            str(venv),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(root / "requirements-agent.txt"),
        ],
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    os.environ["FOLDERLENS_BOOTSTRAPPED"] = "1"
    os.execv(str(venv), [str(venv), str(Path(__file__).resolve()), *argv])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser().error("port must be 1..65535")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", args.name):
        parser().error(
            "name must start with a letter and contain 1..64 letters, numbers, _ or -"
        )
    if args.auth == "oauth" and (args.transport != "http" or not args.public_origin):
        parser().error(
            "OAuth requires --transport http and --public-origin HTTPS_ORIGIN"
        )
    if args.auth != "oauth" and (
        args.oauth_list_clients
        or args.oauth_revoke_client
        or args.oauth_state_dir
        or args.oauth_redirect_uri
    ):
        parser().error("OAuth options require --auth oauth")
    oauth_provider = None
    history_store = None
    device_catalog = None
    try:
        if args.setup_xls:
            from agentarm.legacy_excel_runtime import install

            print(json.dumps({"ok": True, "xls_runtime_key": install()["key"]}))
            return 0
        if args.print_config:
            print(client_config(args))
            return 0
        try:
            import importlib.util

            dependencies = ["markdown", "playwright", "pypdfium2", "regex", "xlrd"]
            if os.name == "nt":
                dependencies.extend(["win32job", "win32security"])
            for dependency in dependencies:
                if importlib.util.find_spec(dependency) is None:
                    raise ModuleNotFoundError(f"Missing agent dependency: {dependency}")
            from agentarm.mcp_server import create_agent
            from agentarm.workspace import AgentPolicy
        except ModuleNotFoundError:
            _bootstrap(argv)
            return 2
        private_root = Path(__file__).resolve().parent / ".folderlens"
        state_dir = args.oauth_state_dir or private_root / "oauth" / args.name
        from agentarm.device_config import load_devices

        devices = load_devices(args.devices, args.name)
        policy = AgentPolicy(
            args.root,
            args.allow_write,
            args.allow_exec,
            url_hosts=tuple(args.allow_url_host),
            protected_paths=(private_root, state_dir),
            device_config=devices,
        )
        if args.auth == "oauth":
            from agentarm.oauth import OwnerOAuthProvider

            oauth_provider = OwnerOAuthProvider(
                policy,
                args.public_origin,
                args.oauth_redirect_uri,
                state_dir,
                rebind=not (
                    args.doctor or args.oauth_list_clients or args.oauth_revoke_client
                ),
            )
            if args.oauth_revoke_client:
                oauth_provider.revoke_client(args.oauth_revoke_client)
            if args.oauth_list_clients or args.oauth_revoke_client:
                print(
                    json.dumps(
                        {"clients": oauth_provider.list_clients()},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
        if args.history != "memory":
            from agentarm.oauth import policy_binding
            from agentarm.tool_history import ToolHistory

            history_store = ToolHistory(
                private_root / "history" / args.name / policy_binding(policy),
                args.history,
            )
        if devices.profiles:
            from agentarm.device_catalog import DeviceCatalog

            device_catalog = DeviceCatalog(devices)
        shutdown_control = None
        if args.transport == "http":
            from agentarm.shutdown import ShutdownControl

            shutdown_control = ShutdownControl()
        server = create_agent(
            policy,
            device_name=args.name,
            oauth_provider=oauth_provider,
            history_store=history_store,
            shutdown_control=shutdown_control,
            device_catalog=device_catalog,
        )
        if args.doctor:
            import asyncio
            import importlib.metadata
            import shutil

            tools = asyncio.run(server.list_tools())
            if args.transport == "http":
                http_app(
                    server,
                    os.getenv("FOLDERLENS_TOKEN", ""),
                    args.public_origin,
                    oauth_provider,
                    shutdown_control=shutdown_control,
                )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "root": str(policy.root),
                        "tools": len(tools),
                        "mcp_version": importlib.metadata.version("mcp"),
                        "git_available": bool(shutil.which("git")),
                        "allow_write": policy.allow_write,
                        "allow_exec": policy.allow_exec,
                        "transport": args.transport,
                        "device_id": args.name,
                        "http_auth": args.auth if args.transport == "http" else None,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(
            f"FolderLens Agent | {policy.root} | write={policy.allow_write} exec={policy.allow_exec}",
            file=sys.stderr,
        )
        if args.allow_exec:
            print(
                "Commands have full OS user access; --root is not a command sandbox.",
                file=sys.stderr,
            )
        if args.transport == "http":
            import uvicorn

            app = http_app(
                server,
                os.getenv("FOLDERLENS_TOKEN", ""),
                args.public_origin,
                oauth_provider,
                shutdown_control=shutdown_control,
            )
            print(f"MCP endpoint: http://127.0.0.1:{args.port}/mcp", file=sys.stderr)
            runner = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=args.port,
                    proxy_headers=False,
                    access_log=False,
                    limit_concurrency=32,
                )
            )
            shutdown_control.bind(lambda: setattr(runner, "should_exit", True))
            runner.run()
        else:
            server.run(transport="stdio")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"FolderLens startup failed: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(
            f"Private storage failed ({type(exc).__name__}); check the dedicated state directory",
            file=sys.stderr,
        )
        return 2
    finally:
        if oauth_provider is not None:
            oauth_provider.close()
        if history_store is not None:
            history_store.close()
        if device_catalog is not None:
            device_catalog.close()


if __name__ == "__main__":
    raise SystemExit(main())
