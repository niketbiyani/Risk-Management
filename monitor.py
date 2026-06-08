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

import logging
import signal
import sys
import time
import threading
from datetime import datetime, timedelta

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
        self._last_orders: list[dict] = []
        self._last_trade_count = 0
        self._prev_realized_pnl = 0.0
        self._lockout_executed = False

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

        self._monitor_loop()

    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                now = datetime.now()

                # Only monitor during market hours (with buffer)
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

            # Calculate realized + unrealized P&L from positions
            realized_pnl = 0.0
            unrealized_pnl = 0.0
            open_position_count = 0

            for pos in positions:
                r_pnl = pos.get("realizedProfit", 0) or 0
                u_pnl = pos.get("unrealizedProfit", 0) or 0
                realized_pnl += r_pnl
                unrealized_pnl += u_pnl
                if pos.get("netQty", 0) != 0:
                    open_position_count += 1

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

            # Fetch pending orders for dashboard display
            try:
                orders = self.api.get_order_book()
                if orders is not None:
                    self._last_orders = orders
            except Exception:
                pass  # Keep previous orders on failure

            # Evaluate P&L against risk rules
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
            self._market_feed_thread = self.api.start_market_feed_async(
                instruments, self._on_market_tick
            )
            logger.info("MarketFeed started for %d instruments", len(instruments))
        except Exception as e:
            logger.error("Failed to start MarketFeed: %s", e)

    def _build_instrument_list(self, positions: list[dict]) -> list:
        """Convert positions to MarketFeed instrument tuples."""
        seg_map = {
            "NSE_FNO": 1,
            "BSE_FNO": 2,
            "NSE_EQ": 3,
            "BSE_EQ": 4,
            "IDX_I": 0,
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
                instruments.append((seg_int, sec_id, "LTP"))
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
            self._market_feed_thread = self.api.start_market_feed_async(
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
            logger.debug("RAW TICK: %s", tick_data)
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

            # Push LTP to option chain UI if this instrument is subscribed for display
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

            # Step 3: Place exchange-level SL on sell leg (STOP_LOSS_MARKET BUY order)
            # This fires at the exchange even if the VPS crashes — hard protection.
            # Also register with trade_mgr for the monitored trailing-SL layer.
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
                    if isinstance(sl_result, dict) and sl_result.get("status") == "failure":
                        logger.warning("Spread %s: exchange SL order failed: %s — falling back to monitored SL only",
                                       spread_id, sl_result.get("remarks", sl_result))
                    else:
                        sl_order_id = str(sl_result.get("orderId", sl_result.get("data", {}).get("orderId", ""))) if isinstance(sl_result, dict) else ""
                        logger.info("Spread %s: exchange SL placed @ trigger %.2f (order: %s)",
                                    spread_id, sell_sl, sl_order_id)
                except Exception as sl_err:
                    logger.warning("Spread %s: exchange SL placement failed: %s — monitored SL still active",
                                   spread_id, sl_err)
                # Also register monitored SL as backup
                self.trade_mgr.set_stop_loss(
                    security_id=spread_action["sell_security_id"],
                    sl_price=sell_sl,
                )
                logger.info("Spread %s: monitored SL set at %.2f for sell leg", spread_id, sell_sl)

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

    def get_status(self) -> dict:
        """Get current monitor status for dashboard."""
        risk_status = self.risk.get_risk_status()
        result = {
            **risk_status,
            "monitor_running": self._running,
            "positions": self._last_positions or [],
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
