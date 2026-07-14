"""
Polymarket Gamma API client — read-only market data for temperature markets.
No auth required.
"""

import json
import re
import time
import logging
from typing import Optional

import requests

from config import REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

_event_cache: dict[str, tuple[dict, float]] = {}
_EVENT_CACHE_TTL = 300

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


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

    url = f"{GAMMA_BASE}/events"
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
            logger.error("Polymarket event error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    return None


def get_market_price(market_id: str) -> Optional[dict]:
    url = f"{GAMMA_BASE}/markets/{market_id}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            mdata = resp.json()
            prices_raw = mdata.get("outcomePrices", "[0.5,0.5]")
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 0.5
            return {
                "yes_price": yes_price,
                "no_price": no_price,
                "volume": float(mdata.get("volume", 0)),
                "closed": mdata.get("closed", False),
                "active": mdata.get("active", True),
            }
        except requests.RequestException as e:
            logger.error("Polymarket market error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
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
            "neg_risk": market.get("negRisk", False),
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
