"""
aviationweather.gov METAR client — free, no auth required.
Provides real-time observed temperature at airport stations.
"""

import time
import logging
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

BASE_URL = "https://aviationweather.gov/api/data/metar"


def get_current_temp(icao: str, hours_back: float = 1.5) -> Optional[dict]:
    params = {
        "ids": icao,
        "format": "json",
        "hours": hours_back,
    }
    headers = {"User-Agent": "WeatherNoBot/1.0"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(BASE_URL, params=params, headers=headers,
                                timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            if not data or not isinstance(data, list) or len(data) == 0:
                return None

            obs = data[0]
            temp_c = obs.get("temp")
            if temp_c is None:
                return None

            temp_f = round(temp_c * 9 / 5 + 32, 1)
            return {
                "temp_c": temp_c,
                "temp_f": temp_f,
                "report_time": obs.get("reportTime", ""),
                "raw_ob": obs.get("rawOb", ""),
                "flight_cat": obs.get("fltCat", ""),
            }
        except requests.RequestException as e:
            logger.error("METAR error for %s (attempt %d/%d): %s", icao, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    return None
