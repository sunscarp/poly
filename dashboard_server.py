#!/usr/bin/env python3
"""
Dashboard server — HTTP API + serves the frontend SPA.
Reads simulator state from disk on every request (hot-reload).
"""

import json
import os
import sys
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from config import DATA_DIR, BANKROLL
from simulator import Simulator

PORT = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", 8081)))
sim = Simulator()

_orchestrator_state = {}


def _reload():
    """Re-read simulator state from disk so we see what the orchestrator wrote."""
    sim.reload()


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _parse_ts(iso_str):
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except Exception:
        return 0


def _ist_info():
    IST = timezone(timedelta(hours=5, minutes=30))
    ist_now = datetime.now(IST)
    hour = ist_now.hour
    if hour < 8:
        allowed = {"asia"}
        window = "Asia Only"
    elif hour < 15:
        allowed = {"asia", "europe", "africa"}
        window = "Asia + Europe + Africa"
    else:
        allowed = {"asia", "europe", "africa", "americas"}
        window = "All Regions"
    return {
        "ist_hour": ist_now.strftime("%H:%M"),
        "ist_date": ist_now.strftime("%Y-%m-%d"),
        "window": window,
        "allowed_regions": sorted(allowed),
    }


def api_state():
    _reload()
    timer = _read_json(DATA_DIR / "timer_state.json") or {}
    s = sim.summary()
    now_ts = time.time()

    started_at = timer.get("started_at")
    uptime_secs = 0
    if started_at:
        try:
            start_dt = datetime.fromisoformat(started_at)
            uptime_secs = int((datetime.now(timezone.utc) - start_dt).total_seconds())
        except Exception:
            pass

    next_scan_ts = timer.get("next_scan_ts", 0)
    next_scan_in = max(0, int(next_scan_ts - now_ts))

    alerts = _read_json(DATA_DIR / "alerts.json") or []
    one_hour_ago = now_ts - 3600
    recent_alerts = [
        a for a in alerts
        if _parse_ts(a.get("ts", "")) > one_hour_ago
    ]

    return {
        "bankroll": {
            "total": s["starting_bankroll"],
            "deployed": s["deployed"],
            "free": s["free"],
            "current_balance": s["current_balance"],
            "portfolio_value": s["portfolio_value"],
            "unrealized_pnl": s["unrealized_pnl"],
            "total_return": s["total_return"],
            "total_return_pct": s["total_return_pct"],
            "portfolio_return_pct": s["portfolio_return_pct"],
        },
        "bot": {
            "pid": timer.get("pid"),
            "uptime_secs": uptime_secs,
            "last_scan_ts": timer.get("last_scan_ts", 0),
            "next_scan_in": next_scan_in,
            "metar_poll_seconds": timer.get("metar_poll_seconds", 45),
            "scan_interval": timer.get("scan_interval", 600),
            "last_monitor_ts": timer.get("last_monitor_ts", 0),
        },
        "regions": _ist_info(),
        "alert_count": len(recent_alerts),
        "positions_open": sim.open_count(),
    }


def api_positions():
    _reload()
    positions = []
    for key, pos in sim.open_positions.items():
        last_event = (pos.get("monitoring_events") or [{}])[-1]
        cur_no = pos["entry_no_price"]

        metar_distances = pos.get("metar_distances", [])
        closing_in = False
        if len(metar_distances) >= 2:
            recent = metar_distances[-2:]
            closing_in = all(recent[i] > recent[i + 1] for i in range(len(recent) - 1))

        pnl_pct = 0.0
        if last_event.get("pnl_pct") is not None:
            pnl_pct = last_event["pnl_pct"]

        state = last_event.get("action") or "new"

        metar_dist = last_event.get("metar_distance")
        wc_dist = last_event.get("wc_distance")

        dist_color = "var(--green)"
        if metar_dist is not None:
            if metar_dist < 1:
                dist_color = "var(--red)"
            elif metar_dist < 2 and pnl_pct < 0:
                dist_color = "var(--orange)"
            elif metar_dist <= 10:
                dist_color = "var(--yellow)"

        opened_at = pos.get("opened_at", "")
        age_secs = 0
        if opened_at:
            try:
                open_dt = datetime.fromisoformat(opened_at)
                age_secs = int((datetime.now(timezone.utc) - open_dt).total_seconds())
            except Exception:
                pass

        last_metar = last_event.get("metar_temp") or pos.get("last_metar_temp")
        wc_current = last_event.get("wc_current") or pos.get("last_wc_current")
        metar_vs_wc = None
        if last_metar is not None and wc_current is not None:
            metar_vs_wc = round(last_metar - wc_current, 1)

        bet_actual = pos["bet_size"]
        bet_calc = pos.get("last_signal", {}).get("bet_size") if pos.get("last_signal") else None
        bet_mismatch = bet_calc is not None and abs(bet_calc - bet_actual) > 0.01

        trend = "flat"
        if closing_in:
            trend = "down" if pnl_pct < 0 else "up"

        positions.append({
            "key": key,
            "city_slug": pos["city_slug"],
            "date": pos["date"],
            "market_id": pos["market_id"],
            "question": pos.get("question", ""),
            "bucket_low": pos["bucket_low"],
            "bucket_high": pos["bucket_high"],
            "bet_size": bet_actual,
            "bet_mismatch": bet_mismatch,
            "entry_no_price": pos["entry_no_price"],
            "current_no_price": pos.get("current_no_price", pos["entry_no_price"]),
            "shares": pos["shares"],
            "wc_high_at_entry": pos["wc_high_at_entry"],
            "om_high_at_entry": pos.get("om_high_at_entry"),
            "distance_at_entry": pos["distance_at_entry"],
            "wc_high": last_event.get("wc_high"),
            "wc_current": wc_current,
            "om_high": pos.get("om_high_at_entry"),
            "metar_temp": last_metar,
            "metar_temp_c": last_event.get("metar_temp_c"),
            "metar_vs_wc": metar_vs_wc,
            "wc_distance": wc_dist,
            "metar_distance": metar_dist,
            "dist_color": dist_color,
            "pnl_pct": pnl_pct,
            "closing_in": closing_in,
            "trend": trend,
            "state": state,
            "reason": last_event.get("reason", ""),
            "opened_at": opened_at,
            "age_secs": age_secs,
            "metar_distances": metar_distances[-10:],
            "last_signal": pos.get("last_signal"),
        })

    positions.sort(key=lambda p: (
        p["state"] not in ("sell_take_profit", "sell_stop_loss", "sell_daily_high_conflict",
                            "sell_metar_at_bucket", "wait_for_stop", "tighten"),
        -(p.get("metar_distance") or 999),
    ))
    return positions


def api_signals():
    disk_signals = _read_json(DATA_DIR / "signals.json") or []
    return disk_signals


def api_events():
    _reload()
    events = []
    for key, pos in sim.open_positions.items():
        for evt in pos.get("monitoring_events", []):
            ev = dict(evt)
            ev["city_slug"] = pos["city_slug"]
            ev["date"] = pos["date"]
            ev["bucket_low"] = pos["bucket_low"]
            ev["bucket_high"] = pos["bucket_high"]
            events.append(ev)

    for pos in sim.closed_positions[-30:]:
        events.append({
            "ts": pos.get("closed_at", ""),
            "city_slug": pos["city_slug"],
            "date": pos["date"],
            "bucket_low": pos["bucket_low"],
            "bucket_high": pos["bucket_high"],
            "type": "exit",
            "pnl": pos.get("pnl", 0),
            "exit_reason": pos.get("exit_reason", ""),
            "bet_size": pos.get("bet_size", 0),
        })

    events.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return events


def api_full_scan():
    return _read_json(DATA_DIR / "full_scan.json") or []


def api_scan_log():
    return _read_json(DATA_DIR / "scan_log.json") or []


def api_alerts():
    alerts = _read_json(DATA_DIR / "alerts.json") or []
    return alerts[-50:]


def api_all():
    _reload()
    return {
        "state": api_state(),
        "positions": api_positions(),
        "signals": api_signals(),
        "events": api_events(),
        "alerts": api_alerts(),
        "full_scan": api_full_scan(),
        "scan_log": api_scan_log(),
        "summary": sim.summary(),
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            if path == "" or path == "/":
                self._serve_file("dashboard.html", "text/html")
            elif path == "/api/state":
                self._json_response(api_state())
            elif path == "/api/positions":
                self._json_response(api_positions())
            elif path == "/api/signals":
                self._json_response(api_signals())
            elif path == "/api/events":
                self._json_response(api_events())
            elif path == "/api/alerts":
                self._json_response(api_alerts())
            elif path == "/api/fullscan":
                self._json_response(api_full_scan())
            elif path == "/api/scanlog":
                self._json_response(api_scan_log())
            elif path == "/api/all":
                self._json_response(api_all())
            elif path == "/api/summary":
                _reload()
                self._json_response(sim.summary())
            else:
                self.send_error(404)
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type):
        filepath = Path(__file__).parent / filename
        if not filepath.exists():
            self.send_error(404, f"{filename} not found")
            return
        body = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def inject_orchestrator_state(state_dict):
    global _orchestrator_state
    _orchestrator_state = state_dict


def main():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"  Dashboard: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
