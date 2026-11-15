"""Gestione del client Garmin Connect (singleton con token salvati su disco)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from garminconnect import Garmin

DEFAULT_TOKENSTORE = "~/.garminconnect"

_lock = threading.Lock()
_client: Garmin | None = None


def tokenstore_path() -> str:
    """Cartella dei token: GARMINTOKENS oppure ~/.garminconnect."""
    return str(Path(os.getenv("GARMINTOKENS", DEFAULT_TOKENSTORE)).expanduser())


def is_cn() -> bool:
    return os.getenv("GARMIN_IS_CN", "false").lower() in ("1", "true", "yes")


def get_client() -> Garmin:
    """Restituisce un client autenticato.

    Usa i token salvati da `garmin-mcp-auth`. Se mancano o sono scaduti e
    GARMIN_EMAIL/GARMIN_PASSWORD sono impostati, ritenta il login (senza MFA:
    il server MCP non ha un terminale per chiedere il codice).
    """
    global _client
    with _lock:
        if _client is not None:
            return _client

        store = tokenstore_path()
        email = os.getenv("GARMIN_EMAIL")
        password = os.getenv("GARMIN_PASSWORD")

        def _no_mfa() -> str:
            raise RuntimeError(
                "Garmin richiede il codice MFA: esegui `garmin-mcp-auth` "
                "da terminale e poi riavvia il server."
            )

        g = Garmin(email=email, password=password, is_cn=is_cn(), prompt_mfa=_no_mfa)
        try:
            g.login(store)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Login Garmin fallito ({e}). Esegui `garmin-mcp-auth` per "
                f"rigenerare i token in {store}."
            ) from e

        _client = g
        return g


def reset_client() -> None:
    global _client
    with _lock:
        _client = None
