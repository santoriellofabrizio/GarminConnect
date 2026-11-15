"""Server MCP per Garmin Connect.

Modalità: stdio (default, Claude Desktop) oppure HTTP con OAuth
(MCP_TRANSPORT=http, per il deploy online e i connettori di claude.ai).

I tool restituiscono riepiloghi compatti per non saturare il contesto del
modello; dove utile c'è `full=True` per avere il JSON grezzo di Garmin.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import get_client, reset_client

HTTP_MODE = os.getenv("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http")
_auth_kwargs: dict[str, Any] = {}
_provider = None

if HTTP_MODE:
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    from .auth_provider import PasswordOAuthProvider

    BASE_URL = (
        os.getenv("PUBLIC_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.getenv("RAILWAY_PUBLIC_DOMAIN") else "")
    ).rstrip("/")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
    DATA_DIR = os.getenv("DATA_DIR", "/data")
    if not BASE_URL:
        sys.exit("Imposta PUBLIC_URL (es. https://garmin-mcp.up.railway.app)")
    if len(ADMIN_PASSWORD) < 12:
        sys.exit("Imposta ADMIN_PASSWORD (almeno 12 caratteri)")
    os.environ.setdefault("GARMINTOKENS", os.path.join(DATA_DIR, "garminconnect"))

    _provider = PasswordOAuthProvider(BASE_URL, ADMIN_PASSWORD, DATA_DIR)
    _auth_kwargs = {
        "auth_server_provider": _provider,
        "auth": AuthSettings(
            issuer_url=BASE_URL,
            resource_server_url=f"{BASE_URL}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    }

mcp = MCPServer(
    "garmin",
    **_auth_kwargs,
    instructions=(
        "Accesso in sola lettura ai dati Garmin Connect dell'utente: salute "
        "giornaliera, sonno, HRV, Body Battery, attività, metriche di allenamento. "
        "Date in formato YYYY-MM-DD; se omesse si usa oggi. Usa garmin_raw solo "
        "se nessun tool specifico copre la richiesta."
    ),
)

MAX_CHARS = 60_000


# ----------------------------------------------------------------- helpers
def _d(day: str | None) -> str:
    return day or date.today().isoformat()


def _pick(obj: dict[str, Any] | None, keys: list[str]) -> dict[str, Any]:
    if not obj:
        return {}
    return {k: obj.get(k) for k in keys if obj.get(k) is not None}


def _out(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f'... [troncato: {len(text)} caratteri totali, restringi il periodo]'
    return text


def _g():
    try:
        return get_client()
    except Exception as e:  # noqa: BLE001
        raise ToolError(str(e)) from e


def _call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """Chiama un metodo del client; su errore di auth resetta e riprova una volta."""
    try:
        try:
            return getattr(get_client(), fn_name)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            if "401" in str(e):
                reset_client()
                return getattr(get_client(), fn_name)(*args, **kwargs)
            raise
    except Exception as e:  # noqa: BLE001
        # ToolError fa arrivare il messaggio al modello invece di un errore generico
        raise ToolError(f"{type(e).__name__}: {e}") from e


def _sec_to_h(s: Any) -> float | None:
    return round(s / 3600, 2) if isinstance(s, (int, float)) else None


ACTIVITY_KEYS = [
    "activityId", "activityName", "startTimeLocal", "activityType",
    "distance", "duration", "movingDuration", "elevationGain",
    "averageSpeed", "averageHR", "maxHR", "calories",
    "aerobicTrainingEffect", "anaerobicTrainingEffect",
    "trainingEffectLabel", "activityTrainingLoad", "vO2MaxValue",
    "averageRunningCadenceInStepsPerMinute", "avgPower", "normPower",
    "locationName",
]


def _activity_summary(a: dict[str, Any]) -> dict[str, Any]:
    s = _pick(a, ACTIVITY_KEYS)
    if isinstance(s.get("activityType"), dict):
        s["activityType"] = s["activityType"].get("typeKey")
    if "distance" in s:
        s["distance_km"] = round(s.pop("distance") / 1000, 2)
    if "duration" in s:
        s["duration_min"] = round(s.pop("duration") / 60, 1)
    if "movingDuration" in s:
        s["moving_min"] = round(s.pop("movingDuration") / 60, 1)
    if s.get("averageSpeed") and s.get("activityType", "").endswith("running"):
        pace = 1000 / s["averageSpeed"] / 60
        s["pace_min_km"] = f"{int(pace)}:{int((pace % 1) * 60):02d}"
    return s


# ------------------------------------------------------------------ profilo
@mcp.tool()
def garmin_profile() -> str:
    """Nome, sistema di unità, fuso orario e dispositivi Garmin dell'utente."""
    g = _g()
    devices = [
        _pick(d, ["productDisplayName", "deviceId", "lastSyncTime", "currentFirmwareVersion"])
        for d in (g.get_devices() or [])
    ]
    return _out({
        "name": g.get_full_name(),
        "units": g.get_unit_system(),
        "timezone": g.get_user_timezone(),
        "devices": devices,
    })


# ------------------------------------------------------------------- salute
@mcp.tool()
def garmin_daily_summary(day: str | None = None, full: bool = False) -> str:
    """Riepilogo giornaliero: passi, calorie, FC a riposo, stress, Body Battery, minuti intensi."""
    data = _call("get_user_summary", _d(day))
    if full:
        return _out(data)
    return _out(_pick(data, [
        "calendarDate", "totalSteps", "dailyStepGoal", "totalDistanceMeters",
        "totalKilocalories", "activeKilocalories", "restingHeartRate",
        "minHeartRate", "maxHeartRate", "averageStressLevel", "maxStressLevel",
        "bodyBatteryHighestValue", "bodyBatteryLowestValue",
        "bodyBatteryMostRecentValue", "bodyBatteryChargedValue",
        "bodyBatteryDrainedValue", "moderateIntensityMinutes",
        "vigorousIntensityMinutes", "floorsAscended", "averageSpo2",
        "avgWakingRespirationValue",
    ]))


@mcp.tool()
def garmin_sleep(day: str | None = None, full: bool = False) -> str:
    """Sonno della notte che termina nel giorno indicato: durata, fasi, punteggio, HRV notturna."""
    data = _call("get_sleep_data", _d(day))
    if full:
        return _out(data)
    dto = (data or {}).get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    return _out({
        "date": dto.get("calendarDate"),
        "sleep_h": _sec_to_h(dto.get("sleepTimeSeconds")),
        "deep_h": _sec_to_h(dto.get("deepSleepSeconds")),
        "light_h": _sec_to_h(dto.get("lightSleepSeconds")),
        "rem_h": _sec_to_h(dto.get("remSleepSeconds")),
        "awake_h": _sec_to_h(dto.get("awakeSleepSeconds")),
        "score": (scores.get("overall") or {}).get("value"),
        "score_qualifier": (scores.get("overall") or {}).get("qualifierKey"),
        "avg_respiration": dto.get("averageRespirationValue"),
        "avg_stress": dto.get("avgSleepStress"),
        "resting_hr": data.get("restingHeartRate") if data else None,
        "avg_overnight_hrv": data.get("avgOvernightHrv") if data else None,
        "body_battery_change": data.get("bodyBatteryChange") if data else None,
        "feedback": dto.get("sleepScoreFeedback"),
    })


@mcp.tool()
def garmin_sleep_range(start: str, end: str) -> str:
    """Punteggi e durate del sonno giorno per giorno tra due date (max ~30 giorni consigliati)."""
    return _out(_call("get_sleep_daily", start, end))


@mcp.tool()
def garmin_hrv(day: str | None = None) -> str:
    """HRV notturna: media, baseline, stato (balanced/unbalanced/low)."""
    data = _call("get_hrv_data", _d(day)) or {}
    return _out(data.get("hrvSummary") or data)


@mcp.tool()
def garmin_hrv_range(start: str, end: str) -> str:
    """HRV giornaliera tra due date (trend)."""
    return _out(_call("get_hrv_data_range", start, end))


@mcp.tool()
def garmin_body_battery(start: str | None = None, end: str | None = None) -> str:
    """Body Battery per giorno (carica/scarica) tra due date; default oggi."""
    data = _call("get_body_battery", _d(start), end)
    slim = [
        {k: v for k, v in day.items() if k != "bodyBatteryValuesArray"}
        for day in (data or [])
    ]
    return _out(slim)


@mcp.tool()
def garmin_stress(day: str | None = None) -> str:
    """Stress medio/massimo del giorno e distribuzione (senza la serie a 3 minuti)."""
    data = _call("get_stress_data", _d(day)) or {}
    return _out({k: v for k, v in data.items() if not k.endswith("ValuesArray")})


@mcp.tool()
def garmin_heart_rate(day: str | None = None) -> str:
    """FC del giorno: riposo, min, max, media 7 giorni (senza serie intraday)."""
    data = _call("get_heart_rates", _d(day)) or {}
    return _out({k: v for k, v in data.items() if k != "heartRateValues"})


@mcp.tool()
def garmin_resting_hr_range(start: str, end: str) -> str:
    """Frequenza cardiaca a riposo giorno per giorno tra due date."""
    return _out(_call("get_rhr_daily", start, end))


@mcp.tool()
def garmin_body_composition(start: str, end: str | None = None) -> str:
    """Peso, BMI, % grasso, massa muscolare tra due date (da bilancia Garmin o inserimenti)."""
    return _out(_call("get_body_composition", start, end))


# ---------------------------------------------------------------- attività
@mcp.tool()
def garmin_activities(
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
    limit: int = 20,
) -> str:
    """Elenco attività (più recenti prima) con distanza, durata, passo, FC, training effect.

    Senza date restituisce le ultime `limit` attività. activity_type es.:
    running, cycling, swimming, walking, hiking, strength_training.
    """
    if start_date:
        acts = _call("get_activities_by_date", start_date, end_date, activity_type, "desc")
    else:
        acts = _call("get_activities", 0, limit, activity_type)
    return _out([_activity_summary(a) for a in (acts or [])[:limit]])


@mcp.tool()
def garmin_activity(activity_id: int, include_splits: bool = True) -> str:
    """Dettaglio di un'attività: riepilogo, lap/split, tempo nelle zone FC, meteo."""
    a = _call("get_activity", activity_id) or {}
    out: dict[str, Any] = {
        "name": a.get("activityName"),
        "type": (a.get("activityTypeDTO") or {}).get("typeKey"),
        "start": (a.get("summaryDTO") or {}).get("startTimeLocal"),
        "summary": a.get("summaryDTO"),
        "location": a.get("locationName"),
    }
    if include_splits:
        laps = (_call("get_activity_splits", activity_id) or {}).get("lapDTOs") or []
        out["laps"] = [
            _pick(l, ["lapIndex", "distance", "duration", "averageSpeed",
                      "averageHR", "maxHR", "elevationGain", "averageRunCadence", "averagePower"])
            for l in laps
        ]
    try:
        out["hr_zones"] = _call("get_activity_hr_in_timezones", activity_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["weather"] = _pick(_call("get_activity_weather", activity_id),
                               ["temp", "apparentTemp", "relativeHumidity", "windSpeed", "weatherTypeDTO"])
    except Exception:  # noqa: BLE001
        pass
    return _out(out)


# ---------------------------------------------------- metriche allenamento
@mcp.tool()
def garmin_training_status(day: str | None = None) -> str:
    """Training status, carico acuto/cronico, VO2max, bilanciamento carico 4 settimane."""
    return _out(_call("get_training_status", _d(day)))


@mcp.tool()
def garmin_training_readiness(day: str | None = None) -> str:
    """Training readiness del giorno con i fattori (sonno, recupero, HRV, carico)."""
    return _out(_call("get_training_readiness", _d(day)))


@mcp.tool()
def garmin_race_predictions() -> str:
    """Tempi previsti su 5k, 10k, mezza e maratona."""
    return _out(_call("get_race_predictions"))


@mcp.tool()
def garmin_personal_records() -> str:
    """Record personali (es. 1k, 5k, 10k, mezza, distanza più lunga)."""
    return _out(_call("get_personal_record"))


@mcp.tool()
def garmin_max_metrics(day: str | None = None) -> str:
    """VO2max (corsa/bici) e fitness age più recenti."""
    return _out(_call("get_max_metrics", _d(day)))


@mcp.tool()
def garmin_endurance_score(start: str, end: str | None = None) -> str:
    """Endurance score tra due date (aggregazione settimanale)."""
    return _out(_call("get_endurance_score", start, end))


@mcp.tool()
def garmin_weekly_overview(weeks: int = 4) -> str:
    """Panoramica delle ultime N settimane: passi, stress, minuti intensi per settimana."""
    end = date.today().isoformat()
    return _out({
        "steps": _call("get_weekly_steps", end, weeks),
        "stress": _call("get_weekly_stress", end, weeks),
        "intensity_minutes": _call(
            "get_weekly_intensity_minutes",
            (date.today() - timedelta(weeks=weeks)).isoformat(), end,
        ),
    })


# ----------------------------------------------------------- escape hatch
@mcp.tool()
def garmin_raw(method: str, args_json: str = "[]", kwargs_json: str = "{}") -> str:
    """Chiama direttamente un metodo `get_*` di python-garminconnect (sola lettura).

    Esempi: method="get_spo2_data", args_json='["2026-11-14"]';
    method="get_lactate_threshold"; method="get_gear_list".
    """
    if not method.startswith("get_"):
        return "Consentiti solo metodi get_* (sola lettura)."
    if not hasattr(_g(), method):
        return f"Metodo sconosciuto: {method}"
    return _out(_call(method, *json.loads(args_json), **json.loads(kwargs_json)))


if HTTP_MODE:
    from .web import register

    register(mcp, _provider)


def main() -> None:
    if HTTP_MODE:
        mcp.run("streamable-http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
