"""
Virtual bankroll and position tracking for paper trading.
Shared $10 pool across all open positions.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import BANKROLL, DATA_DIR

logger = logging.getLogger(__name__)


class Simulator:
    def __init__(self, bankroll: float = BANKROLL):
        self.starting_bankroll = bankroll
        self.balance = bankroll
        self.open_positions: dict[str, dict] = {}
        self.closed_positions: list[dict] = []
        self._load_state()

    def _state_path(self) -> Path:
        return DATA_DIR / "simulator_state.json"

    def _load_state(self):
        path = self._state_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.balance = data.get("balance", self.starting_bankroll)
                self.starting_bankroll = data.get("starting_bankroll", self.starting_bankroll)
                self.open_positions = data.get("open_positions", {})
                self.closed_positions = data.get("closed_positions", [])
            except (json.JSONDecodeError, KeyError):
                pass

    def reload(self):
        """Re-read state from disk. Call this before every dashboard API read."""
        self._load_state()

    def save_state(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "starting_bankroll": self.starting_bankroll,
            "balance": self.balance,
            "open_positions": self.open_positions,
            "closed_positions": self.closed_positions,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def open_position(self, city_slug: str, date_str: str, bet_size: float,
                      entry_no_price: float, market_id: str, question: str,
                      bucket_range: tuple[float, float],
                      wc_high: float, om_high: Optional[float],
                      distance: float) -> Optional[dict]:
        key = f"{city_slug}_{date_str}"
        if key in self.open_positions:
            return None
        if bet_size > self.balance:
            return None

        self.balance -= bet_size
        self.balance = round(self.balance, 2)

        position = {
            "city_slug": city_slug,
            "date": date_str,
            "market_id": market_id,
            "question": question,
            "bucket_low": bucket_range[0],
            "bucket_high": bucket_range[1],
            "bet_size": round(bet_size, 2),
            "entry_no_price": entry_no_price,
            "shares": round(bet_size / entry_no_price, 2) if entry_no_price > 0 else 0,
            "wc_high_at_entry": wc_high,
            "om_high_at_entry": om_high,
            "distance_at_entry": round(distance, 2),
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "monitoring_events": [],
            "metar_distances": [],
            "last_metar_temp": None,
            "last_wc_current": None,
            "last_wc_high": None,
            "last_signal": None,
        }

        self.open_positions[key] = position
        self.save_state()
        return position

    def close_position(self, city_slug: str, date_str: str, exit_reason: str,
                       current_no_price: Optional[float] = None) -> Optional[dict]:
        key = f"{city_slug}_{date_str}"
        pos = self.open_positions.pop(key, None)
        if not pos:
            return None

        bet = pos["bet_size"]
        entry_no = pos["entry_no_price"]

        if exit_reason == "resolution_win":
            pnl = round(bet * (1.0 / entry_no - 1.0), 2)
            proceeds = round(bet + pnl, 2)
        elif exit_reason == "resolution_loss":
            pnl = -bet
            proceeds = 0.0
        elif current_no_price is not None and current_no_price > 0:
            pnl = round(bet * (current_no_price / entry_no - 1.0), 2)
            proceeds = round(bet + pnl, 2)
            proceeds = max(proceeds, 0.0)
        else:
            pnl = 0.0
            proceeds = bet

        self.balance += proceeds
        self.balance = round(self.balance, 2)

        pos["exit_reason"] = exit_reason
        pos["exit_no_price"] = current_no_price
        pos["pnl"] = pnl
        pos["proceeds"] = proceeds
        pos["closed_at"] = datetime.now(timezone.utc).isoformat()
        pos["hold_time_hours"] = round(
            (datetime.fromisoformat(pos["closed_at"]) -
             datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600, 1
        )

        self.closed_positions.append(pos)
        self.save_state()
        return pos

    def has_position(self, city_slug: str, date_str: str) -> bool:
        return f"{city_slug}_{date_str}" in self.open_positions

    def open_count(self) -> int:
        return len(self.open_positions)

    def total_deployed(self) -> float:
        return round(sum(p["bet_size"] for p in self.open_positions.values()), 2)

    def summary(self) -> dict:
        total = len(self.closed_positions)
        wins = sum(1 for p in self.closed_positions if (p.get("pnl", 0) or 0) >= 0)
        losses = total - wins
        total_pnl = sum(p.get("pnl", 0) or 0 for p in self.closed_positions)
        avg_hold = (sum(p.get("hold_time_hours", 0) or 0 for p in self.closed_positions)
                    / total if total else 0)

        # Unrealized P/L from open positions (using entry NO price as proxy for current)
        unrealized = 0.0
        for pos in self.open_positions.values():
            entry_no = pos.get("entry_no_price", 0)
            if entry_no > 0:
                unrealized += pos["shares"] * (entry_no / entry_no - 1.0)
        # At entry, unrealized is 0 by definition; use last monitoring event's pnl_pct if available
        unrealized = 0.0
        for pos in self.open_positions.values():
            events = pos.get("monitoring_events", [])
            if events:
                last_pnl = events[-1].get("pnl_pct", 0)
                unrealized += pos["bet_size"] * (last_pnl / 100.0)

        portfolio_value = round(self.balance + unrealized, 2)

        return {
            "starting_bankroll": self.starting_bankroll,
            "current_balance": self.balance,
            "portfolio_value": portfolio_value,
            "deployed": self.total_deployed(),
            "free": round(self.balance, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_return": round(total_pnl, 2),
            "total_return_pct": round(
                total_pnl / self.starting_bankroll * 100, 1
            ) if total else 0,
            "portfolio_return_pct": round(
                (portfolio_value - self.starting_bankroll) / self.starting_bankroll * 100, 1
            ),
            "positions_opened": total + len(self.open_positions),
            "positions_closed": total,
            "positions_still_open": len(self.open_positions),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_hold_hours": round(avg_hold, 1),
        }
