"""
Strategy logic for the NO paper trading bot.

Entry: Buy NO when weather.com forecast high is 2-4°F from a bucket threshold,
       Open-Meteo agrees, and the NO price is attractive.

Monitoring: Poll weather.com (10 min) and METAR (45s) to detect temperature shifts.
            Exit based on immediate triggers and trend-based state machine.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from config import (
    DISTANCE_MIN, DISTANCE_MAX, MIN_NO_PRICE, MAX_NO_PRICE, MIN_VOLUME,
    STOP_LOSS_PCT, PROXIMITY_THRESHOLD, METAR_CLOSE_READINGS,
    FORECAST_DRIFT_THRESHOLD, OM_MIN_DISTANCE,
)
from data_sources import weather_com, open_meteo, aviation_weather, polymarket

logger = logging.getLogger(__name__)


# ── Entry Logic ───────────────────────────────────────────────────────────

def _compute_threshold(wc_high: float, t_low: float, t_high: float) -> float:
    if t_low == -999:
        return t_high
    if t_high == 999:
        return t_low
    return t_high if wc_high > (t_low + t_high) / 2 else t_low


def evaluate_entry(city_slug: str, station: dict, date_str: str) -> Optional[dict]:
    unit = station["unit"]
    lat, lon = station["lat"], station["lon"]

    temp_unit_wc = "e" if unit == "F" else "m"
    temp_unit_om = "fahrenheit" if unit == "F" else "celsius"

    wc_high = weather_com.get_daily_high(lat, lon, date_str, temp_unit_wc)
    if wc_high is None:
        return None

    buckets = polymarket.get_city_buckets(city_slug, date_str)
    if not buckets:
        return None

    best_signal = None
    best_distance = 0.0

    for bucket in buckets:
        if bucket["closed"] or not bucket["active"]:
            continue

        t_low, t_high = bucket["range"]
        threshold = _compute_threshold(wc_high, t_low, t_high)
        distance = abs(wc_high - threshold)

        if distance < DISTANCE_MIN or distance > DISTANCE_MAX:
            continue
        if bucket["yes_price"] >= MIN_NO_PRICE:
            continue
        if bucket["no_price"] > MAX_NO_PRICE:
            continue
        if bucket["volume"] < MIN_VOLUME:
            continue

        om_result = open_meteo.get_forecast_direction(
            lat, lon, date_str, threshold, temp_unit_om, station["timezone"]
        )
        if om_result is None:
            continue
        if om_result["distance"] < OM_MIN_DISTANCE:
            continue

        if distance > best_distance:
            best_distance = distance
            best_signal = {
                "city_slug": city_slug,
                "date": date_str,
                "market_id": bucket["market_id"],
                "token_id": bucket.get("token_id", ""),
                "question": bucket["question"],
                "bucket_range": (t_low, t_high),
                "threshold": threshold,
                "yes_price": bucket["yes_price"],
                "no_price": bucket["no_price"],
                "volume": bucket["volume"],
                "wc_high": wc_high,
                "om_high": om_result["high"],
                "distance": distance,
            }

    return best_signal


def evaluate_bucket_signals(city_slug: str, station: dict, date_str: str) -> list[dict]:
    unit = station["unit"]
    lat, lon = station["lat"], station["lon"]

    temp_unit_wc = "e" if unit == "F" else "m"
    temp_unit_om = "fahrenheit" if unit == "F" else "celsius"

    wc_high = weather_com.get_daily_high(lat, lon, date_str, temp_unit_wc)
    if wc_high is None:
        return []

    buckets = polymarket.get_city_buckets(city_slug, date_str)
    if not buckets:
        return []

    signals = []
    for bucket in buckets:
        if bucket["closed"] or not bucket["active"]:
            continue

        t_low, t_high = bucket["range"]
        if t_low == 0.0 and t_high == 0.0:
            continue

        threshold = _compute_threshold(wc_high, t_low, t_high)
        distance = abs(wc_high - threshold)

        failed = []
        if distance < DISTANCE_MIN or distance > DISTANCE_MAX:
            failed.append("distance")
        if bucket["yes_price"] >= MIN_NO_PRICE:
            failed.append("yes_price")
        if bucket["no_price"] > MAX_NO_PRICE:
            failed.append("no_price")
        if bucket["volume"] < MIN_VOLUME:
            failed.append("volume")

        if not failed:
            om_result = open_meteo.get_forecast_direction(
                lat, lon, date_str, threshold, temp_unit_om, station["timezone"]
            )
            if om_result is None:
                failed.append("open_meteo_unavailable")
            elif om_result["distance"] < OM_MIN_DISTANCE:
                failed.append("open_meteo_disagree")

        if failed:
            signals.append({
                "city_slug": city_slug,
                "date": date_str,
                "question": bucket["question"],
                "bucket_range": (t_low, t_high),
                "threshold": threshold,
                "yes_price": bucket["yes_price"],
                "no_price": bucket["no_price"],
                "volume": bucket["volume"],
                "wc_high": wc_high,
                "distance": distance,
                "failed_filters": failed,
            })

    return signals


def evaluate_all_buckets_detail(city_slug: str, station: dict, date_str: str) -> dict:
    """Evaluate every active bucket and return full detail for logging."""
    unit = station["unit"]
    lat, lon = station["lat"], station["lon"]

    temp_unit_wc = "e" if unit == "F" else "m"
    temp_unit_om = "fahrenheit" if unit == "F" else "celsius"

    wc_high = weather_com.get_daily_high(lat, lon, date_str, temp_unit_wc)
    if wc_high is None:
        return {"wc_high": None, "om_high": None, "buckets": []}

    buckets = polymarket.get_city_buckets(city_slug, date_str)
    if not buckets:
        return {"wc_high": wc_high, "om_high": None, "buckets": []}

    result_buckets = []
    om_high = None

    for bucket in buckets:
        if bucket["closed"] or not bucket["active"]:
            continue

        t_low, t_high = bucket["range"]
        if t_low == 0.0 and t_high == 0.0:
            continue

        threshold = _compute_threshold(wc_high, t_low, t_high)
        distance = abs(wc_high - threshold)

        failed = []
        if distance < DISTANCE_MIN or distance > DISTANCE_MAX:
            failed.append("distance")
        if bucket["yes_price"] >= MIN_NO_PRICE:
            failed.append("yes_price")
        if bucket["no_price"] > MAX_NO_PRICE:
            failed.append("no_price")
        if bucket["volume"] < MIN_VOLUME:
            failed.append("volume")

        om_distance = None
        om_val = None
        if not failed:
            om_result = open_meteo.get_forecast_direction(
                lat, lon, date_str, threshold, temp_unit_om, station["timezone"]
            )
            if om_result is None:
                failed.append("open_meteo_unavailable")
            elif om_result["distance"] < OM_MIN_DISTANCE:
                failed.append("open_meteo_disagree")
            else:
                om_val = om_result["high"]
                om_distance = om_result["distance"]
                if om_high is None:
                    om_high = om_val

        result_buckets.append({
            "range": [t_low, t_high],
            "threshold": round(threshold, 1),
            "distance": round(distance, 1),
            "yes_price": bucket["yes_price"],
            "no_price": bucket["no_price"],
            "volume": bucket["volume"],
            "question": bucket.get("question", ""),
            "market_id": bucket.get("market_id", ""),
            "om_distance": round(om_distance, 1) if om_distance is not None else None,
            "failed_filters": failed,
            "passed": len(failed) == 0,
        })

    result_buckets.sort(key=lambda b: b["distance"], reverse=True)

    return {"wc_high": wc_high, "om_high": om_high, "buckets": result_buckets}


# ── Monitoring State Machine ──────────────────────────────────────────────

def monitor_position(city_slug: str, station: dict, date_str: str,
                     position: dict,
                     current_no_price: Optional[float] = None) -> Optional[str]:
    """
    Monitor an open position and decide action.

    Returns: "hold", "sell", "tighten", "wait_for_stop", or "forecast_drift"
    """
    unit = station["unit"]
    lat, lon = station["lat"], station["lon"]
    icao = station["icao"]
    temp_unit_wc = "e" if unit == "F" else "m"

    # 1. Fetch weather.com daily high (re-fetched every cycle)
    wc_high = weather_com.get_daily_high(lat, lon, date_str, temp_unit_wc)

    # 1b. Forecast drift check
    prev_wc_high = position.get("last_wc_high")
    if prev_wc_high is not None and wc_high is not None and prev_wc_high != wc_high:
        shift = wc_high - prev_wc_high
        bucket_low = position["bucket_low"]
        bucket_high = position["bucket_high"]

        toward_bucket = False
        if bucket_high == 999:
            toward_bucket = shift > 0
        elif bucket_low == -999:
            toward_bucket = shift < 0
        else:
            bucket_mid = (bucket_low + bucket_high) / 2
            old_dist = abs(prev_wc_high - bucket_mid)
            new_dist = abs(wc_high - bucket_mid)
            toward_bucket = new_dist < old_dist

        if toward_bucket and abs(shift) >= FORECAST_DRIFT_THRESHOLD:
            entry_no = position.get("entry_no_price", 0)
            cur_no = current_no_price if current_no_price and current_no_price > 0 else entry_no
            pnl_pct = ((cur_no / entry_no - 1.0) * 100) if entry_no > 0 else 0

            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "wc_high": wc_high,
                "prev_wc_high": prev_wc_high,
                "shift": round(shift, 1),
                "metar_temp": None,
                "distance_to_threshold": None,
                "metar_to_threshold": None,
                "closing_in": False,
                "in_profit": pnl_pct >= 0,
                "pnl_pct": round(pnl_pct, 1),
                "action": "forecast_drift",
                "reason": f"wc_high {prev_wc_high:.1f} -> {wc_high:.1f} ({shift:+.1f})",
            }
            position.setdefault("monitoring_events", []).append(event)
            return "forecast_drift"

    # 2. Get weather.com current-hour forecast
    wc_current = weather_com.get_current_hour_temp(lat, lon, temp_unit_wc)
    if wc_current is None:
        return "hold"

    # 3. Get METAR
    metar = aviation_weather.get_current_temp(icao)
    if metar is None:
        return "hold"

    metar_temp = metar["temp_f"] if unit == "F" else metar["temp_c"]
    metar_temp_c = metar["temp_c"]

    position["last_metar_temp"] = metar_temp
    position["last_wc_current"] = wc_current
    position["last_wc_high"] = wc_high

    # 4. Compute distances
    bucket_low = position["bucket_low"]
    bucket_high = position["bucket_high"]
    if bucket_low == -999:
        bucket_mid = bucket_high
    elif bucket_high == 999:
        bucket_mid = bucket_low
    else:
        bucket_mid = (bucket_low + bucket_high) / 2

    wc_distance = abs(wc_high - bucket_mid) if wc_high is not None else None
    metar_distance = abs(metar_temp - bucket_mid)

    # 5. Immediate sell: NO @ $0.99
    if current_no_price is not None and current_no_price >= 0.99:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wc_high": wc_high, "wc_current": wc_current,
            "metar_temp": metar_temp, "metar_temp_c": metar_temp_c,
            "wc_distance": wc_distance, "metar_distance": metar_distance,
            "closing_in": False, "in_profit": True, "pnl_pct": 0,
            "action": "sell_take_profit", "reason": "no_at_99",
        }
        position.setdefault("monitoring_events", []).append(event)
        return "sell"

    # 6. Immediate sell: daily high conflict
    if wc_high is not None:
        conflict = False
        if bucket_high == 999:
            conflict = wc_high >= bucket_low
        elif bucket_low == -999:
            conflict = wc_high <= bucket_high
        else:
            conflict = wc_high >= bucket_low and wc_high <= bucket_high

        if conflict:
            entry_no = position.get("entry_no_price", 0)
            cur_no = current_no_price if current_no_price and current_no_price > 0 else entry_no
            pnl_pct = ((cur_no / entry_no - 1.0) * 100) if entry_no > 0 else 0
            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "wc_high": wc_high, "wc_current": wc_current,
                "metar_temp": metar_temp, "metar_temp_c": metar_temp_c,
                "wc_distance": wc_distance, "metar_distance": metar_distance,
                "closing_in": False, "in_profit": pnl_pct >= 0, "pnl_pct": round(pnl_pct, 1),
                "action": "sell_daily_high_conflict",
                "reason": f"wc_high {wc_high:.1f} covers bucket {bucket_low:.0f}-{bucket_high:.0f}",
            }
            position.setdefault("monitoring_events", []).append(event)
            return "sell"

    # 7. Immediate sell: METAR at bucket
    metar_at_bucket = False
    if bucket_high == 999:
        metar_at_bucket = metar_temp >= bucket_low
    elif bucket_low == -999:
        metar_at_bucket = metar_temp <= bucket_high
    else:
        metar_at_bucket = metar_temp >= bucket_low

    if metar_at_bucket:
        entry_no = position.get("entry_no_price", 0)
        cur_no = current_no_price if current_no_price and current_no_price > 0 else entry_no
        pnl_pct = ((cur_no / entry_no - 1.0) * 100) if entry_no > 0 else 0
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wc_high": wc_high, "wc_current": wc_current,
            "metar_temp": metar_temp, "metar_temp_c": metar_temp_c,
            "wc_distance": wc_distance, "metar_distance": metar_distance,
            "closing_in": False, "in_profit": pnl_pct >= 0, "pnl_pct": round(pnl_pct, 1),
            "action": "sell_metar_at_bucket",
            "reason": f"metar {metar_temp:.1f} at/above bucket_low {bucket_low:.0f}",
        }
        position.setdefault("monitoring_events", []).append(event)
        return "sell"

    # 8. Proximity gate — only trend analysis when within threshold
    if metar_distance > PROXIMITY_THRESHOLD:
        entry_no = position.get("entry_no_price", 0)
        cur_no = current_no_price if current_no_price and current_no_price > 0 else entry_no
        pnl_pct = ((cur_no / entry_no - 1.0) * 100) if entry_no > 0 else 0
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wc_high": wc_high, "wc_current": wc_current,
            "metar_temp": metar_temp, "metar_temp_c": metar_temp_c,
            "wc_distance": wc_distance, "metar_distance": metar_distance,
            "closing_in": False, "in_profit": pnl_pct >= 0, "pnl_pct": round(pnl_pct, 1),
            "action": "hold", "reason": "too_far_from_bucket",
        }
        position.setdefault("monitoring_events", []).append(event)
        return "hold"

    # 9. Track METAR-based distance trend (actual observations, not forecast)
    prev_distances = position.get("metar_distances", [])
    prev_distances.append(round(metar_distance, 2))
    if len(prev_distances) > 10:
        prev_distances = prev_distances[-10:]
    position["metar_distances"] = prev_distances

    closing_in = False
    if len(prev_distances) >= METAR_CLOSE_READINGS:
        recent = prev_distances[-METAR_CLOSE_READINGS:]
        closing_in = all(recent[i] > recent[i + 1] for i in range(len(recent) - 1))

    # 10. P/L calc
    entry_no = position.get("entry_no_price", 0)
    cur_no = current_no_price if current_no_price and current_no_price > 0 else entry_no
    pnl_pct = ((cur_no / entry_no - 1.0) * 100) if entry_no > 0 else 0
    in_profit = pnl_pct >= 0

    # 11. Trend-based exits
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "wc_high": wc_high, "wc_current": wc_current,
        "metar_temp": metar_temp, "metar_temp_c": metar_temp_c,
        "wc_distance": wc_distance, "metar_distance": metar_distance,
        "closing_in": closing_in, "in_profit": in_profit,
        "pnl_pct": round(pnl_pct, 1),
    }

    if not closing_in:
        event["action"] = "hold"
        event["reason"] = "trend_not_closing"
        position.setdefault("monitoring_events", []).append(event)
        return "hold"

    if not in_profit:
        if metar_distance >= DISTANCE_MIN:
            event["action"] = "tighten"
            event["reason"] = f"closing_in_loss_dist_{metar_distance:.1f}"
            position.setdefault("monitoring_events", []).append(event)
            return "tighten"
        if pnl_pct <= STOP_LOSS_PCT * 100:
            event["action"] = "sell_stop_loss"
            event["reason"] = f"stop_hit_{pnl_pct:.1f}pct"
            position.setdefault("monitoring_events", []).append(event)
            return "sell"
        event["action"] = "wait_for_stop"
        event["reason"] = f"closing_in_loss_close_pnl_{pnl_pct:.1f}"
        position.setdefault("monitoring_events", []).append(event)
        return "wait_for_stop"

    if metar_distance <= 1.0:
        event["action"] = "sell_take_profit"
        event["reason"] = f"closing_in_profit_dist_{metar_distance:.1f}"
        position.setdefault("monitoring_events", []).append(event)
        return "sell"

    event["action"] = "hold"
    event["reason"] = f"closing_in_profit_dist_{metar_distance:.1f}"
    position.setdefault("monitoring_events", []).append(event)
    return "hold"
