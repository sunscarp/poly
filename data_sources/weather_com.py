"""
weather.com data source — The Weather Company (TWC) API.
Provides daily high and current-hour forecasts. API key is public (from weather.com frontend).
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

API_KEY = "6532d6454b8aa370768e63d6ba5a832e"
BASE_URL = "https://api.weather.com/v3/wx"

_last_request_time = 0.0
_MIN_DELAY = 0.5


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_DELAY:
        time.sleep(_MIN_DELAY - elapsed)
    _last_request_time = time.time()


def _get(endpoint: str, lat: float, lon: float, units: str = "e") -> Optional[dict]:
    _rate_limit()
    url = f"{BASE_URL}/{endpoint}"
    params = {
        "geocode": f"{lat:.4f},{lon:.4f}",
        "units": units,
        "language": "en-US",
        "format": "json",
        "apiKey": API_KEY,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("TWC API error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    return None


def get_daily_high(lat: float, lon: float, target_date: str,
                   unit: str = "e") -> Optional[float]:
    data = _get("forecast/daily/5day", lat, lon, unit)
    if not data:
        return None

    times = data.get("validTimeLocal", [])
    highs = data.get("calendarDayTemperatureMax", data.get("temperatureMax", []))

    for i, high in enumerate(highs):
        if i < len(times) and times[i]:
            date_str = times[i][:10]
            if date_str == target_date and high is not None:
                return float(high)
    return None


def get_current_hour_temp(lat: float, lon: float, unit: str = "e") -> Optional[float]:
    data = _get("forecast/hourly/2day", lat, lon, unit)
    if not data:
        return None

    temps = data.get("temperature", [])
    times = data.get("validTimeLocal", [])
    if not temps:
        return None

    now = datetime.now(timezone.utc)
    best_idx = 0
    best_diff = float("inf")
    for i, t_str in enumerate(times):
        try:
            t = datetime.fromisoformat(t_str)
            diff = abs((t - now).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        except (ValueError, TypeError):
            continue

    return float(temps[best_idx])
