"""Outdoor weather via Open-Meteo (free, no API key).

Provides current outdoor temperature/humidity and a short forecast lookahead so
the controller's predictive feedforward can pre-cool before a heat ramp.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


@dataclass
class Weather:
    temp_f: float
    humidity: Optional[float]
    forecast_temp_f: Optional[float]   # temp ~lead minutes ahead
    source: str = "open-meteo"


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def get_weather(lat: float, lon: float, lead_min: float = 60.0,
                timeout: float = 10.0) -> Weather:
    """Fetch current conditions + an interpolated forecast `lead_min` ahead.

    Open-Meteo returns hourly arrays; we take the current hour and the next hour
    and linearly interpolate to approximate the temperature `lead_min` ahead.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m",
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "forecast_days": 2,
        "timezone": "auto",
    }
    r = requests.get(OPEN_METEO, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    cur = data.get("current", {})
    t = cur.get("temperature_2m")
    if t is None:
        raise ValueError("Open-Meteo response missing current temperature_2m")
    temp_f = float(t)
    humidity = cur.get("relative_humidity_2m")
    humidity = float(humidity) if humidity is not None else None

    forecast_temp = _forecast_ahead(data, cur.get("time"), lead_min)
    return Weather(temp_f=temp_f, humidity=humidity, forecast_temp_f=forecast_temp)


def _forecast_ahead(data: dict, current_time: Optional[str],
                    lead_min: float) -> Optional[float]:
    hourly = data.get("hourly", {})
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    if not times or not temps or current_time is None:
        return None
    # find the current hour index (current_time is like 'YYYY-MM-DDTHH:MM')
    cur_hour = current_time[:13]  # truncate to hour
    idx = next((i for i, t in enumerate(times) if t[:13] == cur_hour), None)
    if idx is None:
        return None
    # measure the lead from NOW, not from the top of the hour: include the current
    # minutes-past-the-hour, else the realized lead swings by up to ~60 min.
    try:
        cur_min = int(current_time[14:16])
    except (ValueError, IndexError):
        cur_min = 0
    frac = (lead_min + cur_min) / 60.0
    j = idx + int(frac)
    if j + 1 < len(temps):
        a, b = temps[j], temps[j + 1]
        r = frac - int(frac)
        return float(a + (b - a) * r)
    if idx < len(temps):
        return float(temps[min(idx + round(frac), len(temps) - 1)])
    return None
