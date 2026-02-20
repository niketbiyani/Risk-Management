"""
Comprehensive tests for the risk management safety systems:
1. Daily Loss Limit
2. Loss Buffer / Cooldowns
3. Trailing Drawdown
4. Interactions / Conflicts between systems
"""

import os
import sys
import time
import shutil
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# Use a temp dir for state so tests don't affect real state
_test_state_dir = tempfile.mkdtemp(prefix="risk_test_")

# Patch config BEFORE importing modules
os.environ["STATE_ENCRYPTION_KEY"] = ""
os.environ["DHAN_CLIENT_ID"] = "test"
os.environ["DHAN_ACCESS_TOKEN"] = "test"

sys.path.insert(0, os.path.dirname(__file__))

from config import Config
Config.STATE_DIR = _test_state_dir


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    """Reset state dir for each test."""
    test_dir = str(tmp_path / "state")
    os.makedirs(test_dir, exist_ok=True)
    Config.STATE_DIR = test_dir
    yield test_dir
    # Cleanup
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def state():
    from state_manager import StateManager
    return StateManager()


@pytest.fixture
def risk(state):
    from risk_engine import RiskEngine
    return RiskEngine(state)


# ═══════════════════════════════════════════════════════════════════
# 1. DAILY LOSS LIMIT TESTS
# ═══════════════════════════════════════════════════════════════════

class TestDailyLossLimit:

    def test_normal_pnl_allows_trading(self, risk, state):
        """P&L within limits should allow trading."""
        risk.evaluate_pnl(0, 0)
        assert risk.can_trade().allowed is True

    def test_small_loss_allows_trading(self, risk, state):
        """Small loss should still allow trading."""
        risk.evaluate_pnl(-1000, 0)
        assert risk.can_trade().allowed is True
        assert not state.is_locked_out

    def test_loss_at_limit_triggers_lockout(self, risk, state):
        """Loss exactly at DAILY_MAX_LOSS should trigger lockout."""
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"
        assert state.is_locked_out is True

    def test_loss_beyond_limit_triggers_lockout(self, risk, state):
        """Loss exceeding DAILY_MAX_LOSS should trigger lockout."""
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS - 1000, 0)
        assert result == "lockout_max_loss"
        assert state.is_locked_out is True

    def test_unrealized_loss_counts(self, risk, state):
        """Unrealized losses should count toward daily limit."""
        # Realized = -2000, unrealized = -3000, total = -5000
        result = risk.evaluate_pnl(-2000, -3000)
        assert result == "lockout_max_loss"
        assert state.is_locked_out is True

    def test_lockout_is_irreversible(self, risk, state):
        """Once locked out, even positive P&L shouldn't unlock."""
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert state.is_locked_out is True
        # Even if P&L recovers, lockout stays
        risk.evaluate_pnl(5000, 0)
        assert state.is_locked_out is True
        assert risk.can_trade().allowed is False

    def test_lockout_reason_is_stored(self, risk, state):
        """Lockout reason should be persisted."""
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        reason = state.get("lockout_reason")
        assert "Daily loss limit" in reason
        assert str(int(Config.DAILY_MAX_LOSS)) in reason.replace(",", "")

    def test_can_trade_returns_lockout_action(self, risk, state):
        """can_trade should return lockout action when locked out."""
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        decision = risk.can_trade()
        assert decision.allowed is False
        assert decision.action == "lockout"

    def test_loss_buffer_remaining_calculation(self, risk, state):
        """Loss remaining should be correctly calculated."""
        risk.evaluate_pnl(-2000, -1000)
        status = risk.get_risk_status()
        # total_pnl = -3000, loss_remaining = 5000 + (-3000) = 2000
        assert status["limits"]["loss_remaining"] == Config.DAILY_MAX_LOSS - 3000

    def test_loss_remaining_does_not_go_negative(self, risk, state):
        """Loss remaining should be clamped at 0."""
        risk.evaluate_pnl(-6000, 0)
        status = risk.get_risk_status()
        loss_remaining = status["limits"]["loss_remaining"]
        assert loss_remaining == 0, \
            f"loss_remaining should be clamped to 0, got {loss_remaining}"


# ═══════════════════════════════════════════════════════════════════
# 2. COOLDOWN / LOSS BUFFER TESTS
# ═══════════════════════════════════════════════════════════════════

class TestCooldownSystem:

    def test_single_loss_triggers_cooldown(self, risk, state):
        """A large loss (>=80% of MAX_SINGLE_TRADE_RISK) should trigger cooldown."""
        large_loss = -Config.MAX_SINGLE_TRADE_RISK * 0.8
        risk.evaluate_trade_result(large_loss, {"security_id": "TEST"})
        assert state.is_cooling_down is True
        assert state.cooldown_remaining > 0

    def test_small_loss_no_cooldown(self, risk, state):
        """A small loss should NOT trigger cooldown."""
        small_loss = -Config.MAX_SINGLE_TRADE_RISK * 0.5
        risk.evaluate_trade_result(small_loss, {"security_id": "TEST"})
        assert state.is_cooling_down is False

    def test_consecutive_losses_trigger_cooldown(self, risk, state):
        """N consecutive losses should trigger extended cooldown."""
        for i in range(Config.CONSECUTIVE_LOSS_COUNT):
            risk.evaluate_trade_result(-100, {"security_id": f"TEST{i}"})
        assert state.is_cooling_down is True
        # Should be the longer cooldown
        assert state.cooldown_remaining > Config.COOLDOWN_AFTER_LOSS

    def test_win_resets_consecutive_losses(self, risk, state):
        """A winning trade should reset the consecutive loss counter."""
        # Record 2 losses
        risk.evaluate_trade_result(-100, {"security_id": "T1"})
        risk.evaluate_trade_result(-100, {"security_id": "T2"})
        assert state.consecutive_losses == 2
        # Win resets counter
        risk.evaluate_trade_result(100, {"security_id": "T3"})
        assert state.consecutive_losses == 0

    def test_cooldown_blocks_trading(self, risk, state):
        """Trading should be blocked during cooldown."""
        state.activate_cooldown(300, "test cooldown")
        decision = risk.can_trade()
        assert decision.allowed is False
        assert decision.action == "cooldown"

    def test_cooldown_auto_expires(self, risk, state):
        """Cooldown should auto-expire after duration."""
        # Set cooldown that already expired
        state._state["is_cooling_down"] = True
        state._state["cooldown_until"] = time.time() - 10  # 10 seconds ago
        state._save()
        assert state.is_cooling_down is False
        assert risk.can_trade().allowed is True

    def test_cooldown_remaining_accuracy(self, risk, state):
        """Cooldown remaining should be accurate."""
        duration = 300
        state.activate_cooldown(duration, "test")
        remaining = state.cooldown_remaining
        assert abs(remaining - duration) <= 2  # Allow 2s tolerance

    def test_longer_cooldown_overwrites_shorter(self, risk, state):
        """If both cooldown triggers fire, the longer one should win."""
        large_loss = -Config.MAX_SINGLE_TRADE_RISK * 0.8
        # Record enough losses to trigger consecutive cooldown too
        for i in range(Config.CONSECUTIVE_LOSS_COUNT - 1):
            risk.evaluate_trade_result(-100, {"security_id": f"T{i}"})
        # This loss triggers BOTH large loss cooldown AND consecutive cooldown
        risk.evaluate_trade_result(large_loss, {"security_id": "LAST"})
        # Consecutive cooldown (600s) should overwrite single loss cooldown (300s)
        remaining = state.cooldown_remaining
        assert remaining > Config.COOLDOWN_AFTER_LOSS

    def test_cooldown_does_not_trigger_on_win(self, risk, state):
        """Winning trades should never trigger cooldown."""
        risk.evaluate_trade_result(5000, {"security_id": "TEST"})
        assert state.is_cooling_down is False

    def test_check_new_trades_calls_evaluate_trade_result(self):
        """Monitor._check_new_trades should call risk.evaluate_trade_result."""
        from monitor import PositionMonitor
        import inspect
        source = inspect.getsource(PositionMonitor._check_new_trades)
        has_evaluate_call = "evaluate_trade_result" in source
        assert has_evaluate_call, \
            "_check_new_trades must call evaluate_trade_result for cooldowns to work"


# ═══════════════════════════════════════════════════════════════════
# 3. TRAILING DRAWDOWN TESTS
# ═══════════════════════════════════════════════════════════════════

class TestTrailingDrawdown:

    def test_hwm_tracks_realized_pnl(self, risk, state):
        """High water mark should track peak realized P&L."""
        risk.evaluate_pnl(5000, 0)
        assert state.high_water_mark == 5000
        risk.evaluate_pnl(10000, 0)
        assert state.high_water_mark == 10000
        # HWM shouldn't decrease
        risk.evaluate_pnl(8000, 0)
        assert state.high_water_mark == 10000

    def test_no_drawdown_below_threshold(self, risk, state):
        """Trailing drawdown should not trigger before profit lock threshold."""
        # Set HWM below threshold
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD - 1000, 0)
        # Drop to 0
        risk.evaluate_pnl(0, 0)
        assert not state.is_locked_out

    def test_drawdown_triggers_lockout_above_threshold(self, risk, state):
        """Trailing drawdown should trigger lockout when HWM >= threshold."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000  # e.g., 15000
        risk.evaluate_pnl(hwm, 0)
        assert state.high_water_mark == hwm

        # Calculate what realized P&L would trigger drawdown
        drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        trigger_level = hwm - drawdown_limit  # e.g., 15000 - 7500 = 7500

        # Just above trigger - should not lockout
        risk.evaluate_pnl(trigger_level + 1, 0)
        assert not state.is_locked_out

        # At trigger level - should lockout
        result = risk.evaluate_pnl(trigger_level, 0)
        assert result == "lockout_trailing_drawdown"
        assert state.is_locked_out is True

    def test_drawdown_disabled_skips_check(self, risk, state):
        """When trailing drawdown is disabled, it should not trigger."""
        original_dd = Config.TRAILING_DRAWDOWN_ENABLED
        original_threshold = Config.PROFIT_LOCK_THRESHOLD
        try:
            Config.TRAILING_DRAWDOWN_ENABLED = False
            # Also set profit lock threshold very high to avoid it interfering
            Config.PROFIT_LOCK_THRESHOLD = 999999
            risk.evaluate_pnl(20000, 0)
            risk.evaluate_pnl(5000, 0)
            assert not state.is_locked_out
        finally:
            Config.TRAILING_DRAWDOWN_ENABLED = original_dd
            Config.PROFIT_LOCK_THRESHOLD = original_threshold

    def test_drawdown_check_when_realized_drops_to_zero(self, risk, state):
        """When realized drops to 0 from a high HWM, some safety system must trigger lockout.

        With default config, profit lock floor catches this first. The key
        guarantee is that the trader is ALWAYS protected - no gap exists.
        """
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000  # e.g., 15000
        risk.evaluate_pnl(hwm, 0)
        assert state.high_water_mark == hwm

        # Realized drops to 0 — either profit lock floor or trailing drawdown must catch this
        result = risk.evaluate_pnl(0, 0)
        assert state.is_locked_out, \
            "Either trailing drawdown or profit lock must trigger lockout when realized drops to 0"

    def test_drawdown_triggers_without_profit_lock(self, risk, state):
        """Trailing drawdown should work independently when profit lock floor is bypassed."""
        # Directly set up state as if HWM was established above threshold
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000  # 15000
        state.update_pnl(hwm, 0)
        state.update_trailing_drawdown(True, hwm, 0)
        # Deactivate profit lock to isolate trailing drawdown
        state._state["profit_lock_active"] = False
        state._save()

        # Drop realized to a level that breaches trailing drawdown
        drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)  # 7500
        trigger_level = hwm - drawdown_limit  # 7500

        result = risk.evaluate_pnl(trigger_level, 0)
        assert result == "lockout_trailing_drawdown"
        assert state.is_locked_out

    def test_drawdown_check_when_realized_negative(self, risk, state):
        """When realized goes negative, some safety system must catch it."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000
        risk.evaluate_pnl(hwm, 0)
        # Realized drops to -2000 (massive drawdown of hwm+2000)
        result = risk.evaluate_pnl(-2000, 0)
        assert state.is_locked_out, \
            "Safety system must trigger lockout when realized goes from profit to negative"

    def test_drawdown_buffer_calculation(self, risk, state):
        """Drawdown buffer should be correctly calculated in status."""
        hwm = 15000
        risk.evaluate_pnl(hwm, 0)
        risk.evaluate_pnl(12000, 0)
        status = risk.get_risk_status()
        dd = status["trailing_drawdown"]
        assert dd["high_water_mark"] == hwm
        assert dd["current_drawdown"] == hwm - 12000  # 3000
        assert dd["drawdown_limit"] == hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        assert dd["buffer"] == dd["drawdown_limit"] - dd["current_drawdown"]

    def test_can_trade_drawdown_check(self, risk, state):
        """_check_trailing_drawdown in can_trade should enforce the limit."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000
        # Simulate: drawdown was previously activated
        state.update_pnl(hwm, 0)
        state.update_trailing_drawdown(True, hwm, 0)

        # Now drop realized below limit
        drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        trigger_level = hwm - drawdown_limit
        state.update_pnl(trigger_level - 1, 0)

        decision = risk.can_trade()
        assert decision.allowed is False
        assert decision.action == "lockout"


# ═══════════════════════════════════════════════════════════════════
# 4. INTERACTION / CONFLICT TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSystemInteractions:

    def test_daily_loss_takes_priority_over_drawdown(self, risk, state):
        """Daily loss should trigger before trailing drawdown."""
        # Loss exceeds daily limit
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"
        # Should be lockout reason = daily loss, not drawdown
        assert "Daily loss" in state.get("lockout_reason")

    def test_cooldown_doesnt_prevent_lockout(self, risk, state):
        """Even during cooldown, a loss limit breach should still trigger lockout."""
        state.activate_cooldown(300, "test cooldown")
        assert state.is_cooling_down is True
        # Evaluate massive loss
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"
        assert state.is_locked_out is True

    def test_profit_lock_and_drawdown_both_active(self, risk, state):
        """Both profit lock and trailing drawdown can coexist."""
        # Hit profit lock threshold
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        assert state.profit_lock_active is True
        floor = state.profit_lock_floor

        # Continue higher (sets HWM)
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD + 5000, 0)

        # Drop to profit lock floor - should trigger profit lock breach
        result = risk.evaluate_pnl(floor - 1, 0)
        assert result == "lockout_profit_lock"

    def test_lockout_blocks_all_orders(self, risk, state):
        """Once locked out, all order checks should fail."""
        state.activate_lockout("test lockout")
        decision = risk.check_new_order(
            order_quantity=1,
            num_open_positions=0,
            estimated_risk=100,
        )
        assert decision.allowed is False
        assert decision.action == "lockout"

    def test_cooldown_blocks_orders_but_not_lockout_level(self, risk, state):
        """During cooldown, orders are blocked but it's not lockout."""
        state.activate_cooldown(300, "test")
        decision = risk.check_new_order(
            order_quantity=1,
            num_open_positions=0,
            estimated_risk=100,
        )
        assert decision.allowed is False
        assert decision.action == "cooldown"
        # But state is NOT locked out
        assert not state.is_locked_out

    def test_evaluate_pnl_priority_order(self, risk, state):
        """evaluate_pnl should check daily loss before trailing drawdown."""
        # Set HWM high enough for trailing drawdown
        risk.evaluate_pnl(15000, 0)
        # Now breach daily loss AND trailing drawdown simultaneously
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"

    def test_profit_lock_activates_at_threshold(self, risk, state):
        """Profit lock should activate exactly at threshold."""
        result = risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        assert result == "profit_lock_activated"
        assert state.profit_lock_active is True
        expected_floor = Config.PROFIT_LOCK_THRESHOLD * (Config.PROFIT_LOCK_PERCENTAGE / 100)
        assert state.profit_lock_floor == expected_floor

    def test_profit_lock_floor_only_checks_realized(self, risk, state):
        """Profit lock floor only checks realized_pnl, not total."""
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        floor = state.profit_lock_floor
        # Realized above floor but unrealized makes total below floor
        risk.evaluate_pnl(floor + 100, -(floor + 200))
        # Should NOT be locked out (realized is above floor)
        assert not state.is_locked_out

    def test_max_positions_blocks_order(self, risk, state):
        """Should block new orders when max positions reached."""
        decision = risk.check_new_order(
            order_quantity=1,
            num_open_positions=Config.MAX_OPEN_POSITIONS,
            estimated_risk=100,
        )
        assert decision.allowed is False
        assert "Max positions" in decision.reason

    def test_max_quantity_blocks_order(self, risk, state):
        """Should block orders exceeding max quantity."""
        decision = risk.check_new_order(
            order_quantity=Config.MAX_ORDER_QUANTITY + 1,
            num_open_positions=0,
            estimated_risk=100,
        )
        assert decision.allowed is False
        assert "exceeds max" in decision.reason

    def test_max_trade_risk_blocks_order(self, risk, state):
        """Should block orders exceeding single trade risk."""
        decision = risk.check_new_order(
            order_quantity=1,
            num_open_positions=0,
            estimated_risk=Config.MAX_SINGLE_TRADE_RISK + 1,
        )
        assert decision.allowed is False
        assert "Trade risk" in decision.reason


# ═══════════════════════════════════════════════════════════════════
# 5. STATE PERSISTENCE TESTS
# ═══════════════════════════════════════════════════════════════════

class TestStatePersistence:

    def test_state_survives_restart(self, fresh_state):
        """State should survive process restart (same day)."""
        from state_manager import StateManager
        s1 = StateManager()
        s1.activate_lockout("test persist")

        # Create new instance (simulates restart)
        s2 = StateManager()
        assert s2.is_locked_out is True
        assert s2.get("lockout_reason") == "test persist"

    def test_state_resets_on_new_day(self, fresh_state):
        """State should auto-reset on a new day."""
        from state_manager import StateManager
        s = StateManager()
        s.activate_lockout("yesterday lockout")
        # Tamper with date to simulate yesterday
        s._state["date"] = "2020-01-01"
        s._save()
        # Reload
        s2 = StateManager()
        assert s2.is_locked_out is False

    def test_tampered_state_triggers_reset(self, fresh_state):
        """Corrupted/tampered state file should trigger a fresh reset."""
        from state_manager import StateManager
        s = StateManager()
        s.activate_lockout("test")
        # Corrupt the file
        with open(s._state_file, "wb") as f:
            f.write(b"corrupted data")
        # Reload
        s2 = StateManager()
        assert s2.is_locked_out is False  # Reset to fresh state

    def test_pnl_update_persists(self, fresh_state):
        """P&L updates should persist across instances."""
        from state_manager import StateManager
        s1 = StateManager()
        s1.update_pnl(5000, 2000)

        s2 = StateManager()
        assert s2.realized_pnl == 5000
        assert s2.unrealized_pnl == 2000
        assert s2.total_pnl == 7000

    def test_hwm_persists(self, fresh_state):
        """High water mark should persist."""
        from state_manager import StateManager
        s1 = StateManager()
        s1.update_pnl(15000, 0)

        s2 = StateManager()
        assert s2.high_water_mark == 15000


# ═══════════════════════════════════════════════════════════════════
# 6. ORDER INTERCEPTOR TESTS
# ═══════════════════════════════════════════════════════════════════

class TestOrderInterceptor:

    def test_closing_trade_has_zero_risk(self):
        """Closing an existing position should have zero estimated risk."""
        from order_interceptor import OrderInterceptor
        api = MagicMock()
        risk_eng = MagicMock()
        state_mgr = MagicMock()
        interceptor = OrderInterceptor(api, risk_eng, state_mgr)

        positions = [{"securityId": "12345", "netQty": 10}]
        risk = interceptor._estimate_order_risk(
            "12345", "NSE_FNO", "SELL", 10, 100, positions
        )
        assert risk == 0

    def test_option_buy_risk_equals_premium(self):
        """Buying options: risk = premium * quantity."""
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), MagicMock(), MagicMock())
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 100, 50, []
        )
        assert risk == 50 * 100  # 5000

    def test_option_sell_risk_capped(self):
        """Selling options: risk should be capped at MAX_SINGLE_TRADE_RISK."""
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), MagicMock(), MagicMock())
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "SELL", 1000, 500, []
        )
        assert risk <= Config.MAX_SINGLE_TRADE_RISK

    def test_sl_based_risk_calculation(self):
        """When SL is provided, risk = |entry - SL| * quantity."""
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), MagicMock(), MagicMock())
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 100, 200, [], sl_price=180
        )
        assert risk == abs(200 - 180) * 100  # 2000

    def test_blocked_order_returns_blocked_status(self, risk, state):
        """Blocked orders should return BLOCKED status dict."""
        from order_interceptor import OrderInterceptor
        api = MagicMock()
        api.get_positions.return_value = []
        interceptor = OrderInterceptor(api, risk, state)

        # Lockout the system
        state.activate_lockout("test")

        result = interceptor.place_order(
            security_id="12345",
            exchange_segment="NSE_FNO",
            transaction_type="BUY",
            quantity=100,
            order_type="MARKET",
            product_type="MARGIN",
            price=100,
        )
        assert result["status"] == "BLOCKED"
        assert "reason" in result


# ═══════════════════════════════════════════════════════════════════
# 7. EDGE CASE TESTS
# ═══════════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_zero_pnl_no_lockout(self, risk, state):
        """Zero P&L should not trigger any lockout."""
        risk.evaluate_pnl(0, 0)
        assert not state.is_locked_out
        assert risk.can_trade().allowed is True

    def test_exactly_at_boundary(self, risk, state):
        """P&L exactly at -DAILY_MAX_LOSS boundary."""
        # -4999 should NOT trigger (just above limit)
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS + 1, 0)
        assert not state.is_locked_out
        # -5000 SHOULD trigger
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"

    def test_multiple_cooldowns_take_latest(self, risk, state):
        """Multiple cooldown activations should keep the latest."""
        state.activate_cooldown(100, "first")
        time.sleep(0.1)
        state.activate_cooldown(500, "second")
        remaining = state.cooldown_remaining
        assert remaining > 100  # Should be ~500, not 100

    def test_peak_pnl_tracking(self, risk, state):
        """Peak P&L should track highest total P&L."""
        risk.evaluate_pnl(5000, 2000)
        assert state.get("peak_pnl") == 7000
        risk.evaluate_pnl(3000, 1000)
        assert state.get("peak_pnl") == 7000  # Shouldn't decrease
        risk.evaluate_pnl(6000, 3000)
        assert state.get("peak_pnl") == 9000

    def test_trade_recording(self, risk, state):
        """Trade recording should update all counters."""
        risk.evaluate_trade_result(-500, {"security_id": "T1", "type": "SELL"})
        assert state.get("total_trades") == 1
        assert state.get("losing_trades") == 1
        assert state.consecutive_losses == 1

        risk.evaluate_trade_result(1000, {"security_id": "T2", "type": "BUY"})
        assert state.get("total_trades") == 2
        assert state.get("winning_trades") == 1
        assert state.consecutive_losses == 0

    def test_profit_target_lockout(self, risk, state):
        """Hitting profit target should trigger lockout."""
        risk.evaluate_pnl(Config.DAILY_PROFIT_TARGET, 0)
        decision = risk.can_trade()
        assert decision.allowed is False

    def test_risk_status_completeness(self, risk, state):
        """get_risk_status should contain all required keys."""
        risk.evaluate_pnl(5000, 1000)
        status = risk.get_risk_status()
        required_keys = [
            "can_trade", "lockout", "cooldown", "pnl", "limits",
            "profit_lock", "trailing_drawdown", "trades", "kill_switch"
        ]
        for key in required_keys:
            assert key in status, f"Missing key: {key}"

    def test_consecutive_losses_across_multiple_trades(self, risk, state):
        """Track consecutive losses properly across many trades."""
        for i in range(10):
            risk.evaluate_trade_result(-100, {"security_id": f"T{i}"})
        assert state.consecutive_losses == 10
        risk.evaluate_trade_result(100, {"security_id": "WIN"})
        assert state.consecutive_losses == 0

    def test_trade_history_capped_at_50(self, risk, state):
        """Trade history should be capped at 50 entries."""
        for i in range(60):
            state.record_trade({"pnl": i, "security_id": f"T{i}"})
        history = state.get("trade_history")
        assert len(history) == 50
        # Should keep the LAST 50
        assert history[0]["pnl"] == 10


# ═══════════════════════════════════════════════════════════════════
# Run summary
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
