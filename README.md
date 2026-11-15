# garmin-mcp

Server MCP (stdio) per Garmin Connect, basato su
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect) 0.3.x
e sull'SDK MCP ufficiale v2 (MCPServer). Sola lettura.

> **Python ≥ 3.12 obbligatorio**: garminconnect 0.3.x (nuova autenticazione
> nativa, senza garth) non supporta 3.11.

## Setup (Windows, uv)

```powershell
cd garmin-mcp
uv python install 3.12
uv sync
uv run garmin-mcp-auth          # email, password, codice MFA -> token in %USERPROFILE%\.garminconnect
uv run garmin-mcp-auth --verify # controllo
```

I token si rinnovano da soli; la password non serve più al server.
Se un giorno il server dice "login fallito", rilancia `garmin-mcp-auth`.

## Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\garmin-mcp", "garmin-mcp"]
    }
  }
}
```

Claude Code: `claude mcp add garmin -- uv run --directory C:\path\to\garmin-mcp garmin-mcp`

Variabili opzionali: `GARMINTOKENS` (cartella token), `GARMIN_IS_CN=true` (account Cina),
`GARMIN_EMAIL` / `GARMIN_PASSWORD` (fallback se i token spariscono; non gestisce MFA).

## Tool

| Tool | Cosa fa |
|---|---|
| `garmin_profile` | nome, unità, fuso, dispositivi |
| `garmin_daily_summary` | passi, calorie, FC riposo, stress, Body Battery |
| `garmin_sleep` / `garmin_sleep_range` | sonno (fasi, punteggio, HRV notturna) |
| `garmin_hrv` / `garmin_hrv_range` | HRV e baseline |
| `garmin_body_battery` | carica/scarica per giorno |
| `garmin_stress`, `garmin_heart_rate`, `garmin_resting_hr_range` | stress e FC |
| `garmin_body_composition` | peso, grasso, muscolo |
| `garmin_activities` | elenco attività (km, durata, passo, FC, TE) |
| `garmin_activity` | dettaglio + lap + zone FC + meteo |
| `garmin_training_status`, `garmin_training_readiness` | stato/prontezza allenamento |
| `garmin_race_predictions`, `garmin_personal_records` | previsioni gara e PR |
| `garmin_max_metrics`, `garmin_endurance_score` | VO2max, fitness age, endurance |
| `garmin_weekly_overview` | trend settimanale |
| `garmin_raw` | qualsiasi altro metodo `get_*` della libreria |

Le risposte sono compattate (niente serie intraday); molti tool accettano
`full=true` per il JSON completo. Output oltre 60k caratteri viene troncato.

## Deploy online (connettore claude.ai, anche da telefono)

Con `MCP_TRANSPORT=http` il server espone:

| Percorso | A cosa serve |
|---|---|
| `/mcp` | endpoint MCP (protetto da OAuth) |
| `/login` | pagina dove autorizzi Claude con la `ADMIN_PASSWORD` |
| `/setup` | login Garmin (email, password, codice MFA), incolla token, revoca client |
| `/health` | healthcheck |

Variabili: `ADMIN_PASSWORD` (≥ 12 caratteri, obbligatoria), `PUBLIC_URL`
(automatica su Railway/Render), `DATA_DIR=/data` (volume persistente per token
Garmin e OAuth).

### Railway (dal browser del telefono)
1. railway.com → New Project → Deploy from GitHub repo → `garmin-mcp`.
2. Variables → `ADMIN_PASSWORD` = una password lunga.
3. Tasto destro sul servizio → Attach Volume → mount path `/data`.
4. Settings → Networking → Generate Domain.
5. Apri `https://<dominio>/setup` e collega Garmin.
6. claude.ai → Impostazioni → Connettori → Aggiungi connettore personalizzato →
   URL `https://<dominio>/mcp` → si apre `/login` → password → fatto.

### Render
New → Blueprint → seleziona il repo (usa `render.yaml`; serve il piano Starter per il disco).

Se il login Garmin da `/setup` viene bloccato (IP del datacenter), genera i token
da un altro dispositivo con `garmin-mcp-auth --print` e incolla il JSON stampato nella
sezione "incolla i token" di `/setup`.

## Debug

```powershell
uv run mcp dev src/garmin_mcp/server.py   # MCP Inspector nel browser
```
Log Claude Desktop: `%APPDATA%\Claude\logs\mcp-server-garmin.log`

## Note

L'API usata non è ufficiale: Garmin può cambiare l'SSO (è successo a marzo 2026).
In quel caso: `uv lock --upgrade-package garminconnect && uv sync`.
