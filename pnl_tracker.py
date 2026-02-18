"""
Intraday P&L Tracker - In-memory snapshot collector for the live equity curve.
Stores P&L snapshots every monitoring tick (2s). Resets daily.
Data is flushed to SQLite at end of day for historical replay.
"""

import time
from datetime import date


class PnLTracker:
    """Collects P&L snapshots in memory for the intraday chart."""

    def __init__(self):
        self._snapshots: list[dict] = []
        self._date: str = str(date.today())

    def record(self, realized: float, unrealized: float):
        """Record a P&L snapshot at the current time."""
        self._reset_if_new_day()
        self._snapshots.append({
            "timestamp": time.time(),
            "date": self._date,
            "realized": round(realized, 2),
            "unrealized": round(unrealized, 2),
            "total": round(realized + unrealized, 2),
        })

    def get_chart_data(self) -> list[dict]:
        """Return downsampled snapshots for SocketIO transmission (max ~500 points)."""
        self._reset_if_new_day()
        data = self._snapshots
        if len(data) <= 500:
            return data
        step = len(data) // 500
        sampled = [data[i] for i in range(0, len(data), step)]
        # Always include the latest point
        if sampled[-1] is not data[-1]:
            sampled.append(data[-1])
        return sampled

    def get_all_snapshots(self) -> list[dict]:
        """Return all snapshots for end-of-day SQLite flush."""
        return list(self._snapshots)

    def reset(self):
        """Clear all snapshots."""
        self._snapshots = []
        self._date = str(date.today())

    def _reset_if_new_day(self):
        """Reset if the date has changed."""
        today = str(date.today())
        if self._date != today:
            self._snapshots = []
            self._date = today
