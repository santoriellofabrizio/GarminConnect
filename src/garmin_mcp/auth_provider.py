"""OAuth 2.1 minimale per un solo utente, protetto da ADMIN_PASSWORD.

claude.ai registra il connettore via Dynamic Client Registration, poi apre
/authorize: qui il server reindirizza a /login, dove inserisci la password.
Client, refresh token e access token sono salvati in DATA_DIR/oauth.json così
il connettore sopravvive ai riavvii.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ACCESS_TTL = 3600  # 1 ora
REFRESH_TTL = 90 * 24 * 3600  # 90 giorni
CODE_TTL = 300
PENDING_TTL = 600


class PasswordOAuthProvider:
    def __init__(self, base_url: str, admin_password: str, data_dir: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_password = admin_password
        self.path = Path(data_dir) / "oauth.json"
        self._lock = threading.Lock()
        self.pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        self.access: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self._failed: list[float] = []
        self._load()

    # ------------------------------------------------------------ persistenza
    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.clients = data.get("clients", {})
            self.access = data.get("access", {})
            self.refresh = data.get("refresh", {})

    def _save(self) -> None:
        now = time.time()
        self.access = {k: v for k, v in self.access.items() if (v.get("expires_at") or now + 1) > now}
        self.refresh = {k: v for k, v in self.refresh.items() if (v.get("expires_at") or now + 1) > now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clients": self.clients, "access": self.access, "refresh": self.refresh}))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # --------------------------------------------------------------- password
    def check_password(self, password: str) -> bool:
        """Confronto a tempo costante + blocco dopo 5 errori in 15 minuti."""
        now = time.time()
        self._failed = [t for t in self._failed if now - t < 900]
        if len(self._failed) >= 5:
            return False
        ok = bool(password) and hmac.compare_digest(password.encode(), self.admin_password.encode())
        if not ok:
            self._failed.append(now)
            time.sleep(1)
        return ok

    def locked_out(self) -> bool:
        now = time.time()
        return len([t for t in self._failed if now - t < 900]) >= 5

    # ------------------------------------------------------------- client DCR
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        c = self.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            self.clients[client_info.client_id] = client_info.model_dump(mode="json")
            self._save()

    # -------------------------------------------------------------- authorize
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        txn = secrets.token_urlsafe(24)
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v[2] < PENDING_TTL}
        self.pending[txn] = (client.client_id, params, now)
        return f"{self.base_url}/login?txn={txn}"

    def complete_login(self, txn: str) -> str | None:
        """Chiamato da /login dopo la password corretta: ritorna il redirect con il code."""
        item = self.pending.pop(txn, None)
        if not item or time.time() - item[2] > PENDING_TTL:
            return None
        client_id, params, _ = item
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    # ------------------------------------------------------------------ token
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        c = self.codes.get(authorization_code)
        if c and c.client_id == client.client_id and c.expires_at > time.time():
            return c
        return None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        at, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        base = {"client_id": client_id, "scopes": scopes, "resource": resource, "subject": "owner"}
        with self._lock:
            self.access[at] = {**base, "token": at, "expires_at": now + ACCESS_TTL}
            self.refresh[rt] = {**base, "token": rt, "expires_at": now + REFRESH_TTL}
            self._save()
        return OAuthToken(
            access_token=at, expires_in=ACCESS_TTL, refresh_token=rt,
            scope=" ".join(scopes) if scopes else None,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "code già usato")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        r = self.refresh.get(refresh_token)
        if r and r["client_id"] == client.client_id and r["expires_at"] > time.time():
            return RefreshToken.model_validate(r)
        return None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self._lock:
            self.refresh.pop(refresh_token.token, None)  # rotazione
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        a = self.access.get(token)
        if a and a["expires_at"] > time.time():
            return AccessToken.model_validate(a)
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            self.access.pop(token.token, None)
            self.refresh.pop(token.token, None)
            self._save()

    def revoke_all(self) -> None:
        with self._lock:
            self.access.clear()
            self.refresh.clear()
            self._save()
