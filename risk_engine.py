"""
Core Risk Management Engine.
Implements prop-firm style risk rules with tamper-proof enforcement.

Rules enforced:
1. Daily max loss limit → lockout
2. Daily profit target → lockout (optional)
3. Profit lock (lock X% of profits once threshold hit)
4. Trailing drawdown from total P&L high water mark
5. Max open positions limit
6. Max single trade risk
7. Max order quantity
8. Spread-aware P&L tracking for credit/debit spreads
"""

import logging
import time
from datetime import datetime
from typing import Optional

from config import Config
from state_manager import StateManager

logger = logging.getLogger(__name__)


class RiskDecision:
    """Result of a risk check."""

    def __init__(self, allowed: bool, reason: str = "", action: str = ""):
        self.allowed = allowed
        self.reason = reason
        self.action = action  # "lockout", "cooldown", "block", ""

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        if self.allowed:
            return "RiskDecision(ALLOWED)"
        return f"RiskDecision(BLOCKED: {self.reason})"


class RiskEngine:
    """
    Evaluates risk rules and enforces limits.
    This is the brain of the system - it decides whether trading can continue.
    """

    def __init__(self, state: StateManager):
        self.state = state
        logger.info("Risk engine initialized with limits: max_loss=%.0f, profit_target=%.0f, "
                     "profit_lock_threshold=%.0f @ %d%%",
                     Config.DAILY_MAX_LOSS, Config.DAILY_PROFIT_TARGET,
                     Config.PROFIT_LOCK_THRESHOLD, Config.PROFIT_LOCK_PERCENTAGE)

    # ── Main Check: Can We Trade? ──────────────────────────────────────

    def can_trade(self) -> RiskDecision:
        """
        Master check: should trading be allowed right now?
        Checks all rules in priority order.
        """
        # 1. Already locked out (cannot be reversed for the day)
        if self.state.is_locked_out:
            return RiskDecision(False, f"Locked out: {self.state.get('lockout_reason')}",
                                "lockout")

        # 1b. Check daily trade count limit
        total_trades = self.state.get("total_trades", 0)
        if total_trades >= Config.MAX_DAILY_TRADES:
            self.state.activate_lockout(
                f"Daily trade limit hit: {total_trades} trades (limit: {Config.MAX_DAILY_TRADES})")
            return RiskDecision(False, "Daily trade count limit reached", "lockout")

        # 2. Daily max loss breached
        loss_check = self._check_daily_loss()
        if not loss_check:
            return loss_check

        # 3. Profit lock floor breached
        profit_lock_check = self._check_profit_lock()
        if not profit_lock_check:
            return profit_lock_check

        # 4. Trailing drawdown breached
        drawdown_check = self._check_trailing_drawdown()
        if not drawdown_check:
            return drawdown_check

        # 5. Daily profit target reached (optional lockout)
        if Config.DAILY_PROFIT_TARGET > 0:
            if self.state.realized_pnl >= Config.DAILY_PROFIT_TARGET:
                self.state.activate_lockout(
                    f"Profit target reached: ₹{self.state.realized_pnl:,.0f}")
                return RiskDecision(False, "Daily profit target reached", "lockout")

        return RiskDecision(True)

    def check_new_order(self, order_quantity: int, num_open_positions: int,
                        estimated_risk: float = 0) -> RiskDecision:
        """
        Check if a specific new order should be allowed.
        Called before any order placement.
        """
        # First check general trading permission
        trade_check = self.can_trade()
        if not trade_check:
            return trade_check

        # Max open positions
        if num_open_positions >= Config.MAX_OPEN_POSITIONS:
            return RiskDecision(False,
                                f"Max positions ({Config.MAX_OPEN_POSITIONS}) reached",
                                "block")

        # Max order quantity
        if order_quantity > Config.MAX_ORDER_QUANTITY:
            return RiskDecision(False,
                                f"Order qty {order_quantity} exceeds max {Config.MAX_ORDER_QUANTITY}",
                                "block")

        # Single trade risk limit
        if estimated_risk > 0 and estimated_risk > Config.MAX_SINGLE_TRADE_RISK:
            return RiskDecision(False,
                                f"Trade risk ₹{estimated_risk:,.0f} exceeds max "
                                f"₹{Config.MAX_SINGLE_TRADE_RISK:,.0f}",
                                "block")

        return RiskDecision(True)

    # ── P&L Update & Rule Evaluation ───────────────────────────────────

    def evaluate_pnl(self, realized_pnl: float, unrealized_pnl: float) -> Optional[str]:
        """
        Called every monitor cycle with current P&L.
        Evaluates all rules and triggers actions.
        Returns action taken or None.
        """
        self.state.update_pnl(realized_pnl, unrealized_pnl)
        
        # Calculate brokerage and net P&L
        executions = self.state.get("total_executions", 0)
        brokerage = executions * Config.BROKERAGE_PER_ORDER
        total_pnl = realized_pnl + unrealized_pnl
        net_total = total_pnl - brokerage

        # Check daily trade limit lockout
        total_trades = self.state.get("total_trades", 0)
        if total_trades >= Config.MAX_DAILY_TRADES:
            self.state.activate_lockout(
                f"Daily trade limit hit: {total_trades} trades (limit: {Config.MAX_DAILY_TRADES})")
            return "lockout_max_trades"

        # ── Check daily max loss (total P&L including brokerage) ──────
        if net_total <= -Config.DAILY_MAX_LOSS:
            self.state.activate_lockout(
                f"Daily loss limit hit (including brokerage charges): ₹{net_total:,.0f} "
                f"(limit: ₹{-Config.DAILY_MAX_LOSS:,.0f})")
            return "lockout_max_loss"

        # ── Check profit lock activation ───────────────────────────────
        if (not self.state.profit_lock_active and
                realized_pnl >= Config.PROFIT_LOCK_THRESHOLD):
            lock_pct = Config.PROFIT_LOCK_PERCENTAGE / 100
            floor = realized_pnl * lock_pct
            self.state.activate_profit_lock(realized_pnl, floor)
            logger.info("Profit lock triggered at ₹%.0f, floor set to ₹%.0f",
                        realized_pnl, floor)
            return "profit_lock_activated"

        # ── Check profit lock floor breach ─────────────────────────────
        if self.state.profit_lock_active:
            floor = self.state.profit_lock_floor
            if realized_pnl < floor:
                self.state.activate_lockout(
                    f"Profit lock floor breached: P&L ₹{realized_pnl:,.0f} "
                    f"fell below floor ₹{floor:,.0f}")
                return "lockout_profit_lock"

        # ── Check trailing drawdown (HWM based on realized P&L only) ────
        # HWM uses realized P&L only — unrealized fluctuates every tick
        # and would cause HWM to ratchet up on paper gains, making the
        # drawdown display jump around. Drawdown is measured as how far
        # total (realized + unrealized) has fallen from the realized HWM.
        if Config.TRAILING_DRAWDOWN_ENABLED:
            hwm = self.state.high_water_mark
            if realized_pnl > hwm:
                hwm = realized_pnl

            # Only track drawdown once HWM has been established above threshold
            if hwm >= Config.PROFIT_LOCK_THRESHOLD:
                drawdown = max(0, hwm - total_pnl)
                drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
                self.state.update_trailing_drawdown(True, hwm, drawdown)

                if drawdown >= drawdown_limit:
                    self.state.activate_lockout(
                        f"Trailing drawdown limit hit: drew down ₹{drawdown:,.0f} "
                        f"from HWM ₹{hwm:,.0f} (limit: {Config.TRAILING_DRAWDOWN_PERCENTAGE}%)")
                    return "lockout_trailing_drawdown"
            elif realized_pnl > 0:
                # Below threshold but track progress
                drawdown = max(0, hwm - total_pnl)
                self.state.update_trailing_drawdown(False, hwm, drawdown)

        return None

    def evaluate_trade_result(self, trade_pnl: float, trade_info: dict):
        """
        Called after each trade completes.
        Records the trade for tracking (win/loss counters, history).
        """
        self.state.record_trade({**trade_info, "pnl": trade_pnl})

    # ── Private Rule Checks ────────────────────────────────────────────

    def _check_daily_loss(self) -> RiskDecision:
        """Check if daily loss limit is breached."""
        total = self.state.total_pnl
        executions = self.state.get("total_executions", 0)
        brokerage = executions * Config.BROKERAGE_PER_ORDER
        net_total = total - brokerage
        if net_total <= -Config.DAILY_MAX_LOSS:
            self.state.activate_lockout(
                f"Daily loss limit (including brokerage charges): ₹{net_total:,.0f}")
            return RiskDecision(False, "Daily loss limit breached", "lockout")
        return RiskDecision(True)

    def _check_profit_lock(self) -> RiskDecision:
        """Check if profit lock floor is breached."""
        if not self.state.profit_lock_active:
            return RiskDecision(True)
        if self.state.realized_pnl < self.state.profit_lock_floor:
            self.state.activate_lockout(
                f"Profit lock floor breached: ₹{self.state.realized_pnl:,.0f} "
                f"< floor ₹{self.state.profit_lock_floor:,.0f}")
            return RiskDecision(False, "Profit lock floor breached", "lockout")
        return RiskDecision(True)

    def _check_trailing_drawdown(self) -> RiskDecision:
        """Check trailing drawdown: HWM is realized P&L, drawdown measures vs total."""
        if not Config.TRAILING_DRAWDOWN_ENABLED:
            return RiskDecision(True)
        if not self.state.get("trailing_drawdown_active"):
            return RiskDecision(True)

        hwm = self.state.high_water_mark
        if hwm < Config.PROFIT_LOCK_THRESHOLD:
            return RiskDecision(True)

        total = self.state.total_pnl
        drawdown = max(0, hwm - total)
        limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        if drawdown >= limit:
            self.state.activate_lockout(
                f"Trailing drawdown: ₹{drawdown:,.0f} from HWM ₹{hwm:,.0f}")
            return RiskDecision(False, "Trailing drawdown limit breached", "lockout")
        return RiskDecision(True)

    # ── Status Reporting ───────────────────────────────────────────────

    def get_risk_status(self) -> dict:
        """Get comprehensive risk status for dashboard."""
        realized = self.state.realized_pnl
        unrealized = self.state.unrealized_pnl
        total = realized + unrealized

        executions = self.state.get("total_executions", 0)
        brokerage = executions * Config.BROKERAGE_PER_ORDER
        net_total = total - brokerage

        # Calculate distances to limits
        loss_remaining = max(0, Config.DAILY_MAX_LOSS + net_total)
        profit_remaining = Config.DAILY_PROFIT_TARGET - realized if Config.DAILY_PROFIT_TARGET > 0 else None

        # Profit lock info
        profit_lock_info = {}
        if self.state.profit_lock_active:
            profit_lock_info = {
                "active": True,
                "lock_level": self.state.get("profit_lock_level"),
                "floor": self.state.profit_lock_floor,
                "buffer": realized - self.state.profit_lock_floor,
            }
        else:
            distance_to_lock = Config.PROFIT_LOCK_THRESHOLD - realized
            profit_lock_info = {
                "active": False,
                "threshold": Config.PROFIT_LOCK_THRESHOLD,
                "distance": distance_to_lock,
            }

        # Trailing drawdown info (based on total P&L)
        drawdown_info = {}
        if Config.TRAILING_DRAWDOWN_ENABLED:
            hwm = self.state.high_water_mark
            drawdown = hwm - total if hwm > 0 else 0
            limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100) if hwm > 0 else 0
            drawdown_info = {
                "enabled": True,
                "high_water_mark": hwm,
                "current_drawdown": drawdown,
                "drawdown_limit": limit,
                "buffer": max(0, limit - drawdown) if limit > 0 else 0,
            }

        return {
            "can_trade": self.can_trade().allowed,
            "lockout": {
                "active": self.state.is_locked_out,
                "reason": self.state.get("lockout_reason", ""),
                "time": self.state.get("lockout_time"),
            },
            "cooldown": {
                "active": False,
                "remaining_seconds": 0,
                "reason": "",
            },
            "pnl": {
                "realized": realized,
                "unrealized": unrealized,
                "total": total,
                "brokerage": brokerage,
                "net_total": net_total,
                "peak": self.state.get("peak_pnl", 0),
            },
            "limits": {
                "daily_max_loss": Config.DAILY_MAX_LOSS,
                "loss_remaining": loss_remaining,
                "loss_used_pct": (1 - loss_remaining / Config.DAILY_MAX_LOSS) * 100 if Config.DAILY_MAX_LOSS > 0 else 0,
                "profit_target": Config.DAILY_PROFIT_TARGET,
                "profit_remaining": profit_remaining,
            },
            "profit_lock": profit_lock_info,
            "trailing_drawdown": drawdown_info,
            "trades": {
                "total": self.state.get("total_trades", 0),
                "winners": self.state.get("winning_trades", 0),
                "losers": self.state.get("losing_trades", 0),
                "consecutive_losses": self.state.consecutive_losses,
                "win_rate": (self.state.get("winning_trades", 0) /
                             max(1, self.state.get("total_trades", 1))) * 100,
                "history": self.state.get("trade_history", []),
            },
            "kill_switch": self.state.get("kill_switch_activated", False),
        }
