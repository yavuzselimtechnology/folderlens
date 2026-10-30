"""Single-owner OAuth provider using the MCP SDK's OAuth/PKCE handlers.

No ChatGPT credentials are read. The owner approves each connection using a
short-lived code shown only in the server's local terminal. Workspace content
never enters the private authorization database.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .http_security import canonical_origin
from .workspace import AgentPolicy

READ_SCOPE = "folderlens:read"
WRITE_SCOPE = "folderlens:write"
EXEC_SCOPE = "folderlens:exec"
AUTH_APP_ID = int.from_bytes(b"FLAO", "big")
AUTH_SCHEMA_VERSION = 1
SCOPE_LABELS = {
    READ_SCOPE: "Dosyaları, Git bilgisini ve izin verilen URL'leri oku",
    WRITE_SCOPE: "Çalışma klasörünü değiştir ve belgeler üret",
    EXEC_SCOPE: "İşletim sistemi kullanıcısı yetkisiyle komut çalıştır (klasör sandbox'ı yok)",
}


@dataclass(frozen=True)
class OAuthLimits:
    pending_seconds: int = 300
    code_seconds: int = 60
    access_seconds: int = 3600
    refresh_seconds: int = 30 * 24 * 3600
    max_clients: int = 64
    max_sessions: int = 64
    max_pending: int = 16
    max_pin_attempts: int = 5


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def policy_binding(policy: AgentPolicy) -> str:
    return token_hash(
        json.dumps(
            {
                "root": str(policy.root),
                "write": policy.allow_write,
                "exec": policy.allow_exec,
                "hosts": sorted(policy.url_hosts),
                **(
                    {"devices": policy.device_config.binding}
                    if policy.device_config.binding
                    else {}
                ),
            },
            sort_keys=True,
        )
    )


def _private_directory(path: Path) -> None:
    for component in [*path.parents, path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if component.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("OAuth state path cannot traverse symlink/reparse points")
    if path.exists():
        expected = {
            "oauth.sqlite3",
            "oauth.sqlite3-journal",
            "oauth.sqlite3-wal",
            "oauth.sqlite3-shm",
        }
        if any(
            item.name not in expected
            or not item.is_file()
            or item.is_symlink()
            or getattr(item.lstat(), "st_file_attributes", 0) & 0x400
            for item in path.iterdir()
        ):
            raise ValueError(
                "OAuth state needs a dedicated directory containing only its own database files"
            )
        database = path / "oauth.sqlite3"
        if database.exists():
            connection = sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
            )
            try:
                signature = connection.execute("PRAGMA application_id").fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if signature != AUTH_APP_ID or version != AUTH_SCHEMA_VERSION:
                    raise ValueError(
                        "This is not a supported FolderLens authorization database; choose a new state directory"
                    )
            finally:
                connection.close()
        elif any(path.iterdir()):
            raise ValueError(
                "OAuth state is incomplete; choose an empty dedicated state directory"
            )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("OAuth state directory cannot be a symlink/reparse point")
    _private_permissions(path, directory=True)
    for item in path.iterdir():
        _private_permissions(item, directory=False)


def _private_permissions(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        acl = win32security.ACL()
        inherit = (
            (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE)
            if directory
            else 0
        )
        for principal in (
            sid,
            win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None),
        ):
            acl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION,
                inherit,
                ntsecuritycon.FILE_ALL_ACCESS,
                principal,
            )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


class OwnerOAuthProvider:
    def __init__(
        self,
        policy: AgentPolicy,
        public_origin: str,
        redirect_uris: list[str],
        state_dir: Path,
        limits: OAuthLimits | None = None,
        rebind: bool = True,
    ):
        self.policy = policy
        self.binding = policy_binding(policy)
        self.origin = canonical_origin(public_origin)
        self.issuer = str(AnyHttpUrl(self.origin))
        self.resource = self.origin + "/mcp"
        self.limits = limits or OAuthLimits()
        self.redirect_uris = frozenset(str(AnyUrl(uri)) for uri in redirect_uris)
        if not self.redirect_uris:
            raise ValueError(
                "OAuth requires at least one exact --oauth-redirect-uri from the client's setup page"
            )
        for uri in self.redirect_uris:
            parsed = urlsplit(uri)
            local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if (
                parsed.scheme not in {"https", "http"}
                or (parsed.scheme == "http" and not local)
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError(
                    "OAuth callbacks must use HTTPS or an explicitly allowlisted loopback HTTP URI"
                )
        self.scopes = [READ_SCOPE]
        if policy.allow_write:
            self.scopes.append(WRITE_SCOPE)
        if policy.allow_exec:
            self.scopes.append(EXEC_SCOPE)
        self.settings = AuthSettings(
            issuer_url=AnyHttpUrl(self.issuer),
            resource_server_url=AnyHttpUrl(self.resource),
            validate_token_resource=True,
            required_scopes=[READ_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=self.scopes, default_scopes=self.scopes
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self.state_dir = state_dir.expanduser().absolute()
        _private_directory(self.state_dir)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            self.state_dir / "oauth.sqlite3", timeout=2, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        _private_permissions(self.state_dir / "oauth.sqlite3", directory=False)
        self.db.execute(f"PRAGMA application_id={AUTH_APP_ID}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, client_id TEXT NOT NULL, scopes TEXT NOT NULL,
                access_hash TEXT UNIQUE NOT NULL, refresh_hash TEXT UNIQUE NOT NULL,
                access_expires INTEGER NOT NULL, refresh_expires INTEGER NOT NULL,
                resource TEXT NOT NULL, binding TEXT NOT NULL);
        """)
        self.db.execute(f"PRAGMA user_version={AUTH_SCHEMA_VERSION}")
        current = self.db.execute(
            "SELECT value FROM settings WHERE key='issuer'"
        ).fetchone()
        if current and current[0] != self.issuer:
            self.db.close()
            raise ValueError(
                "This OAuth state belongs to another public origin; use a different --name/state directory"
            )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('issuer', ?)", (self.issuer,)
            )
            if rebind:
                binding = self.binding
                previous = self.db.execute(
                    "SELECT value FROM settings WHERE key='binding'"
                ).fetchone()
                if previous and previous[0] != binding:
                    self.db.execute("DELETE FROM sessions")
                self.db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('binding', ?)", (binding,)
                )
        self.pending_clients: dict[str, tuple[float, OAuthClientInformationFull]] = {}
        self.pending: dict[str, dict] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.tool_scopes: dict[str, list[str]] = {}
        self.closed = False

    def cleanup(self) -> None:
        now = time.time()
        for key, (expires, _) in list(self.pending_clients.items()):
            if expires <= now:
                del self.pending_clients[key]
        for key, pending in list(self.pending.items()):
            if pending["expires"] <= now:
                del self.pending[key]
        for key, code in list(self.codes.items()):
            if code.expires_at <= now:
                del self.codes[key]
        with self.db:
            self.db.execute(
                "DELETE FROM sessions WHERE refresh_expires <= ?", (int(now),)
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.lock:
            self.cleanup()
            row = self.db.execute(
                "SELECT metadata FROM clients WHERE id=?", (client_id,)
            ).fetchone()
            if row:
                client = OAuthClientInformationFull.model_validate_json(row[0])
                # A removed local callback allowance immediately invalidates the client.
                if all(
                    str(uri) in self.redirect_uris for uri in client.redirect_uris or []
                ):
                    return client
                return None
            pending = self.pending_clients.get(client_id)
            return pending[1] if pending else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self.lock:
            self.cleanup()
            redirects = client_info.redirect_uris or []
            if (
                not redirects
                or len(redirects) > 8
                or any(str(uri) not in self.redirect_uris for uri in redirects)
            ):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Every callback must exactly match the owner's local allowlist",
                )
            if client_info.token_endpoint_auth_method not in {
                "none",
                "client_secret_post",
                "client_secret_basic",
            }:
                raise RegistrationError(
                    "invalid_client_metadata", "Unsupported token authentication method"
                )
            if not set(client_info.grant_types).issubset(
                {"authorization_code", "refresh_token"}
            ) or set(client_info.response_types) != {"code"}:
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Only authorization-code and refresh-token flows are supported",
                )
            # Registration lists scopes the client may request, not authority
            # already granted. Each grant still requires owner consent; keeping
            # the startup ceiling available permits standard OAuth scope step-up.
            client_info.scope = " ".join(self.scopes)
            if len(client_info.model_dump_json().encode()) > 16384:
                raise RegistrationError(
                    "invalid_client_metadata", "Client metadata exceeds the budget"
                )
            if len(self.pending_clients) >= self.limits.max_pending:
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Pending client registration budget reached; retry later",
                )
            count = self.db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            if count >= self.limits.max_clients:
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Client budget reached; owner must revoke an old connection",
                )
            self.pending_clients[client_info.client_id] = (
                time.time() + self.limits.pending_seconds,
                client_info,
            )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        with self.lock:
            self.cleanup()
            if params.resource != self.resource:
                raise AuthorizeError(
                    "invalid_target", "resource must match the advertised MCP resource"
                )
            scopes = params.scopes or self.scopes
            if READ_SCOPE not in scopes or not set(scopes).issubset(self.scopes):
                raise AuthorizeError(
                    "invalid_scope",
                    "Requested scopes exceed this instance's startup permissions",
                )
            if str(params.redirect_uri) not in self.redirect_uris:
                raise AuthorizeError("invalid_request", "Callback is no longer allowed")
            if len(self.pending) >= self.limits.max_pending:
                raise AuthorizeError(
                    "temporarily_unavailable", "Owner approval queue is full"
                )
            request_id = secrets.token_urlsafe(32)
            # 192 bits rather than a remotely brute-forceable short numeric PIN.
            pin = secrets.token_urlsafe(24)
            self.pending[request_id] = {
                "client": client,
                "params": params,
                "scopes": scopes,
                "pin_hash": token_hash(pin),
                "attempts": 0,
                "csrf_hash": None,
                "expires": time.time() + self.limits.pending_seconds,
            }
            self.notify_owner(request_id, pin, client, scopes)
            return self.origin + "/consent/" + request_id

    def notify_owner(
        self,
        request_id: str,
        pin: str,
        client: OAuthClientInformationFull,
        scopes: list[str],
    ) -> None:
        label = "".join(
            char
            for char in (client.client_name or client.client_id)
            if not unicodedata.category(char).startswith("C")
        )[:80]
        peer_details = "".join(
            "Authorized peer: "
            + json.dumps(
                {
                    "id": profile.id,
                    "url": profile.url,
                    "root": profile.root,
                    "write": WRITE_SCOPE in scopes and profile.allow_write,
                    "exec": EXEC_SCOPE in scopes and profile.allow_exec,
                },
                ensure_ascii=True,
            )
            + "\n"
            for profile in self.policy.device_config.profiles
        )
        print(
            f"\nFolderLens OAuth approval {request_id[:12]}\nClient: {label}\n"
            f"Workspace: {self.policy.root}\nScopes: {', '.join(scopes)}\n"
            f"{peer_details}"
            f"One-time approval key: {pin}\nExpires in {self.limits.pending_seconds} seconds. "
            "Enter it only for the connection you initiated.\n",
            file=sys.stderr,
            flush=True,
        )

    def finish_consent(
        self, request_id: str, pin: str, csrf: str, cookie: str, approve: bool
    ) -> str:
        with self.lock:
            self.cleanup()
            pending = self.pending.get(request_id)
            if not pending or not pending["csrf_hash"] or not csrf or not cookie:
                raise ValueError(
                    "Approval request expired or missing browser verification"
                )
            if not hmac.compare_digest(csrf, cookie) or not hmac.compare_digest(
                token_hash(csrf), pending["csrf_hash"]
            ):
                raise ValueError(
                    "Browser verification failed; reopen the approval page"
                )
            params = pending["params"]
            result = {"state": params.state, "iss": self.issuer}
            if not approve:
                del self.pending[request_id]
                result["error"] = "access_denied"
            else:
                pending["attempts"] += 1
                if not hmac.compare_digest(token_hash(pin), pending["pin_hash"]):
                    if pending["attempts"] >= self.limits.max_pin_attempts:
                        del self.pending[request_id]
                    raise ValueError("Approval code invalid or request expired")
                code = secrets.token_urlsafe(32)
                if len(self.codes) >= self.limits.max_sessions:
                    raise ValueError("Authorization code budget reached; retry later")
                known = self.db.execute(
                    "SELECT 1 FROM clients WHERE id=?", (pending["client"].client_id,)
                ).fetchone()
                if (
                    not known
                    and self.db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
                    >= self.limits.max_clients
                ):
                    raise ValueError(
                        "Approved client budget reached; revoke an old connection"
                    )
                self.codes[token_hash(code)] = AuthorizationCode(
                    code=token_hash(code),
                    scopes=pending["scopes"],
                    expires_at=time.time() + self.limits.code_seconds,
                    client_id=pending["client"].client_id,
                    code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource,
                    subject="local-owner",
                )
                with self.db:
                    self.db.execute(
                        "INSERT OR REPLACE INTO clients VALUES (?, ?)",
                        (
                            pending["client"].client_id,
                            pending["client"].model_dump_json(),
                        ),
                    )
                self.pending_clients.pop(pending["client"].client_id, None)
                del self.pending[request_id]
                result["code"] = code
            separator = "&" if urlsplit(str(params.redirect_uri)).query else "?"
            return (
                str(params.redirect_uri)
                + separator
                + urlencode(
                    {key: value for key, value in result.items() if value is not None}
                )
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self.lock:
            self.cleanup()
            code = self.codes.get(token_hash(authorization_code))
            return (
                code.model_copy(update={"code": authorization_code})
                if code and code.client_id == client.client_id
                else None
            )

    def issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        if (
            self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            >= self.limits.max_sessions
        ):
            raise TokenError(
                "invalid_grant", "Session budget reached; revoke an old connection"
            )
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        self.db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                secrets.token_urlsafe(16),
                client_id,
                json.dumps(scopes),
                token_hash(access),
                token_hash(refresh),
                now + self.limits.access_seconds,
                now + self.limits.refresh_seconds,
                self.resource,
                self.binding,
            ),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.limits.access_seconds,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self.lock:
            self.cleanup()
            found = self.codes.pop(token_hash(authorization_code.code), None)
            if (
                not found
                or found.client_id != client.client_id
                or found.expires_at <= time.time()
            ):
                raise TokenError(
                    "invalid_grant", "Authorization code expired or already used"
                )
            with self.db:
                return self.issue(client.client_id, found.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        with self.lock:
            self.cleanup()
            row = self.db.execute(
                "SELECT * FROM sessions WHERE refresh_hash=? AND client_id=? AND binding=?",
                (token_hash(refresh_token), client.client_id, self.binding),
            ).fetchone()
            return (
                RefreshToken(
                    token=refresh_token,
                    client_id=client.client_id,
                    scopes=json.loads(row["scopes"]),
                    expires_at=row["refresh_expires"],
                    resource=row["resource"],
                    subject="local-owner",
                )
                if row
                else None
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with self.lock:
            self.cleanup()
            row = self.db.execute(
                "SELECT scopes FROM sessions WHERE refresh_hash=? AND client_id=? AND binding=?",
                (token_hash(refresh_token.token), client.client_id, self.binding),
            ).fetchone()
            if row is None:
                raise TokenError(
                    "invalid_grant", "Refresh token expired or already rotated"
                )
            if (
                not set(scopes).issubset(json.loads(row["scopes"]))
                or not set(scopes).issubset(self.scopes)
                or READ_SCOPE not in scopes
            ):
                raise TokenError(
                    "invalid_scope",
                    "Refresh cannot add or remove the required read scope",
                )
            with self.db:
                deleted = self.db.execute(
                    "DELETE FROM sessions WHERE refresh_hash=? AND client_id=? AND binding=?",
                    (token_hash(refresh_token.token), client.client_id, self.binding),
                ).rowcount
                if deleted != 1:
                    raise TokenError(
                        "invalid_grant", "Refresh token expired or already rotated"
                    )
                return self.issue(client.client_id, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self.lock:
            self.cleanup()
            row = self.db.execute(
                "SELECT * FROM sessions WHERE access_hash=? AND access_expires>? AND resource=? AND binding=?",
                (token_hash(token), int(time.time()), self.resource, self.binding),
            ).fetchone()
            if not row:
                return None
            client = self.db.execute(
                "SELECT metadata FROM clients WHERE id=?", (row["client_id"],)
            ).fetchone()
            if not client or not all(
                str(uri) in self.redirect_uris
                for uri in OAuthClientInformationFull.model_validate_json(
                    client[0]
                ).redirect_uris
                or []
            ):
                return None
            return AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=json.loads(row["scopes"]),
                expires_at=row["access_expires"],
                resource=row["resource"],
                subject="local-owner",
            )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM sessions WHERE access_hash=? OR refresh_hash=?",
                (token_hash(token.token), token_hash(token.token)),
            )

    def list_clients(self) -> list[dict]:
        with self.lock:
            self.cleanup()
            rows = self.db.execute(
                "SELECT id, metadata FROM clients ORDER BY id"
            ).fetchall()
            return [
                {
                    "client_id": row["id"],
                    "name": OAuthClientInformationFull.model_validate_json(
                        row["metadata"]
                    ).client_name,
                    "sessions": self.db.execute(
                        "SELECT COUNT(*) FROM sessions WHERE client_id=?", (row["id"],)
                    ).fetchone()[0],
                }
                for row in rows
            ]

    def revoke_client(self, client_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE client_id=?", (client_id,))
            self.db.execute("DELETE FROM clients WHERE id=?", (client_id,))
            self.pending_clients.pop(client_id, None)
            self.codes = {
                key: code
                for key, code in self.codes.items()
                if code.client_id != client_id
            }
            self.pending = {
                key: value
                for key, value in self.pending.items()
                if value["client"].client_id != client_id
            }

    def install_routes(self, server) -> None:
        @server.custom_route("/consent/{request_id}", methods=["GET", "POST"])
        async def consent(request: Request):
            request_id = request.path_params["request_id"]
            cookie_name = "folderlens-consent-" + request_id[:12]
            if request.method == "POST":
                if request.headers.get("origin") != self.origin:
                    return HTMLResponse("Invalid approval origin", status_code=403)
                form = await request.form()
                try:
                    destination = self.finish_consent(
                        request_id,
                        str(form.get("pin", "")),
                        str(form.get("csrf", "")),
                        request.cookies.get(cookie_name, ""),
                        form.get("decision") == "approve",
                    )
                except ValueError as exc:
                    return HTMLResponse(html.escape(str(exc)), status_code=403)
                response = RedirectResponse(destination, status_code=303)
                response.delete_cookie(
                    cookie_name,
                    path="/consent",
                    secure=True,
                    httponly=True,
                    samesite="strict",
                )
                return response
            with self.lock:
                self.cleanup()
                pending = self.pending.get(request_id)
                if not pending:
                    return HTMLResponse("Approval request expired", status_code=410)
                csrf = secrets.token_urlsafe(32)
                pending["csrf_hash"] = token_hash(csrf)
                name = html.escape(
                    pending["client"].client_name or pending["client"].client_id
                )
                scopes = "".join(
                    f"<li>{html.escape(SCOPE_LABELS[scope])}</li>"
                    for scope in pending["scopes"]
                )
            page = f'''<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
            <title>FolderLens bağlantı onayı</title><style>body{{font:16px system-ui;max-width:36rem;margin:8vh auto;padding:24px;line-height:1.6;color:#153544;background:#f5f9fa}}input,button{{font:inherit;padding:12px;border:1px solid #577988;border-radius:6px}}input{{width:14rem;max-width:90%}}button{{margin-top:16px;cursor:pointer}}:focus-visible{{outline:3px solid #168b83;outline-offset:3px}}.approve{{background:#145a78;color:white}}</style>
            <h1>FolderLens bağlantısını onayla</h1><p>İstemcinin bildirdiği ad: <strong>{name}</strong></p>
            <p>İstenen izinler:</p><ul>{scopes}</ul><p>Ek yapılandırılmış cihaz sayısı: {len(self.policy.device_config.profiles)}. Bu onay, bu sunucunun yerelde gösterilen çalışma klasörü ve cihaz erişim sınırlarını kapsar.</p><p>Kökleri, cihazları ve bu isteğin kimliğini yerel terminalde kontrol edin: <code>{html.escape(request_id[:12])}</code>.</p>
            <form method="post"><input type="hidden" name="csrf" value="{csrf}"><label for="pin">Terminaldeki tek kullanımlık onay anahtarı</label><br>
            <input id="pin" name="pin" type="password" autocomplete="one-time-code" pattern="[A-Za-z0-9_-]{{32}}" maxlength="32" style="width:26rem">
            <br><button class="approve" name="decision" value="approve">Bağlantıya izin ver</button> <button name="decision" value="deny">Reddet</button></form>
            <p>Kodu sohbete yazmayın. Onay yalnız bu istemciye, yerelde gösterilen çalışma kapsamına ve gösterilen izinlere aittir.</p></html>'''
            response = HTMLResponse(page)
            response.set_cookie(
                cookie_name,
                csrf,
                max_age=self.limits.pending_seconds,
                path="/consent",
                secure=True,
                httponly=True,
                samesite="strict",
            )
            return response

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.db.close()
                self.closed = True
