"""
Comprehensive tests for the risk management safety systems:
1. Daily Loss Limit
2. Trailing Drawdown (based on total P&L)
3. Interactions between systems
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
# 2. TRAILING DRAWDOWN TESTS (based on total P&L)
# ═══════════════════════════════════════════════════════════════════

class TestTrailingDrawdown:

    def test_hwm_tracks_total_pnl(self, risk, state):
        """HWM should track peak total P&L (realized + unrealized)."""
        risk.evaluate_pnl(3000, 2000)  # total = 5000
        assert state.high_water_mark == 5000
        risk.evaluate_pnl(6000, 4000)  # total = 10000
        assert state.high_water_mark == 10000
        # HWM shouldn't decrease
        risk.evaluate_pnl(5000, 3000)  # total = 8000
        assert state.high_water_mark == 10000

    def test_hwm_includes_unrealized(self, risk, state):
        """If unrealized spikes to 18000, HWM should be 18000."""
        risk.evaluate_pnl(5000, 13000)  # total = 18000
        assert state.high_water_mark == 18000
        # Close trade at 15000 realized, 0 unrealized
        risk.evaluate_pnl(15000, 0)  # total = 15000
        assert state.high_water_mark == 18000  # HWM stays at 18000

    def test_drawdown_from_unrealized_peak(self, risk, state):
        """Your scenario: unrealized hits 18000, close at 15000, 6000 from lockout."""
        original_threshold = Config.PROFIT_LOCK_THRESHOLD
        try:
            Config.PROFIT_LOCK_THRESHOLD = 999999  # isolate trailing drawdown
            # Unrealized peak
            risk.evaluate_pnl(5000, 13000)  # total = 18000, HWM = 18000
            assert state.high_water_mark == 18000
            # Close at 15000
            risk.evaluate_pnl(15000, 0)  # total = 15000
            status = risk.get_risk_status()
            dd = status["trailing_drawdown"]
            assert dd["high_water_mark"] == 18000
            assert dd["current_drawdown"] == 3000  # 18000 - 15000
            assert dd["drawdown_limit"] == 9000    # 50% of 18000
            assert dd["buffer"] == 6000             # 9000 - 3000
            assert not state.is_locked_out
        finally:
            Config.PROFIT_LOCK_THRESHOLD = original_threshold

    def test_no_drawdown_below_threshold(self, risk, state):
        """Trailing drawdown should not trigger before profit lock threshold."""
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD - 1000, 0)
        risk.evaluate_pnl(0, 0)
        assert not state.is_locked_out

    def test_drawdown_triggers_lockout_above_threshold(self, risk, state):
        """Trailing drawdown should trigger lockout when HWM >= threshold."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000  # e.g., 15000
        risk.evaluate_pnl(hwm, 0)
        assert state.high_water_mark == hwm

        drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        trigger_level = hwm - drawdown_limit  # 15000 - 7500 = 7500

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
            Config.PROFIT_LOCK_THRESHOLD = 999999
            risk.evaluate_pnl(20000, 0)
            risk.evaluate_pnl(5000, 0)
            assert not state.is_locked_out
        finally:
            Config.TRAILING_DRAWDOWN_ENABLED = original_dd
            Config.PROFIT_LOCK_THRESHOLD = original_threshold

    def test_drawdown_check_when_total_drops_to_zero(self, risk, state):
        """Safety system must protect when total P&L drops from profit to zero."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000
        risk.evaluate_pnl(hwm, 0)
        assert state.high_water_mark == hwm
        # Total drops to 0 — either profit lock floor or trailing drawdown catches this
        result = risk.evaluate_pnl(0, 0)
        assert state.is_locked_out, \
            "Safety system must trigger lockout when total drops to 0"

    def test_drawdown_triggers_without_profit_lock(self, risk, state):
        """Trailing drawdown should work independently when profit lock is bypassed."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000
        state.update_pnl(hwm, 0)
        state.update_trailing_drawdown(True, hwm, 0)
        state._state["profit_lock_active"] = False
        state._save()

        drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        trigger_level = hwm - drawdown_limit

        result = risk.evaluate_pnl(trigger_level, 0)
        assert result == "lockout_trailing_drawdown"
        assert state.is_locked_out

    def test_drawdown_check_when_total_negative(self, risk, state):
        """Safety system must catch when total goes from profit to negative."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000
        risk.evaluate_pnl(hwm, 0)
        result = risk.evaluate_pnl(-2000, 0)
        assert state.is_locked_out, \
            "Safety system must trigger lockout when total goes negative from profit"

    def test_drawdown_with_unrealized_swing(self, risk, state):
        """Unrealized P&L swings should affect drawdown calculation."""
        # First call: realized 15000 >= threshold 10000 → profit lock activates
        result1 = risk.evaluate_pnl(15000, 3000)
        assert result1 == "profit_lock_activated"
        assert state.high_water_mark == 18000
        # Unrealized drops: realized = 15000, unrealized = -6000, total = 9000
        # Drawdown = 18000 - 9000 = 9000, limit = 18000 * 50% = 9000 → lockout!
        result2 = risk.evaluate_pnl(15000, -6000)
        assert result2 == "lockout_trailing_drawdown"

    def test_drawdown_buffer_calculation(self, risk, state):
        """Drawdown buffer should be correctly calculated."""
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
        """_check_trailing_drawdown in can_trade should use total P&L."""
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000
        state.update_pnl(hwm, 0)
        state.update_trailing_drawdown(True, hwm, 0)

        drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        trigger_level = hwm - drawdown_limit
        state.update_pnl(trigger_level - 1, 0)

        decision = risk.can_trade()
        assert decision.allowed is False
        assert decision.action == "lockout"


# ═══════════════════════════════════════════════════════════════════
# 3. INTERACTION / CONFLICT TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSystemInteractions:

    def test_daily_loss_takes_priority_over_drawdown(self, risk, state):
        """Daily loss should trigger before trailing drawdown."""
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"
        assert "Daily loss" in state.get("lockout_reason")

    def test_no_cooldown_system(self, risk, state):
        """Cooldown should not block trading (removed)."""
        # Even after losses, no cooldown
        risk.evaluate_trade_result(-2000, {"security_id": "T1"})
        risk.evaluate_trade_result(-2000, {"security_id": "T2"})
        risk.evaluate_trade_result(-2000, {"security_id": "T3"})
        decision = risk.can_trade()
        # Should be allowed (no cooldown) — may be locked from daily loss though
        assert decision.action != "cooldown"

    def test_profit_lock_and_drawdown_both_active(self, risk, state):
        """Both profit lock and trailing drawdown can coexist."""
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        assert state.profit_lock_active is True
        floor = state.profit_lock_floor

        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD + 5000, 0)

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

    def test_evaluate_pnl_priority_order(self, risk, state):
        """evaluate_pnl should check daily loss before trailing drawdown."""
        risk.evaluate_pnl(15000, 0)
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
        original_dd = Config.TRAILING_DRAWDOWN_ENABLED
        try:
            # Disable trailing drawdown so we can test profit lock in isolation
            Config.TRAILING_DRAWDOWN_ENABLED = False
            risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
            floor = state.profit_lock_floor
            # Realized (floor+100) > floor → profit lock floor NOT breached
            # Total is negative but that's irrelevant for profit lock floor
            risk.evaluate_pnl(floor + 100, -(floor + 200))
            assert not state.is_locked_out
        finally:
            Config.TRAILING_DRAWDOWN_ENABLED = original_dd

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
# 4. STATE PERSISTENCE TESTS
# ═══════════════════════════════════════════════════════════════════

class TestStatePersistence:

    def test_state_survives_restart(self, fresh_state):
        """State should survive process restart (same day)."""
        from state_manager import StateManager
        s1 = StateManager()
        s1.activate_lockout("test persist")
        s2 = StateManager()
        assert s2.is_locked_out is True
        assert s2.get("lockout_reason") == "test persist"

    def test_state_resets_on_new_day(self, fresh_state):
        """State should auto-reset on a new day."""
        from state_manager import StateManager
        s = StateManager()
        s.activate_lockout("yesterday lockout")
        s._state["date"] = "2020-01-01"
        s._save()
        s2 = StateManager()
        assert s2.is_locked_out is False

    def test_tampered_state_triggers_reset(self, fresh_state):
        """Corrupted/tampered state file should trigger a fresh reset."""
        from state_manager import StateManager
        s = StateManager()
        s.activate_lockout("test")
        with open(s._state_file, "wb") as f:
            f.write(b"corrupted data")
        s2 = StateManager()
        assert s2.is_locked_out is False

    def test_pnl_update_persists(self, fresh_state):
        """P&L updates should persist across instances."""
        from state_manager import StateManager
        s1 = StateManager()
        s1.update_pnl(5000, 2000)
        s2 = StateManager()
        assert s2.realized_pnl == 5000
        assert s2.unrealized_pnl == 2000
        assert s2.total_pnl == 7000

    def test_hwm_persists_as_total(self, fresh_state):
        """HWM should persist and reflect total P&L."""
        from state_manager import StateManager
        s1 = StateManager()
        s1.update_pnl(10000, 5000)  # total = 15000
        s2 = StateManager()
        assert s2.high_water_mark == 15000


# ═══════════════════════════════════════════════════════════════════
# 5. ORDER INTERCEPTOR TESTS
# ═══════════════════════════════════════════════════════════════════

class TestOrderInterceptor:

    def test_closing_trade_has_zero_risk(self):
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), MagicMock(), MagicMock())
        positions = [{"securityId": "12345", "netQty": 10}]
        risk = interceptor._estimate_order_risk(
            "12345", "NSE_FNO", "SELL", 10, 100, positions
        )
        assert risk == 0

    def test_option_buy_risk_equals_premium(self):
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), MagicMock(), MagicMock())
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 100, 50, []
        )
        assert risk == 50 * 100

    def test_option_sell_risk_capped(self):
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), MagicMock(), MagicMock())
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "SELL", 1000, 500, []
        )
        assert risk <= Config.MAX_SINGLE_TRADE_RISK

    def test_sl_based_risk_calculation(self):
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), MagicMock(), MagicMock())
        risk = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 100, 200, [], sl_price=180
        )
        assert risk == abs(200 - 180) * 100

    def test_blocked_order_returns_blocked_status(self, risk, state):
        from order_interceptor import OrderInterceptor
        api = MagicMock()
        api.get_positions.return_value = []
        interceptor = OrderInterceptor(api, risk, state)
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
# 6. EDGE CASE TESTS
# ═══════════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_zero_pnl_no_lockout(self, risk, state):
        risk.evaluate_pnl(0, 0)
        assert not state.is_locked_out
        assert risk.can_trade().allowed is True

    def test_exactly_at_boundary(self, risk, state):
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS + 1, 0)
        assert not state.is_locked_out
        result = risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert result == "lockout_max_loss"

    def test_peak_pnl_tracking(self, risk, state):
        """Peak P&L should track highest total P&L."""
        risk.evaluate_pnl(5000, 2000)
        assert state.get("peak_pnl") == 7000
        risk.evaluate_pnl(3000, 1000)
        assert state.get("peak_pnl") == 7000
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
        risk.evaluate_pnl(Config.DAILY_PROFIT_TARGET, 0)
        decision = risk.can_trade()
        assert decision.allowed is False

    def test_risk_status_completeness(self, risk, state):
        risk.evaluate_pnl(5000, 1000)
        status = risk.get_risk_status()
        required_keys = [
            "can_trade", "lockout", "cooldown", "pnl", "limits",
            "profit_lock", "trailing_drawdown", "trades", "kill_switch"
        ]
        for key in required_keys:
            assert key in status, f"Missing key: {key}"

    def test_win_resets_consecutive_losses(self, risk, state):
        """A win should reset consecutive loss counter."""
        risk.evaluate_trade_result(-100, {"security_id": "T1"})
        risk.evaluate_trade_result(-100, {"security_id": "T2"})
        assert state.consecutive_losses == 2
        risk.evaluate_trade_result(100, {"security_id": "T3"})
        assert state.consecutive_losses == 0

    def test_trade_history_capped_at_50(self, risk, state):
        for i in range(60):
            state.record_trade({"pnl": i, "security_id": f"T{i}"})
        history = state.get("trade_history")
        assert len(history) == 50
        assert history[0]["pnl"] == 10


# ═══════════════════════════════════════════════════════════════════
# Run summary
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
