"""
═══════════════════════════════════════════════════════════════════
STRESS TESTS for Risk Management Safety Systems

Vigorous testing of all safety parameters:
1. Daily Max Loss (boundary, unrealized, combined)
2. Profit Lock (activation, floor, interactions)
3. Trailing Drawdown (HWM total P&L, swing scenarios)
4. Order Limits (positions, quantity, trade risk)
5. Profit Target (exact boundary, edge cases)
6. Multi-step realistic trading day simulations
7. Floating-point edge cases
8. State integrity under rapid updates
═══════════════════════════════════════════════════════════════════
"""

import os
import sys
import time
import shutil
import tempfile
from unittest.mock import MagicMock

import pytest

# Isolated test state
os.environ["STATE_ENCRYPTION_KEY"] = ""
os.environ["DHAN_CLIENT_ID"] = "test"
os.environ["DHAN_ACCESS_TOKEN"] = "test"

sys.path.insert(0, os.path.dirname(__file__))

from config import Config

_test_state_dir = tempfile.mkdtemp(prefix="stress_test_")
Config.STATE_DIR = _test_state_dir


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    test_dir = str(tmp_path / "state")
    os.makedirs(test_dir, exist_ok=True)
    Config.STATE_DIR = test_dir
    yield test_dir
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def state():
    from state_manager import StateManager
    return StateManager()


@pytest.fixture
def risk(state):
    from risk_engine import RiskEngine
    return RiskEngine(state)


# Helper to save/restore config
class ConfigOverride:
    def __init__(self, **overrides):
        self.overrides = overrides
        self.originals = {}

    def __enter__(self):
        for key, value in self.overrides.items():
            self.originals[key] = getattr(Config, key)
            setattr(Config, key, value)
        return self

    def __exit__(self, *args):
        for key, value in self.originals.items():
            setattr(Config, key, value)


# ═══════════════════════════════════════════════════════════════════
# 1. DAILY MAX LOSS — EXHAUSTIVE BOUNDARY TESTING
# ═══════════════════════════════════════════════════════════════════

class TestDailyMaxLossStress:
    """Stress test the daily max loss limit from every angle."""

    def test_one_rupee_below_limit(self, risk, state):
        """₹1 below limit should NOT trigger lockout."""
        risk.evaluate_pnl(-(Config.DAILY_MAX_LOSS - 1), 0)
        assert not state.is_locked_out
        assert risk.can_trade().allowed

    def test_one_rupee_at_limit(self, risk, state):
        """Exactly at limit should trigger lockout."""
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"
        assert state.is_locked_out

    def test_one_rupee_above_limit(self, risk, state):
        """₹1 above limit should trigger lockout."""
        result = risk.evaluate_pnl(-(Config.DAILY_MAX_LOSS + 1), 0)
        assert result == "lockout_max_loss"
        assert state.is_locked_out

    def test_combined_realized_unrealized_exactly_at_limit(self, risk, state):
        """Realized + unrealized summing to exactly the limit."""
        half = Config.DAILY_MAX_LOSS / 2
        result = risk.evaluate_pnl(-half, -half)
        assert result == "lockout_max_loss"
        assert state.is_locked_out

    def test_unrealized_only_at_limit(self, risk, state):
        """Pure unrealized loss at limit should trigger."""
        result = risk.evaluate_pnl(0, -Config.DAILY_MAX_LOSS)
        assert result == "lockout_max_loss"
        assert state.is_locked_out

    def test_realized_positive_unrealized_pushes_over(self, risk, state):
        """Positive realized but massive unrealized loss pushes over limit."""
        result = risk.evaluate_pnl(3000, -(Config.DAILY_MAX_LOSS + 3000))
        assert result == "lockout_max_loss"

    def test_loss_recovery_does_not_unlock(self, risk, state):
        """P&L recovery after lockout must NOT unlock."""
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert state.is_locked_out
        # P&L recovers to positive
        risk.evaluate_pnl(10000, 0)
        assert state.is_locked_out
        assert not risk.can_trade().allowed

    def test_rapid_pnl_oscillation(self, risk, state):
        """Rapid oscillation around the limit boundary."""
        for i in range(20):
            pnl = -(Config.DAILY_MAX_LOSS - 100 + (i * 10))
            risk.evaluate_pnl(pnl, 0)
            if state.is_locked_out:
                break
        assert state.is_locked_out, "Must lock out when oscillating across boundary"

    def test_gradual_loss_accumulation(self, risk, state):
        """Gradual losses building up to the limit."""
        steps = 50
        per_step = Config.DAILY_MAX_LOSS / steps
        for i in range(1, steps + 1):
            total = -per_step * i
            risk.evaluate_pnl(total, 0)
            if i < steps:
                assert not state.is_locked_out, f"Should not lock out at step {i}/{steps}"
        assert state.is_locked_out, "Must lock out at final step"

    def test_loss_remaining_tracks_correctly(self, risk, state):
        """Loss remaining should always equal max_loss + total_pnl, clamped at 0."""
        test_pnls = [0, -1000, -2500, -4000, -5000, -6000, -10000]
        for pnl in test_pnls:
            from state_manager import StateManager
            from risk_engine import RiskEngine
            state2 = StateManager()
            risk2 = RiskEngine(state2)
            risk2.evaluate_pnl(pnl, 0)
            status = risk2.get_risk_status()
            expected = max(0, Config.DAILY_MAX_LOSS + pnl)
            assert status["limits"]["loss_remaining"] == expected, \
                f"At pnl={pnl}, loss_remaining should be {expected}"

    def test_loss_used_percentage(self, risk, state):
        """Loss used percentage should be accurate."""
        risk.evaluate_pnl(-2500, 0)  # 50% of 5000
        status = risk.get_risk_status()
        assert abs(status["limits"]["loss_used_pct"] - 50.0) < 0.01

    def test_zero_max_loss_config(self, risk, state):
        """Edge case: DAILY_MAX_LOSS = 0 should lock immediately on any loss."""
        with ConfigOverride(DAILY_MAX_LOSS=0):
            from state_manager import StateManager
            from risk_engine import RiskEngine
            s = StateManager()
            r = RiskEngine(s)
            result = r.evaluate_pnl(0, 0)
            assert result == "lockout_max_loss"

    def test_very_large_loss(self, risk, state):
        """Extreme loss should still trigger correctly."""
        result = risk.evaluate_pnl(-999999, 0)
        assert result == "lockout_max_loss"
        assert state.is_locked_out


# ═══════════════════════════════════════════════════════════════════
# 2. PROFIT LOCK — EXHAUSTIVE TESTING
# ═══════════════════════════════════════════════════════════════════

class TestProfitLockStress:
    """Stress test profit lock activation and floor enforcement."""

    def test_activates_at_exact_threshold(self, risk, state):
        """Profit lock activates when realized == threshold."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            result = risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            assert result == "profit_lock_activated"
            assert state.profit_lock_active

    def test_does_not_activate_below_threshold(self, risk, state):
        """Profit lock should NOT activate below threshold."""
        result = risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD - 1, 0)
        assert result != "profit_lock_activated"
        assert not state.profit_lock_active

    def test_floor_calculation(self, risk, state):
        """Floor should be PROFIT_LOCK_PERCENTAGE of realized at activation."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            expected_floor = Config.PROFIT_LOCK_THRESHOLD * (Config.PROFIT_LOCK_PERCENTAGE / 100)
            assert state.profit_lock_floor == expected_floor

    def test_floor_breach_triggers_lockout(self, risk, state):
        """Realized dropping below floor should trigger lockout."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            floor = state.profit_lock_floor
            result = risk.evaluate_pnl(floor - 1, 0)
            assert result == "lockout_profit_lock"
            assert state.is_locked_out

    def test_realized_at_floor_does_not_trigger(self, risk, state):
        """Realized exactly at floor should NOT trigger lockout."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            floor = state.profit_lock_floor
            result = risk.evaluate_pnl(floor, 0)
            assert result != "lockout_profit_lock"
            assert not state.is_locked_out

    def test_unrealized_does_not_affect_floor_check(self, risk, state):
        """Only realized matters for profit lock floor."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            floor = state.profit_lock_floor
            # Realized above floor, unrealized negative but total still above daily loss
            unrealized = -(floor + 500)  # Keep total above -DAILY_MAX_LOSS
            result = risk.evaluate_pnl(floor + 1000, unrealized)
            assert not state.is_locked_out

    def test_profit_lock_only_activates_once(self, risk, state):
        """Profit lock should activate only once per day."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            r1 = risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            assert r1 == "profit_lock_activated"
            r2 = risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD + 5000, 0)
            assert r2 != "profit_lock_activated"

    def test_profit_lock_floor_with_different_percentages(self, fresh_state):
        """Test floor calculation with various percentages."""
        for pct in [25, 50, 75, 100]:
            with ConfigOverride(PROFIT_LOCK_PERCENTAGE=pct, TRAILING_DRAWDOWN_ENABLED=False):
                # Wipe state between iterations
                for f in os.listdir(Config.STATE_DIR):
                    os.remove(os.path.join(Config.STATE_DIR, f))
                from state_manager import StateManager
                from risk_engine import RiskEngine
                s = StateManager()
                r = RiskEngine(s)
                r.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
                expected = Config.PROFIT_LOCK_THRESHOLD * (pct / 100)
                assert s.profit_lock_floor == expected, f"Floor at {pct}% should be {expected}"

    def test_profit_lock_with_high_threshold(self, risk, state):
        """Very high threshold should delay activation."""
        with ConfigOverride(PROFIT_LOCK_THRESHOLD=100000, TRAILING_DRAWDOWN_ENABLED=False):
            from state_manager import StateManager
            from risk_engine import RiskEngine
            s = StateManager()
            r = RiskEngine(s)
            r.evaluate_pnl(50000, 0)
            assert not s.profit_lock_active
            r.evaluate_pnl(100000, 0)
            assert s.profit_lock_active

    def test_gradual_decline_through_floor(self, risk, state):
        """Gradual realized decline through the floor."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            floor = state.profit_lock_floor
            # Step down gradually
            current = Config.PROFIT_LOCK_THRESHOLD
            while current > floor:
                risk.evaluate_pnl(current, 0)
                assert not state.is_locked_out, f"Should not lock at {current}, floor is {floor}"
                current -= 100
            # Now breach
            risk.evaluate_pnl(floor - 1, 0)
            assert state.is_locked_out


# ═══════════════════════════════════════════════════════════════════
# 3. TRAILING DRAWDOWN — TOTAL P&L HWM STRESS
# ═══════════════════════════════════════════════════════════════════

class TestTrailingDrawdownStress:
    """Stress test trailing drawdown with total P&L HWM."""

    def test_hwm_never_decreases(self, risk, state):
        """HWM must only go up, never down."""
        pnl_sequence = [5000, 10000, 8000, 15000, 12000, 18000, 14000, 20000, 16000]
        max_seen = 0
        for pnl in pnl_sequence:
            risk.evaluate_pnl(pnl, 0)
            if pnl > max_seen:
                max_seen = pnl
            assert state.high_water_mark == max_seen, \
                f"After pnl={pnl}, HWM should be {max_seen} but got {state.high_water_mark}"

    def test_hwm_captures_unrealized_peak(self, risk, state):
        """HWM should capture unrealized peaks."""
        risk.evaluate_pnl(5000, 10000)  # total = 15000
        assert state.high_water_mark == 15000
        risk.evaluate_pnl(5000, 0)  # total = 5000
        assert state.high_water_mark == 15000  # stays at 15000
        risk.evaluate_pnl(5000, 12000)  # total = 17000
        assert state.high_water_mark == 17000  # new peak

    def test_drawdown_only_active_above_threshold(self, risk, state):
        """Drawdown tracking only activates after HWM >= PROFIT_LOCK_THRESHOLD."""
        with ConfigOverride(PROFIT_LOCK_THRESHOLD=999999):
            risk.evaluate_pnl(8000, 0)
            risk.evaluate_pnl(2000, 0)
            assert not state.is_locked_out
            assert not state.get("trailing_drawdown_active")

    def test_drawdown_exact_50pct_lockout(self, risk, state):
        """Drawdown exactly at 50% should trigger lockout."""
        hwm = 20000
        risk.evaluate_pnl(hwm, 0)
        # Drawdown limit = 50% of 20000 = 10000
        # Lockout when drawdown >= 10000, i.e. total <= 10000
        result = risk.evaluate_pnl(10000, 0)
        assert result == "lockout_trailing_drawdown"

    def test_drawdown_one_below_50pct_no_lockout(self, risk, state):
        """Drawdown 1 below 50% should NOT trigger lockout."""
        hwm = 20000
        risk.evaluate_pnl(hwm, 0)
        risk.evaluate_pnl(10001, 0)
        assert not state.is_locked_out

    def test_drawdown_with_different_percentages(self, fresh_state):
        """Test drawdown with various percentage configs."""
        for pct in [25, 50, 75]:
            with ConfigOverride(TRAILING_DRAWDOWN_PERCENTAGE=pct,
                               PROFIT_LOCK_THRESHOLD=999999):
                # Wipe state between iterations
                for f in os.listdir(Config.STATE_DIR):
                    os.remove(os.path.join(Config.STATE_DIR, f))
                from state_manager import StateManager
                from risk_engine import RiskEngine
                s = StateManager()
                r = RiskEngine(s)
                hwm = 20000
                r.evaluate_pnl(hwm, 0)
                limit = hwm * (pct / 100)
                trigger = hwm - limit
                # Just above trigger — no lockout expected
                r.evaluate_pnl(trigger + 1, 0)
                assert not s.is_locked_out, f"Should not lock at {trigger+1} with {pct}% DD"

    def test_unrealized_spike_and_crash(self, risk, state):
        """Unrealized spikes high then crashes — should lockout if drawdown exceeded."""
        # Unrealized spikes to 20000 total
        risk.evaluate_pnl(0, 20000)
        assert state.high_water_mark == 20000
        # Unrealized crashes to 0
        result = risk.evaluate_pnl(0, 0)
        # Drawdown = 20000, limit = 10000, 20000 >= 10000 → lockout
        assert state.is_locked_out

    def test_realized_profit_then_unrealized_loss(self, risk, state):
        """Realized profit high, then unrealized loss creates drawdown."""
        risk.evaluate_pnl(15000, 0)
        assert state.high_water_mark == 15000
        # Add unrealized loss
        risk.evaluate_pnl(15000, -8000)  # total = 7000
        # Drawdown = 15000 - 7000 = 8000
        # Limit = 15000 * 50% = 7500
        # 8000 >= 7500 → lockout
        assert state.is_locked_out

    def test_user_scenario_18k_close_15k(self, risk, state):
        """Exact user scenario: unrealized 18K, close at 15K."""
        with ConfigOverride(PROFIT_LOCK_THRESHOLD=999999):
            risk.evaluate_pnl(0, 18000)
            assert state.high_water_mark == 18000
            risk.evaluate_pnl(15000, 0)
            assert state.high_water_mark == 18000
            status = risk.get_risk_status()
            dd = status["trailing_drawdown"]
            assert dd["current_drawdown"] == 3000
            assert dd["drawdown_limit"] == 9000
            assert dd["buffer"] == 6000
            assert not state.is_locked_out

    def test_hwm_with_mixed_realized_unrealized(self, risk, state):
        """HWM tracking with both realized and unrealized changing."""
        scenarios = [
            (2000, 1000, 3000),   # total=3000, hwm=3000
            (3000, 5000, 8000),   # total=8000, hwm=8000
            (3000, 2000, 8000),   # total=5000, hwm=8000
            (6000, 4000, 10000),  # total=10000, hwm=10000
            (6000, 0, 10000),     # total=6000, hwm=10000
            (8000, 5000, 13000),  # total=13000, hwm=13000
        ]
        for realized, unrealized, expected_hwm in scenarios:
            risk.evaluate_pnl(realized, unrealized)
            assert state.high_water_mark == expected_hwm, \
                f"With r={realized}, u={unrealized}: HWM should be {expected_hwm}, got {state.high_water_mark}"

    def test_drawdown_buffer_always_non_negative(self, risk, state):
        """Drawdown buffer should never go negative in status."""
        risk.evaluate_pnl(15000, 0)
        risk.evaluate_pnl(2000, 0)  # Massive drawdown
        status = risk.get_risk_status()
        dd = status["trailing_drawdown"]
        assert dd["buffer"] >= 0

    def test_100_rapid_pnl_updates(self, risk, state):
        """100 rapid P&L updates should maintain HWM integrity."""
        import random
        random.seed(42)
        max_total = 0
        for _ in range(100):
            r = random.uniform(-2000, 5000)
            u = random.uniform(-3000, 8000)
            total = r + u
            if total > max_total:
                max_total = total
            risk.evaluate_pnl(r, u)
            assert state.high_water_mark >= max_total - 0.01, \
                f"HWM {state.high_water_mark} should be >= {max_total}"


# ═══════════════════════════════════════════════════════════════════
# 4. ORDER LIMITS — STRESS TESTING
# ═══════════════════════════════════════════════════════════════════

class TestOrderLimitsStress:
    """Stress test all order-level risk checks."""

    def test_max_positions_exact_boundary(self, risk, state):
        """At exactly max positions: blocked. At max-1: allowed."""
        d1 = risk.check_new_order(1, Config.MAX_OPEN_POSITIONS - 1, 100)
        assert d1.allowed
        d2 = risk.check_new_order(1, Config.MAX_OPEN_POSITIONS, 100)
        assert not d2.allowed
        assert d2.action == "block"

    def test_max_quantity_exact_boundary(self, risk, state):
        """At exactly max qty: allowed. At max+1: blocked."""
        d1 = risk.check_new_order(Config.MAX_ORDER_QUANTITY, 0, 100)
        assert d1.allowed
        d2 = risk.check_new_order(Config.MAX_ORDER_QUANTITY + 1, 0, 100)
        assert not d2.allowed
        assert "exceeds max" in d2.reason

    def test_max_trade_risk_exact_boundary(self, risk, state):
        """At exactly max risk: allowed. At max+1: blocked."""
        d1 = risk.check_new_order(1, 0, Config.MAX_SINGLE_TRADE_RISK)
        assert d1.allowed
        d2 = risk.check_new_order(1, 0, Config.MAX_SINGLE_TRADE_RISK + 1)
        assert not d2.allowed
        assert "Trade risk" in d2.reason

    def test_zero_risk_order_allowed(self, risk, state):
        """Zero risk (closing trade) should always be allowed."""
        d = risk.check_new_order(1, 0, 0)
        assert d.allowed

    def test_order_check_calls_can_trade(self, risk, state):
        """Order check should first verify general trading permission."""
        state.activate_lockout("test")
        d = risk.check_new_order(1, 0, 100)
        assert not d.allowed
        assert d.action == "lockout"

    def test_all_limits_pass(self, risk, state):
        """Order within all limits should pass."""
        d = risk.check_new_order(
            order_quantity=10,
            num_open_positions=0,
            estimated_risk=500,
        )
        assert d.allowed

    def test_multiple_violations(self, risk, state):
        """Order violating multiple limits — first violation caught."""
        # qty too high, positions maxed, risk too high
        d = risk.check_new_order(
            order_quantity=Config.MAX_ORDER_QUANTITY + 1000,
            num_open_positions=Config.MAX_OPEN_POSITIONS + 5,
            estimated_risk=Config.MAX_SINGLE_TRADE_RISK + 10000,
        )
        assert not d.allowed
        # Should hit max positions first (checked before qty in the code)
        assert d.action == "block"


# ═══════════════════════════════════════════════════════════════════
# 5. PROFIT TARGET — STRESS TESTING
# ═══════════════════════════════════════════════════════════════════

class TestProfitTargetStress:
    """Stress test the daily profit target lockout."""

    def test_exact_profit_target_lockout(self, risk, state):
        """Realized at exact profit target triggers lockout via can_trade."""
        risk.evaluate_pnl(Config.DAILY_PROFIT_TARGET, 0)
        decision = risk.can_trade()
        assert not decision.allowed

    def test_just_below_profit_target(self, risk, state):
        """₹1 below profit target should still allow trading."""
        risk.evaluate_pnl(Config.DAILY_PROFIT_TARGET - 1, 0)
        # Need to avoid profit lock triggering lockout
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False, PROFIT_LOCK_THRESHOLD=999999):
            from state_manager import StateManager
            from risk_engine import RiskEngine
            s = StateManager()
            r = RiskEngine(s)
            r.evaluate_pnl(Config.DAILY_PROFIT_TARGET - 1, 0)
            decision = r.can_trade()
            assert decision.allowed

    def test_profit_target_only_checks_realized(self, risk, state):
        """Profit target should check realized only (not unrealized)."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False, PROFIT_LOCK_THRESHOLD=999999):
            from state_manager import StateManager
            from risk_engine import RiskEngine
            s = StateManager()
            r = RiskEngine(s)
            # Realized below target but total above
            r.evaluate_pnl(Config.DAILY_PROFIT_TARGET - 1000, 5000)
            decision = r.can_trade()
            assert decision.allowed

    def test_profit_target_disabled(self, risk, state):
        """When profit target is 0, it should not trigger."""
        with ConfigOverride(DAILY_PROFIT_TARGET=0, TRAILING_DRAWDOWN_ENABLED=False,
                           PROFIT_LOCK_THRESHOLD=999999):
            from state_manager import StateManager
            from risk_engine import RiskEngine
            s = StateManager()
            r = RiskEngine(s)
            r.evaluate_pnl(50000, 0)
            decision = r.can_trade()
            assert decision.allowed


# ═══════════════════════════════════════════════════════════════════
# 6. ORDER INTERCEPTOR — STRESS TESTING
# ═══════════════════════════════════════════════════════════════════

class TestOrderInterceptorStress:
    """Stress test the order interceptor risk estimation."""

    def _make_interceptor(self):
        from order_interceptor import OrderInterceptor
        return OrderInterceptor(MagicMock(), MagicMock(), MagicMock())

    def test_closing_long_position(self):
        """Selling to close a long position = 0 risk."""
        interceptor = self._make_interceptor()
        positions = [{"securityId": "12345", "netQty": 10}]
        risk = interceptor._estimate_order_risk(
            "12345", "NSE_FNO", "SELL", 10, 100, positions
        )
        assert risk == 0

    def test_closing_short_position(self):
        """Buying to close a short position = 0 risk."""
        interceptor = self._make_interceptor()
        positions = [{"securityId": "12345", "netQty": -10}]
        risk = interceptor._estimate_order_risk(
            "12345", "NSE_FNO", "BUY", 10, 100, positions
        )
        assert risk == 0

    def test_new_position_with_sl(self):
        """New position with SL: risk = |entry - SL| * qty."""
        interceptor = self._make_interceptor()
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 100, 200, [], sl_price=180
        )
        assert risk == 20 * 100  # 2000

    def test_tight_stop_loss(self):
        """Very tight SL should calculate small risk."""
        interceptor = self._make_interceptor()
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 100, 200, [], sl_price=199
        )
        assert risk == 1 * 100  # 100

    def test_wide_stop_loss(self):
        """Wide SL should calculate large risk."""
        interceptor = self._make_interceptor()
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 100, 200, [], sl_price=100
        )
        assert risk == 100 * 100  # 10000

    def test_option_buy_risk_equals_premium(self):
        """Buying options: risk = premium * qty."""
        interceptor = self._make_interceptor()
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 50, 100, []
        )
        assert risk == 100 * 50  # 5000

    def test_option_sell_risk_capped(self):
        """Selling options: risk capped at MAX_SINGLE_TRADE_RISK."""
        interceptor = self._make_interceptor()
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "SELL", 1000, 500, []
        )
        assert risk <= Config.MAX_SINGLE_TRADE_RISK

    def test_equity_risk_estimate(self):
        """Equity risk should use 5% adverse move heuristic."""
        interceptor = self._make_interceptor()
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_EQ", "BUY", 10, 1000, []
        )
        assert risk == 10 * 1000 * 0.05  # 500

    def test_market_order_no_price(self):
        """Market order with no price should use fallback."""
        interceptor = self._make_interceptor()
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 50, 0, []
        )
        assert risk == Config.MAX_SINGLE_TRADE_RISK * 0.5

    def test_blocked_order_after_lockout(self, risk, state):
        """Full order flow blocked after lockout."""
        from order_interceptor import OrderInterceptor
        api = MagicMock()
        api.get_positions.return_value = []
        interceptor = OrderInterceptor(api, risk, state)
        state.activate_lockout("test lockout")
        result = interceptor.place_order(
            security_id="12345", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=100,
            order_type="MARKET", product_type="MARGIN", price=100,
        )
        assert result["status"] == "BLOCKED"
        api.place_order.assert_not_called()

    def test_approved_order_calls_api(self, risk, state):
        """Approved order should call the underlying API."""
        from order_interceptor import OrderInterceptor
        api = MagicMock()
        api.get_positions.return_value = []
        api.place_order.return_value = {"orderId": "123", "status": "PLACED"}
        interceptor = OrderInterceptor(api, risk, state)
        result = interceptor.place_order(
            security_id="12345", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=10,
            order_type="MARKET", product_type="MARGIN", price=50,
        )
        api.place_order.assert_called_once()
        assert result["status"] == "PLACED"


# ═══════════════════════════════════════════════════════════════════
# 7. MULTI-STEP REALISTIC SCENARIOS
# ═══════════════════════════════════════════════════════════════════

class TestRealisticScenarios:
    """Full trading day simulations testing system integrity."""

    def test_bad_morning_daily_loss(self, risk, state):
        """Simulate a bad morning hitting daily loss limit."""
        trades = [
            (-500, 0), (-800, 0), (-1200, 0), (-700, 0),  # realized losses
            (-3200, -500),  # open position losing
            (-3200, -1800),  # position gets worse
        ]
        locked = False
        for realized, unrealized in trades:
            result = risk.evaluate_pnl(realized, unrealized)
            if result == "lockout_max_loss":
                locked = True
                break
        assert locked, "Should have hit daily loss limit"
        assert state.is_locked_out

    def test_good_morning_profit_lock_afternoon_decline(self, risk, state):
        """Morning profit, profit lock activates, afternoon decline triggers drawdown."""
        # Morning gains
        risk.evaluate_pnl(3000, 0)
        risk.evaluate_pnl(7000, 0)
        result = risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        assert result == "profit_lock_activated"
        floor = state.profit_lock_floor

        # Continue higher
        risk.evaluate_pnl(15000, 0)
        risk.evaluate_pnl(18000, 0)
        hwm = state.high_water_mark
        assert hwm == 18000

        # Afternoon decline
        risk.evaluate_pnl(14000, 0)
        assert not state.is_locked_out  # drawdown = 4000, limit = 9000

        risk.evaluate_pnl(10000, 0)
        assert not state.is_locked_out  # drawdown = 8000, limit = 9000

        result = risk.evaluate_pnl(9000, 0)
        assert state.is_locked_out  # drawdown = 9000 >= limit 9000

    def test_unrealized_peak_then_close_then_loss(self, risk, state):
        """Unrealized peaks, positions closed, then losses erode."""
        # Build unrealized
        risk.evaluate_pnl(5000, 10000)  # total=15000, HWM=15000
        risk.evaluate_pnl(5000, 15000)  # total=20000, HWM=20000
        assert state.high_water_mark == 20000

        # Close positions at realized 17000
        risk.evaluate_pnl(17000, 0)  # total=17000, drawdown=3000
        assert not state.is_locked_out

        # Start losing
        risk.evaluate_pnl(14000, 0)  # drawdown=6000
        assert not state.is_locked_out

        result = risk.evaluate_pnl(10000, 0)  # drawdown=10000, limit=10000
        assert result == "lockout_trailing_drawdown"
        assert state.is_locked_out

    def test_scalping_day_many_small_trades(self, risk, state):
        """50 small scalp trades, building P&L slowly."""
        realized = 0
        for i in range(50):
            pnl = 200 if i % 3 != 0 else -150
            realized += pnl
            risk.evaluate_pnl(realized, 0)
            risk.evaluate_trade_result(pnl, {"security_id": f"T{i}"})
        assert state.get("total_trades") == 50
        assert state.get("winning_trades") + state.get("losing_trades") == 50

    def test_intraday_unrealized_swing_no_close(self, risk, state):
        """Unrealized swings wildly without closing — HWM tracks peaks."""
        with ConfigOverride(PROFIT_LOCK_THRESHOLD=999999):
            unrealized_sequence = [5000, 12000, 8000, 18000, 10000, 15000, 7000]
            max_seen = 0
            for u in unrealized_sequence:
                risk.evaluate_pnl(0, u)
                if u > max_seen:
                    max_seen = u
                assert state.high_water_mark == max_seen

    def test_profit_lock_saves_minimum(self, risk, state):
        """Profit lock + trailing drawdown: trader keeps at least floor amount."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            floor = state.profit_lock_floor
            # Gradual decline toward floor
            current = Config.PROFIT_LOCK_THRESHOLD
            while current > floor:
                risk.evaluate_pnl(current, 0)
                assert not state.is_locked_out, f"Should not lock at {current}, floor is {floor}"
                current -= 500
            # Now breach the floor
            result = risk.evaluate_pnl(floor - 1, 0)
            assert result == "lockout_profit_lock"
            assert state.is_locked_out
            reason = state.get("lockout_reason")
            assert "Profit lock" in reason


# ═══════════════════════════════════════════════════════════════════
# 8. STATE INTEGRITY UNDER STRESS
# ═══════════════════════════════════════════════════════════════════

class TestStateIntegrityStress:
    """Test state consistency under rapid updates."""

    def test_50_rapid_save_load_cycles(self, fresh_state):
        """50 rapid save/load cycles should maintain data integrity."""
        from state_manager import StateManager
        s = StateManager()
        for i in range(50):
            s.update_pnl(float(i * 100), float(i * 50))
        s2 = StateManager()
        assert s2.realized_pnl == 4900
        assert s2.unrealized_pnl == 2450

    def test_lockout_persists_across_reloads(self, fresh_state):
        """Lockout must persist across any number of reloads."""
        from state_manager import StateManager
        s = StateManager()
        s.activate_lockout("stress test lockout")
        for _ in range(10):
            s = StateManager()
            assert s.is_locked_out, "Lockout must persist"
            assert s.get("lockout_reason") == "stress test lockout"

    def test_hwm_persists_across_reloads(self, fresh_state):
        """HWM must persist across reloads."""
        from state_manager import StateManager
        s = StateManager()
        s.update_pnl(10000, 5000)  # total=15000
        s2 = StateManager()
        assert s2.high_water_mark == 15000

    def test_trade_history_integrity(self, fresh_state):
        """Trade history should maintain order and content after reloads."""
        from state_manager import StateManager
        s = StateManager()
        for i in range(30):
            s.record_trade({"pnl": i * 100, "security_id": f"SEC{i}"})
        s2 = StateManager()
        history = s2.get("trade_history")
        assert len(history) == 30
        assert history[0]["pnl"] == 0
        assert history[-1]["pnl"] == 2900

    def test_corrupted_file_recovery(self, fresh_state):
        """Corrupted state file should result in clean reset."""
        from state_manager import StateManager
        s = StateManager()
        s.activate_lockout("important lockout")
        # Corrupt the file
        with open(s._state_file, "wb") as f:
            f.write(b"CORRUPTED" * 100)
        # Reload should get fresh state
        s2 = StateManager()
        assert not s2.is_locked_out
        assert s2.realized_pnl == 0

    def test_profit_lock_state_persists(self, fresh_state):
        """Profit lock activation persists across reloads."""
        from state_manager import StateManager
        from risk_engine import RiskEngine
        s = StateManager()
        r = RiskEngine(s)
        r.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        assert s.profit_lock_active

        s2 = StateManager()
        assert s2.profit_lock_active
        assert s2.profit_lock_floor == Config.PROFIT_LOCK_THRESHOLD * (Config.PROFIT_LOCK_PERCENTAGE / 100)


# ═══════════════════════════════════════════════════════════════════
# 9. FLOATING POINT & EDGE CASES
# ═══════════════════════════════════════════════════════════════════

class TestFloatingPointEdgeCases:
    """Test for floating-point precision issues."""

    def test_fractional_pnl_at_boundary(self, risk, state):
        """Fractional P&L near the boundary."""
        risk.evaluate_pnl(-4999.99, 0)
        assert not state.is_locked_out
        risk.evaluate_pnl(-5000.01, 0)
        assert state.is_locked_out

    def test_very_small_unrealized(self, risk, state):
        """Very small unrealized should not cause issues."""
        risk.evaluate_pnl(0, 0.001)
        assert not state.is_locked_out
        assert state.high_water_mark == 0.001

    def test_negative_zero(self, risk, state):
        """Negative zero should behave like zero."""
        risk.evaluate_pnl(-0.0, 0)
        assert not state.is_locked_out

    def test_large_numbers(self, risk, state):
        """Very large P&L numbers should work correctly."""
        risk.evaluate_pnl(1000000, 500000)
        assert state.high_water_mark == 1500000
        assert state.total_pnl == 1500000


# ═══════════════════════════════════════════════════════════════════
# 10. RISK STATUS REPORTING
# ═══════════════════════════════════════════════════════════════════

class TestRiskStatusReporting:
    """Test that status reporting is always accurate."""

    def test_status_reflects_current_state(self, risk, state):
        """Status should always reflect the latest state."""
        risk.evaluate_pnl(5000, 2000)
        status = risk.get_risk_status()
        assert status["pnl"]["realized"] == 5000
        assert status["pnl"]["unrealized"] == 2000
        assert status["pnl"]["total"] == 7000

    def test_status_after_lockout(self, risk, state):
        """Status should show lockout details."""
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        status = risk.get_risk_status()
        assert status["can_trade"] is False
        assert status["lockout"]["active"] is True
        assert "Daily loss" in status["lockout"]["reason"]

    def test_status_drawdown_info(self, risk, state):
        """Drawdown info should be accurate in status."""
        risk.evaluate_pnl(15000, 0)
        risk.evaluate_pnl(12000, 0)
        status = risk.get_risk_status()
        dd = status["trailing_drawdown"]
        assert dd["enabled"] is True
        assert dd["high_water_mark"] == 15000
        assert dd["current_drawdown"] == 3000
        assert dd["drawdown_limit"] == 7500  # 50% of 15000

    def test_status_profit_lock_info_inactive(self, risk, state):
        """Profit lock info when not yet activated."""
        risk.evaluate_pnl(5000, 0)
        status = risk.get_risk_status()
        pl = status["profit_lock"]
        assert pl["active"] is False
        assert pl["threshold"] == Config.PROFIT_LOCK_THRESHOLD
        assert pl["distance"] == Config.PROFIT_LOCK_THRESHOLD - 5000

    def test_status_profit_lock_info_active(self, risk, state):
        """Profit lock info when activated."""
        with ConfigOverride(TRAILING_DRAWDOWN_ENABLED=False):
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD + 2000, 0)
            status = risk.get_risk_status()
            pl = status["profit_lock"]
            assert pl["active"] is True
            floor = Config.PROFIT_LOCK_THRESHOLD * (Config.PROFIT_LOCK_PERCENTAGE / 100)
            assert pl["floor"] == floor
            assert pl["buffer"] == (Config.PROFIT_LOCK_THRESHOLD + 2000) - floor

    def test_trade_stats_accuracy(self, risk, state):
        """Trade statistics should be accurate."""
        risk.evaluate_trade_result(500, {"security_id": "T1"})
        risk.evaluate_trade_result(-200, {"security_id": "T2"})
        risk.evaluate_trade_result(300, {"security_id": "T3"})
        status = risk.get_risk_status()
        assert status["trades"]["total"] == 3
        assert status["trades"]["winners"] == 2
        assert status["trades"]["losers"] == 1
        assert abs(status["trades"]["win_rate"] - 66.67) < 1

    def test_status_all_keys_present(self, risk, state):
        """Every expected key should be present in status."""
        risk.evaluate_pnl(5000, 1000)
        status = risk.get_risk_status()
        # Top-level keys
        for key in ["can_trade", "lockout", "cooldown", "pnl", "limits",
                     "profit_lock", "trailing_drawdown", "trades", "kill_switch"]:
            assert key in status, f"Missing top-level key: {key}"
        # P&L sub-keys
        for key in ["realized", "unrealized", "total", "peak"]:
            assert key in status["pnl"], f"Missing pnl.{key}"
        # Limits sub-keys
        for key in ["daily_max_loss", "loss_remaining", "loss_used_pct"]:
            assert key in status["limits"], f"Missing limits.{key}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
