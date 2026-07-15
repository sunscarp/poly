#!/usr/bin/env python3
"""
Polymarket Holdings Tracker — Dashboard + Telegram Alerts
Tracks NO positions on highest-temperature markets.
Shows Weather.com, Open-Meteo daily highs, METAR current temps, and live NO prices.
Sends Telegram alerts on critical forecast/price changes.

Usage:
    python run.py              # start dashboard on port 8080
    python run.py --port 9000  # custom port

TELEGRAM SETUP:
    1. Message @BotFather on Telegram -> /newbot -> copy the token
    2. Message your bot, then visit:
       https://api.telegram.org/bot<TOKEN>/getUpdates
       Find "chat":{"id":123456789} -> that's your chat ID
    3. Set env vars:
       TELEGRAM_BOT_TOKEN = 123456789:ABCdefGhi...
       TELEGRAM_CHAT_ID = 123456789
"""

import json
import os
import re
import ssl
import sys
import time
import threading
import logging
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
from typing import Optional

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tracker")

WALLET = "0x5184512743497f5b1f7843ce0c992b87d2889211"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
PORT = int(os.environ.get("TRACKER_PORT", "8080"))
STATIONS_FILE = Path(__file__).parent / "stations.json"
REFRESH_INTERVAL = 30

TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_ENABLED = bool(TG_BOT_TOKEN and TG_CHAT_ID)

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
RETRY_DELAY = 2

_stations: dict = {}
_holdings: list[dict] = []
_last_refresh: float = 0
_refresh_lock = threading.Lock()

TWC_API_KEY = "6532d6454b8aa370768e63d6ba5a832e"
TWC_BASE = "https://api.weather.com/v3/wx"
OM_BASE = "https://api.open-meteo.com/v1/forecast"

_twc_last_req = 0.0
_om_last_req = 0.0
_metar_last_req = 0.0
_last_telegram_ts = 0.0

ALERT_STATE_FILE = Path(__file__).parent / "alert_state.json"
_alert_state: dict[str, dict] = {}


# ── Stations ─────────────────────────────────────────────────────────────────

def _load_stations() -> dict:
    global _stations
    try:
        _stations = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
        logger.info("Loaded %d stations", len(_stations))
    except Exception as e:
        logger.error("Failed to load stations.json: %s", e)
        _stations = {}


# ── Rate Limiters ────────────────────────────────────────────────────────────

def _twc_rate_limit():
    global _twc_last_req
    elapsed = time.time() - _twc_last_req
    if elapsed < 0.6:
        time.sleep(0.6 - elapsed)
    _twc_last_req = time.time()


def _om_rate_limit():
    global _om_last_req
    elapsed = time.time() - _om_last_req
    if elapsed < 2.0:
        time.sleep(2.0 - elapsed)
    _om_last_req = time.time()


# ── HTTP Helpers ─────────────────────────────────────────────────────────────

def _http_get(url: str, params: dict = None, headers: dict = None,
              timeout: int = REQUEST_TIMEOUT, verify: bool = True) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=timeout, verify=verify)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.debug("HTTP GET %s failed (attempt %d/%d): %s", url, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    return None


def _fetch_urllib(url: str, params: dict, timeout: int = 20) -> Optional[dict]:
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        full_url = f"{url}?{urlencode(params)}"
        req = Request(full_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("urllib fetch failed: %s", e)
        return None


def _fetch_nossl(url: str, params: dict, timeout: int = 15) -> Optional[dict]:
    try:
        resp = requests.get(url, params=params, timeout=timeout, verify=False,
                            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("requests(nossl) fetch failed: %s", e)
        return None


# ── Polymarket Data API ──────────────────────────────────────────────────────

def fetch_positions() -> list[dict]:
    url = f"{DATA_API}/positions"
    data = _http_get(url, params={"user": WALLET, "limit": 200})
    if data is None:
        logger.error("Failed to fetch positions from Data API")
        return []
    return data if isinstance(data, list) else []


def fetch_market(market_id: str) -> Optional[dict]:
    url = f"{GAMMA_API}/markets/{market_id}"
    return _http_get(url)


# ── Slug / Title Parsing ─────────────────────────────────────────────────────

def parse_slug(slug: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(r'highest-temperature-in-(.+?)-on-(.+)', slug)
    if not m:
        return None, None
    city_slug = m.group(1)
    date_part = m.group(2)
    for i, month_name in enumerate(MONTHS):
        pattern = f"{month_name}-(\\d+)-(\\d{{4}})"
        dm = re.match(pattern, date_part)
        if dm:
            day = int(dm.group(1))
            year = int(dm.group(2))
            date_str = f"{year}-{i + 1:02d}-{day:02d}"
            return city_slug, date_str
    return city_slug, None


CITY_NAME_MAP = {
    "new york city": "nyc", "new york": "nyc", "nyc": "nyc",
    "chicago": "chicago", "miami": "miami", "dallas": "dallas",
    "seattle": "seattle", "atlanta": "atlanta",
    "los angeles": "los-angeles", "san francisco": "san-francisco",
    "houston": "houston", "toronto": "toronto",
    "mexico city": "mexico-city", "austin": "austin", "denver": "denver",
    "buenos aires": "buenos-aires", "sao paulo": "sao-paulo",
    "panama city": "panama-city",
    "london": "london", "paris": "paris", "madrid": "madrid",
    "warsaw": "warsaw", "helsinki": "helsinki", "amsterdam": "amsterdam",
    "munich": "munich", "milan": "milan", "moscow": "moscow",
    "istanbul": "istanbul", "ankara": "ankara", "tel aviv": "tel-aviv",
    "jeddah": "jeddah", "cape town": "cape-town",
    "shanghai": "shanghai", "seoul": "seoul", "wellington": "wellington",
    "tokyo": "tokyo", "taipei": "taipei", "wuhan": "wuhan",
    "shenzhen": "shenzhen", "beijing": "beijing", "chengdu": "chengdu",
    "guangzhou": "guangzhou", "qingdao": "qingdao", "chongqing": "chongqing",
    "singapore": "singapore", "kuala lumpur": "kuala-lumpur",
    "manila": "manila", "lucknow": "lucknow", "busan": "busan",
    "karachi": "karachi",
}

MONTH_NAME_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_title(title: str) -> tuple[Optional[str], Optional[str]]:
    m = re.search(
        r'highest temperature in (.+?) (?:be\s+\d+.*?\s+)?on (\w+) (\d+)(?:,?\s*(\d{4}))?',
        title, re.IGNORECASE
    )
    if not m:
        return None, None
    city_name = m.group(1).strip().lower()
    month_name = m.group(2).strip().lower()
    day = int(m.group(3))
    year_str = m.group(4)
    year = int(year_str) if year_str else datetime.now(timezone.utc).year
    city_slug = CITY_NAME_MAP.get(city_name, city_name.replace(" ", "-"))
    month_num = MONTH_NAME_MAP.get(month_name)
    if not month_num:
        return city_slug, None
    date_str = f"{year}-{month_num:02d}-{day:02d}"
    return city_slug, date_str


def parse_bucket(title: str) -> str:
    m = re.search(r'be\s+(\d+)\s*\u00b0\s*([FC])', title, re.IGNORECASE)
    if m:
        return f"{m.group(1)}\u00b0{m.group(2).upper()}"
    m = re.search(r'between\s+(\d+)\s*[-\u2013]\s*(\d+)\s*\u00b0\s*([FC])', title, re.IGNORECASE)
    if m:
        return f"{m.group(1)}-{m.group(2)}\u00b0{m.group(3).upper()}"
    m = re.search(r'(\d+)\s*\u00b0\s*([FC])\s+or\s+(?:higher|below)', title, re.IGNORECASE)
    if m:
        return f"{m.group(1)}\u00b0{m.group(2).upper()}"
    return "?"


def parse_bucket_threshold(bucket: str) -> Optional[float]:
    m = re.match(r'(\d+)', bucket)
    return float(m.group(1)) if m else None


# ── Weather.com (TWC) ────────────────────────────────────────────────────────

def get_tw_daily_high(lat: float, lon: float, target_date: str,
                      unit: str = "e") -> Optional[float]:
    _twc_rate_limit()
    url = f"{TWC_BASE}/forecast/daily/5day"
    params = {
        "geocode": f"{lat:.4f},{lon:.4f}",
        "units": unit, "language": "en-US", "format": "json", "apiKey": TWC_API_KEY,
    }
    data = _http_get(url, params=params)
    if not data:
        return None
    times = data.get("validTimeLocal", [])
    highs = data.get("calendarDayTemperatureMax", data.get("temperatureMax", []))
    for i, high in enumerate(highs):
        if i < len(times) and times[i] and times[i][:10] == target_date and high is not None:
            return float(high)
    return None


def get_tw_current_temp(lat: float, lon: float, unit: str = "e") -> Optional[float]:
    _twc_rate_limit()
    url = f"{TWC_BASE}/forecast/hourly/2day"
    params = {
        "geocode": f"{lat:.4f},{lon:.4f}",
        "units": unit, "language": "en-US", "format": "json", "apiKey": TWC_API_KEY,
    }
    data = _http_get(url, params=params)
    if not data:
        return None
    temps = data.get("temperature", [])
    times = data.get("validTimeLocal", [])
    if not temps:
        return None
    now = datetime.now(timezone.utc)
    best_idx, best_diff = 0, float("inf")
    for i, t_str in enumerate(times):
        try:
            diff = abs((datetime.fromisoformat(t_str) - now).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        except (ValueError, TypeError):
            continue
    return float(temps[best_idx])


# ── Open-Meteo ───────────────────────────────────────────────────────────────

def get_om_daily_high(lat: float, lon: float, target_date: str,
                      unit: str = "celsius", tz: str = "UTC") -> Optional[float]:
    _om_rate_limit()
    params = {
        "latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
        "temperature_unit": unit, "forecast_days": 7, "timezone": tz,
    }
    for fetcher in [lambda u, p: _fetch_urllib(u, p), lambda u, p: _fetch_nossl(u, p)]:
        try:
            data = fetcher(OM_BASE, params)
            if data and not data.get("error"):
                times = data.get("daily", {}).get("time", [])
                temps = data.get("daily", {}).get("temperature_2m_max", [])
                for i, t in enumerate(times):
                    if t == target_date and i < len(temps) and temps[i] is not None:
                        return float(temps[i])
        except Exception as e:
            logger.debug("Open-Meteo fetch failed: %s", e)
    return None


# ── METAR (aviationweather.gov) ─────────────────────────────────────────────

METAR_BASE = "https://aviationweather.gov/api/data/metar"


def get_metar_temp(icao: str) -> Optional[dict]:
    global _metar_last_req
    elapsed = time.time() - _metar_last_req
    if elapsed < 0.5:
        time.sleep(0.5 - elapsed)
    _metar_last_req = time.time()

    params = {"ids": icao, "format": "json", "hours": 1.5}
    headers = {"User-Agent": "Tracker/1.0"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(METAR_BASE, params=params, headers=headers,
                                timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                return None
            obs = data[0]
            temp_c = obs.get("temp")
            if temp_c is None:
                return None
            return {
                "temp_c": round(temp_c, 1),
                "temp_f": round(temp_c * 9 / 5 + 32, 1),
                "report_time": obs.get("reportTime", ""),
                "raw_ob": obs.get("rawOb", ""),
            }
        except requests.RequestException as e:
            logger.debug("METAR error %s (attempt %d): %s", icao, attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    return None


# ── Gamma API Live NO Price ────────────────────────────────────────────────

_price_cache: dict[str, tuple[float, float]] = {}
_PRICE_CACHE_TTL = 15


def get_live_no_price(market_id: str) -> Optional[float]:
    if not market_id:
        return None
    cached = _price_cache.get(market_id)
    if cached and cached[1] > time.time():
        return cached[0]
    url = f"{GAMMA_API}/markets/{market_id}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        mdata = resp.json()
        prices_raw = mdata.get("outcomePrices", "[0.5,0.5]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        no_price = float(prices[1]) if len(prices) > 1 else None
        if no_price is not None:
            _price_cache[market_id] = (no_price, time.time() + _PRICE_CACHE_TTL)
        return no_price
    except Exception as e:
        logger.debug("Gamma price error for %s: %s", market_id, e)
        return None


# ── Gamma API Bucket Discovery ───────────────────────────────────────────────

_event_cache: dict[str, tuple[dict, float]] = {}
_EVENT_CACHE_TTL = 300

REC_STATE_FILE = Path(__file__).parent / "rec_state.json"
_rec_state: dict[str, dict] = {}
_recommendations: list[dict] = []
_last_rec_refresh: float = 0
_rec_lock = threading.Lock()
_seen_recs: set[str] = set()
REC_REFRESH_INTERVAL = 300  # 5 minutes
_rec_first_scan_done = False


def _slug_for_date(city_slug: str, date_str: str) -> str:
    parts = date_str.split("-")
    year = parts[0]
    month = MONTHS[int(parts[1]) - 1]
    day = int(parts[2])
    return f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"


def _parse_temp_range(question: str) -> tuple[float, float]:
    m = re.search(r'between\s+(\d+(?:\.\d+)?)\s*[-\u2013]\s*(\d+(?:\.\d+)?)\s*\u00b0', question)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*\u00b0[FC]\s+or\s+below', question, re.IGNORECASE)
    if m:
        return (-999.0, float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*\u00b0[FC]\s+or\s+higher', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), 999.0)
    m = re.search(r'be\s+(\d+(?:\.\d+)?)\s*\u00b0[FC]\s+on', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return (val, val)
    return (0.0, 0.0)


def get_event(city_slug: str, date_str: str) -> Optional[dict]:
    slug = _slug_for_date(city_slug, date_str)
    cached = _event_cache.get(slug)
    if cached and cached[1] > time.time():
        return cached[0]
    url = f"{GAMMA_API}/events"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params={"slug": slug}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                event = data[0]
                _event_cache[slug] = (event, time.time() + _EVENT_CACHE_TTL)
                return event
            return None
        except requests.RequestException as e:
            logger.debug("Polymarket event error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    return None


def get_city_buckets(city_slug: str, date_str: str) -> list[dict]:
    event = get_event(city_slug, date_str)
    if not event:
        return []
    buckets = []
    for market in event.get("markets", []):
        question = market.get("question", "")
        t_low, t_high = _parse_temp_range(question)
        if t_low == 0.0 and t_high == 0.0:
            continue
        prices_raw = market.get("outcomePrices", "[0.5,0.5]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        yes_price = float(prices[0]) if len(prices) > 0 else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 0.5
        volume = float(market.get("volume", 0))
        clob_token_ids = market.get("clobTokenIds", "")
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids) if clob_token_ids else []
            except json.JSONDecodeError:
                clob_token_ids = []
        no_token_id = clob_token_ids[1] if len(clob_token_ids) > 1 else ""
        buckets.append({
            "market_id": market.get("id", ""),
            "token_id": no_token_id,
            "question": question,
            "range": (t_low, t_high),
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": volume,
            "active": market.get("active", True),
            "closed": market.get("closed", False),
        })
    buckets.sort(key=lambda b: b["range"][0])
    return buckets


def _compute_threshold(wc_high: float, t_low: float, t_high: float) -> float:
    if t_low == -999:
        return t_high
    if t_high == 999:
        return t_low
    return t_high if wc_high > (t_low + t_high) / 2 else t_low


# ── Region Scheduler ────────────────────────────────────────────────────────

def get_allowed_regions() -> set:
    IST = timezone(timedelta(hours=5, minutes=30))
    hour = datetime.now(IST).hour
    if hour < 9:
        return {"asia"}
    elif hour < 16:
        return {"asia", "europe", "africa"}
    else:
        return {"asia", "europe", "africa", "americas"}


def get_target_date() -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).strftime("%Y-%m-%d")


# ── Recommendation Scanner ──────────────────────────────────────────────────

def scan_recommendations() -> list[dict]:
    target_date = get_target_date()
    allowed = get_allowed_regions()
    results = []
    for slug, station in _stations.items():
        if station.get("region") not in allowed:
            continue
        lat, lon = station["lat"], station["lon"]
        unit = station.get("unit", "F")
        if lat is None or lon is None:
            continue
        twc_unit = "e" if unit == "F" else "m"
        wc_high = get_tw_daily_high(lat, lon, target_date, unit=twc_unit)
        if wc_high is None:
            continue
        om_high_c = get_om_daily_high(lat, lon, target_date, unit="celsius", tz=station.get("timezone", "UTC"))
        om_high = round(om_high_c * 9 / 5 + 32, 1) if om_high_c is not None and unit == "F" else om_high_c
        buckets = get_city_buckets(slug, target_date)
        for bucket in buckets:
            if bucket["closed"] or not bucket["active"]:
                continue
            if bucket["volume"] < 100:
                continue
            t_low, t_high = bucket["range"]
            if t_low == 0.0 and t_high == 0.0:
                continue
            threshold = _compute_threshold(wc_high, t_low, t_high)
            distance = abs(wc_high - threshold)
            no_price = bucket["no_price"]
            if no_price > 0.92:
                continue
            if distance < 0.5 or distance > 2.0:
                continue
            if no_price < 0.01:
                continue
            if om_high is not None:
                om_threshold = _compute_threshold(om_high, t_low, t_high)
                om_distance = abs(om_high - om_threshold)
                if om_distance < 0.5:
                    continue
            link = build_event_link(slug, target_date)
            results.append({
                "city_slug": slug,
                "city": station.get("name", slug),
                "date": target_date,
                "bucket_low": t_low,
                "bucket_high": t_high,
                "threshold": threshold,
                "distance": round(distance, 1),
                "no_price": no_price,
                "yes_price": bucket["yes_price"],
                "volume": bucket["volume"],
                "wc_high": wc_high,
                "om_high": om_high,
                "market_id": bucket["market_id"],
                "question": bucket["question"],
                "link": link,
                "region": station.get("region", ""),
            })
    results.sort(key=lambda r: r["distance"])
    return results


# ── Recommendation State & Alerts ───────────────────────────────────────────

def _load_rec_state():
    global _rec_state, _seen_recs
    try:
        raw = REC_STATE_FILE.read_text(encoding="utf-8")
        _rec_state = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        _rec_state = {}
    _seen_recs = set(_rec_state.keys())


def _save_rec_state():
    try:
        REC_STATE_FILE.write_text(
            json.dumps(_rec_state, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        logger.debug("Failed to save rec state: %s", e)


def _rec_key(r: dict) -> str:
    return f"{r['city_slug']}_{r['date']}_{r['bucket_low']}_{r['bucket_high']}"


def _send_rec_alerts(recs: list[dict]):
    global _rec_first_scan_done
    if not _rec_first_scan_done:
        _rec_first_scan_done = True
        for r in recs:
            key = _rec_key(r)
            _rec_state[key] = {
                "no_price": r["no_price"],
                "alerted_at": datetime.now(timezone.utc).isoformat(),
                "distance": r["distance"],
            }
            _seen_recs.add(key)
        _save_rec_state()
        logger.info("First rec scan: seeded %d recs (no alerts sent)", len(recs))
        return
    for r in recs:
        key = _rec_key(r)
        if key in _seen_recs:
            prev = _rec_state.get(key, {})
            prev_price = prev.get("no_price", 1.0)
            if r["no_price"] >= prev_price - 0.02:
                continue
        unit_char = "F" if r.get("wc_high") and r["city_slug"] in _stations and _stations[r["city_slug"]].get("unit") == "F" else "C"
        bucket_label = f"{int(r['threshold'])}°{unit_char}"
        if r["bucket_low"] == -999:
            bucket_label = f"≤{int(r['bucket_high'])}°{unit_char}"
        elif r["bucket_high"] == 999:
            bucket_label = f"≥{int(r['bucket_low'])}°{unit_char}"
        elif r["bucket_low"] == r["bucket_high"]:
            bucket_label = f"{int(r['bucket_low'])}°{unit_char}"
        else:
            bucket_label = f"{int(r['bucket_low'])}-{int(r['bucket_high'])}°{unit_char}"
        msg = (
            f"\U0001f7e2 <b>NEW BUY RECOMMENDATION</b>\n\n"
            f"<b>{r['city']}</b> | {bucket_label} | {r['date']}\n"
            f"NO Price: ${r['no_price']:.3f} | Distance: {r['distance']:.1f}°\n"
            f"WC High: {r['wc_high']:.1f}°{unit_char}"
        )
        if r["om_high"] is not None:
            msg += f" | OM High: {r['om_high']:.1f}°C"
        msg += f"\nVolume: {r['volume']:.0f}\n\n{r['link']}"
        send_telegram(msg)
        _rec_state[key] = {
            "no_price": r["no_price"],
            "alerted_at": datetime.now(timezone.utc).isoformat(),
            "distance": r["distance"],
        }
        _seen_recs.add(key)
    _save_rec_state()


def _rec_scan_loop():
    while True:
        time.sleep(REC_REFRESH_INTERVAL)
        try:
            refresh_recommendations()
        except Exception as e:
            logger.error("Recommendation scan error: %s", e)


def refresh_recommendations():
    global _recommendations, _last_rec_refresh
    with _rec_lock:
        logger.info("Scanning recommendations...")
        recs = scan_recommendations()
        _recommendations = recs
        _last_rec_refresh = time.time()
        _send_rec_alerts(recs)
        logger.info("Found %d recommendations", len(recs))


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    global _last_telegram_ts
    if not TG_ENABLED:
        logger.debug("Telegram disabled (no token/chat_id)")
        return
    elapsed = time.time() - _last_telegram_ts
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        resp.raise_for_status()
        _last_telegram_ts = time.time()
        logger.info("Telegram message sent")
    except Exception as e:
        logger.error("Telegram send failed: %s", e)


def build_event_link(city_slug: str, date_str: str) -> str:
    parts = date_str.split("-")
    month = MONTHS[int(parts[1]) - 1]
    day = int(parts[2])
    year = parts[0]
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    return f"https://polymarket.com/event/{slug}"


# ── Alert State ──────────────────────────────────────────────────────────────

def _load_alert_state():
    global _alert_state
    try:
        raw = ALERT_STATE_FILE.read_text(encoding="utf-8")
        _alert_state = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        _alert_state = {}


def _save_alert_state():
    try:
        ALERT_STATE_FILE.write_text(
            json.dumps(_alert_state, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        logger.debug("Failed to save alert state: %s", e)


def _get_alert_key(city_slug: str, date_str: str) -> str:
    return f"{city_slug}_{date_str}"


# ── Alert Checks ─────────────────────────────────────────────────────────────

def _check_alerts(holdings: list[dict]):
    for h in holdings:
        if h.get("error"):
            continue
        city_slug = h["city_slug"]
        date_str = h["date"]
        key = _get_alert_key(city_slug, date_str)
        state = _alert_state.setdefault(key, {})
        link = build_event_link(city_slug, date_str)
        bucket_str = h["bucket"]
        bucket_threshold = parse_bucket_threshold(bucket_str)
        city = h["city"]
        unit = h["unit"]
        entry = h["entry_no"]
        current = h["current_no"]
        pnl_pct = h["pnl_pct"]
        size = h["size"]
        wc_high = h.get("wc_high")
        om_high = h.get("om_high")
        metar = h.get("metar_temp")
        wc_cur = h.get("wc_current")
        delta = h.get("metar_vs_wc")

        header = f"\U0001f4a1 <b>{city}</b> | {bucket_str} | {date_str}\n"
        details = (
            f"Entry: ${entry:.3f} | Now: ${current:.3f}\n"
            f"P&amp;L: {pnl_pct:+.1f}% | Size: {size:.0f}\n"
            f"METAR: {metar}\u00b0{unit} | WC Now: {wc_cur}\u00b0{unit}\n"
            f"WC High: {wc_high}\u00b0{unit} | OM High: {om_high}\u00b0C\n"
            f"<a href=\"{link}\">Polymarket</a>"
        )

        def _send(tag: str, msg: str):
            if state.get(f"alerted_{tag}"):
                return
            state[f"alerted_{tag}"] = datetime.now(timezone.utc).isoformat()
            send_telegram(msg)

        if bucket_threshold is not None and wc_high is not None:
            prev_wc = state.get("last_wc_high")
            if prev_wc is not None and prev_wc != wc_high:
                old_dist = abs(prev_wc - bucket_threshold)
                new_dist = abs(wc_high - bucket_threshold)
                if new_dist < old_dist and old_dist - new_dist >= 1.0:
                    _send("wc_forecast",
                          f"\u26a0\ufe0f <b>WC forecast changed toward bucket</b>\n"
                          f"{prev_wc}\u00b0{unit} \u2192 {wc_high}\u00b0{unit} "
                          f"({old_dist - new_dist:.1f}\u00b0 closer to {bucket_str})\n\n{details}")
            state["last_wc_high"] = wc_high

        if bucket_threshold is not None and om_high is not None:
            prev_om = state.get("last_om_high")
            if prev_om is not None and prev_om != om_high:
                old_dist = abs(prev_om - bucket_threshold)
                new_dist = abs(om_high - bucket_threshold)
                if new_dist < old_dist and old_dist - new_dist >= 1.0:
                    _send("om_forecast",
                          f"\u26a0\ufe0f <b>OM forecast changed toward bucket</b>\n"
                          f"{prev_om}\u00b0C \u2192 {om_high}\u00b0C "
                          f"({old_dist - new_dist:.1f}\u00b0 closer to {bucket_str})\n\n{details}")
            state["last_om_high"] = om_high

        if current >= 0.98:
            _send("payout_98",
                  f"\U0001f4b0 <b>Payout reached 98c!</b>\n"
                  f"Current NO: ${current:.3f} \u2014 consider selling\n\n{details}")

        peak = state.get("peak_pnl_pct", pnl_pct)
        if pnl_pct > peak:
            state["peak_pnl_pct"] = pnl_pct
            peak = pnl_pct
        tiers = state.setdefault("profit_tier_alerted", [])
        if peak >= 20 and pnl_pct <= 10 and 20 not in tiers:
            tiers.append(20)
            _send("profit_drop_20_10",
                  f"\U0001f4c9 <b>Profit dropped: {peak:+.1f}% \u2192 {pnl_pct:+.1f}%</b>\n"
                  f"Recommended sell \u2014 profit halved\n\n{details}")
        if peak >= 10 and pnl_pct <= 0 and 10 not in tiers:
            tiers.append(10)
            _send("profit_drop_10_0",
                  f"\U0001f4c9 <b>Profit dropped: {peak:+.1f}% \u2192 {pnl_pct:+.1f}%</b>\n"
                  f"Position now at break-even or loss\n\n{details}")

        if pnl_pct <= -15:
            _send("loss_15",
                  f"\U0001f534 <b>Position at {pnl_pct:+.1f}% loss</b>\n"
                  f"Stop-loss recommended\n\n{details}")

        if delta is not None and abs(delta) > 1.0 and not state.get("alerted_delta"):
            state["alerted_delta"] = datetime.now(timezone.utc).isoformat()
            send_telegram(
                f"\u26a1 <b>Delta {'>'} 1\u00b0: METAR vs WC mismatch</b>\n"
                f"METAR: {metar}\u00b0{unit} | WC: {wc_cur}\u00b0{unit} | Delta: {delta:+.1f}\u00b0\n\n{details}"
            )

        if bucket_threshold is not None:
            for label, high in [("WC", wc_high), ("OM", om_high)]:
                if high is None:
                    continue
                dist = bucket_threshold - high
                alert_key = f"close_{label.lower()}"
                if 0 <= dist < 1.5 and not state.get(alert_key):
                    state[alert_key] = datetime.now(timezone.utc).isoformat()
                    send_telegram(
                        f"\U0001f525 <b>Only {dist:.1f}\u00b0 gap to bucket!</b>\n"
                        f"{label} high: {high}\u00b0{unit} vs bucket {bucket_str}\n\n{details}"
                    )


# ── Data Aggregation ─────────────────────────────────────────────────────────

def _build_city_name_map() -> dict:
    return {slug: s["name"] for slug, s in _stations.items()}


def refresh_holdings():
    global _holdings, _last_refresh
    with _refresh_lock:
        logger.info("Refreshing holdings...")

        raw_positions = fetch_positions()
        logger.info("Fetched %d raw positions", len(raw_positions))

        no_temp_positions = [
            pos for pos in raw_positions
            if (pos.get("outcome") or "").lower() == "no"
            and "highest temperature" in (pos.get("title") or "").lower()
        ]
        logger.info("Found %d NO temperature positions", len(no_temp_positions))

        slug_map = _build_city_name_map()
        results = []

        for pos in no_temp_positions:
            title = pos.get("title", "")
            market_id = pos.get("market", "") or pos.get("conditionId", "")
            city_slug, date_str = parse_title(title)

            if not city_slug or not date_str:
                if market_id:
                    market_data = fetch_market(market_id)
                    if market_data:
                        event_slug = market_data.get("groupSlug", "")
                        if event_slug:
                            cs2, ds2 = parse_slug(event_slug)
                            if cs2:
                                city_slug = cs2
                            if ds2:
                                date_str = ds2

            live_no = get_live_no_price(market_id)

            if not city_slug or not date_str:
                entry_no = float(pos.get("avgPrice", 0))
                current_no = live_no if live_no is not None else float(pos.get("curPrice", 0))
                pnl_pct = round((current_no / entry_no - 1.0) * 100, 1) if entry_no > 0 else 0.0
                results.append({
                    "city": "Unknown", "city_slug": city_slug or "?", "date": date_str or "?",
                    "bucket": parse_bucket(title), "entry_no": entry_no, "current_no": current_no,
                    "size": float(pos.get("size", 0)),
                    "pnl": round((current_no - entry_no) * float(pos.get("size", 0)), 4),
                    "pnl_pct": pnl_pct, "wc_high": None, "om_high": None,
                    "metar_temp": None, "wc_current": None, "metar_vs_wc": None,
                    "unit": "?", "icao": None, "error": "Could not map city/date",
                })
                continue

            station = _stations.get(city_slug, {})
            city_name = station.get("name") or slug_map.get(city_slug, city_slug)
            unit = station.get("unit", "F")
            lat = station.get("lat")
            lon = station.get("lon")
            tz_name = station.get("timezone", "UTC")
            icao = station.get("icao")

            wc_high = om_high = metar_temp = wc_current = None
            if lat is not None and lon is not None:
                twc_unit = "e" if unit == "F" else "m"
                wc_high = get_tw_daily_high(lat, lon, date_str, unit=twc_unit)
                om_high = get_om_daily_high(lat, lon, date_str, unit="celsius", tz=tz_name)
                wc_current = get_tw_current_temp(lat, lon, unit=twc_unit)

            if icao:
                metar_data = get_metar_temp(icao)
                if metar_data:
                    metar_temp = metar_data["temp_c"] if unit == "C" else metar_data["temp_f"]

            metar_vs_wc = round(metar_temp - wc_current, 1) if metar_temp is not None and wc_current is not None else None

            entry_no = float(pos.get("avgPrice", 0))
            current_no = live_no if live_no is not None else float(pos.get("curPrice", 0))
            pnl_pct = round((current_no / entry_no - 1.0) * 100, 1) if entry_no > 0 else 0.0

            results.append({
                "city": city_name, "city_slug": city_slug, "date": date_str,
                "bucket": parse_bucket(title), "entry_no": entry_no, "current_no": current_no,
                "size": float(pos.get("size", 0)),
                "pnl": round((current_no - entry_no) * float(pos.get("size", 0)), 4),
                "pnl_pct": pnl_pct, "wc_high": wc_high, "om_high": om_high,
                "metar_temp": metar_temp, "wc_current": wc_current, "metar_vs_wc": metar_vs_wc,
                "unit": unit, "icao": icao, "error": None,
            })

        results.sort(key=lambda r: (r["date"], r["city"]))
        _holdings = results
        _last_refresh = time.time()
        logger.info("Refreshed %d holdings", len(results))

        _check_alerts(results)
        _save_alert_state()


# ── Dashboard HTML ───────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Temperature Holdings</title>
<style>
:root {
  --bg: #0a0e17; --surface: #111827; --surface2: #1a2234; --border: #1e2d45;
  --text: #e2e8f0; --text-dim: #64748b;
  --green: #22c55e; --red: #ef4444; --yellow: #eab308;
  --cyan: #06b6d4; --purple: #a78bfa; --orange: #f97316;
  --font: 'Segoe UI', system-ui, -apple-system, sans-serif;
  --mono: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: var(--font); }
.container { max-width: 1200px; margin: 0 auto; padding: 12px 14px; }
.header {
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 8px;
  padding: 10px 0; margin-bottom: 12px; border-bottom: 1px solid var(--border);
}
.header h1 { font-size: 1rem; font-weight: 700; white-space: nowrap; }
.header h1 span { color: var(--cyan); }
.meta { font-size: 0.72rem; color: var(--text-dim); display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.wallet { font-family: var(--mono); font-size: 0.65rem; color: var(--purple); background: var(--surface2);
  padding: 2px 8px; border-radius: 5px; }
.refresh-btn {
  background: var(--surface2); border: 1px solid var(--border); color: var(--cyan);
  padding: 4px 12px; border-radius: 5px; cursor: pointer; font-size: 0.72rem; font-weight: 600;
}
.refresh-btn:hover { background: var(--border); }
.refresh-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.countdown { font-family: var(--mono); font-size: 0.7rem; color: var(--cyan);
  background: var(--surface2); padding: 2px 8px; border-radius: 5px;
  border: 1px solid var(--border); text-align: center; }
.summary { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.summary-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; min-width: 100px; flex: 1;
}
.summary-card .label { font-size: 0.62rem; color: var(--text-dim); text-transform: uppercase;
  letter-spacing: 0.6px; margin-bottom: 3px; }
.summary-card .value { font-size: 1.05rem; font-weight: 700; font-family: var(--mono); }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px; transition: border-color 0.15s;
}
.card:hover { border-color: var(--cyan); }
.card-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
.card-city { font-weight: 700; font-size: 0.95rem; }
.card-icao { font-size: 0.6rem; color: var(--text-dim); background: var(--surface2);
  padding: 1px 5px; border-radius: 3px; font-family: var(--mono); margin-left: 5px; }
.card-bucket { font-family: var(--mono); font-weight: 700; color: var(--yellow); font-size: 1rem; }
.card-date { font-size: 0.7rem; color: var(--text-dim); font-family: var(--mono); margin-top: 2px; }
.card-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 0; border-top: 1px solid rgba(30,45,69,0.5);
  font-size: 0.78rem; gap: 8px;
}
.card-row-label { color: var(--text-dim); font-size: 0.68rem; text-transform: uppercase;
  letter-spacing: 0.4px; min-width: 60px; }
.card-row-val { font-family: var(--mono); font-weight: 600; text-align: right; }
.p-pos { color: var(--green); } .p-neg { color: var(--red); } .p-zero { color: var(--text-dim); }
.t-metar { color: var(--green); } .t-wc { color: var(--orange); } .t-om { color: var(--purple); }
.d-pos { color: var(--green); font-weight: 700; }
.d-neg { color: var(--red); font-weight: 700; }
.d-zero { color: var(--text-dim); }
.card-link { display: block; text-align: center; margin-top: 10px; padding-top: 8px;
  border-top: 1px solid rgba(30,45,69,0.5); }
.card-link a { color: var(--cyan); font-size: 0.72rem; text-decoration: none; font-weight: 600; }
.card-link a:hover { text-decoration: underline; }
.no-data { color: var(--text-dim); }
.loading { text-align: center; padding: 50px 20px; color: var(--text-dim); }
.loading .spinner {
  display: inline-block; width: 24px; height: 24px;
  border: 3px solid var(--border); border-top-color: var(--cyan);
  border-radius: 50%; animation: spin 0.8s linear infinite; margin-bottom: 10px;
}
@keyframes spin { to { transform: rotate(360deg); } }
.empty { text-align: center; padding: 40px; color: var(--text-dim); }
.empty h3 { margin-bottom: 6px; color: var(--text); }
.section-title {
  font-size: 0.82rem; font-weight: 700; color: var(--green); margin: 18px 0 10px 0;
  padding-bottom: 6px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px;
}
.section-title .badge {
  background: var(--green); color: var(--bg); font-size: 0.62rem; font-weight: 700;
  padding: 1px 7px; border-radius: 10px;
}
.rec-card {
  background: var(--surface); border: 1px solid rgba(34,197,94,0.25); border-radius: 10px;
  padding: 14px; transition: border-color 0.15s;
}
.rec-card:hover { border-color: var(--green); }
.rec-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
.rec-city { font-weight: 700; font-size: 0.95rem; }
.rec-region { font-size: 0.58rem; color: var(--text-dim); background: var(--surface2);
  padding: 1px 5px; border-radius: 3px; font-family: var(--mono); margin-left: 5px; text-transform: uppercase; }
.rec-bucket { font-family: var(--mono); font-weight: 700; color: var(--green); font-size: 1rem; }
.rec-date { font-size: 0.7rem; color: var(--text-dim); font-family: var(--mono); margin-top: 2px; }
.rec-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 5px 0; border-top: 1px solid rgba(30,45,69,0.5);
  font-size: 0.76rem; gap: 8px;
}
.rec-row-label { color: var(--text-dim); font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.4px; min-width: 55px; }
.rec-row-val { font-family: var(--mono); font-weight: 600; text-align: right; }
.rec-price { color: var(--green); font-weight: 700; }
.rec-dist { font-weight: 700; }
.rec-dist-close { color: var(--green); }
.rec-dist-ok { color: var(--yellow); }
.rec-link { display: block; text-align: center; margin-top: 8px; padding-top: 6px;
  border-top: 1px solid rgba(30,45,69,0.5); }
.rec-link a { color: var(--green); font-size: 0.72rem; text-decoration: none; font-weight: 600; }
.rec-link a:hover { text-decoration: underline; }
@media (max-width: 600px) {
  .cards, .recs { grid-template-columns: 1fr; }
  .header { flex-direction: column; align-items: flex-start; }
  .summary { gap: 6px; }
  .summary-card { min-width: 0; }
}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Polymarket <span>Temperature NO</span></h1>
    <div class="meta">
      <span class="wallet">0x5184...9211</span>
      <span id="status">Loading...</span>
      <span class="countdown" id="countdown">--:--</span>
      <button class="refresh-btn" id="refreshBtn" onclick="refresh()">Refresh</button>
    </div>
  </div>
  <div class="summary" id="summary"></div>
  <div id="recs"></div>
  <div id="content"><div class="loading"><div class="spinner"></div><div>Loading...</div></div></div>
</div>
<script>
let refreshing = false;
let lastRefreshTs = 0;
const REFRESH_SEC = 30;

function updateCountdown() {
  if (!lastRefreshTs) return;
  const rem = Math.max(0, REFRESH_SEC - ((Date.now()/1000) - lastRefreshTs));
  document.getElementById('countdown').textContent =
    rem <= 0 ? 'Refreshing...' : `${Math.floor(rem/60)}:${String(Math.floor(rem%60)).padStart(2,'0')}`;
}

async function loadData() {
  try {
    const [dataR, recR] = await Promise.all([fetch('/api/data'), fetch('/api/recommendations')]);
    const d = await dataR.json();
    const recs = await recR.json();
    lastRefreshTs = d.last_refresh || (Date.now()/1000);
    render(d);
    renderRecs(recs);
  } catch(e) {
    document.getElementById('content').innerHTML = '<div class="empty"><h3>Error</h3><p>'+e.message+'</p></div>';
  }
}

function render(data) {
  const h = data.holdings || [];
  document.getElementById('status').textContent =
    'Updated: ' + (data.last_refresh ? new Date(data.last_refresh*1000).toLocaleTimeString() : 'Never');

  const totSz = h.reduce((s,x) => s+x.size, 0);
  const totPnl = h.reduce((s,x) => s+x.pnl, 0);
  const avgE = h.length ? h.reduce((s,x) => s+x.entry_no, 0)/h.length : 0;
  const avgC = h.length ? h.reduce((s,x) => s+x.current_no, 0)/h.length : 0;

  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><div class="label">Positions</div><div class="value">${h.length}</div></div>
    <div class="summary-card"><div class="label">Shares</div><div class="value">${totSz.toFixed(0)}</div></div>
    <div class="summary-card"><div class="label">Avg Entry</div><div class="value" style="color:var(--text-dim)">$${avgE.toFixed(3)}</div></div>
    <div class="summary-card"><div class="label">Avg Current</div><div class="value" style="color:var(--cyan)">$${avgC.toFixed(3)}</div></div>
    <div class="summary-card"><div class="label">Total P&L</div><div class="value ${totPnl>=0?'p-pos':'p-neg'}">${totPnl>=0?'+':''}$${totPnl.toFixed(2)}</div></div>`;

  if (!h.length) {
    document.getElementById('content').innerHTML = '<div class="empty"><h3>No NO temperature positions</h3></div>';
    return;
  }

  let html = '<div class="cards">';
  for (const p of h) {
    const pc = p.pnl>0.001?'p-pos':(p.pnl<-0.001?'p-neg':'p-zero');
    const ps = p.pnl>=0?'+':'';
    const nd = '<span class="no-data">\u2014</span>';
    const metar = p.metar_temp!==null ? p.metar_temp.toFixed(1)+'\u00b0'+p.unit : nd;
    const wcCur = p.wc_current!==null ? p.wc_current.toFixed(1)+'\u00b0'+p.unit : nd;
    const wcHi = p.wc_high!==null ? p.wc_high.toFixed(1)+'\u00b0'+p.unit : nd;
    const omHi = p.om_high!==null ? p.om_high.toFixed(1)+'\u00b0C' : nd;
    let delta = nd;
    if (p.metar_vs_wc!==null) {
      const d=p.metar_vs_wc, s=d>0.05?'+':(d<-0.05?'':'\u00b1');
      const c=d>0.05?'d-pos':(d<-0.05?'d-neg':'d-zero');
      delta='<span class="'+c+'">'+s+d.toFixed(1)+'\u00b0</span>';
    }
    const icao = p.icao ? '<span class="card-icao">'+p.icao+'</span>' : '';
    const link = 'https://polymarket.com/event/highest-temperature-in-'+p.city_slug+'-on-'+
      ['january','february','march','april','may','june','july','august','september','october','november','december']
      [parseInt(p.date.split('-')[1])-1]+'-'+parseInt(p.date.split('-')[2])+'-'+p.date.split('-')[0];

    html += `<div class="card">
      <div class="card-top">
        <div><span class="card-city">${p.city}${icao}</span><div class="card-date">${p.date}</div></div>
        <div class="card-bucket">${p.bucket}</div>
      </div>
      <div class="card-row"><span class="card-row-label">Entry</span><span class="card-row-val" style="color:var(--text-dim)">$${p.entry_no.toFixed(3)}</span></div>
      <div class="card-row"><span class="card-row-label">Current</span><span class="card-row-val" style="color:var(--cyan)">$${p.current_no.toFixed(3)}</span></div>
      <div class="card-row"><span class="card-row-label">P&L</span><span class="card-row-val ${pc}">${ps}${p.pnl.toFixed(2)} (${ps}${p.pnl_pct.toFixed(1)}%)</span></div>
      <div class="card-row"><span class="card-row-label">METAR</span><span class="card-row-val t-metar">${metar}</span></div>
      <div class="card-row"><span class="card-row-label">WC Now</span><span class="card-row-val t-wc">${wcCur}</span></div>
      <div class="card-row"><span class="card-row-label">Delta</span><span class="card-row-val">${delta}</span></div>
      <div class="card-row"><span class="card-row-label">WC High</span><span class="card-row-val t-wc">${wcHi}</span></div>
      <div class="card-row"><span class="card-row-label">OM High</span><span class="card-row-val t-om">${omHi}</span></div>
      <div class="card-link"><a href="${link}" target="_blank">View on Polymarket \u2197</a></div>
    </div>`;
  }
  html += '</div>';
  document.getElementById('content').innerHTML = html;
}

function renderRecs(recs) {
  const list = recs.recommendations || [];
  const region = recs.region || '?';
  const target = recs.target_date || '?';
  const nd = '<span class="no-data">\u2014</span>';
  if (!list.length) {
    document.getElementById('recs').innerHTML = '<div class="section-title">Buy Recommendations (' + region + ' | ' + target + ')</div><div class="empty" style="padding:15px"><h3>No qualifying recommendations right now</h3></div>';
    return;
  }
  let html = '<div class="section-title">Buy Recommendations <span class="badge">' + list.length + '</span> <span style="font-weight:400;font-size:0.68rem;color:var(--text-dim)">' + region + ' | ' + target + '</span></div>';
  html += '<div class="cards recs">';
  for (const r of list) {
    const unitChar = (r.city_slug && r.city_slug.includes('paris')) ? 'C' : (r.region === 'americas' ? 'F' : 'C');
    let bLabel;
    if (r.bucket_low === -999) bLabel = '\u2264' + Math.round(r.bucket_high) + '\u00b0' + unitChar;
    else if (r.bucket_high === 999) bLabel = '\u2265' + Math.round(r.bucket_low) + '\u00b0' + unitChar;
    else if (r.bucket_low === r.bucket_high) bLabel = Math.round(r.bucket_low) + '\u00b0' + unitChar;
    else bLabel = Math.round(r.bucket_low) + '-' + Math.round(r.bucket_high) + '\u00b0' + unitChar;
    const dClass = r.distance <= 1.0 ? 'rec-dist-close' : 'rec-dist-ok';
    const omHi = r.om_high !== null ? r.om_high.toFixed(1) + '\u00b0C' : nd;
    html += '<div class="rec-card">' +
      '<div class="rec-top"><div><span class="rec-city">' + r.city + '<span class="rec-region">' + r.region + '</span></span>' +
      '<div class="rec-date">' + r.date + '</div></div>' +
      '<div class="rec-bucket">' + bLabel + '</div></div>' +
      '<div class="rec-row"><span class="rec-row-label">NO Price</span><span class="rec-row-val rec-price">$' + r.no_price.toFixed(3) + '</span></div>' +
      '<div class="rec-row"><span class="rec-row-label">Distance</span><span class="rec-row-val rec-dist ' + dClass + '">' + r.distance.toFixed(1) + '\u00b0</span></div>' +
      '<div class="rec-row"><span class="rec-row-label">WC High</span><span class="rec-row-val t-wc">' + r.wc_high.toFixed(1) + '\u00b0</span></div>' +
      '<div class="rec-row"><span class="rec-row-label">OM High</span><span class="rec-row-val t-om">' + omHi + '</span></div>' +
      '<div class="rec-row"><span class="rec-row-label">Volume</span><span class="rec-row-val">' + Math.round(r.volume) + '</span></div>' +
      '<div class="rec-link"><a href="' + r.link + '" target="_blank">View on Polymarket \u2197</a></div></div>';
  }
  html += '</div>';
  document.getElementById('recs').innerHTML = html;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  document.getElementById('refreshBtn').disabled = true;
  document.getElementById('status').textContent = 'Refreshing...';
  try { await fetch('/api/refresh',{method:'POST'}); await loadData(); }
  finally { refreshing = false; document.getElementById('refreshBtn').disabled = false; }
}
loadData();
setInterval(updateCountdown, 1000);
setInterval(() => { if(!refreshing) loadData(); }, REFRESH_SEC*1000);
</script>
</body>
</html>"""


# ── HTTP Server ──────────────────────────────────────────────────────────────

class TrackerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path in ("", "/"):
                self._serve_html()
            elif path == "/api/data":
                self._json_response({
                    "holdings": _holdings,
                    "last_refresh": _last_refresh,
                    "wallet": WALLET,
                    "station_count": len(_stations),
                    "telegram": TG_ENABLED,
                })
            elif path == "/api/health":
                self._json_response({"ok": True, "holdings": len(_holdings)})
            elif path == "/api/recommendations":
                self._json_response({
                    "recommendations": _recommendations,
                    "last_refresh": _last_rec_refresh,
                    "target_date": get_target_date(),
                    "region": ", ".join(sorted(get_allowed_regions())),
                })
            else:
                self.send_error(404)
        except Exception as e:
            logger.error("Handler error: %s", e)
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/refresh":
            threading.Thread(target=refresh_holdings, daemon=True).start()
            self._json_response({"ok": True})
        else:
            self.send_error(404)

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        body = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def _auto_refresh_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            refresh_holdings()
        except Exception as e:
            logger.error("Auto-refresh error: %s", e)


def main():
    _load_stations()
    _load_alert_state()
    _load_rec_state()

    logger.info("Fetching initial holdings + recommendations...")
    threading.Thread(target=refresh_holdings, daemon=True).start()
    threading.Thread(target=refresh_recommendations, daemon=True).start()
    threading.Thread(target=_auto_refresh_loop, daemon=True, name="auto-refresh").start()
    threading.Thread(target=_rec_scan_loop, daemon=True, name="rec-scan").start()

    server = HTTPServer(("0.0.0.0", PORT), TrackerHandler)
    tg_status = f"Telegram: {'ON' if TG_ENABLED else 'OFF (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)'}"
    print(f"\n{'='*60}")
    print(f"  POLYMARKET TEMPERATURE HOLDINGS TRACKER")
    print(f"  Dashboard:   http://localhost:{PORT}")
    print(f"  Wallet:      {WALLET[:6]}...{WALLET[-4:]}")
    print(f"  Stations:    {len(_stations)} loaded")
    print(f"  Refresh:     every {REFRESH_INTERVAL}s (holdings) / {REC_REFRESH_INTERVAL}s (recs)")
    print(f"  Region:      {', '.join(sorted(get_allowed_regions()))} | Target: {get_target_date()}")
    print(f"  {tg_status}")
    print(f"{'='*60}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--port" and i + 1 < len(sys.argv) - 1:
            PORT = int(sys.argv[i + 2])
    main()
