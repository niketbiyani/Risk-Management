"""
Cross-module integration tests for the Risk Management Platform.
Tests interactions between RiskEngine, StateManager, TradeManager,
and OrderInterceptor using realistic trading day scenarios.
"""

import os
import sys
import shutil
import tempfile
import time
from unittest.mock import MagicMock

import pytest

os.environ["STATE_ENCRYPTION_KEY"] = ""
os.environ["DHAN_CLIENT_ID"] = "test"
os.environ["DHAN_ACCESS_TOKEN"] = "test"

sys.path.insert(0, os.path.dirname(__file__))

from config import Config

_test_state_dir = tempfile.mkdtemp(prefix="integ_test_")
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


@pytest.fixture
def api():
    mock = MagicMock()
    mock.get_positions.return_value = []
    return mock


@pytest.fixture
def interceptor(api, risk, state):
    from order_interceptor import OrderInterceptor
    return OrderInterceptor(api, risk, state)


@pytest.fixture
def trade_mgr(api):
    from trade_manager import TradeManager
    return TradeManager(api)


def make_position(security_id="100", net_qty=75, avg_price=100.0, ltp=95.0,
                  exchange_segment="NSE_FNO", product_type="MARGIN"):
    return {
        "securityId": security_id,
        "netQty": net_qty,
        "avgPrice": avg_price,
        "lastTradedPrice": ltp,
        "exchangeSegment": exchange_segment,
        "productType": product_type,
    }


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
# 1. LOCKOUT INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════

class TestLockoutIntegration:

    def test_loss_lockout_blocks_orders(self, risk, state, interceptor):
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert state.is_locked_out
        result = interceptor.place_order(
            security_id="100", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=75,
            order_type="MARKET", product_type="MARGIN", price=100,
        )
        assert result["status"] == "BLOCKED"

    def test_profit_lock_floor_breach_blocks(self, risk, state, interceptor):
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        assert state.profit_lock_active
        floor = state.profit_lock_floor
        risk.evaluate_pnl(floor - 1, 0)
        assert state.is_locked_out
        result = interceptor.place_order(
            security_id="100", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=75,
            order_type="MARKET", product_type="MARGIN", price=100,
        )
        assert result["status"] == "BLOCKED"

    def test_drawdown_lockout_blocks(self, risk, state, interceptor):
        hwm = Config.PROFIT_LOCK_THRESHOLD + 5000
        risk.evaluate_pnl(hwm, 0)
        drawdown_limit = hwm * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
        trigger_level = hwm - drawdown_limit
        risk.evaluate_pnl(trigger_level, 0)
        assert state.is_locked_out
        result = interceptor.place_order(
            security_id="100", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=75,
            order_type="MARKET", product_type="MARGIN", price=100,
        )
        assert result["status"] == "BLOCKED"

    def test_profit_target_lockout_blocks(self, risk, state, interceptor):
        risk.evaluate_pnl(Config.DAILY_PROFIT_TARGET, 0)
        decision = risk.can_trade()
        assert decision.allowed is False
        result = interceptor.place_order(
            security_id="100", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=75,
            order_type="MARKET", product_type="MARGIN", price=100,
        )
        assert result["status"] == "BLOCKED"

    def test_lockout_survives_state_reload(self, risk, state, fresh_state):
        from state_manager import StateManager
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert state.is_locked_out
        s2 = StateManager()
        assert s2.is_locked_out is True

    def test_lockout_with_sl_tp_still_triggers(self, risk, state, trade_mgr):
        """SL/TP should still trigger even during lockout (to close positions)."""
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert state.is_locked_out
        trade_mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0)]
        triggered = trade_mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1

    def test_multiple_breaches_first_wins(self, risk, state):
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        reason = state.get("lockout_reason")
        assert "Daily loss" in reason
        # Second lockout attempt doesn't change reason (already locked)
        risk.evaluate_pnl(Config.DAILY_PROFIT_TARGET, 0)
        assert state.get("lockout_reason") == reason

    def test_can_trade_before_and_after_lockout(self, risk, state):
        assert risk.can_trade().allowed is True
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert risk.can_trade().allowed is False

    def test_order_interceptor_blocks_after_lockout(self, risk, state, interceptor):
        state.activate_lockout("test lockout")
        result = interceptor.place_order(
            security_id="100", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=75,
            order_type="MARKET", product_type="MARGIN", price=100,
        )
        assert result["status"] == "BLOCKED"
        assert "reason" in result

    def test_risk_status_accurate_after_lockout(self, risk, state):
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        status = risk.get_risk_status()
        assert status["can_trade"] is False
        assert status["lockout"]["active"] is True
        assert "Daily loss" in status["lockout"]["reason"]


# ═══════════════════════════════════════════════════════════════════
# 2. REALISTIC TRADING DAY SIMULATIONS
# ═══════════════════════════════════════════════════════════════════

class TestRealisticTradingDay:

    def test_morning_scalp_hits_daily_loss(self, risk, state):
        """5 losing trades → daily loss → lockout."""
        for i in range(5):
            risk.evaluate_trade_result(-1000, {"security_id": f"T{i}", "type": "SELL"})
            risk.evaluate_pnl(-1000 * (i + 1), 0)
            if state.is_locked_out:
                break
        assert state.is_locked_out is True
        assert state.get("total_trades") == 5  # exactly at -5000

    def test_winning_morning_profit_lock_afternoon_giveback(self, risk, state):
        """Win to profit lock threshold → giveback → floor breach → lockout."""
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD, 0)
        assert state.profit_lock_active
        floor = state.profit_lock_floor
        # Afternoon: P&L drops below floor
        result = risk.evaluate_pnl(floor - 100, 0)
        assert result == "lockout_profit_lock"
        assert state.is_locked_out

    def test_intraday_drawdown_trip(self, risk, state):
        """HWM from unrealized → position reverses → drawdown lockout."""
        with ConfigOverride(PROFIT_LOCK_THRESHOLD=5000):
            risk.evaluate_pnl(2000, 8000)  # total = 10000, HWM = 10000
            assert state.high_water_mark == 10000
            # Position reverses
            drawdown_limit = 10000 * (Config.TRAILING_DRAWDOWN_PERCENTAGE / 100)
            trigger = 10000 - drawdown_limit
            result = risk.evaluate_pnl(2000, trigger - 2000)
            assert state.is_locked_out

    def test_50_small_trades_no_breach(self, risk, state):
        """High volume trading, all within limits."""
        for i in range(50):
            pnl = 50 if i % 2 == 0 else -40
            risk.evaluate_trade_result(pnl, {"security_id": f"T{i}"})
            running = state.realized_pnl
            risk.evaluate_pnl(running + pnl, 0)
        assert not state.is_locked_out
        assert state.get("total_trades") == 50

    def test_sl_tp_throughout_day(self, risk, state, trade_mgr):
        """Set SL/TP, some trigger, risk checks pass between."""
        assert risk.can_trade().allowed is True
        trade_mgr.set_stop_loss("100", 95.0)
        trade_mgr.set_take_profit("200", 110.0)

        # Position 100 hits SL
        pos = [
            make_position("100", net_qty=75, avg_price=100, ltp=94.0),
            make_position("200", net_qty=75, avg_price=100, ltp=105.0),
        ]
        triggered = trade_mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["security_id"] == "100"
        # Record the loss
        risk.evaluate_trade_result(-450, {"security_id": "100"})
        risk.evaluate_pnl(-450, 0)
        assert risk.can_trade().allowed is True

    def test_spread_trade_with_sl(self, risk, state, trade_mgr):
        """Detect spread, set SL on a leg, SL triggers."""
        positions = [
            {"securityId": "1", "netQty": -75, "avgPrice": 120, "lastTradedPrice": 115,
             "exchangeSegment": "NSE_FNO", "drvOptionType": "PUT", "drvStrikePrice": 24000,
             "expiryDate": "2026-03-05"},
            {"securityId": "2", "netQty": 75, "avgPrice": 80, "lastTradedPrice": 75,
             "exchangeSegment": "NSE_FNO", "drvOptionType": "PUT", "drvStrikePrice": 23900,
             "expiryDate": "2026-03-05"},
        ]
        spreads = trade_mgr.detect_spreads(positions)
        assert len(spreads) >= 1
        # Set SL on the short leg
        trade_mgr.set_stop_loss("1", 140.0)
        pos = [make_position("1", net_qty=-75, avg_price=120, ltp=141.0)]
        triggered = trade_mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["transaction_type"] == "BUY"

    def test_unrealized_swing_does_not_false_lockout(self, risk, state):
        """Large unrealized swings within bounds should not trigger lockout."""
        risk.evaluate_pnl(0, 3000)
        assert not state.is_locked_out
        risk.evaluate_pnl(0, -3000)
        assert not state.is_locked_out
        risk.evaluate_pnl(0, 4000)
        assert not state.is_locked_out

    def test_exact_boundary_all_day(self, risk, state):
        """P&L hovers at exact limit, never crosses."""
        just_above = -Config.DAILY_MAX_LOSS + 1
        for _ in range(10):
            risk.evaluate_pnl(just_above, 0)
            assert not state.is_locked_out
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert state.is_locked_out

    def test_profit_lock_then_drawdown_interaction(self, risk, state):
        """Profit lock floor breached while drawdown also active."""
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD + 5000, 0)
        assert state.profit_lock_active
        floor = state.profit_lock_floor
        # Drop below floor
        result = risk.evaluate_pnl(floor - 1, 0)
        assert result == "lockout_profit_lock"

    def test_winning_streak_to_profit_target(self, risk, state):
        """10 winners → profit target hit → lockout."""
        per_trade = Config.DAILY_PROFIT_TARGET / 10
        running = 0.0
        for i in range(10):
            running += per_trade
            risk.evaluate_trade_result(per_trade, {"security_id": f"T{i}"})
            risk.evaluate_pnl(running, 0)
        decision = risk.can_trade()
        assert decision.allowed is False

    def test_losing_streak_to_daily_loss(self, risk, state):
        """Consecutive losses accumulate to limit."""
        per_trade = Config.DAILY_MAX_LOSS / 5
        running = 0.0
        for i in range(5):
            running -= per_trade
            risk.evaluate_trade_result(-per_trade, {"security_id": f"T{i}"})
            risk.evaluate_pnl(running, 0)
        assert state.is_locked_out
        assert state.consecutive_losses == 5

    def test_recovery_after_morning_loss(self, risk, state):
        """Lose 3K, win back 3K → buffer consumed but no lockout."""
        risk.evaluate_pnl(-3000, 0)
        assert not state.is_locked_out
        risk.evaluate_pnl(0, 0)
        assert not state.is_locked_out
        status = risk.get_risk_status()
        assert status["limits"]["loss_remaining"] == Config.DAILY_MAX_LOSS

    def test_sl_tp_trigger_during_lockout_exit(self, risk, state, trade_mgr):
        """During lockout, SL/TP execute to close positions."""
        trade_mgr.set_stop_loss("100", 95.0)
        risk.evaluate_pnl(-Config.DAILY_MAX_LOSS, 0)
        assert state.is_locked_out
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0)]
        triggered = trade_mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1

    def test_state_integrity_after_full_day(self, risk, state):
        """After many operations, state is consistent."""
        for i in range(10):
            pnl = 200 if i % 3 != 0 else -300
            risk.evaluate_trade_result(pnl, {"security_id": f"T{i}"})
        risk.evaluate_pnl(state.realized_pnl, 500)
        status = risk.get_risk_status()
        assert status["trades"]["total"] == 10
        assert status["trades"]["winners"] + status["trades"]["losers"] == 10
        assert isinstance(status["pnl"]["realized"], float)
        assert isinstance(status["pnl"]["total"], float)


# ═══════════════════════════════════════════════════════════════════
# 3. ORDER RISK ESTIMATION
# ═══════════════════════════════════════════════════════════════════

class TestOrderRiskEstimation:

    def test_option_buy_within_limit(self, risk, state):
        decision = risk.check_new_order(
            order_quantity=75,
            num_open_positions=0,
            estimated_risk=Config.MAX_SINGLE_TRADE_RISK - 100,
        )
        assert decision.allowed is True

    def test_option_buy_exceeds_limit(self, risk, state):
        decision = risk.check_new_order(
            order_quantity=75,
            num_open_positions=0,
            estimated_risk=Config.MAX_SINGLE_TRADE_RISK + 100,
        )
        assert decision.allowed is False
        assert "Trade risk" in decision.reason

    def test_option_sell_capped_risk(self, risk, state):
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), risk, state)
        risk_val = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "SELL", 1000, 500, []
        )
        assert risk_val <= Config.MAX_SINGLE_TRADE_RISK

    def test_sl_based_risk_within_limit(self, risk, state):
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), risk, state)
        risk_val = interceptor._estimate_order_risk(
            "99999", "NSE_FNO", "BUY", 75, 200, [], sl_price=195
        )
        expected = abs(200 - 195) * 75
        assert risk_val == expected

    def test_sl_based_risk_exceeds_limit(self, risk, state):
        decision = risk.check_new_order(
            order_quantity=75,
            num_open_positions=0,
            estimated_risk=Config.MAX_SINGLE_TRADE_RISK + 1,
        )
        assert decision.allowed is False

    def test_closing_trade_zero_risk(self, risk, state):
        from order_interceptor import OrderInterceptor
        interceptor = OrderInterceptor(MagicMock(), risk, state)
        positions = [{"securityId": "12345", "netQty": 75}]
        risk_val = interceptor._estimate_order_risk(
            "12345", "NSE_FNO", "SELL", 75, 100, positions
        )
        assert risk_val == 0

    def test_max_positions_reached(self, risk, state):
        decision = risk.check_new_order(
            order_quantity=75,
            num_open_positions=Config.MAX_OPEN_POSITIONS,
            estimated_risk=100,
        )
        assert decision.allowed is False
        assert "Max positions" in decision.reason

    def test_max_quantity_exceeded(self, risk, state):
        decision = risk.check_new_order(
            order_quantity=Config.MAX_ORDER_QUANTITY + 1,
            num_open_positions=0,
            estimated_risk=100,
        )
        assert decision.allowed is False
        assert "exceeds max" in decision.reason


# ═══════════════════════════════════════════════════════════════════
# 4. STATE PERSISTENCE INTEGRATION
# ═══════════════════════════════════════════════════════════════════

class TestStatePersistenceIntegration:

    def test_full_day_state_survives_restart(self, risk, state, fresh_state):
        from state_manager import StateManager
        risk.evaluate_pnl(5000, 2000)
        risk.evaluate_trade_result(500, {"security_id": "T1"})
        s2 = StateManager()
        assert s2.realized_pnl == 5000
        assert s2.unrealized_pnl == 2000
        assert s2.get("total_trades") == 1

    def test_hwm_profit_lock_drawdown_persist(self, risk, state, fresh_state):
        from state_manager import StateManager
        risk.evaluate_pnl(Config.PROFIT_LOCK_THRESHOLD + 3000, 0)
        s2 = StateManager()
        assert s2.profit_lock_active is True
        assert s2.high_water_mark >= Config.PROFIT_LOCK_THRESHOLD

    def test_trade_history_persists(self, state, fresh_state):
        from state_manager import StateManager
        for i in range(50):
            state.record_trade({"pnl": i * 10, "security_id": f"T{i}"})
        s2 = StateManager()
        history = s2.get("trade_history")
        assert len(history) == 50

    def test_day_boundary_resets_everything(self, state, fresh_state):
        from state_manager import StateManager
        state.activate_lockout("yesterday lockout")
        state._state["date"] = "2020-01-01"
        state._save()
        s2 = StateManager()
        assert s2.is_locked_out is False
        assert s2.realized_pnl == 0

    def test_corrupted_state_resets_cleanly(self, state, fresh_state):
        from state_manager import StateManager
        state.activate_lockout("test")
        with open(state._state_file, "wb") as f:
            f.write(b"totally corrupted garbage data")
        s2 = StateManager()
        assert s2.is_locked_out is False

    def test_concurrent_save_load_cycles(self, state, fresh_state):
        from state_manager import StateManager
        for i in range(50):
            state.update_pnl(i * 100, i * 50)
        s2 = StateManager()
        assert s2.realized_pnl == 4900
        assert s2.unrealized_pnl == 2450

    def test_integrity_hash_detects_tampering(self, state, fresh_state):
        from state_manager import StateManager
        state.activate_lockout("critical lockout")
        # Overwrite with garbage
        with open(state._state_file, "wb") as f:
            f.write(b"\x00" * 100)
        s2 = StateManager()
        assert s2.is_locked_out is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
