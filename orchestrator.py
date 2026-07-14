#!/usr/bin/env python3
"""
Weather NO Paper Trading Bot — Main Orchestrator.

Discovers Polymarket temperature markets, evaluates entry signals,
opens paper positions, monitors them, and auto-closes on sell triggers.

Usage:
    python orchestrator.py              # run loop
    python orchestrator.py scan         # one-shot scan
    python orchestrator.py status       # current state
"""

import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    METAR_POLL_SECONDS, SCAN_INTERVAL, MIN_VOLUME, DATA_DIR,
    MAX_OPEN_PAPER, MIN_BET, MAX_BET, DISTANCE_MIN, DISTANCE_MAX,
)
from simulator import Simulator
from strategy import evaluate_entry, monitor_position, evaluate_bucket_signals, evaluate_all_buckets_detail
from data_sources import polymarket

logger = logging.getLogger("orchestrator")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

sim = Simulator()

# Shared state for dashboard
_orchestrator_state = {
    "pid": None,
    "started_at": None,
    "last_scan_ts": 0,
    "next_scan_ts": 0,
    "last_monitor_ts": 0,
    "active_regions": [],
    "recent_signals": [],
    "recent_alerts": [],
    "scan_log": [],
}


def load_stations() -> dict:
    from config import STATIONS_FILE
    return json.loads(STATIONS_FILE.read_text(encoding="utf-8"))


def get_allowed_regions() -> set:
    IST = timezone(timedelta(hours=5, minutes=30))
    hour = datetime.now(IST).hour
    if hour < 8:
        return {"asia"}
    elif hour < 15:
        return {"asia", "europe", "africa"}
    else:
        return {"asia", "europe", "africa", "americas"}


def _compute_bet_size(distance: float) -> float:
    if distance <= DISTANCE_MIN:
        return MIN_BET
    if distance >= DISTANCE_MAX:
        return MAX_BET
    return round(MIN_BET + (distance - DISTANCE_MIN) / (DISTANCE_MAX - DISTANCE_MIN) * (MAX_BET - MIN_BET), 2)


def discover_markets(stations: dict, target_date: str = None) -> list[dict]:
    IST = timezone(timedelta(hours=5, minutes=30))
    if target_date is None:
        target_date = datetime.now(IST).strftime("%Y-%m-%d")

    allowed = get_allowed_regions()
    _orchestrator_state["active_regions"] = sorted(allowed)

    markets = []
    for city_slug, station in stations.items():
        if station.get("region", "asia") not in allowed:
            continue

        event = polymarket.get_event(city_slug, target_date)
        if event and event.get("markets"):
            has_volume = any(
                float(m.get("volume", 0)) >= MIN_VOLUME
                for m in event.get("markets", [])
            )
            if has_volume:
                buckets = polymarket.get_city_buckets(city_slug, target_date)
                markets.append({
                    "city_slug": city_slug,
                    "station": station,
                    "date_str": target_date,
                    "buckets": buckets,
                })
        time.sleep(0.3)

    return markets


def scan_entries(stations: dict, markets: list[dict]) -> int:
    all_signals = []
    near_miss_signals = []
    full_scan_data = []
    city_wc_high = {}

    for market in markets:
        city_slug = market["city_slug"]
        station = market["station"]
        date_str = market["date_str"]

        city_buckets = market.get("buckets", [])
        active_buckets = [b for b in city_buckets if not b.get("closed") and b.get("active")]

        detail = evaluate_all_buckets_detail(city_slug, station, date_str)
        wc_high = detail["wc_high"]
        om_high = detail["om_high"]
        if wc_high is not None:
            city_wc_high[city_slug] = wc_high

        passed_buckets = [b for b in detail["buckets"] if b["passed"]]
        failed_buckets = [b for b in detail["buckets"] if not b["passed"]]

        best_passed = passed_buckets[0] if passed_buckets else None

        _orchestrator_state["scan_log"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "city_scan",
            "city": city_slug,
            "date": date_str,
            "wc_high": wc_high,
            "om_high": om_high,
            "unit": station.get("unit", "F"),
            "active_buckets": len(detail["buckets"]),
            "passed_count": len(passed_buckets),
            "failed_count": len(failed_buckets),
            "signal": best_passed is not None,
            "signal_bucket": [best_passed["range"][0], best_passed["range"][1]] if best_passed else None,
            "signal_distance": best_passed["distance"] if best_passed else None,
            "signal_no_price": best_passed["no_price"] if best_passed else None,
            "signal_yes_price": best_passed["yes_price"] if best_passed else None,
            "signal_volume": best_passed["volume"] if best_passed else None,
            "signal_threshold": best_passed["threshold"] if best_passed else None,
            "signal_om_distance": best_passed["om_distance"] if best_passed else None,
            "near_misses": [{
                "range": b["range"],
                "threshold": b["threshold"],
                "distance": b["distance"],
                "yes_price": b["yes_price"],
                "no_price": b["no_price"],
                "volume": b["volume"],
                "failed_filters": b["failed_filters"],
                "om_distance": b["om_distance"],
            } for b in failed_buckets[:8]],
        })

        signal = evaluate_entry(city_slug, station, date_str)
        if signal is not None:
            all_signals.append(signal)
            city_wc_high[city_slug] = signal["wc_high"]

        misses = evaluate_bucket_signals(city_slug, station, date_str)
        near_miss_signals.extend(misses)
        if misses and misses[0].get("wc_high") is not None:
            city_wc_high[city_slug] = misses[0]["wc_high"]

        for bucket in city_buckets:
            if bucket.get("closed") or not bucket.get("active"):
                continue
            t_low, t_high = bucket["range"]
            if t_low == 0.0 and t_high == 0.0:
                continue
            full_scan_data.append({
                "city_slug": city_slug,
                "date": date_str,
                "bucket_range": (t_low, t_high),
                "yes_price": bucket["yes_price"],
                "no_price": bucket["no_price"],
                "volume": bucket["volume"],
                "wc_high": city_wc_high.get(city_slug),
            })

    all_signals.sort(key=lambda s: s["distance"], reverse=True)
    _orchestrator_state["recent_signals"] = near_miss_signals
    _orchestrator_state["full_scan_data"] = full_scan_data

    for signal in all_signals:
        signal["bet_size"] = _compute_bet_size(signal["distance"])

    if not all_signals:
        return 0

    for signal in all_signals:
        if sim.open_count() >= MAX_OPEN_PAPER:
            break
        city_slug = signal["city_slug"]
        date_str = signal["date"]

        if not sim.has_position(city_slug, date_str):
            pos = sim.open_position(
                city_slug=city_slug,
                date_str=date_str,
                bet_size=signal["bet_size"],
                entry_no_price=signal["no_price"],
                market_id=signal["market_id"],
                question=signal["question"],
                bucket_range=signal["bucket_range"],
                wc_high=signal["wc_high"],
                om_high=signal.get("om_high"),
                distance=signal["distance"],
            )
            if pos:
                pos["last_signal"] = signal
                sim.save_state()
                _orchestrator_state["scan_log"].append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "type": "position_opened",
                    "city": city_slug,
                    "bucket": f"{signal['bucket_range'][0]}-{signal['bucket_range'][1]}",
                    "bet": signal["bet_size"],
                    "no_price": signal["no_price"],
                    "yes_price": signal["yes_price"],
                    "distance": signal["distance"],
                    "wc_high": signal["wc_high"],
                    "om_high": signal.get("om_high"),
                    "threshold": signal.get("threshold"),
                    "question": signal.get("question", ""),
                })
                logger.info("[PAPER] OPENED %s/$%.2f NO@$%.3f dist=%.1f wc_high=%.1f",
                            city_slug, signal["bet_size"], signal["no_price"],
                            signal["distance"], signal["wc_high"])

    return len(all_signals)


def monitor_paper_positions(markets: list[dict]) -> int:
    if sim.open_count() == 0:
        return 0

    city_station = {}
    for wm in markets:
        city_station[wm["city_slug"]] = wm["station"]

    closes = 0
    for key, pos in list(sim.open_positions.items()):
        city_slug = pos["city_slug"]
        date_str = pos["date"]
        station = city_station.get(city_slug)
        if not station:
            from config import STATIONS_FILE
            all_stations = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
            station = all_stations.get(city_slug)
        if not station:
            continue

        current_no = pos.get("current_no_price", pos["entry_no_price"])
        price_data = polymarket.get_market_price(pos["market_id"])
        if price_data:
            current_no = price_data["no_price"]
        pos["current_no_price"] = current_no
        if price_data and price_data.get("closed"):
            reason = "resolution_win" if price_data["yes_price"] <= 0.05 else "resolution_loss"
            sim.close_position(city_slug, date_str, reason, current_no)
            closes += 1
            continue

        action = monitor_position(city_slug, station, date_str, pos,
                                  current_no_price=current_no)

        events = pos.get("monitoring_events", [])
        if events:
            last_event = events[-1]
            entry_no = pos.get("entry_no_price", 0)
            pnl_pct = ((current_no / entry_no - 1.0) * 100) if entry_no > 0 else 0
            _orchestrator_state["scan_log"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": "position_monitor",
                "city": city_slug,
                "bucket_low": pos.get("bucket_low"),
                "bucket_high": pos.get("bucket_high"),
                "wc_high": last_event.get("wc_high"),
                "wc_current": last_event.get("wc_current"),
                "metar_temp": last_event.get("metar_temp"),
                "wc_distance": last_event.get("wc_distance"),
                "metar_distance": last_event.get("metar_distance"),
                "no_price": current_no,
                "entry_no": entry_no,
                "pnl_pct": round(pnl_pct, 1),
                "action": last_event.get("action", action),
                "reason": last_event.get("reason", ""),
                "closing_in": last_event.get("closing_in", False),
            })

        if action == "sell":
            reason = "monitor_sell"
            if events:
                last_action = events[-1].get("action", "")
                action_reason_map = {
                    "sell_take_profit": "take_profit",
                    "sell_stop_loss": "stop_loss",
                    "sell_daily_high_conflict": "daily_high_conflict",
                    "sell_metar_at_bucket": "metar_at_bucket",
                }
                reason = action_reason_map.get(last_action, "monitor_sell")

            closed = sim.close_position(city_slug, date_str, reason, current_no)
            if closed:
                closes += 1
                _orchestrator_state["scan_log"].append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "type": "position_closed",
                    "city": city_slug,
                    "reason": reason,
                    "pnl": closed.get("pnl", 0),
                    "no_price": current_no,
                    "balance": sim.balance,
                })
                logger.info("[PAPER] CLOSED %s reason=%s pnl=$%.2f bal=$%.2f",
                            key, reason, closed.get("pnl", 0), sim.balance)

        elif action == "forecast_drift":
            events = pos.get("monitoring_events", [])
            if events:
                last = events[-1]
                alert = {
                    "ts": last["ts"],
                    "city_slug": city_slug,
                    "date": date_str,
                    "bucket_low": pos["bucket_low"],
                    "bucket_high": pos["bucket_high"],
                    "old_high": last.get("prev_wc_high"),
                    "new_high": last.get("wc_high"),
                    "shift": last.get("shift"),
                }
                _orchestrator_state["recent_alerts"].append(alert)
                _orchestrator_state["recent_alerts"] = _orchestrator_state["recent_alerts"][-50:]

    sim.save_state()
    return closes


def _write_timer_state():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "timer_state.json").write_text(
            json.dumps({
                "pid": _orchestrator_state["pid"],
                "started_at": _orchestrator_state["started_at"],
                "last_scan_ts": _orchestrator_state["last_scan_ts"],
                "next_scan_ts": _orchestrator_state["next_scan_ts"],
                "last_monitor_ts": _orchestrator_state["last_monitor_ts"],
                "metar_poll_seconds": METAR_POLL_SECONDS,
                "scan_interval": SCAN_INTERVAL,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8"
        )
        (DATA_DIR / "alerts.json").write_text(
            json.dumps(_orchestrator_state["recent_alerts"], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        (DATA_DIR / "signals.json").write_text(
            json.dumps(_orchestrator_state["recent_signals"], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        (DATA_DIR / "full_scan.json").write_text(
            json.dumps(_orchestrator_state.get("full_scan_data", []), indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        scan_log = _orchestrator_state.get("scan_log", [])
        (DATA_DIR / "scan_log.json").write_text(
            json.dumps(scan_log[-500:], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass


def cmd_run():
    import os
    stations = load_stations()
    IST = timezone(timedelta(hours=5, minutes=30))

    _orchestrator_state["pid"] = os.getpid()
    _orchestrator_state["started_at"] = datetime.now(timezone.utc).isoformat()

    last_full_scan = 0
    cached_markets = []
    current_ist_date = datetime.now(IST).strftime("%Y-%m-%d")

    logger.info("STARTING paper trading bot (bankroll=$%.2f, max_open=%d)",
                sim.balance, MAX_OPEN_PAPER)

    while True:
        now_ts = time.time()
        try:
            # Full scan
            if now_ts - last_full_scan >= SCAN_INTERVAL:
                today_ist = datetime.now(IST).strftime("%Y-%m-%d")
                yesterday_ist = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")

                if today_ist != current_ist_date:
                    current_ist_date = today_ist

                all_markets = []
                for d in [yesterday_ist, today_ist]:
                    all_markets.extend(discover_markets(stations, target_date=d))

                today_markets = [m for m in all_markets if m["date_str"] == today_ist]
                buy_recs = scan_entries(stations, today_markets)
                cached_markets = all_markets

                last_full_scan = time.time()
                _orchestrator_state["last_scan_ts"] = last_full_scan
                _orchestrator_state["next_scan_ts"] = last_full_scan + SCAN_INTERVAL
                _orchestrator_state["last_monitor_ts"] = last_full_scan

                logger.info("SCAN: %d markets | %d signals | paper: $%.2f (%d open)",
                            len(all_markets), buy_recs, sim.balance, sim.open_count())

                _orchestrator_state["scan_log"].append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "type": "scan_summary",
                    "cities_scanned": len(all_markets),
                    "signals_found": buy_recs,
                    "balance": sim.balance,
                    "positions_open": sim.open_count(),
                    "near_misses": len(_orchestrator_state.get("recent_signals", [])),
                    "full_scan_buckets": len(_orchestrator_state.get("full_scan_data", [])),
                })

            # Monitor every iteration
            if cached_markets and sim.open_count() > 0:
                paper_closes = monitor_paper_positions(cached_markets)
                _orchestrator_state["last_monitor_ts"] = time.time()
                if paper_closes:
                    logger.info("[PAPER] Closed %d | balance: $%.2f", paper_closes, sim.balance)

            _write_timer_state()

        except KeyboardInterrupt:
            logger.info("STOPPING")
            sim.save_state()
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e)
            time.sleep(30)
            continue

        time.sleep(METAR_POLL_SECONDS)


def cmd_scan():
    stations = load_stations()
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    yesterday_ist = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")

    all_markets = []
    for d in [yesterday_ist, today_ist]:
        all_markets.extend(discover_markets(stations, target_date=d))

    today_markets = [m for m in all_markets if m["date_str"] == today_ist]
    recs = scan_entries(stations, today_markets)
    print(f"Scan: {len(all_markets)} markets, {recs} signals, paper: ${sim.balance:.2f} ({sim.open_count()} open)")

    if sim.open_count() > 0:
        paper_closes = monitor_paper_positions(all_markets)
        print(f"Monitored: {paper_closes} closed, balance: ${sim.balance:.2f}")


def cmd_status():
    s = sim.summary()
    print(f"\n{'='*50}")
    print(f"  WEATHER NO PAPER TRADER — STATUS")
    print(f"{'='*50}")
    print(f"  Balance:    ${s['current_balance']:.2f} (start ${s['starting_bankroll']:.2f})")
    print(f"  Deployed:   ${s['deployed']:.2f} in {s['positions_still_open']} positions")
    print(f"  Free:       ${s['free']:.2f}")
    if s['positions_closed'] > 0:
        print(f"  W/L:        {s['wins']}W / {s['losses']}L ({s['win_rate']}%)")
        print(f"  P/L:        ${s['total_pnl']:+.2f}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        cmd_run()
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "status":
        cmd_status()
    else:
        print("Usage: python orchestrator.py [run|scan|status]")
