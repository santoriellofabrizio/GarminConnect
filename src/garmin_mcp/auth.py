"""Login interattivo una tantum: salva i token OAuth per il server MCP.

Uso:
    uv run garmin-mcp-auth            # login (chiede email, password, MFA)
    uv run garmin-mcp-auth --verify   # verifica i token salvati
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from garminconnect import Garmin

from .client import is_cn, tokenstore_path


def _verify(store: str) -> int:
    try:
        g = Garmin(is_cn=is_cn())
        g.login(store)
        print(f"OK: token validi per {g.get_full_name()} ({store})")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"Token non validi: {e}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Login Garmin Connect per garmin-mcp")
    parser.add_argument("--verify", action="store_true", help="verifica i token salvati")
    parser.add_argument("--print", dest="print_tokens", action="store_true",
                        help="stampa i token in JSON (da incollare in /setup del server online)")
    parser.add_argument("--tokenstore", default=None, help="cartella token (default ~/.garminconnect)")
    args = parser.parse_args()

    store = str(Path(args.tokenstore).expanduser()) if args.tokenstore else tokenstore_path()

    if args.verify:
        sys.exit(_verify(store))

    email = os.getenv("GARMIN_EMAIL") or input("Email Garmin: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Password Garmin: ")

    Path(store).mkdir(parents=True, exist_ok=True)
    g = Garmin(
        email=email,
        password=password,
        is_cn=is_cn(),
        prompt_mfa=lambda: input("Codice MFA (email/app): ").strip(),
    )
    try:
        g.login(store)
        g.client.dump(store)  # garantisce il salvataggio anche se già caricati
    except Exception as e:  # noqa: BLE001
        print(f"Login fallito: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Login riuscito come {g.get_full_name()}. Token salvati in {store}")
    print("Ora puoi avviare il server MCP senza password.")
    if args.print_tokens:
        print("\n--- token JSON (trattali come una password) ---")
        print(g.client.dumps())


if __name__ == "__main__":
    main()
