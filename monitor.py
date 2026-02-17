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

        self._running = False
        self._lock = threading.Lock()
        self._last_positions: list[dict] = []
        self._last_trade_count = 0
        self._lockout_executed = False

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

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

    def _check_new_trades(self):
        """Check tradebook for newly executed trades and evaluate them."""
        try:
            trades = self.api.get_trade_book()
            current_count = len(trades) if trades else 0

            if current_count > self._last_trade_count and self._last_trade_count > 0:
                # New trades detected
                new_trades = trades[self._last_trade_count:]
                for trade in new_trades:
                    # Approximate trade P&L from position data
                    # (Dhan tradebook doesn't directly give P&L per trade)
                    trade_info = {
                        "security_id": trade.get("securityId", ""),
                        "type": trade.get("transactionType", ""),
                        "quantity": trade.get("tradedQuantity", 0),
                    }
                    # P&L is tracked at position level, so we use realized P&L changes
                    # The risk engine tracks this through evaluate_pnl

            self._last_trade_count = current_count
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
            hedge_result = self.api.place_order(
                security_id=spread_action["buy_security_id"],
                exchange_segment=spread_action["buy_exchange_segment"],
                transaction_type="BUY",
                quantity=spread_action["quantity"],
                order_type="MARKET",
                product_type="MARGIN",
                price=0,
            )

            hedge_order_id = ""
            if isinstance(hedge_result, dict):
                hedge_order_id = str(hedge_result.get("orderId", hedge_result.get("data", {}).get("orderId", "")))

            logger.info("Spread %s: hedge BUY placed (order: %s)", spread_id, hedge_order_id)

            # Brief pause to let hedge fill
            time.sleep(0.5)

            # Step 2: Place sell at LIMIT price
            sell_result = self.api.place_order(
                security_id=spread_action["sell_security_id"],
                exchange_segment=spread_action["sell_exchange_segment"],
                transaction_type="SELL",
                quantity=spread_action["quantity"],
                order_type="LIMIT",
                product_type="MARGIN",
                price=spread_action["sell_price"],
            )

            sell_order_id = ""
            if isinstance(sell_result, dict):
                sell_order_id = str(sell_result.get("orderId", sell_result.get("data", {}).get("orderId", "")))

            logger.info("Spread %s: SELL placed @ %.2f (order: %s)",
                        spread_id, spread_action["sell_price"], sell_order_id)

            # Step 3: Auto-set SL on sell leg if specified
            if spread_action.get("sell_sl", 0) > 0:
                self.trade_mgr.set_stop_loss(
                    security_id=spread_action["sell_security_id"],
                    sl_price=spread_action["sell_sl"],
                )
                logger.info("Spread %s: SL set at %.2f for sell leg",
                            spread_id, spread_action["sell_sl"])

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

    def get_status(self) -> dict:
        """Get current monitor status for dashboard."""
        risk_status = self.risk.get_risk_status()
        return {
            **risk_status,
            "monitor_running": self._running,
            "positions": self._last_positions,
            "spreads": self.trade_mgr.get_spread_summary(),
            "sl_tp_orders": self.trade_mgr.get_active_sl_tp(),
            "pending_spreads": self.trade_mgr.get_pending_spreads_summary(),
        }


def run_monitor():
    """Entry point for the monitor daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("monitor.log"),
        ],
    )
    monitor = PositionMonitor()
    monitor.start()


if __name__ == "__main__":
    run_monitor()
