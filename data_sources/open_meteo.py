"""
Open-Meteo API client — free, no auth required.
Provides secondary forecast cross-check for entry decisions.
Tries multiple connection methods to bypass firewall restrictions.
"""

import time
import json
import ssl
import logging
from typing import Optional
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"
ALT_URLS = [
    "https://api.open-meteo.com/v1/forecast",
]

_cache: dict[str, tuple[float, float]] = {}
_CACHE_TTL = 600
_last_request_ts: float = 0.0
_MIN_GAP = 5.0
_cooldown_until: float = 0.0


def _cache_key(lat: float, lon: float, target_date: str, unit: str, model: str) -> str:
    return f"{lat:.2f},{lon:.2f},{target_date},{unit},{model}"


def _cache_get(key: str) -> Optional[float]:
    entry = _cache.get(key)
    if entry and entry[1] > time.time():
        return entry[0]
    _cache.pop(key, None)
    return None


def _cache_set(key: str, value: float):
    _cache[key] = (value, time.time() + _CACHE_TTL)


def _throttle():
    global _last_request_ts, _cooldown_until
    now = time.time()
    if _cooldown_until > now:
        time.sleep(_cooldown_until - now)
    wait = _MIN_GAP - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()


def _fetch_urllib(url: str, params: dict, timeout: int = 20) -> Optional[dict]:
    """Method 1: urllib with custom SSL context."""
    global _cooldown_until
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        full_url = f"{url}?{urlencode(params)}"
        req = Request(full_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 429:
            _cooldown_until = time.time() + 60
            logger.warning("Open-Meteo 429 rate limited, backing off 60s")
        else:
            logger.debug("urllib fetch failed: %s", e)
        return None
    except Exception as e:
        logger.debug("urllib fetch failed: %s", e)
        return None


def _fetch_requests(url: str, params: dict, timeout: tuple = (5, 15)) -> Optional[dict]:
    """Method 2: requests with session reuse."""
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })
        resp = s.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("requests fetch failed: %s", e)
        return None


def _fetch_requests_nossl(url: str, params: dict, timeout: tuple = (5, 15)) -> Optional[dict]:
    """Method 2: requests with SSL verification disabled."""
    global _cooldown_until
    try:
        resp = requests.get(
            url, params=params, timeout=timeout,
            verify=False,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        if resp.status_code == 429:
            _cooldown_until = time.time() + 60
            logger.warning("Open-Meteo 429 rate limited, backing off 60s")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("requests(nossl) fetch failed: %s", e)
        return None


def _try_fetch(params: dict) -> Optional[dict]:
    """Try multiple methods to fetch from Open-Meteo."""
    for url in ALT_URLS:
        # Method 1: urllib
        data = _fetch_urllib(url, params)
        if data and not data.get("error"):
            return data
        # Method 2: requests without SSL verify
        data = _fetch_requests_nossl(url, params)
        if data and not data.get("error"):
            return data
    return None


def get_daily_high(lat: float, lon: float, target_date: str,
                   unit: str = "celsius", tz: str = "UTC",
                   model: str = "ecmwf_ifs025") -> Optional[float]:
    key = _cache_key(lat, lon, target_date, unit, model)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": unit,
        "forecast_days": 7,
        "timezone": tz,
        "models": model,
        "bias_correction": "true",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            data = _try_fetch(params)
            if data is None:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt * 2)
                continue
            times = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            for i, t in enumerate(times):
                if t == target_date and i < len(temps) and temps[i] is not None:
                    temp = temps[i]
                    _cache_set(key, temp)
                    return temp
            return None
        except Exception as e:
            logger.error("Open-Meteo error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt * 2)
    return None


def get_forecast_direction(lat: float, lon: float, target_date: str,
                           threshold: float, unit: str = "celsius",
                           tz: str = "UTC") -> Optional[dict]:
    high = get_daily_high(lat, lon, target_date, unit, tz)
    if high is None:
        return None
    return {
        "high": high,
        "above_threshold": high >= threshold,
        "distance": abs(high - threshold),
    }
