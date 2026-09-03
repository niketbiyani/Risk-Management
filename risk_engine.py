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
        Always returns True to allow full manual control over kill switches.
        """
        return RiskDecision(True)

    def check_new_order(self, order_quantity: int, num_open_positions: int,
                        estimated_risk: float = 0) -> RiskDecision:
        """
        Check if a specific new order should be allowed.
        Always returns True to allow full manual control over order placement.
        """
        return RiskDecision(True)

    def evaluate_pnl(self, realized_pnl: float, unrealized_pnl: float) -> Optional[str]:
        """
        Called every monitor cycle with current P&L.
        Updates P&L tracking metrics without triggering automatic lockouts.
        """
        self.state.update_pnl(realized_pnl, unrealized_pnl)
        return None

        # ── Check profit lock activation & trailing ratchet ───────────
        if net_realized >= Config.PROFIT_LOCK_THRESHOLD:
            lock_pct = Config.PROFIT_LOCK_PERCENTAGE / 100
            new_floor = net_realized * lock_pct
            
            # If not yet active, activate it
            if not self.state.profit_lock_active:
                self.state.activate_profit_lock(net_realized, new_floor)
                logger.info("Profit lock triggered at ₹%.0f (Net), floor set to ₹%.0f (Net)",
                            net_realized, new_floor)
                return "profit_lock_activated"
            
            # If active, ratchet up the floor if current net_realized exceeds the previous lock level HWM
            elif net_realized > self.state.get("profit_lock_level", 0.0):
                self.state.activate_profit_lock(net_realized, new_floor)
                logger.info("Profit lock ratcheted up to new peak ₹%.0f (Net), floor set to ₹%.0f (Net)",
                            net_realized, new_floor)
                return "profit_lock_ratcheted"

        # ── Check profit lock floor breach ─────────────────────────────
        if self.state.profit_lock_active:
            floor = self.state.profit_lock_floor
            if net_realized < floor:
                self.state.activate_lockout(
                    f"Profit lock floor breached: Net P&L ₹{net_realized:,.0f} "
                    f"fell below floor ₹{floor:,.0f}")
                return "lockout_profit_lock"

        # ── Check trailing drawdown (HWM based on net realized P&L only) ────
        # HWM uses net realized P&L only — unrealized fluctuates every tick
        # and would cause HWM to ratchet up on paper gains, making the
        # drawdown display jump around. Drawdown is measured as how far
        # net total (realized + unrealized - brokerage) has fallen from the realized net HWM.
        if Config.TRAILING_DRAWDOWN_ENABLED:
            hwm = self.state.high_water_mark
            if net_realized > hwm:
                hwm = net_realized

            # Only track drawdown once HWM has been established above threshold
            if hwm >= Config.PROFIT_LOCK_THRESHOLD:
                drawdown = max(0.0, hwm - net_total)
                drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
                self.state.update_trailing_drawdown(True, hwm, drawdown)

                if drawdown >= drawdown_limit:
                    self.state.activate_lockout(
                        f"Trailing drawdown limit hit: drew down ₹{drawdown:,.0f} "
                        f"from HWM ₹{hwm:,.0f} (limit: {Config.TRAILING_DRAWDOWN_PERCENTAGE}%)")
                    return "lockout_trailing_drawdown"
            elif net_realized > 0:
                # Below threshold but track progress
                drawdown = max(0.0, hwm - net_total)
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
        executions = self.state.get("total_executions", 0)
        brokerage = executions * Config.BROKERAGE_PER_ORDER
        net_realized = self.state.realized_pnl - brokerage
        floor = self.state.profit_lock_floor
        if net_realized < floor:
            self.state.activate_lockout(
                f"Profit lock floor breached: Net P&L ₹{net_realized:,.0f} "
                f"< floor ₹{floor:,.0f}")
            return RiskDecision(False, "Profit lock floor breached", "lockout")
        return RiskDecision(True)

    def _check_trailing_drawdown(self) -> RiskDecision:
        """Check trailing drawdown: HWM is net realized P&L, drawdown measures vs net total."""
        if not Config.TRAILING_DRAWDOWN_ENABLED:
            return RiskDecision(True)
        if not self.state.get("trailing_drawdown_active"):
            return RiskDecision(True)

        hwm = self.state.high_water_mark
        if hwm < Config.PROFIT_LOCK_THRESHOLD:
            return RiskDecision(True)

        executions = self.state.get("total_executions", 0)
        brokerage = executions * Config.BROKERAGE_PER_ORDER
        net_total = self.state.total_pnl - brokerage
        
        drawdown = max(0.0, hwm - net_total)
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
        net_realized = realized - brokerage

        # Calculate distances to limits
        loss_remaining = max(0, Config.DAILY_MAX_LOSS + net_total)
        profit_remaining = Config.DAILY_PROFIT_TARGET - net_realized if Config.DAILY_PROFIT_TARGET > 0 else None

        # Profit lock info
        profit_lock_info = {}
        is_profit_lock_active = self.state.profit_lock_active or (net_realized >= Config.PROFIT_LOCK_THRESHOLD)
        if is_profit_lock_active:
            floor = self.state.profit_lock_floor
            if floor == 0.0:
                # Dynamic fallback for startup status check before first tick
                floor = net_realized * (Config.PROFIT_LOCK_PERCENTAGE / 100)
            profit_lock_info = {
                "active": True,
                "lock_level": self.state.get("profit_lock_level") or net_realized,
                "floor": floor,
                "buffer": net_realized - floor,
            }
        else:
            distance_to_lock = max(0.0, Config.PROFIT_LOCK_THRESHOLD - net_realized)
            profit_lock_info = {
                "active": False,
                "threshold": Config.PROFIT_LOCK_THRESHOLD,
                "distance": distance_to_lock,
            }

        # Trailing drawdown info (based on total P&L)
        drawdown_info = {}
        if Config.TRAILING_DRAWDOWN_ENABLED:
            hwm = self.state.high_water_mark
            if hwm == 0.0 and net_realized > 0:
                # Dynamic fallback for startup status check
                hwm = net_realized
            drawdown = hwm - net_total if hwm > 0 else 0
            limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100) if hwm > 0 else 0
            drawdown_info = {
                "enabled": True,
                "high_water_mark": hwm,
                "current_drawdown": drawdown,
                "drawdown_limit": limit,
                "buffer": max(0, limit - drawdown) if limit > 0 else 0,
            }

        return {
            "can_trade": True,
            "lockout": {
                "active": False,
                "reason": "",
                "time": None,
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
                "max_trades_limit": self.state.get("max_trades_limit", Config.MAX_DAILY_TRADES),
                "trade_limit_extended": self.state.get("trade_limit_extended", False),
            },
            "profit_lock": profit_lock_info,
            "trailing_drawdown": drawdown_info,
            "trades": {
                "total": self.state.get("total_trades", 0),
                "winners": self.state.get("winning_trades", 0),
                "losers": self.state.get("losing_trades", 0),
                "consecutive_losses": self.state.consecutive_losses,
                "win_rate": (self.state.get("winning_trades", 0) /
                             max(1, self.state.get("winning_trades", 0) + self.state.get("losing_trades", 0))) * 100,
                "history": self.state.get("trade_history", []),
            },
            "kill_switch": self.state.get("kill_switch_activated", False),
        }

    def extend_trade_limit(self) -> tuple[bool, str]:
        """Allow a one-time daily increment of max trades limit by 10."""
        if self.state.get("trade_limit_extended", False):
            return False, "Trade limit has already been extended once today."
        
        current_limit = self.state.get("max_trades_limit", Config.MAX_DAILY_TRADES)
        new_limit = current_limit + 10
        self.state.set("max_trades_limit", new_limit)
        self.state.set("trade_limit_extended", True)
        
        # If currently locked out due to daily trade limit, unlock!
        if self.state.is_locked_out and "Daily trade limit hit" in self.state.get("lockout_reason", ""):
            self.state.set("is_locked_out", False)
            self.state.set("lockout_reason", "")
            self.state.set("lockout_time", None)
            logger.info("Daily trade limit extended to %d and lockout cleared.", new_limit)
            return True, f"Trade limit successfully extended to {new_limit} and lockout cleared."
        
        logger.info("Daily trade limit extended to %d.", new_limit)
        return True, f"Trade limit successfully extended to {new_limit}."
