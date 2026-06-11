"""
Trade Journal - SQLite-backed persistent trade history and analytics.
This is SUPPLEMENTARY to the encrypted daily state. It does NOT participate
in risk decisions. The tamper-proof state remains the source of truth for
risk enforcement. This module can be deleted without affecting risk rules.
"""

import base64
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DB_PATH         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_journal.db")
SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal_screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


class TradeJournal:
    """Persistent trade journal backed by SQLite."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        logger.info("Trade journal initialized: %s", db_path)

    def _create_tables(self):
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    date TEXT NOT NULL,
                    time_of_day TEXT NOT NULL,
                    pnl REAL NOT NULL,
                    security_id TEXT NOT NULL DEFAULT '',
                    trade_type TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 0,
                    is_winner INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
                CREATE INDEX IF NOT EXISTS idx_trades_security ON trades(security_id);

                CREATE TABLE IF NOT EXISTS daily_summary (
                    date TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    winners INTEGER DEFAULT 0,
                    losers INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0.0,
                    gross_profit REAL DEFAULT 0.0,
                    gross_loss REAL DEFAULT 0.0,
                    peak_pnl REAL DEFAULT 0.0,
                    win_rate REAL DEFAULT 0.0,
                    avg_win REAL DEFAULT 0.0,
                    avg_loss REAL DEFAULT 0.0,
                    last_updated REAL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS pnl_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    date TEXT NOT NULL,
                    realized REAL NOT NULL,
                    unrealized REAL NOT NULL,
                    total REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pnl_date ON pnl_snapshots(date);

                CREATE TABLE IF NOT EXISTS trade_entries (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    status TEXT DEFAULT 'open',
                    trade_type TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    hedge_instrument TEXT,
                    sell_security_id TEXT,
                    sell_entry_price REAL,
                    buy_entry_price REAL,
                    sell_exit_price REAL,
                    buy_exit_price REAL,
                    lots INTEGER DEFAULT 0,
                    lot_size INTEGER DEFAULT 25,
                    pnl REAL,
                    entry_screenshot TEXT,
                    exit_screenshot TEXT,
                    notes TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_entries_created ON trade_entries(created_at);
            """)

    def record_trade(self, trade_info: dict):
        """Record a single trade and update daily summary."""
        ts = trade_info.get("time", time.time())
        pnl = trade_info.get("pnl", 0.0)
        dt = datetime.fromtimestamp(ts)
        day = dt.strftime("%Y-%m-%d")
        tod = dt.strftime("%H:%M:%S")
        is_winner = 1 if pnl >= 0 else 0

        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO trades (timestamp, date, time_of_day, pnl, security_id, trade_type, quantity, is_winner) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (ts, day, tod, pnl,
                         str(trade_info.get("security_id", "")),
                         trade_info.get("type", ""),
                         trade_info.get("quantity", 0),
                         is_winner),
                    )
                self._update_daily_summary(day)
            except Exception as e:
                logger.warning("Journal record_trade failed: %s", e)

    def _update_daily_summary(self, day: str):
        """Recompute daily summary from trades table."""
        cur = self._conn.execute(
            "SELECT pnl, is_winner FROM trades WHERE date = ?", (day,)
        )
        rows = cur.fetchall()
        if not rows:
            return

        total = len(rows)
        winners = sum(1 for r in rows if r["is_winner"])
        losers = total - winners
        total_pnl = sum(r["pnl"] for r in rows)
        gross_profit = sum(r["pnl"] for r in rows if r["pnl"] >= 0)
        gross_loss = sum(r["pnl"] for r in rows if r["pnl"] < 0)
        win_rate = (winners / total * 100) if total > 0 else 0
        avg_win = (gross_profit / winners) if winners > 0 else 0
        avg_loss = (gross_loss / losers) if losers > 0 else 0

        # Peak P&L (running sum)
        running = 0.0
        peak = 0.0
        for r in rows:
            running += r["pnl"]
            if running > peak:
                peak = running

        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO daily_summary "
                "(date, total_trades, winners, losers, total_pnl, gross_profit, gross_loss, "
                "peak_pnl, win_rate, avg_win, avg_loss, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day, total, winners, losers, total_pnl, gross_profit, gross_loss,
                 peak, win_rate, avg_win, avg_loss, time.time()),
            )

    def flush_pnl_snapshots(self, snapshots: list[dict]):
        """Batch insert P&L snapshots (called at end of day)."""
        if not snapshots:
            return
        with self._lock:
            try:
                with self._conn:
                    self._conn.executemany(
                        "INSERT INTO pnl_snapshots (timestamp, date, realized, unrealized, total) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [
                            (s["timestamp"], s.get("date", str(date.today())),
                             s["realized"], s["unrealized"], s["total"])
                            for s in snapshots
                        ],
                    )
                logger.info("Flushed %d P&L snapshots to journal", len(snapshots))
            except Exception as e:
                logger.warning("Journal flush_pnl_snapshots failed: %s", e)

    def get_trades(self, day: str = None, limit: int = 200) -> list[dict]:
        """Get trades for a specific day."""
        day = day or str(date.today())
        cur = self._conn.execute(
            "SELECT timestamp, time_of_day, pnl, security_id, trade_type, quantity, is_winner "
            "FROM trades WHERE date = ? ORDER BY timestamp DESC LIMIT ?",
            (day, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_daily_summaries(self, days: int = 30) -> list[dict]:
        """Get daily P&L summaries for the last N days."""
        cur = self._conn.execute(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?", (days,)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_analytics(self, days: int = 30) -> dict:
        """Get aggregated analytics across multiple days."""
        cur = self._conn.execute(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?", (days,)
        )
        summaries = [dict(r) for r in cur.fetchall()]
        if not summaries:
            return {
                "total_days": 0, "total_trades": 0, "overall_pnl": 0,
                "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                "best_day": None, "worst_day": None,
                "profitable_days": 0, "losing_days": 0,
                "avg_daily_pnl": 0, "pnl_by_hour": [], "pnl_by_instrument": [],
            }

        total_days = len(summaries)
        total_trades = sum(s["total_trades"] for s in summaries)
        total_winners = sum(s["winners"] for s in summaries)
        total_losers = sum(s["losers"] for s in summaries)
        overall_pnl = sum(s["total_pnl"] for s in summaries)
        gross_profit = sum(s["gross_profit"] for s in summaries)
        gross_loss = sum(s["gross_loss"] for s in summaries)
        win_rate = (total_winners / total_trades * 100) if total_trades > 0 else 0
        avg_win = (gross_profit / total_winners) if total_winners > 0 else 0
        avg_loss = (gross_loss / total_losers) if total_losers > 0 else 0
        profitable_days = sum(1 for s in summaries if s["total_pnl"] >= 0)
        losing_days = total_days - profitable_days

        best_day = max(summaries, key=lambda s: s["total_pnl"])
        worst_day = min(summaries, key=lambda s: s["total_pnl"])

        # P&L by hour
        date_cutoff = summaries[-1]["date"] if summaries else str(date.today())
        cur = self._conn.execute(
            "SELECT CAST(SUBSTR(time_of_day, 1, 2) AS INTEGER) as hour, "
            "SUM(pnl) as total_pnl, COUNT(*) as trade_count "
            "FROM trades WHERE date >= ? GROUP BY hour ORDER BY hour",
            (date_cutoff,),
        )
        pnl_by_hour = [dict(r) for r in cur.fetchall()]

        # P&L by instrument — join with trade_entries to get readable symbol
        cur = self._conn.execute(
            "SELECT t.security_id, "
            "COALESCE(e.instrument, t.security_id) as instrument, "
            "SUM(t.pnl) as total_pnl, COUNT(*) as trade_count, "
            "SUM(CASE WHEN t.is_winner = 1 THEN 1 ELSE 0 END) as winners "
            "FROM trades t "
            "LEFT JOIN trade_entries e ON e.sell_security_id = t.security_id "
            "WHERE t.date >= ? GROUP BY t.security_id "
            "ORDER BY total_pnl DESC LIMIT 20",
            (date_cutoff,),
        )
        pnl_by_instrument = [dict(r) for r in cur.fetchall()]

        return {
            "total_days": total_days,
            "total_trades": total_trades,
            "overall_pnl": round(overall_pnl, 2),
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "best_day": {"date": best_day["date"], "pnl": best_day["total_pnl"]},
            "worst_day": {"date": worst_day["date"], "pnl": worst_day["total_pnl"]},
            "profitable_days": profitable_days,
            "losing_days": losing_days,
            "avg_daily_pnl": round(overall_pnl / total_days, 2) if total_days > 0 else 0,
            "pnl_by_hour": pnl_by_hour,
            "pnl_by_instrument": pnl_by_instrument,
        }

    def get_pnl_chart_data(self, day: str = None) -> list[dict]:
        """Get historical P&L snapshots for a given day (for chart replay)."""
        day = day or str(date.today())
        cur = self._conn.execute(
            "SELECT timestamp, realized, unrealized, total "
            "FROM pnl_snapshots WHERE date = ? ORDER BY timestamp",
            (day,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ── Detailed trade entries (with screenshots) ─────────────────────

    def create_entry(self, data: dict) -> str:
        entry_id = str(uuid.uuid4())[:12]
        # Allow caller to pass explicit timestamp (e.g. from Dhan order createTime)
        ts = data.get("created_at_ts")
        now = ts if (ts is not None and ts > 0) else time.time()
        entry_time = datetime.utcfromtimestamp(now).strftime("%d-%b %H:%M:%S")
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO trade_entries (id, created_at, entry_time, status, trade_type, "
                    "instrument, hedge_instrument, sell_security_id, sell_entry_price, "
                    "buy_entry_price, lots, lot_size, entry_screenshot, notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entry_id, now, entry_time, "open",
                     data.get("trade_type", "spread"),
                     data.get("instrument", ""),
                     data.get("hedge_instrument"),
                     data.get("sell_security_id"),
                     data.get("sell_entry_price"),
                     data.get("buy_entry_price"),
                     data.get("lots", 0),
                     data.get("lot_size", 25),
                     data.get("entry_screenshot"),
                     data.get("notes", ""))
                )
        return entry_id

    def update_entry_exit(self, entry_id: str, data: dict):
        exit_time = datetime.now().strftime("%d-%b %H:%M:%S")
        sell_exit = data.get("sell_exit_price")
        buy_exit  = data.get("buy_exit_price")
        screenshot = data.get("exit_screenshot")
        pnl = data.get("pnl")
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE trade_entries SET status='closed', exit_time=?, "
                    "sell_exit_price=?, buy_exit_price=?, exit_screenshot=?, pnl=? "
                    "WHERE id=?",
                    (exit_time, sell_exit, buy_exit, screenshot, pnl, entry_id)
                )

    def update_notes(self, entry_id: str, notes: str):
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE trade_entries SET notes=? WHERE id=?", (notes, entry_id)
                )

    def save_screenshot(self, data_url: str) -> str:
        """Save base64 PNG data_url to file, return filename."""
        header, b64 = data_url.split(",", 1)
        img_bytes = base64.b64decode(b64)
        filename = str(uuid.uuid4())[:12] + ".png"
        path = os.path.join(SCREENSHOTS_DIR, filename)
        with open(path, "wb") as f:
            f.write(img_bytes)
        return filename

    def get_screenshot_path(self, filename: str) -> str:
        return os.path.join(SCREENSHOTS_DIR, filename)

    def get_today_security_ids(self) -> set:
        """Return sell_security_ids already journaled today (to avoid duplicates on restart)."""
        import time as _time
        today_start = _time.time() - 86400  # last 24h
        cur = self._conn.execute(
            "SELECT sell_security_id FROM trade_entries WHERE created_at > ?", (today_start,)
        )
        return {row[0] for row in cur.fetchall() if row[0]}

    def get_entries(self, limit: int = 100, date_str: str = None) -> list:
        if date_str:
            # date_str is "YYYY-MM-DD" in IST; convert to UTC Unix range
            ist = timezone(timedelta(hours=5, minutes=30))
            day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ist)
            day_end = day_start + timedelta(days=1)
            cur = self._conn.execute(
                "SELECT * FROM trade_entries WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC",
                (day_start.timestamp(), day_end.timestamp()),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM trade_entries ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in cur.fetchall()]

    def get_entry(self, entry_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM trade_entries WHERE id=?", (entry_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_open_entries(self) -> list:
        cur = self._conn.execute(
            "SELECT * FROM trade_entries WHERE status='open' ORDER BY created_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def delete_entry(self, entry_id: str):
        with self._lock:
            self._conn.execute("DELETE FROM trade_entries WHERE id=?", (entry_id,))
            self._conn.commit()

    def close(self):
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass
