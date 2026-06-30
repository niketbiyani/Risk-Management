"""
Position Monitor Daemon.
Runs continuously during market hours, polling positions and enforcing
risk rules in real-time. This is the main loop of the system.

When a lockout is triggered:
1. All pending orders are cancelled
2. All open positions are closed with market orders
3. Dhan kill switch is activated (prevents ANY new orders)
4. State is permanently locked for the day (encrypted, non-reversible)
"""

import json
import logging
import os
import signal
import sys
import time
import threading
import urllib.request
from datetime import datetime, timedelta, timezone, time as dtime

from config import Config
from dhan_api import DhanAPI
from state_manager import StateManager
from risk_engine import RiskEngine
from trade_manager import TradeManager
from order_interceptor import OrderInterceptor
from pnl_tracker import PnLTracker

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Continuously monitors positions and enforces risk rules.
    This is the enforcement layer - it cannot be overridden once running.
    """

    def __init__(self):
        self.api = DhanAPI()
        self.state = StateManager()
        self.risk = RiskEngine(self.state)
        self.trade_mgr = TradeManager(self.api)
        self.interceptor = OrderInterceptor(self.api, self.risk, self.state)

        self.pnl_tracker = PnLTracker()

        self._running = False
        self._lock = threading.Lock()
        self._last_positions: list[dict] = []
        self._last_trade_count = 0
        self._prev_realized_pnl = 0.0
        self._lockout_executed = False
        self._unlock_grace_until: float = 0.0  # epoch time; lockout suppressed until then
        self._analyser_realized_pnl: float | None = None  # True realized from trade-analyser
        self._analyser_last_import: float = 0.0           # Timestamp of last import
        self._analyser_import_interval: float = 60.0      # Re-import every 60s
        self._journaled_order_ids: set = set()  # prevent duplicate journal entries
        self._order_cache_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "order_cache.json"
        )
        # Load persisted order cache from previous session if available
        self._last_orders = self._load_order_cache()

        # WebSocket state
        self._market_feed_thread: threading.Thread | None = None
        self._order_update_thread: threading.Thread | None = None
        self._subscribed_instruments: list = []
        self._extra_instruments: list = []   # OC / UI-requested instruments, preserved across position changes
        self._sl_tp_executing: set = set()

    def start(self):
        """Start the monitoring loop."""
        errors = Config.validate()
        if errors:
            for e in errors:
                logger.error("Config error: %s", e)
            sys.exit(1)

        logger.info("=" * 60)
        logger.info("TRADE MANAGEMENT PLATFORM - MONITOR STARTING")
        logger.info("=" * 60)
        logger.info("Client ID: %s", Config.DHAN_CLIENT_ID)
        logger.info("Daily Max Loss: ₹%s", f"{Config.DAILY_MAX_LOSS:,.0f}")
        logger.info("Profit Target: ₹%s", f"{Config.DAILY_PROFIT_TARGET:,.0f}")
        logger.info("Profit Lock: %d%% after ₹%s",
                     Config.PROFIT_LOCK_PERCENTAGE, f"{Config.PROFIT_LOCK_THRESHOLD:,.0f}")
        logger.info("Trailing Drawdown: %s (%d%%)",
                     "ON" if Config.TRAILING_DRAWDOWN_ENABLED else "OFF",
                     Config.TRAILING_DRAWDOWN_PERCENTAGE)
        logger.info("Max Positions: %d", Config.MAX_OPEN_POSITIONS)
        logger.info("Monitor Interval: %ds", Config.MONITOR_INTERVAL)
        logger.info("=" * 60)

        self._running = True

        # Start WebSocket feeds (non-blocking daemon threads)
        self._start_market_feed()
        self._order_update_thread = self.api.start_order_updates_async(self._on_order_update)

        # Backfill journal entries from today's order book (catches trades placed
        # via Dhan directly, or when monitor was not running)
        try:
            orders = self.api.get_order_book()
            if orders:
                logger.info("Startup journal backfill: checking %d orders...", len(orders))
                self._auto_journal_orders(orders)
        except Exception as e:
            logger.warning("Startup journal backfill failed: %s", e)

        self._monitor_loop()

    @staticmethod
    def _now_ist() -> datetime:
        """Return current time in IST (UTC+5:30)."""
        ist = timezone(timedelta(hours=5, minutes=30))
        return datetime.now(ist).replace(tzinfo=None)

    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                now = self._now_ist()

                # Only monitor during market hours (with buffer), times are in IST
                market_start = now.replace(
                    hour=Config.MARKET_OPEN_HOUR,
                    minute=Config.MARKET_OPEN_MINUTE - 5,
                    second=0)
                market_end = now.replace(
                    hour=Config.MARKET_CLOSE_HOUR,
                    minute=Config.MARKET_CLOSE_MINUTE + 5,
                    second=0)

                if now < market_start or now > market_end:
                    if now > market_end and not self.state.is_fresh_day():
                        logger.info("Market closed. Final P&L: ₹%.0f",
                                    self.state.total_pnl)
                        # Flush intraday P&L snapshots to journal for historical replay
                        try:
                            snapshots = self.pnl_tracker.get_all_snapshots()
                            if snapshots:
                                self.state.journal.flush_pnl_snapshots(snapshots)
                        except Exception as e:
                            logger.warning("Failed to flush P&L snapshots: %s", e)
                        self._running = False
                        break
                    time.sleep(30)
                    continue

                self._tick()

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Monitor error: %s", e, exc_info=True)

            time.sleep(Config.MONITOR_INTERVAL)

    def _tick(self):
        """Single monitoring cycle."""
        with self._lock:
            # If already locked out and positions closed, just monitor state
            if self.state.is_locked_out and self._lockout_executed:
                return

            # Fetch current positions
            positions = self.api.get_positions()
            self._last_positions = positions

            # Keep position cache fresh for WebSocket SL/TP callback
            self.trade_mgr.update_position_cache(positions)

            # Re-subscribe MarketFeed if open positions changed
            self._refresh_market_feed(positions)

            # Refresh true realized P&L from trade-analyser every 60s
            now = time.time()
            if now - self._analyser_last_import >= self._analyser_import_interval:
                self._analyser_last_import = now
                threading.Thread(target=self._refresh_analyser_realized, daemon=True).start()

            # Calculate unrealized P&L from open positions; use analyser for realized.
            # Do NOT use Dhan's unrealizedProfit — it blends the current open leg with
            # historical avg price across all re-entries, giving wrong unrealized on active days.
            # Instead compute: (LTP - avgPrice) * netQty for each open position.
            # netQty < 0 means short (sold); avgPrice is the average price of the open leg only.
            unrealized_pnl = 0.0
            open_position_count = 0

            for pos in positions:
                net_qty = pos.get("netQty", 0) or 0
                if net_qty != 0:
                    logger.warning("POSITION FIELDS: %s", {k: v for k, v in pos.items()})
                    ltp = pos.get("lastTradedPrice") or pos.get("ltp") or 0
                    avg = pos.get("avgPrice") or pos.get("costPrice") or 0
                    if ltp and avg:
                        u_pnl = (ltp - avg) * net_qty
                    else:
                        u_pnl = pos.get("unrealizedProfit", 0) or 0
                    unrealized_pnl += u_pnl
                    open_position_count += 1

            # Use trade-analyser realized if available, else fall back to Dhan's field.
            # Dhan's realizedProfit = (sellAvg - buyAvg) * totalQty — inflated on active
            # scalping days because it averages across all re-entries, not FIFO per trade.
            if self._analyser_realized_pnl is not None:
                realized_pnl = self._analyser_realized_pnl
            else:
                realized_pnl = sum((pos.get("realizedProfit", 0) or 0) for pos in positions)

            # Detect spreads
            spreads = self.trade_mgr.detect_spreads(positions)
            self.state.update_positions([
                {
                    "securityId": p.get("securityId"),
                    "netQty": p.get("netQty"),
                    "avgPrice": p.get("avgPrice"),
                    "ltp": p.get("lastTradedPrice"),
                    "pnl": (p.get("realizedProfit", 0) or 0) + (p.get("unrealizedProfit", 0) or 0),
                }
                for p in positions if p.get("netQty", 0) != 0
            ])

            if spreads:
                self.state.update_spreads(self.trade_mgr.get_spread_summary())

            # Record P&L snapshot for intraday chart
            self.pnl_tracker.record(realized_pnl, unrealized_pnl)

            # Fetch pending orders for dashboard display + auto-journal
            try:
                orders = self.api.get_order_book()
                if orders is not None:
                    self._last_orders = orders
                    if orders:
                        self._persist_order_cache(orders)
                    self._auto_journal_orders(orders)
            except Exception:
                pass  # Keep previous orders on failure

            # Evaluate P&L against risk rules (skip during post-unlock grace period)
            if time.time() < self._unlock_grace_until:
                logger.info("Post-unlock grace period active — skipping risk evaluation")
                action = None
            else:
                action = self.risk.evaluate_pnl(realized_pnl, unrealized_pnl)

            # Check trade-level SL/TP
            sl_tp_triggers = self.trade_mgr.check_sl_tp_triggers(positions)
            for trigger in sl_tp_triggers:
                self._execute_sl_tp(trigger)

            # Check pending spread orders
            spread_triggers = self.trade_mgr.check_pending_spreads()
            for spread_action in spread_triggers:
                self._execute_spread(spread_action)

            # Check trade results for cooldown evaluation
            self._check_new_trades()

            # If lockout triggered, execute emergency procedures
            if self.state.is_locked_out and not self._lockout_executed:
                self._execute_lockout()

            # Log status periodically
            status = self.risk.get_risk_status()
            if open_position_count > 0:
                logger.info(
                    "P&L: ₹%.0f (R: ₹%.0f + U: ₹%.0f) | "
                    "Positions: %d | Loss buffer: ₹%.0f | "
                    "Trades: %d (W:%d L:%d)",
                    status["pnl"]["total"],
                    status["pnl"]["realized"],
                    status["pnl"]["unrealized"],
                    open_position_count,
                    status["limits"]["loss_remaining"],
                    status["trades"]["total"],
                    status["trades"]["winners"],
                    status["trades"]["losers"],
                )

    def _start_market_feed(self):
        """Build instrument list from current positions and start MarketFeed."""
        try:
            positions = self.api.get_positions()
            instruments = self._build_instrument_list(positions)
            if not instruments:
                logger.info("No open positions — MarketFeed not started yet")
                return
            self._subscribed_instruments = instruments
            self._market_feed_thread = self.api.start_ltp_feed_async(
                instruments, self._on_market_tick
            )
            logger.info("MarketFeed started for %d instruments", len(instruments))
        except Exception as e:
            logger.error("Failed to start MarketFeed: %s", e)

    def _build_instrument_list(self, positions: list[dict]) -> list:
        """Convert positions to MarketFeed instrument tuples."""
        seg_map = {
            "IDX_I": 0,
            "NSE_EQ": 1,
            "NSE_FNO": 2,
            "NSE_CURR": 3,
            "BSE_EQ": 4,
            "MCX": 5,
            "BSE_CURR": 7,
            "BSE_FNO": 8,
        }
        instruments = []
        seen = set()
        for pos in positions:
            if pos.get("netQty", 0) == 0:
                continue
            seg_str = pos.get("exchangeSegment", "NSE_FNO")
            seg_int = seg_map.get(seg_str, 1)
            sec_id = str(pos.get("securityId", ""))
            if sec_id and (seg_int, sec_id) not in seen:
                seen.add((seg_int, sec_id))
                instruments.append((seg_int, sec_id, 15))
        return instruments

    def _refresh_market_feed(self, positions: list[dict]):
        """Restart MarketFeed only when position set changes, preserving extra (OC) instruments."""
        new_pos = self._build_instrument_list(positions)
        new_set = set((i[0], i[1]) for i in new_pos)
        old_set = set((i[0], i[1]) for i in self._subscribed_instruments)

        if new_set == old_set:
            return

        logger.info("Position change detected — refreshing MarketFeed subscriptions")
        self._subscribed_instruments = new_pos
        # Merge with extra (OC) instruments, dedup by (seg, sid)
        merged = list(new_pos)
        existing = {(i[0], i[1]) for i in new_pos}
        for inst in self._extra_instruments:
            if (inst[0], inst[1]) not in existing:
                merged.append(inst)
                existing.add((inst[0], inst[1]))
        if merged:
            self._market_feed_thread = self.api.start_ltp_feed_async(
                merged, self._on_market_tick
            )
        else:
            logger.info("No open positions — MarketFeed paused")

    def _on_market_tick(self, tick_data: dict):
        """
        Called from MarketFeed daemon thread on every LTP tick.
        Must return quickly — no blocking I/O.
        """
        try:
            security_id = str(
                tick_data.get("security_id")
                or tick_data.get("securityId")
                or tick_data.get("Security Id", "")
            )
            ltp = float(
                tick_data.get("LTP")
                or tick_data.get("ltp")
                or tick_data.get("last_price")
                or 0
            )
            if not security_id or ltp <= 0:
                return

            # Push LTP to option chain UI
            try:
                from dashboard import emit_oc_ltp, _oc_ltp_subscribed
                if security_id in _oc_ltp_subscribed:
                    emit_oc_ltp(security_id, ltp)
            except Exception:
                pass

            # Check SL/TP — skip if already executing for this instrument
            if security_id in self._sl_tp_executing:
                return
            triggers = self.trade_mgr.check_sl_tp_for_security(security_id, ltp)
            for trigger in triggers:
                self._sl_tp_executing.add(security_id)
                threading.Thread(
                    target=self._execute_sl_tp_and_cleanup,
                    args=(trigger,),
                    daemon=True,
                ).start()

            # Check ARM TRIGGER pending spreads for this security on every tick
            pending_triggers = self.trade_mgr.check_pending_spreads_for_security(security_id, ltp)
            for spread_action in pending_triggers:
                threading.Thread(
                    target=self._execute_spread,
                    args=(spread_action,),
                    daemon=True,
                ).start()

        except Exception as e:
            logger.error("Error in market tick callback: %s", e)

    def _execute_sl_tp_and_cleanup(self, trigger: dict):
        """Execute SL/TP order then remove instrument from executing set."""
        try:
            self._execute_sl_tp(trigger)
        finally:
            self._sl_tp_executing.discard(trigger["security_id"])

    def _on_order_update(self, update: dict):
        """
        Called from OrderUpdate daemon thread on every order status change.
        Emits SocketIO events to dashboard for real-time order status.
        """
        try:
            logger.debug("OrderUpdate RAW: %s", update)
            order_id = str(update.get("orderId", ""))
            status = update.get("orderStatus", "")
            symbol = update.get("tradingSymbol", order_id)
            qty = update.get("tradedQuantity", 0)
            reason = update.get("omsErrorDescription") or update.get("rejectedReason", "")

            logger.info("Order %s → %s (%s)", order_id, status, symbol)

            try:
                from dashboard import socketio
                socketio.emit("order_update", {
                    "orderId": order_id,
                    "orderStatus": status,
                    "symbol": symbol,
                    "tradedQty": qty,
                    "reason": reason,
                })

                if status == "REJECTED":
                    socketio.emit("SPREAD_FAILED", {
                        "orderId": order_id,
                        "reason": reason or "Rejected by exchange",
                    })
            except Exception:
                pass

        except Exception as e:
            logger.error("Error in order update callback: %s", e)

    def _refresh_analyser_realized(self):
        """Import latest trades from Dhan into trade-analyser, then sum closed P&L."""
        analyser = "http://localhost:5556"
        try:
            # Trigger re-import
            req = urllib.request.Request(
                f"{analyser}/api/import",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.warning("Trade-analyser import failed: %s", e)
            return  # Keep previous value

        try:
            from datetime import date as _date
            today = str(_date.today())
            url = f"{analyser}/api/trades?date={today}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                trades = json.loads(resp.read())
            realized = sum(t.get("pnl", 0) or 0 for t in trades if t.get("status") == "CLOSED")
            self._analyser_realized_pnl = realized
            logger.debug("Analyser realized P&L: ₹%.0f (%d closed trades)", realized, sum(1 for t in trades if t.get("status") == "CLOSED"))
        except Exception as e:
            logger.warning("Trade-analyser fetch failed: %s", e)

    def _persist_order_cache(self, orders: list):
        """Save today's order list to disk so backfill works after service restart."""
        try:
            from datetime import date as _date
            today = str(_date.today())
            with open(self._order_cache_path, "w") as f:
                json.dump({"date": today, "orders": orders}, f)
        except Exception as e:
            logger.debug("Failed to persist order cache: %s", e)

    def _load_order_cache(self) -> list:
        """Load persisted order cache if it's from today."""
        try:
            from datetime import date as _date
            if not os.path.exists(self._order_cache_path):
                return []
            with open(self._order_cache_path) as f:
                data = json.load(f)
            if data.get("date") == str(_date.today()):
                orders = data.get("orders", [])
                if orders:
                    logger.info("Loaded %d orders from today's order cache", len(orders))
                return orders
        except Exception as e:
            logger.debug("Failed to load order cache: %s", e)
        return []

    def _normalize_trade_book(self, trades: list) -> list:
        """Convert trade book records to order-book format for _auto_journal_orders."""
        if trades:
            sample = trades[0]
            logger.info("TRADE BOOK SAMPLE FIELDS: %s", list(sample.keys()))
        result = []
        seen_ids = set()
        for t in trades:
            # Dhan trade book fields differ from order book — map them
            oid = str(t.get("orderId") or t.get("exchangeOrderId") or t.get("exchangeTradeId") or id(t))
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            result.append({
                "orderId": oid,
                "orderStatus": "TRADED",
                "transactionType": t.get("transactionType", ""),
                "securityId": t.get("securityId", ""),
                "tradingSymbol": t.get("tradingSymbol", str(t.get("securityId", ""))),
                "exchangeSegment": t.get("exchangeSegment", ""),
                "price": t.get("tradedPrice") or t.get("price") or 0,
                "averageTradedPrice": t.get("tradedPrice") or t.get("averageTradedPrice") or 0,
                "filledQty": t.get("tradedQuantity") or t.get("quantity") or 0,
                "quantity": t.get("tradedQuantity") or t.get("quantity") or 0,
                "createTime": t.get("createTime") or t.get("exchangeTime") or t.get("updateTime") or "",
                "updateTime": t.get("updateTime") or "",
                "exchangeTime": t.get("exchangeTime") or "",
            })
        return result

    def _process_csv_trades(self, orders: list):
        """Process CSV trade book rows into journal entries.

        Rule: process rows in time order. Each row pairs with the next
        opposite-transaction row for the same symbol (FIFO, bidirectional).
        - SELL followed by BUY same symbol → short trade, P&L = (sell - buy) * qty
        - BUY followed by SELL same symbol → long/hedge trade, P&L = (sell - buy) * qty
        All rows are accounted for; nothing is skipped.
        Remaining unmatched rows at the end → OPEN entries.
        """
        from collections import deque
        from datetime import timezone as _tz, timedelta as _td

        ist = _tz(_td(hours=5, minutes=30))

        def _ts(o):
            t = (o.get("createTime") or "").strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M:%S",
                        "%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                try:
                    return datetime.strptime(t, fmt).replace(tzinfo=ist).timestamp()
                except ValueError:
                    pass
            return 0.0

        def _lot_size(sym):
            s = sym.upper()
            if "NIFTY" in s and "BANK" not in s: return 75
            if "SENSEX" in s or ("BSE" in s and "NIFTY" not in s): return 20
            if "BANKNIFTY" in s or "BANKEX" in s: return 35
            return 25

        sorted_rows = sorted(orders, key=_ts)

        # Per-symbol queue: rows waiting for their opposite leg
        queues = {}  # symbol -> deque of order dicts
        created = 0

        for o in sorted_rows:
            sym = o.get("tradingSymbol", "")
            txn = o.get("transactionType", "")
            price = float(o.get("averageTradedPrice") or o.get("price") or 0)
            qty   = int(o.get("filledQty") or o.get("quantity") or 0)
            ts    = _ts(o)
            ls    = _lot_size(sym)
            lots  = max(1, qty // ls) if ls > 0 else 1

            queue = queues.setdefault(sym, deque())

            # Check if the head of queue is the opposite transaction type
            if queue and queue[0].get("transactionType") != txn:
                paired = queue.popleft()
                paired_price = float(paired.get("averageTradedPrice") or paired.get("price") or 0)
                paired_ts    = _ts(paired)
                paired_qty   = int(paired.get("filledQty") or paired.get("quantity") or 0)

                # Determine which leg was the entry (first in time)
                if paired.get("transactionType") == "SELL":
                    sell_price, sell_ts, exit_price = paired_price, paired_ts, price
                    entry_ts = paired_ts
                else:  # paired was BUY, current is SELL
                    sell_price, exit_price, entry_ts = price, paired_price, paired_ts

                pnl = round((sell_price - exit_price) * paired_qty, 2)
                entry_id = self.state.journal.create_entry({
                    "trade_type":       "naked",
                    "instrument":       sym,
                    "sell_security_id": sym,
                    "sell_entry_price": sell_price,
                    "lots":             lots,
                    "lot_size":         ls,
                    "created_at_ts":    entry_ts if entry_ts > 0 else None,
                })
                if entry_id:
                    self.state.journal.update_entry_exit(entry_id, {
                        "sell_exit_price": exit_price,
                        "pnl": pnl,
                    })
                    created += 1
                    logger.info("CSV-journal CLOSED: %s  sell=%.2f buy=%.2f  P&L ₹%.0f",
                                sym, sell_price, exit_price, pnl)
            else:
                # Same direction or empty queue → wait for opposite leg
                queue.append(o)

        # Remaining unmatched rows = genuinely open positions (only SELLs create journal entries)
        for sym, queue in queues.items():
            for o in queue:
                if o.get("transactionType") != "SELL":
                    continue  # unmatched BUY with no SELL = long with no exit, skip
                sell_price = float(o.get("averageTradedPrice") or o.get("price") or 0)
                sell_ts    = _ts(o)
                sell_qty   = int(o.get("filledQty") or o.get("quantity") or 0)
                ls = _lot_size(sym)
                lots = max(1, sell_qty // ls) if ls > 0 else 1
                self.state.journal.create_entry({
                    "trade_type":       "naked",
                    "instrument":       sym,
                    "sell_security_id": sym,
                    "sell_entry_price": sell_price,
                    "lots":             lots,
                    "lot_size":         ls,
                    "created_at_ts":    sell_ts if sell_ts > 0 else None,
                })
                created += 1
                logger.info("CSV-journal OPEN: %s @ %.2f", sym, sell_price)

        logger.info("CSV import complete: %d journal entries created from %d rows",
                    created, len(orders))
        return created

    def _auto_journal_orders(self, orders: list):
        """Auto-create journal entries for all filled SELL option orders not yet journaled.
        Fires on every tick so it catches trades placed directly on Dhan too."""
        # Seed on first call: mark orders already represented in today's journal
        if not hasattr(self, "_auto_journal_seeded"):
            existing_sids = self.state.journal.get_today_security_ids()
            # Also include open entries (created by JS execute) so we don't duplicate
            open_entries = self.state.journal.get_open_entries()
            existing_sids.update(e.get("sell_security_id", "") for e in open_entries)
            for o in orders:
                if str(o.get("securityId", "")) in existing_sids:
                    self._journaled_order_ids.add(o.get("orderId"))
            self._auto_journal_seeded = True

        def _order_ts(o: dict) -> float:
            """Parse Dhan createTime to Unix timestamp (IST)."""
            from datetime import timezone as _tz, timedelta as _td
            t = o.get("createTime") or o.get("updateTime") or o.get("exchangeTime") or ""
            if not t:
                return 0.0
            t = t.strip()
            ist = _tz(_td(hours=5, minutes=30))
            # Try multiple formats
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M:%S",
                        "%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(t, fmt).replace(tzinfo=ist)
                    return dt.timestamp()
                except ValueError:
                    pass
            if len(t) <= 8:  # "HH:MM:SS" only
                try:
                    today_ist = datetime.now(ist).date()
                    dt = datetime.strptime(str(today_ist) + " " + t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ist)
                    return dt.timestamp()
                except Exception:
                    pass
            logger.debug("_order_ts: unrecognised format %r", t)
            return 0.0

        # Log createTime from first order once to confirm format
        if orders and not getattr(self, "_order_ts_logged", False):
            s = orders[0]
            logger.info("ORDER TIME FIELDS: createTime=%r updateTime=%r exchangeTime=%r orderId=%r",
                        s.get("createTime"), s.get("updateTime"), s.get("exchangeTime"), s.get("orderId"))
            self._order_ts_logged = True

        # Index all TRADED orders by security_id, sorted by time
        traded_by_sid: dict = {}
        for o in orders:
            if o.get("orderStatus") == "TRADED":
                sid = str(o.get("securityId", ""))
                traded_by_sid.setdefault(sid, []).append(o)
        for sid in traded_by_sid:
            traded_by_sid[sid].sort(key=_order_ts)

        filled_sells = [
            o for o in orders
            if o.get("orderStatus") == "TRADED"
            and o.get("transactionType") == "SELL"
            and o.get("orderId") not in self._journaled_order_ids
        ]
        filled_sells.sort(key=_order_ts)

        # Cache DB security IDs once per call to avoid repeated queries
        _journaled_sids_db = self.state.journal.get_today_security_ids()

        for sell_order in filled_sells:
            order_id = sell_order.get("orderId", "")
            sell_ts = _order_ts(sell_order)
            security_id = str(sell_order.get("securityId", ""))
            symbol = sell_order.get("tradingSymbol", security_id)
            sell_price = float(sell_order.get("price") or sell_order.get("averageTradedPrice") or 0)
            qty = int(sell_order.get("filledQty") or sell_order.get("quantity") or 0)

            # DB-level dedup: skip if this security already has a journal entry today
            if security_id in _journaled_sids_db:
                self._journaled_order_ids.add(order_id)
                continue

            sym_up = symbol.upper()
            if "NIFTY" in sym_up and "BANK" not in sym_up:
                lot_size = 75
            elif "SENSEX" in sym_up:
                lot_size = 20
            elif "BANKNIFTY" in sym_up:
                lot_size = 35
            else:
                lot_size = 25
            lots = max(1, qty // lot_size) if lot_size > 0 else 1

            # Exit: first BUY on same security_id AFTER the sell timestamp
            exit_order = None
            for o in traded_by_sid.get(security_id, []):
                if (o.get("transactionType") == "BUY"
                        and o.get("orderId") not in self._journaled_order_ids
                        and o.get("orderId") != order_id
                        and _order_ts(o) >= sell_ts):
                    exit_order = o
                    break

            # Hedge: BUY on a different security_id within 5 minutes of sell
            hedge_order = None
            for sid2, orders2 in traded_by_sid.items():
                if sid2 == security_id:
                    continue
                for o in orders2:
                    if (o.get("transactionType") == "BUY"
                            and o.get("orderId") not in self._journaled_order_ids
                            and abs(_order_ts(o) - sell_ts) <= 300):
                        hedge_order = o
                        break
                if hedge_order:
                    break

            buy_exit_price = float(exit_order.get("price") or exit_order.get("averageTradedPrice") or 0) if exit_order else 0
            is_closed = exit_order is not None
            pnl = round((sell_price - buy_exit_price) * qty, 2) if is_closed else None

            entry_data = {
                "trade_type": "spread" if hedge_order else "naked",
                "instrument": symbol,
                "hedge_instrument": hedge_order.get("tradingSymbol") if hedge_order else None,
                "sell_security_id": security_id,
                "sell_entry_price": sell_price,
                "buy_entry_price": float(hedge_order.get("price") or hedge_order.get("averageTradedPrice") or 0) if hedge_order else 0,
                "lots": lots,
                "lot_size": lot_size,
                "created_at_ts": sell_ts if sell_ts > 0 else None,
            }

            entry_id = self.state.journal.create_entry(entry_data)

            # If we found an exit order, immediately close the entry
            if is_closed and entry_id:
                self.state.journal.update_entry_exit(entry_id, {
                    "sell_exit_price": buy_exit_price,
                    "pnl": pnl,
                })

            self._journaled_order_ids.add(order_id)
            if exit_order:
                self._journaled_order_ids.add(exit_order.get("orderId"))
            if hedge_order:
                self._journaled_order_ids.add(hedge_order.get("orderId"))
            logger.info("Auto-journal: %s %s%s", symbol,
                        f"P&L ₹{pnl:.0f}" if pnl is not None else "OPEN",
                        " [spread]" if hedge_order else "")

        # Second pass: close any OPEN entries whose exit BUY has now arrived
        try:
            open_entries = self.state.journal.get_open_entries()
            for entry in open_entries:
                sell_sid = entry.get("sell_security_id", "")
                entry_id = entry.get("id", "")
                if not sell_sid or not entry_id:
                    continue
                sell_price = float(entry.get("sell_entry_price") or 0)
                # Find first unjourned BUY on same security after entry creation time
                entry_ts = float(entry.get("created_at") or 0)
                for o in traded_by_sid.get(sell_sid, []):
                    if (o.get("transactionType") == "BUY"
                            and o.get("orderId") not in self._journaled_order_ids
                            and _order_ts(o) >= entry_ts):
                        buy_price = float(o.get("price") or o.get("averageTradedPrice") or 0)
                        qty_exit = int(o.get("filledQty") or o.get("quantity") or 0)
                        pnl_exit = round((sell_price - buy_price) * qty_exit, 2)
                        self.state.journal.update_entry_exit(entry_id, {
                            "sell_exit_price": buy_price,
                            "pnl": pnl_exit,
                        })
                        self._journaled_order_ids.add(o.get("orderId"))
                        logger.info("Auto-closed journal entry %s (%s): P&L ₹%.0f",
                                    entry_id, entry.get("instrument", sell_sid), pnl_exit)
                        break
        except Exception as e:
            logger.debug("Auto-close open entries failed: %s", e)

    def _check_new_trades(self):
        """Check tradebook for newly executed trades and record them."""
        try:
            trades = self.api.get_trade_book()
            current_count = len(trades) if trades else 0

            if current_count > self._last_trade_count and self._last_trade_count > 0:
                new_trades = trades[self._last_trade_count:]

                # Approximate per-trade P&L from realized P&L changes
                current_realized = self.state.realized_pnl
                prev_realized = getattr(self, "_prev_realized_pnl", 0)
                realized_change = current_realized - prev_realized
                per_trade_pnl = realized_change / max(1, len(new_trades))

                for trade in new_trades:
                    trade_info = {
                        "security_id": trade.get("securityId", ""),
                        "type": trade.get("transactionType", ""),
                        "quantity": trade.get("tradedQuantity", 0),
                    }
                    self.risk.evaluate_trade_result(per_trade_pnl, trade_info)

            self._last_trade_count = current_count
            self._prev_realized_pnl = self.state.realized_pnl
        except Exception as e:
            logger.error("Failed to check trades: %s", e)

    def _execute_sl_tp(self, trigger: dict):
        """Execute a stop loss or take profit order."""
        try:
            logger.warning("Executing %s for %s @ ₹%.2f (LTP: ₹%.2f)",
                           trigger["action"], trigger["security_id"],
                           trigger["trigger_price"], trigger["ltp"])

            self.api.place_order(
                security_id=trigger["security_id"],
                exchange_segment=trigger["exchange_segment"],
                transaction_type=trigger["transaction_type"],
                quantity=trigger["quantity"],
                order_type="MARKET",
                product_type=trigger["product_type"],
                price=0,
            )

            # Notify dashboard clients
            try:
                from dashboard import emit_sl_tp_trigger
                emit_sl_tp_trigger(trigger)
            except Exception:
                pass  # Dashboard may not be running
        except Exception as e:
            logger.error("Failed to execute %s: %s", trigger["action"], e)

    def _execute_spread(self, spread_action: dict):
        """
        Execute a triggered spread order:
        1. Buy hedge at MARKET (immediate)
        2. Place sell at LIMIT price
        3. Auto-set SL on sell leg
        """
        spread_id = spread_action["spread_id"]
        logger.warning(
            "Executing spread %s: BUY hedge %s @ MKT, then SELL %s @ %.2f",
            spread_id, spread_action["buy_security_id"],
            spread_action["sell_security_id"], spread_action["sell_price"],
        )

        try:
            # Step 1: Buy hedge at MARKET
            product_type = spread_action.get("product_type", "MARGIN")
            hedge_result = self.api.place_order(
                security_id=spread_action["buy_security_id"],
                exchange_segment=spread_action["buy_exchange_segment"],
                transaction_type="BUY",
                quantity=spread_action["quantity"],
                order_type="MARKET",
                product_type=product_type,
                price=0,
            )

            if isinstance(hedge_result, dict) and hedge_result.get("status") == "failure":
                raise RuntimeError(f"Hedge BUY rejected by broker: {hedge_result.get('remarks', hedge_result)}")

            hedge_order_id = ""
            if isinstance(hedge_result, dict):
                hedge_order_id = str(hedge_result.get("orderId", hedge_result.get("data", {}).get("orderId", "")))

            logger.info("Spread %s: hedge BUY placed (order: %s)", spread_id, hedge_order_id)

            # Brief pause to let hedge fill before placing sell leg
            time.sleep(0.2)

            # Step 2: Place sell (LIMIT or MARKET depending on caller)
            sell_order_type = spread_action.get("sell_order_type", "LIMIT")
            sell_price = spread_action["sell_price"] if sell_order_type == "LIMIT" else 0
            sell_result = self.api.place_order(
                security_id=spread_action["sell_security_id"],
                exchange_segment=spread_action["sell_exchange_segment"],
                transaction_type="SELL",
                quantity=spread_action["quantity"],
                order_type=sell_order_type,
                product_type=product_type,
                price=sell_price,
            )

            if isinstance(sell_result, dict) and sell_result.get("status") == "failure":
                raise RuntimeError(f"Sell {sell_order_type} rejected by broker: {sell_result.get('remarks', sell_result)}")

            sell_order_id = ""
            if isinstance(sell_result, dict):
                sell_order_id = str(sell_result.get("orderId", sell_result.get("data", {}).get("orderId", "")))

            logger.info("Spread %s: SELL placed @ %.2f (order: %s)",
                        spread_id, spread_action["sell_price"], sell_order_id)

            # Poll order status after brief delay to detect exchange rejections
            # (Dhan API returns success on submit but exchange may reject after ~1-2s)
            time.sleep(2.5)
            hedge_status = "UNKNOWN"
            sell_status = "UNKNOWN"
            reject_reason = ""
            try:
                orders = self.api.get_order_book()
                if orders:
                    self._last_orders = orders
                    for o in orders:
                        oid = str(o.get("orderId", ""))
                        if oid == hedge_order_id:
                            hedge_status = o.get("orderStatus", "UNKNOWN")
                        if oid == sell_order_id:
                            sell_status = o.get("orderStatus", "UNKNOWN")
                            if sell_status == "REJECTED":
                                reject_reason = (
                                    o.get("omsErrorDescription", "")
                                    or o.get("rejectedReason", "")
                                    or "Rejected by exchange"
                                )
            except Exception as poll_err:
                logger.warning("Spread %s: order status poll failed: %s", spread_id, poll_err)

            any_rejected = hedge_status == "REJECTED" or sell_status == "REJECTED"

            if any_rejected:
                leg = "BUY hedge" if hedge_status == "REJECTED" else "SELL"
                err_msg = f"{leg} order REJECTED by exchange: {reject_reason}"
                logger.warning("Spread %s: %s", spread_id, err_msg)
                self.trade_mgr.update_spread_status(spread_id, "FAILED", error_message=err_msg)
                try:
                    from dashboard import emit_sl_tp_trigger
                    emit_sl_tp_trigger({
                        "action": "SPREAD_FAILED",
                        "security_id": spread_action.get("sell_security_id", ""),
                        "error": err_msg,
                    })
                except Exception:
                    pass
                return

            # Step 3: Place exchange-level SL on sell leg only after confirming sell filled.
            # Dhan accepts submissions synchronously but rejects via RMS ~1s later —
            # placing SL before the status check caused a spurious 3rd order on rejections.
            sell_sl = spread_action.get("sell_sl", 0)
            if sell_sl > 0:
                try:
                    sl_result = self.api.place_order(
                        security_id=spread_action["sell_security_id"],
                        exchange_segment=spread_action["sell_exchange_segment"],
                        transaction_type="BUY",
                        quantity=spread_action["quantity"],
                        order_type="STOP_LOSS_MARKET",
                        product_type=product_type,
                        price=0,
                        trigger_price=sell_sl,
                    )
                    sl_order_id = ""
                    if isinstance(sl_result, dict) and sl_result.get("status") == "failure":
                        logger.warning("Spread %s: exchange SL order failed: %s — falling back to monitored SL only",
                                       spread_id, sl_result.get("remarks", sl_result))
                    else:
                        sl_order_id = str(sl_result.get("orderId", sl_result.get("data", {}).get("orderId", ""))) if isinstance(sl_result, dict) else ""
                        logger.info("Spread %s: exchange SL placed @ trigger %.2f (order: %s)",
                                    spread_id, sell_sl, sl_order_id)
                except Exception as sl_err:
                    sl_order_id = ""
                    logger.warning("Spread %s: exchange SL placement failed: %s — monitored SL still active",
                                   spread_id, sl_err)
                self.trade_mgr.set_stop_loss(
                    security_id=spread_action["sell_security_id"],
                    sl_price=sell_sl,
                    exchange_sl_order_id=sl_order_id,
                )
                logger.info("Spread %s: monitored SL set at %.2f for sell leg", spread_id, sell_sl)

            # Update spread status
            self.trade_mgr.update_spread_status(
                spread_id, "FILLED",
                hedge_order_id=hedge_order_id,
                sell_order_id=sell_order_id,
            )

            # Notify dashboard
            try:
                from dashboard import emit_sl_tp_trigger
                emit_sl_tp_trigger({
                    "action": "SPREAD_FILLED",
                    "security_id": spread_action["sell_security_id"],
                    "trigger_price": spread_action["sell_price"],
                    "ltp": spread_action.get("ltp", 0),
                })
            except Exception:
                pass

        except Exception as e:
            logger.error("Spread %s execution FAILED: %s", spread_id, e, exc_info=True)
            self.trade_mgr.update_spread_status(
                spread_id, "FAILED", error_message=str(e),
            )
            try:
                from dashboard import emit_sl_tp_trigger
                emit_sl_tp_trigger({
                    "action": "SPREAD_FAILED",
                    "security_id": spread_action.get("sell_security_id", ""),
                    "error": str(e),
                })
            except Exception:
                pass

    def _execute_lockout(self):
        """
        Execute full lockout procedure.
        This is the nuclear option - cannot be reversed for the day.
        """
        logger.warning("=" * 60)
        logger.warning("EXECUTING LOCKOUT PROCEDURE")
        logger.warning("Reason: %s", self.state.get("lockout_reason"))
        logger.warning("=" * 60)

        try:
            # Step 1: Cancel all pending orders
            logger.warning("Step 1: Cancelling all pending orders...")
            cancel_results = self.api.cancel_all_pending_orders()
            logger.warning("Cancelled %d orders", len(cancel_results))

            # Step 2: Close all open positions
            logger.warning("Step 2: Closing all open positions...")
            close_results = self.api.close_all_positions()
            logger.warning("Closed %d positions", len(close_results))

            # Step 3: Wait briefly for orders to execute
            time.sleep(3)

            # Step 4: Activate Dhan kill switch
            logger.warning("Step 3: Activating Dhan kill switch...")
            try:
                self.api.activate_kill_switch()
                self.state.set_kill_switch(True)
                logger.warning("Kill switch ACTIVATED - no more orders possible")
            except Exception as e:
                logger.error("Kill switch failed (positions may still be open): %s", e)
                # Kill switch requires no open positions; retry after positions close
                time.sleep(5)
                try:
                    self.api.activate_kill_switch()
                    self.state.set_kill_switch(True)
                except Exception as e2:
                    logger.error("Kill switch retry failed: %s", e2)

            self._lockout_executed = True
            logger.warning("LOCKOUT COMPLETE - Trading disabled for the day")
            logger.warning("=" * 60)

        except Exception as e:
            logger.error("CRITICAL: Lockout execution failed: %s", e)
            # Even if execution fails, state remains locked

    def _shutdown(self, signum=None, frame=None):
        """Graceful shutdown."""
        logger.info("Shutting down monitor...")
        self._running = False

    def _get_pending_orders(self) -> list[dict]:
        """Filter order book for pending/transit orders."""
        pending = []
        for order in self._last_orders:
            status = order.get("orderStatus", "")
            if status in ("PENDING", "TRANSIT", "PART_TRADED"):
                pending.append(self._format_order(order))
        return pending

    def _get_recent_orders(self) -> list[dict]:
        """Get recent non-pending orders (rejected, executed, cancelled) for dashboard display."""
        recent = []
        for order in self._last_orders:
            status = order.get("orderStatus", "")
            if status in ("REJECTED", "TRADED", "CANCELLED"):
                formatted = self._format_order(order)
                # Add rejection reason for rejected orders
                if status == "REJECTED":
                    formatted["rejectedReason"] = (
                        order.get("omsErrorDescription", "")
                        or order.get("rejectedReason", "")
                        or ""
                    )
                recent.append(formatted)
        return recent

    @staticmethod
    def _format_order(order: dict) -> dict:
        """Format a Dhan order dict into a consistent shape for the dashboard."""
        return {
            "orderId": order.get("orderId", ""),
            "tradingSymbol": order.get("tradingSymbol", ""),
            "securityId": order.get("securityId", ""),
            "exchangeSegment": order.get("exchangeSegment", ""),
            "transactionType": order.get("transactionType", ""),
            "quantity": order.get("quantity", 0),
            "tradedQuantity": order.get("tradedQuantity", 0),
            "price": order.get("price", 0),
            "triggerPrice": order.get("triggerPrice", 0),
            "orderType": order.get("orderType", ""),
            "productType": order.get("productType", ""),
            "orderStatus": order.get("orderStatus", ""),
            "createTime": order.get("createTime", ""),
            "updateTime": order.get("updateTime", ""),
        }

    def _normalize_positions(self, positions: list) -> list:
        """Map Dhan position fields to names the dashboard JS expects."""
        result = []
        for p in positions:
            result.append({
                **p,
                # JS uses avgPrice; Dhan uses costPrice
                "avgPrice": p.get("costPrice") or p.get("buyAvg") or 0,
                # JS uses lastTradedPrice; Dhan positions don't include LTP
                "lastTradedPrice": p.get("lastTradedPrice") or 0,
            })
        return result

    def get_status(self) -> dict:
        """Get current monitor status for dashboard."""
        risk_status = self.risk.get_risk_status()
        result = {
            **risk_status,
            "monitor_running": self._running,
            "positions": self._normalize_positions(self._last_positions or []),
            "spreads": [],
            "sl_tp_orders": {},
            "pending_spreads": [],
            "pnl_chart": [],
            "pending_orders": [],
            "recent_orders": [],
        }
        # Fetch non-critical components individually so one failure doesn't break all
        try:
            result["spreads"] = self.trade_mgr.get_spread_summary()
        except Exception as e:
            logger.debug("get_status: spreads error: %s", e)
        try:
            result["sl_tp_orders"] = self.trade_mgr.get_active_sl_tp()
        except Exception as e:
            logger.debug("get_status: sl_tp error: %s", e)
        try:
            result["pending_spreads"] = self.trade_mgr.get_pending_spreads_summary()
        except Exception as e:
            logger.debug("get_status: pending_spreads error: %s", e)
        try:
            result["pnl_chart"] = self.pnl_tracker.get_chart_data()
        except Exception as e:
            logger.debug("get_status: pnl_chart error: %s", e)
        try:
            result["pending_orders"] = self._get_pending_orders()
        except Exception as e:
            logger.debug("get_status: pending_orders error: %s", e)
        try:
            result["recent_orders"] = self._get_recent_orders()
        except Exception as e:
            logger.debug("get_status: recent_orders error: %s", e)
        return result


def run_monitor():
    """Entry point for the monitor daemon (standalone mode)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("monitor.log"),
        ],
    )
    monitor = PositionMonitor()
    # Register signal handlers only in standalone mode (main thread)
    signal.signal(signal.SIGINT, monitor._shutdown)
    signal.signal(signal.SIGTERM, monitor._shutdown)
    monitor.start()


if __name__ == "__main__":
    run_monitor()
