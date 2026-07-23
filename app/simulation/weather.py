"""Weather Outlook — server-side 3-day forecast for the simulator setup page.

Fetched from Open-Meteo (free, no API key, no signup) on the server so no
API call happens in the browser and the CSP stays clean. Same defensive
principle as the AI layer: the forecast is a nice-to-have. If Open-Meteo is
slow, down, or changes shape, get_outlook() returns None and the page renders
a plain "unavailable" note — it must NEVER raise into the request.

Response is cached in-process for CACHE_TTL so refreshes don't re-hit the API.
Single-process cache; a multi-worker deployment would move this to a shared cache.
"""

import time
from datetime import datetime

import httpx

# San Pedro, Laguna (approx. municipal center).
_LAT = 14.3583
_LON = 121.0583
_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 5.0            # seconds — fail fast; the page must not hang on weather
CACHE_TTL = 30 * 60       # 30 minutes
FORECAST_DAYS = 7         # horizontal 7-tile strip on the setup page


def _icon_for_code(code) -> str:
    """Map a WMO weather code to a Bootstrap Icons name for the day tile.
    Unknown/missing codes fall back to a neutral cloud."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "bi-cloud"
    if c == 0:
        return "bi-sun"                    # clear
    if c in (1, 2):
        return "bi-cloud-sun"              # mainly clear / partly cloudy
    if c == 3:
        return "bi-clouds"                 # overcast
    if c in (45, 48):
        return "bi-cloud-fog"              # fog
    if c in (51, 53, 55, 56, 57):
        return "bi-cloud-drizzle"          # drizzle
    if c in (61, 63, 65, 66, 67):
        return "bi-cloud-rain"             # rain
    if c in (80, 81, 82):
        return "bi-cloud-rain-heavy"       # rain showers
    if c in (95, 96, 99):
        return "bi-cloud-lightning-rain"   # thunderstorm
    if c in (71, 73, 75, 77, 85, 86):
        return "bi-cloud-snow"             # snow (not expected locally)
    return "bi-cloud"

# Module-level cache: {"expires_at": float, "data": <outlook dict>}
_cache = {"expires_at": 0.0, "data": None}


def _fetch() -> dict:
    """Call Open-Meteo and shape the daily arrays into a compact outlook dict.
    Raises on any network/parse error — the caller wraps this."""
    params = {
        "latitude": _LAT,
        "longitude": _LON,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,"
                 "precipitation_sum,precipitation_probability_max",
        "timezone": "Asia/Manila",
        "forecast_days": FORECAST_DAYS,
    }
    resp = httpx.get(_URL, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    daily = payload["daily"]
    units = payload.get("daily_units", {})
    codes = daily.get("weathercode", [None] * len(daily["time"]))
    days = []
    for i, iso in enumerate(daily["time"]):
        d = datetime.strptime(iso, "%Y-%m-%d")
        days.append({
            "day": d.strftime("%a"),           # compact tile heading, e.g. "Thu"
            "label": d.strftime("%a, %b %d"),   # full label (tooltip/other uses)
            "icon": _icon_for_code(codes[i]),
            "tmax": round(daily["temperature_2m_max"][i]),
            "tmin": round(daily["temperature_2m_min"][i]),
            "precip_mm": daily["precipitation_sum"][i],
            "rain_chance": daily["precipitation_probability_max"][i],
        })

    return {
        "days": days,
        "unit_temp": units.get("temperature_2m_max", "°C"),
        "unit_precip": units.get("precipitation_sum", "mm"),
    }


def get_outlook() -> dict | None:
    """Return the cached/fresh 3-day outlook, or None if unavailable.

    Never raises: any failure (timeout, HTTP error, unexpected JSON) is caught,
    logged by TYPE only, and reported as None so the page still renders."""
    now = time.time()
    if _cache["data"] is not None and now < _cache["expires_at"]:
        return _cache["data"]

    try:
        data = _fetch()
    except Exception as exc:
        # Log the failure TYPE only (no message/URL noise), mirroring ai_layer.
        print(f"[weather] outlook unavailable: {type(exc).__name__}")
        return None

    _cache["data"] = data
    _cache["expires_at"] = now + CACHE_TTL
    return data
