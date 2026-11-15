"""Pagine web del deploy online: /login (OAuth), /setup (login Garmin), /health."""

from __future__ import annotations

import html
import secrets
import time
from typing import Any

import anyio
from garminconnect import Garmin
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth_provider import PasswordOAuthProvider
from .client import is_cn, reset_client, tokenstore_path

CSS = """
body{font-family:system-ui,sans-serif;max-width:440px;margin:40px auto;padding:0 16px;color:#222}
h1{font-size:1.4em}h2{font-size:1.1em;margin-top:2em}
input,textarea,button{width:100%;box-sizing:border-box;padding:12px;margin:6px 0;font-size:16px;
border:1px solid #ccc;border-radius:8px}button{background:#0a66c2;color:#fff;border:0;font-weight:600}
.ok{background:#e7f6ec;padding:10px;border-radius:8px}.err{background:#fde8e8;padding:10px;border-radius:8px}
small{color:#666}
"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=it><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=robots content=noindex><title>{title}</title><style>{CSS}</style></head>"
        f"<body><h1>{title}</h1>{body}</body></html>",
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


def msg(text: str, ok: bool) -> str:
    return f"<p class={'ok' if ok else 'err'}>{html.escape(text)}</p>"


# Login Garmin in attesa del codice MFA: sid -> (Garmin, client_state, timestamp)
_mfa_pending: dict[str, tuple[Garmin, Any, float]] = {}


def _token_status() -> str:
    try:
        g = Garmin(is_cn=is_cn())
        g.login(tokenstore_path())
        return msg("Stato: collegato a Garmin.", True)
    except Exception:  # noqa: BLE001
        return msg("Stato: Garmin non collegato.", False)


def register(mcp: Any, provider: PasswordOAuthProvider) -> None:
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------------- /login
    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        if request.method == "GET":
            txn = html.escape(request.query_params.get("txn", ""))
            return page("Autorizza Claude", f"""
<p>Claude chiede l'accesso ai tuoi dati Garmin. Inserisci la password di amministrazione.</p>
<form method=post><input type=hidden name=txn value="{txn}">
<input type=password name=password placeholder="Password admin" autofocus required>
<button>Autorizza</button></form>""")
        form = await request.form()
        if provider.locked_out():
            return page("Autorizza Claude", msg("Troppi tentativi, riprova tra 15 minuti.", False), 429)
        if not await anyio.to_thread.run_sync(provider.check_password, str(form.get("password", ""))):
            return page("Autorizza Claude", msg("Password errata.", False) +
                        f"<p><a href='/login?txn={html.escape(str(form.get('txn', '')))}'>Riprova</a></p>", 401)
        redirect = provider.complete_login(str(form.get("txn", "")))
        if not redirect:
            return page("Autorizza Claude", msg("Richiesta scaduta: riavvia il collegamento da Claude.", False), 400)
        return RedirectResponse(redirect, status_code=302)

    # ---------------------------------------------------------------- /setup
    def setup_forms(extra: str = "") -> HTMLResponse:
        return page("Setup Garmin", extra + _token_status() + """
<h2>Accedi a Garmin Connect</h2>
<form method=post><input type=hidden name=action value=login>
<input type=password name=admin placeholder="Password admin (server)" required>
<input type=email name=email placeholder="Email Garmin" required autocomplete=username>
<input type=password name=password placeholder="Password Garmin" required autocomplete=current-password>
<button>Accedi</button></form>
<small>La password Garmin non viene salvata: si conservano solo i token.</small>
<h2>Oppure incolla i token</h2>
<form method=post><input type=hidden name=action value=tokens>
<input type=password name=admin placeholder="Password admin (server)" required>
<textarea name=tokens rows=4 placeholder='JSON dei token (output di garmin.client.dumps())'></textarea>
<button>Salva token</button></form>
<h2>Sicurezza</h2>
<form method=post><input type=hidden name=action value=revoke>
<input type=password name=admin placeholder="Password admin (server)" required>
<button style="background:#b42318">Scollega tutti i client Claude</button></form>""")

    @mcp.custom_route("/setup", methods=["GET", "POST"])
    async def setup(request: Request) -> Response:
        if request.method == "GET":
            return await anyio.to_thread.run_sync(setup_forms)

        form = await request.form()
        action = form.get("action")
        store = tokenstore_path()

        # Passo 2 dell'MFA: il sid casuale fa da credenziale temporanea
        if action == "mfa":
            item = _mfa_pending.pop(str(form.get("sid", "")), None)
            if not item or time.time() - item[2] > 600:
                return await anyio.to_thread.run_sync(setup_forms, msg("Sessione MFA scaduta, ripeti il login.", False))
            g, state, _ = item

            def finish() -> None:
                g.resume_login(state, str(form.get("code", "")).strip())
                g.client.dump(store)

            try:
                await anyio.to_thread.run_sync(finish)
            except Exception as e:  # noqa: BLE001
                return await anyio.to_thread.run_sync(setup_forms, msg(f"Codice MFA rifiutato: {e}", False))
            reset_client()
            return await anyio.to_thread.run_sync(setup_forms, msg("Fatto! Token salvati.", True))

        if provider.locked_out():
            return page("Setup Garmin", msg("Troppi tentativi, riprova tra 15 minuti.", False), 429)
        if not await anyio.to_thread.run_sync(provider.check_password, str(form.get("admin", ""))):
            return await anyio.to_thread.run_sync(setup_forms, msg("Password admin errata.", False))

        if action == "revoke":
            provider.revoke_all()
            return await anyio.to_thread.run_sync(setup_forms, msg("Tutti i token Claude revocati: ricollega il connettore.", True))

        if action == "tokens":
            raw = str(form.get("tokens", "")).strip()

            def load_tokens() -> None:
                g = Garmin(is_cn=is_cn())
                g.login(raw)
                g.client.dump(store)

            try:
                await anyio.to_thread.run_sync(load_tokens)
            except Exception as e:  # noqa: BLE001
                return await anyio.to_thread.run_sync(setup_forms, msg(f"Token non validi: {e}", False))
            reset_client()
            return await anyio.to_thread.run_sync(setup_forms, msg("Token salvati.", True))

        if action == "login":
            g = Garmin(email=str(form.get("email", "")), password=str(form.get("password", "")),
                       is_cn=is_cn(), return_on_mfa=True)
            try:
                status, state = await anyio.to_thread.run_sync(g.login)
            except Exception as e:  # noqa: BLE001
                return await anyio.to_thread.run_sync(setup_forms, msg(f"Login Garmin fallito: {e}", False))
            if status == "needs_mfa":
                sid = secrets.token_urlsafe(24)
                now = time.time()
                for k in [k for k, v in _mfa_pending.items() if now - v[2] > 600]:
                    _mfa_pending.pop(k, None)
                _mfa_pending[sid] = (g, state, now)
                return page("Codice MFA", f"""
<p>Garmin ti ha inviato un codice (email o app). Inseriscilo entro 10 minuti.</p>
<form method=post><input type=hidden name=action value=mfa><input type=hidden name=sid value="{sid}">
<input name=code inputmode=numeric autocomplete=one-time-code placeholder="Codice" autofocus required>
<button>Conferma</button></form>""")
            try:
                await anyio.to_thread.run_sync(g.client.dump, store)
            except Exception as e:  # noqa: BLE001
                return await anyio.to_thread.run_sync(setup_forms, msg(f"Salvataggio token fallito: {e}", False))
            reset_client()
            return await anyio.to_thread.run_sync(setup_forms, msg("Fatto! Token salvati.", True))

        return await anyio.to_thread.run_sync(setup_forms, msg("Azione sconosciuta.", False))
