"""
Comprehensive tests for TradeManager: SL/TP engine, spread detection, projections.
Covers ~120 tests across 7 test classes.
"""

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

os.environ["STATE_ENCRYPTION_KEY"] = ""
os.environ["DHAN_CLIENT_ID"] = "test"
os.environ["DHAN_ACCESS_TOKEN"] = "test"

sys.path.insert(0, os.path.dirname(__file__))

from trade_manager import (
    TradeManager, OptionLeg, SpreadPosition, StopLossTarget, PendingSpreadOrder,
)


# ── Helpers ────────────────────────────────────────────────────────

def make_position(security_id="100", net_qty=75, avg_price=100.0, ltp=95.0,
                  exchange_segment="NSE_FNO", option_type="CALL",
                  strike_price=24000, expiry="2026-03-05", product_type="MARGIN"):
    return {
        "securityId": security_id,
        "netQty": net_qty,
        "avgPrice": avg_price,
        "lastTradedPrice": ltp,
        "exchangeSegment": exchange_segment,
        "drvOptionType": option_type,
        "drvStrikePrice": strike_price,
        "expiryDate": expiry,
        "productType": product_type,
    }


@pytest.fixture
def mgr():
    return TradeManager(MagicMock())


# ═══════════════════════════════════════════════════════════════════
# 1. SL/TP BASIC TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSLTPBasic:

    def test_set_stop_loss(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        cfg = mgr._sl_tp_orders["100"]
        assert cfg.stop_loss_price == 95.0
        assert cfg.current_sl_price == 95.0

    def test_set_take_profit(self, mgr):
        mgr.set_take_profit("100", 110.0)
        cfg = mgr._sl_tp_orders["100"]
        assert cfg.take_profit_price == 110.0

    def test_set_sl_and_tp(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr.set_take_profit("100", 110.0)
        cfg = mgr._sl_tp_orders["100"]
        assert cfg.stop_loss_price == 95.0
        assert cfg.take_profit_price == 110.0

    def test_update_existing_sl(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr.set_stop_loss("100", 90.0)
        assert mgr._sl_tp_orders["100"].stop_loss_price == 90.0
        assert mgr._sl_tp_orders["100"].current_sl_price == 90.0

    def test_update_existing_tp(self, mgr):
        mgr.set_take_profit("100", 110.0)
        mgr.set_take_profit("100", 120.0)
        assert mgr._sl_tp_orders["100"].take_profit_price == 120.0

    def test_remove_sl_tp(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr.remove_sl_tp("100")
        assert "100" not in mgr._sl_tp_orders

    def test_get_active_sl_tp(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr.set_take_profit("200", 110.0)
        active = mgr.get_active_sl_tp()
        assert "100" in active
        assert "200" in active
        assert active["100"]["stop_loss"] == 95.0
        assert active["200"]["take_profit"] == 110.0

    def test_get_active_excludes_inactive(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr._sl_tp_orders["100"].is_active = False
        active = mgr.get_active_sl_tp()
        assert "100" not in active

    def test_set_trailing_sl(self, mgr):
        mgr.set_stop_loss("100", 95.0, trailing=True, trail_points=5.0, trail_trigger=3.0)
        cfg = mgr._sl_tp_orders["100"]
        assert cfg.trailing_sl is True
        assert cfg.trailing_sl_points == 5.0
        assert cfg.trailing_sl_trigger == 3.0

    def test_multiple_instruments(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr.set_stop_loss("200", 190.0)
        assert mgr._sl_tp_orders["100"].stop_loss_price == 95.0
        assert mgr._sl_tp_orders["200"].stop_loss_price == 190.0

    def test_sl_with_zero_price(self, mgr):
        mgr.set_stop_loss("100", 0.0)
        cfg = mgr._sl_tp_orders["100"]
        assert cfg.stop_loss_price == 0.0

    def test_set_sl_creates_new_if_missing(self, mgr):
        assert "999" not in mgr._sl_tp_orders
        mgr.set_stop_loss("999", 50.0)
        assert "999" in mgr._sl_tp_orders
        assert isinstance(mgr._sl_tp_orders["999"], StopLossTarget)

    def test_set_tp_creates_new_if_missing(self, mgr):
        assert "999" not in mgr._sl_tp_orders
        mgr.set_take_profit("999", 150.0)
        assert "999" in mgr._sl_tp_orders

    def test_remove_nonexistent_security(self, mgr):
        # Should not raise
        mgr.remove_sl_tp("nonexistent")

    def test_sl_tp_initial_is_active_true(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        assert mgr._sl_tp_orders["100"].is_active is True


# ═══════════════════════════════════════════════════════════════════
# 2. SL/TP TRIGGER TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSLTPTriggers:

    def test_long_sl_hit(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["action"] == "STOP_LOSS"

    def test_long_sl_not_hit(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=96.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0

    def test_long_sl_exact_boundary(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=95.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["action"] == "STOP_LOSS"

    def test_short_sl_hit(self, mgr):
        mgr.set_stop_loss("100", 105.0)
        pos = [make_position("100", net_qty=-75, avg_price=100, ltp=106.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["action"] == "STOP_LOSS"

    def test_short_sl_not_hit(self, mgr):
        mgr.set_stop_loss("100", 105.0)
        pos = [make_position("100", net_qty=-75, avg_price=100, ltp=104.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0

    def test_long_tp_hit(self, mgr):
        mgr.set_take_profit("100", 110.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=110.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["action"] == "TAKE_PROFIT"

    def test_long_tp_not_hit(self, mgr):
        mgr.set_take_profit("100", 110.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=109.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0

    def test_short_tp_hit(self, mgr):
        mgr.set_take_profit("100", 90.0)
        pos = [make_position("100", net_qty=-75, avg_price=100, ltp=90.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["action"] == "TAKE_PROFIT"

    def test_short_tp_not_hit(self, mgr):
        mgr.set_take_profit("100", 90.0)
        pos = [make_position("100", net_qty=-75, avg_price=100, ltp=91.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0

    def test_is_active_prevents_double_trigger(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0)]
        t1 = mgr.check_sl_tp_triggers(pos)
        assert len(t1) == 1
        assert mgr._sl_tp_orders["100"].is_active is False
        t2 = mgr.check_sl_tp_triggers(pos)
        assert len(t2) == 0

    def test_zero_qty_skipped(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=0, avg_price=100, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0

    def test_no_config_skipped(self, mgr):
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0

    def test_sl_and_tp_both_set_both_trigger(self, mgr):
        """When LTP hits both SL and TP simultaneously, both trigger on same tick."""
        mgr.set_stop_loss("100", 100.0)
        mgr.set_take_profit("100", 100.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=100.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        # Both SL and TP fire on the same tick (code checks both independently)
        assert len(triggered) == 2
        actions = {t["action"] for t in triggered}
        assert "STOP_LOSS" in actions
        assert "TAKE_PROFIT" in actions
        # After triggering, is_active=False prevents re-trigger
        t2 = mgr.check_sl_tp_triggers(pos)
        assert len(t2) == 0

    def test_multiple_positions_independent(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr.set_stop_loss("200", 195.0)
        pos = [
            make_position("100", net_qty=75, avg_price=100, ltp=94.0),
            make_position("200", net_qty=75, avg_price=200, ltp=196.0),
        ]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["security_id"] == "100"

    def test_trigger_returns_correct_fields(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0,
                             exchange_segment="NSE_FNO", product_type="MARGIN")]
        triggered = mgr.check_sl_tp_triggers(pos)
        t = triggered[0]
        assert t["action"] == "STOP_LOSS"
        assert t["security_id"] == "100"
        assert t["exchange_segment"] == "NSE_FNO"
        assert t["product_type"] == "MARGIN"
        assert t["quantity"] == 75
        assert t["transaction_type"] == "SELL"
        assert t["trigger_price"] == 95.0
        assert t["ltp"] == 94.0

    def test_long_trigger_sells(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert triggered[0]["transaction_type"] == "SELL"

    def test_short_trigger_buys(self, mgr):
        mgr.set_stop_loss("100", 105.0)
        pos = [make_position("100", net_qty=-75, ltp=106.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert triggered[0]["transaction_type"] == "BUY"

    def test_sl_no_tp_only_sl_checked(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["action"] == "STOP_LOSS"

    def test_tp_no_sl_only_tp_checked(self, mgr):
        mgr.set_take_profit("100", 110.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=111.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["action"] == "TAKE_PROFIT"

    def test_neither_sl_nor_tp_no_trigger(self, mgr):
        """Config exists but both SL and TP are None."""
        mgr._sl_tp_orders["100"] = StopLossTarget(position_security_id="100")
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0

    def test_trailing_sl_long_moves_up(self, mgr):
        mgr.set_stop_loss("100", 95.0, trailing=True, trail_points=5.0, trail_trigger=3.0)
        # avg=100, ltp=108 → pnl_per_unit=8 >= trigger=3 → new_sl = 108 - 5 = 103
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=108.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 0
        assert mgr._sl_tp_orders["100"].current_sl_price == 103.0

    def test_trailing_sl_long_does_not_move_down(self, mgr):
        mgr.set_stop_loss("100", 95.0, trailing=True, trail_points=5.0, trail_trigger=3.0)
        # First: price up to 110 → sl = 105
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=110.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 105.0
        # Then: price drops to 106 → sl should stay at 105, not drop to 101
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=106.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 105.0

    def test_trailing_sl_short_moves_down(self, mgr):
        mgr.set_stop_loss("100", 105.0, trailing=True, trail_points=5.0, trail_trigger=3.0)
        # Short: avg=100, ltp=92 → pnl_per_unit = 100-92 = 8 >= 3 → new_sl = 92 + 5 = 97
        pos = [make_position("100", net_qty=-75, avg_price=100, ltp=92.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 97.0

    def test_trailing_sl_short_does_not_move_up(self, mgr):
        mgr.set_stop_loss("100", 105.0, trailing=True, trail_points=5.0, trail_trigger=3.0)
        # Price drops to 90 → sl = 95
        pos = [make_position("100", net_qty=-75, avg_price=100, ltp=90.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 95.0
        # Price rises to 94 → sl stays 95
        pos = [make_position("100", net_qty=-75, avg_price=100, ltp=94.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 95.0

    def test_trailing_sl_not_triggered_below_threshold(self, mgr):
        mgr.set_stop_loss("100", 95.0, trailing=True, trail_points=5.0, trail_trigger=10.0)
        # avg=100, ltp=108 → pnl_per_unit=8 < trigger=10 → SL stays at 95
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=108.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 95.0


# ═══════════════════════════════════════════════════════════════════
# 3. SL/TP STRESS TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSLTPStress:

    def test_rapid_price_updates_50_ticks(self, mgr):
        mgr.set_stop_loss("100", 95.0, trailing=True, trail_points=5.0, trail_trigger=2.0)
        # Simulate 50 ticks with rising then falling prices
        prices = [100 + i * 0.5 for i in range(25)] + [112 - i * 0.3 for i in range(25)]
        max_sl = 0.0
        for p in prices:
            pos = [make_position("100", net_qty=75, avg_price=100, ltp=p)]
            mgr.check_sl_tp_triggers(pos)
            sl = mgr._sl_tp_orders["100"].current_sl_price
            if sl and sl > max_sl:
                max_sl = sl
        # SL should never go down for longs
        assert mgr._sl_tp_orders["100"].current_sl_price == max_sl

    def test_sl_hit_on_gap_down(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=80.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["ltp"] == 80.0

    def test_tp_hit_on_gap_up(self, mgr):
        mgr.set_take_profit("100", 110.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=130.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1
        assert triggered[0]["ltp"] == 130.0

    def test_100_positions_performance(self, mgr):
        for i in range(100):
            mgr.set_stop_loss(str(i), 90.0)
        positions = [make_position(str(i), net_qty=75, avg_price=100, ltp=91.0) for i in range(100)]
        start = time.time()
        triggered = mgr.check_sl_tp_triggers(positions)
        elapsed = time.time() - start
        assert elapsed < 1.0  # Should be < 1 second
        assert len(triggered) == 0

    def test_trailing_sl_realistic_scalp(self, mgr):
        mgr.set_stop_loss("100", 95.0, trailing=True, trail_points=5.0, trail_trigger=3.0)
        # price: 100→110→108→115→112
        expected_sl = [95.0, 105.0, 105.0, 110.0, 110.0]
        prices = [100, 110, 108, 115, 112]
        for p, expected in zip(prices, expected_sl):
            pos = [make_position("100", net_qty=75, avg_price=100, ltp=p)]
            mgr.check_sl_tp_triggers(pos)
            assert mgr._sl_tp_orders["100"].current_sl_price == expected, \
                f"At price {p}, expected SL {expected}, got {mgr._sl_tp_orders['100'].current_sl_price}"

    def test_simultaneous_sl_tp_at_same_price(self, mgr):
        mgr.set_stop_loss("100", 100.0)
        mgr.set_take_profit("100", 100.0)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=100.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        # Both trigger on same tick since code checks independently
        assert len(triggered) == 2

    def test_negative_pnl_trailing_not_activated(self, mgr):
        mgr.set_stop_loss("100", 95.0, trailing=True, trail_points=5.0, trail_trigger=3.0)
        # avg=100, ltp=98 → pnl_per_unit=-2 < trigger=3 → trailing not engaged
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=98.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 95.0

    def test_very_small_trailing_points(self, mgr):
        mgr.set_stop_loss("100", 99.90, trailing=True, trail_points=0.05, trail_trigger=0.10)
        # avg=100, ltp=100.15 → pnl_per_unit=0.15 >= 0.10 → new_sl = 100.15 - 0.05 = 100.10
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=100.15)]
        mgr.check_sl_tp_triggers(pos)
        assert abs(mgr._sl_tp_orders["100"].current_sl_price - 100.10) < 0.001

    def test_very_large_quantity(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos = [make_position("100", net_qty=10000, avg_price=100, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert triggered[0]["quantity"] == 10000

    def test_floating_point_sl_boundary(self, mgr):
        mgr.set_stop_loss("100", 99.95)
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=99.95)]
        triggered = mgr.check_sl_tp_triggers(pos)
        assert len(triggered) == 1

    def test_position_changes_qty_between_ticks(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        pos1 = [make_position("100", net_qty=75, avg_price=100, ltp=96.0)]
        mgr.check_sl_tp_triggers(pos1)
        # Partial fill reduces qty
        pos2 = [make_position("100", net_qty=50, avg_price=100, ltp=94.0)]
        triggered = mgr.check_sl_tp_triggers(pos2)
        assert triggered[0]["quantity"] == 50

    def test_config_survives_across_multiple_checks(self, mgr):
        mgr.set_stop_loss("100", 95.0)
        mgr.set_take_profit("100", 110.0)
        for _ in range(10):
            pos = [make_position("100", net_qty=75, avg_price=100, ltp=100.0)]
            mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].is_active is True
        assert mgr._sl_tp_orders["100"].stop_loss_price == 95.0

    def test_all_positions_trigger_simultaneously(self, mgr):
        for i in range(5):
            mgr.set_stop_loss(str(i), 95.0)
        positions = [make_position(str(i), net_qty=75, avg_price=100, ltp=94.0) for i in range(5)]
        triggered = mgr.check_sl_tp_triggers(positions)
        assert len(triggered) == 5

    def test_mixed_long_short_portfolio(self, mgr):
        # 3 longs with SL=95, 2 shorts with SL=105
        for i in range(3):
            mgr.set_stop_loss(f"L{i}", 95.0)
        for i in range(2):
            mgr.set_stop_loss(f"S{i}", 105.0)
        positions = [
            make_position(f"L{i}", net_qty=75, avg_price=100, ltp=94.0) for i in range(3)
        ] + [
            make_position(f"S{i}", net_qty=-75, avg_price=100, ltp=106.0) for i in range(2)
        ]
        triggered = mgr.check_sl_tp_triggers(positions)
        assert len(triggered) == 5
        long_triggers = [t for t in triggered if t["transaction_type"] == "SELL"]
        short_triggers = [t for t in triggered if t["transaction_type"] == "BUY"]
        assert len(long_triggers) == 3
        assert len(short_triggers) == 2

    def test_trailing_sl_from_none_initial(self, mgr):
        """When trailing SL starts with current_sl_price=None, first trigger sets it."""
        mgr._sl_tp_orders["100"] = StopLossTarget(
            position_security_id="100",
            stop_loss_price=95.0,
            current_sl_price=None,
            trailing_sl=True,
            trailing_sl_points=5.0,
            trailing_sl_trigger=3.0,
        )
        pos = [make_position("100", net_qty=75, avg_price=100, ltp=108.0)]
        mgr.check_sl_tp_triggers(pos)
        assert mgr._sl_tp_orders["100"].current_sl_price == 103.0


# ═══════════════════════════════════════════════════════════════════
# 4. SPREAD DETECTION TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSpreadDetection:

    def _make_fno_position(self, security_id, net_qty, avg_price, ltp,
                           option_type, strike, expiry="2026-03-05"):
        return {
            "securityId": security_id,
            "netQty": net_qty,
            "avgPrice": avg_price,
            "lastTradedPrice": ltp,
            "exchangeSegment": "NSE_FNO",
            "drvOptionType": option_type,
            "drvStrikePrice": strike,
            "expiryDate": expiry,
        }

    def test_bull_put_spread_detected(self, mgr):
        positions = [
            self._make_fno_position("1", -75, 120, 115, "PUT", 24000),  # short high put
            self._make_fno_position("2", 75, 80, 75, "PUT", 23900),     # long low put
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) >= 1
        types = [s.spread_type for s in spreads]
        assert "BULL_PUT_SPREAD" in types

    def test_bear_call_spread_detected(self, mgr):
        positions = [
            self._make_fno_position("1", -75, 120, 115, "CALL", 24000),  # short low call
            self._make_fno_position("2", 75, 80, 75, "CALL", 24100),     # long high call
        ]
        spreads = mgr.detect_spreads(positions)
        types = [s.spread_type for s in spreads]
        assert "BEAR_CALL_SPREAD" in types

    def test_bull_call_spread_detected(self, mgr):
        positions = [
            self._make_fno_position("1", 75, 120, 130, "CALL", 24000),  # long low call
            self._make_fno_position("2", -75, 80, 75, "CALL", 24100),   # short high call
        ]
        spreads = mgr.detect_spreads(positions)
        types = [s.spread_type for s in spreads]
        assert "BULL_CALL_SPREAD" in types

    def test_bear_put_spread_detected(self, mgr):
        positions = [
            self._make_fno_position("1", 75, 120, 130, "PUT", 24100),  # long high put
            self._make_fno_position("2", -75, 80, 75, "PUT", 24000),   # short low put
        ]
        spreads = mgr.detect_spreads(positions)
        types = [s.spread_type for s in spreads]
        assert "BEAR_PUT_SPREAD" in types

    def test_iron_condor_detected(self, mgr):
        positions = [
            self._make_fno_position("1", -75, 50, 45, "CALL", 24200),   # short call
            self._make_fno_position("2", 75, 20, 18, "CALL", 24300),    # long call (higher)
            self._make_fno_position("3", -75, 50, 45, "PUT", 23800),    # short put
            self._make_fno_position("4", 75, 20, 18, "PUT", 23700),     # long put (lower)
        ]
        spreads = mgr.detect_spreads(positions)
        types = [s.spread_type for s in spreads]
        assert "IRON_CONDOR" in types

    def test_no_spread_single_leg(self, mgr):
        positions = [
            self._make_fno_position("1", 75, 100, 95, "CALL", 24000),
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) == 0

    def test_no_spread_equity_only(self, mgr):
        positions = [
            {"securityId": "1", "netQty": 100, "avgPrice": 1500,
             "lastTradedPrice": 1510, "exchangeSegment": "NSE_EQ"},
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) == 0

    def test_no_spread_zero_qty(self, mgr):
        positions = [
            self._make_fno_position("1", 0, 100, 95, "CALL", 24000),
            self._make_fno_position("2", 0, 80, 85, "CALL", 24100),
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) == 0

    def test_matched_quantity_min(self, mgr):
        positions = [
            self._make_fno_position("1", -150, 120, 115, "PUT", 24000),  # short 150
            self._make_fno_position("2", 75, 80, 75, "PUT", 23900),      # long 75
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) >= 1
        for leg in spreads[0].legs:
            assert abs(leg.quantity) == 75

    def test_different_expiries_separate_groups(self, mgr):
        positions = [
            self._make_fno_position("1", -75, 120, 115, "CALL", 24000, "2026-03-05"),
            self._make_fno_position("2", 75, 80, 75, "CALL", 24100, "2026-03-12"),
        ]
        spreads = mgr.detect_spreads(positions)
        # Different expiries → should not form a spread within the same group
        # They're in different groups, each group has only 1 leg
        assert len(spreads) == 0

    def test_net_premium_credit_spread(self, mgr):
        # Bull put: short high put (high premium) + long low put (low premium) → credit
        positions = [
            self._make_fno_position("1", -75, 120, 115, "PUT", 24000),
            self._make_fno_position("2", 75, 80, 75, "PUT", 23900),
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) >= 1
        bull_put = [s for s in spreads if s.spread_type == "BULL_PUT_SPREAD"][0]
        # net_premium = (short_entry - long_entry) * matched_qty = (120 - 80) * 75 = 3000
        assert bull_put.net_premium == (120 - 80) * 75

    def test_net_premium_debit_spread(self, mgr):
        # Bull call: long low call (expensive) + short high call (cheap) → debit
        positions = [
            self._make_fno_position("1", 75, 120, 130, "CALL", 24000),  # long, paid 120
            self._make_fno_position("2", -75, 40, 35, "CALL", 24100),   # short, received 40
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) >= 1
        bull_call = [s for s in spreads if s.spread_type == "BULL_CALL_SPREAD"][0]
        # net_premium = (short_entry - long_entry) * matched_qty = (40 - 120) * 75 = -6000
        assert bull_call.net_premium == (40 - 120) * 75

    def test_multiple_spreads_same_expiry(self, mgr):
        # 2 bull put spreads with different strikes
        positions = [
            self._make_fno_position("1", -75, 120, 115, "PUT", 24000),
            self._make_fno_position("2", 75, 80, 75, "PUT", 23900),
            self._make_fno_position("3", -75, 100, 95, "PUT", 23800),
            self._make_fno_position("4", 75, 60, 55, "PUT", 23700),
        ]
        spreads = mgr.detect_spreads(positions)
        # Should detect multiple spreads (combinatorial matching)
        assert len(spreads) >= 2

    def test_empty_positions_returns_empty(self, mgr):
        spreads = mgr.detect_spreads([])
        assert spreads == []

    def test_spread_type_classification(self, mgr):
        # Bear call spread
        positions = [
            self._make_fno_position("1", -75, 120, 115, "CALL", 24000),
            self._make_fno_position("2", 75, 80, 75, "CALL", 24100),
        ]
        spreads = mgr.detect_spreads(positions)
        assert spreads[0].spread_type == "BEAR_CALL_SPREAD"

    def test_legs_have_correct_quantities(self, mgr):
        positions = [
            self._make_fno_position("1", -75, 120, 115, "PUT", 24000),
            self._make_fno_position("2", 75, 80, 75, "PUT", 23900),
        ]
        spreads = mgr.detect_spreads(positions)
        for spread in spreads:
            for leg in spread.legs:
                if leg.strike_price == 24000:
                    assert leg.quantity < 0  # short
                else:
                    assert leg.quantity > 0  # long

    def test_spread_detection_clears_previous(self, mgr):
        positions1 = [
            self._make_fno_position("1", -75, 120, 115, "PUT", 24000),
            self._make_fno_position("2", 75, 80, 75, "PUT", 23900),
        ]
        spreads1 = mgr.detect_spreads(positions1)
        assert len(spreads1) >= 1
        # Now detect with empty
        spreads2 = mgr.detect_spreads([])
        assert len(spreads2) == 0

    def test_iron_condor_matched_qty_all_four(self, mgr):
        positions = [
            self._make_fno_position("1", -150, 50, 45, "CALL", 24200),
            self._make_fno_position("2", 75, 20, 18, "CALL", 24300),
            self._make_fno_position("3", -100, 50, 45, "PUT", 23800),
            self._make_fno_position("4", 200, 20, 18, "PUT", 23700),
        ]
        spreads = mgr.detect_spreads(positions)
        ic_spreads = [s for s in spreads if s.spread_type == "IRON_CONDOR"]
        if ic_spreads:
            ic = ic_spreads[0]
            qtys = [abs(leg.quantity) for leg in ic.legs]
            assert all(q == min(qtys) for q in qtys)

    def test_iron_condor_requires_all_four_types(self, mgr):
        # Missing long put → no iron condor
        positions = [
            self._make_fno_position("1", -75, 50, 45, "CALL", 24200),
            self._make_fno_position("2", 75, 20, 18, "CALL", 24300),
            self._make_fno_position("3", -75, 50, 45, "PUT", 23800),
            # No long put
        ]
        spreads = mgr.detect_spreads(positions)
        ic_spreads = [s for s in spreads if s.spread_type == "IRON_CONDOR"]
        assert len(ic_spreads) == 0

    def test_same_option_type_both_long(self, mgr):
        """Two long calls should not form a spread."""
        positions = [
            self._make_fno_position("1", 75, 120, 130, "CALL", 24000),
            self._make_fno_position("2", 75, 80, 85, "CALL", 24100),
        ]
        spreads = mgr.detect_spreads(positions)
        assert len(spreads) == 0


# ═══════════════════════════════════════════════════════════════════
# 5. SPREAD PROPERTIES (DATACLASS METHODS) TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSpreadProperties:

    def test_option_leg_pnl_long_profit(self):
        leg = OptionLeg("1", "NSE_FNO", "CALL", 24000, "2026-03-05",
                        quantity=75, entry_price=100, current_price=120)
        assert leg.pnl == (120 - 100) * 75

    def test_option_leg_pnl_long_loss(self):
        leg = OptionLeg("1", "NSE_FNO", "CALL", 24000, "2026-03-05",
                        quantity=75, entry_price=100, current_price=80)
        assert leg.pnl == (80 - 100) * 75

    def test_option_leg_pnl_short_profit(self):
        leg = OptionLeg("1", "NSE_FNO", "PUT", 24000, "2026-03-05",
                        quantity=-75, entry_price=100, current_price=80)
        assert leg.pnl == (100 - 80) * 75

    def test_option_leg_pnl_short_loss(self):
        leg = OptionLeg("1", "NSE_FNO", "PUT", 24000, "2026-03-05",
                        quantity=-75, entry_price=100, current_price=120)
        assert leg.pnl == (100 - 120) * 75

    def test_option_leg_is_long(self):
        leg = OptionLeg("1", "NSE_FNO", "CALL", 24000, "2026-03-05",
                        quantity=75, entry_price=100)
        assert leg.is_long is True
        assert leg.is_short is False

    def test_option_leg_is_short(self):
        leg = OptionLeg("1", "NSE_FNO", "CALL", 24000, "2026-03-05",
                        quantity=-75, entry_price=100)
        assert leg.is_long is False
        assert leg.is_short is True

    def test_credit_spread_max_profit(self):
        spread = SpreadPosition(
            spread_type="BULL_PUT_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "PUT", 24000, "exp", -75, 120, 115),
                OptionLeg("2", "NSE_FNO", "PUT", 23900, "exp", 75, 80, 75),
            ],
            net_premium=3000,
        )
        assert spread.max_profit == 3000

    def test_credit_spread_max_loss(self):
        spread = SpreadPosition(
            spread_type="BULL_PUT_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "PUT", 24000, "exp", -75, 120, 115),
                OptionLeg("2", "NSE_FNO", "PUT", 23900, "exp", 75, 80, 75),
            ],
            net_premium=3000,
        )
        # width = |24000 - 23900| = 100, max_loss = 100 * 75 - 3000 = 4500
        assert spread.max_loss == 100 * 75 - 3000

    def test_debit_spread_max_profit(self):
        spread = SpreadPosition(
            spread_type="BULL_CALL_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "CALL", 24000, "exp", 75, 120, 130),
                OptionLeg("2", "NSE_FNO", "CALL", 24100, "exp", -75, 40, 35),
            ],
            net_premium=-6000,  # Debit paid
        )
        # max_profit = width * qty - |premium| = 100 * 75 - 6000 = 1500
        assert spread.max_profit == 100 * 75 - 6000

    def test_debit_spread_max_loss(self):
        spread = SpreadPosition(
            spread_type="BULL_CALL_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "CALL", 24000, "exp", 75, 120, 130),
                OptionLeg("2", "NSE_FNO", "CALL", 24100, "exp", -75, 40, 35),
            ],
            net_premium=-6000,
        )
        assert spread.max_loss == 6000

    def test_iron_condor_max_profit(self):
        spread = SpreadPosition(
            spread_type="IRON_CONDOR",
            legs=[
                OptionLeg("4", "NSE_FNO", "PUT", 23700, "exp", 75, 20, 18),
                OptionLeg("3", "NSE_FNO", "PUT", 23800, "exp", -75, 50, 45),
                OptionLeg("1", "NSE_FNO", "CALL", 24200, "exp", -75, 50, 45),
                OptionLeg("2", "NSE_FNO", "CALL", 24300, "exp", 75, 20, 18),
            ],
            net_premium=4500,
        )
        assert spread.max_profit == 4500

    def test_iron_condor_max_loss(self):
        spread = SpreadPosition(
            spread_type="IRON_CONDOR",
            legs=[
                OptionLeg("4", "NSE_FNO", "PUT", 23700, "exp", 75, 20, 18),
                OptionLeg("3", "NSE_FNO", "PUT", 23800, "exp", -75, 50, 45),
                OptionLeg("1", "NSE_FNO", "CALL", 24200, "exp", -75, 50, 45),
                OptionLeg("2", "NSE_FNO", "CALL", 24300, "exp", 75, 20, 18),
            ],
            net_premium=4500,
        )
        # call_width = |24200 - 24300| = 100, put_width = |23700 - 23800| = 100
        # max_loss = max(100, 100) * 75 - 4500 = 3000
        assert spread.max_loss == max(100, 100) * 75 - 4500

    def test_spread_current_pnl(self):
        spread = SpreadPosition(
            spread_type="BULL_PUT_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "PUT", 24000, "exp", -75, 120, 100),
                OptionLeg("2", "NSE_FNO", "PUT", 23900, "exp", 75, 80, 90),
            ],
            net_premium=3000,
        )
        expected = sum(leg.pnl for leg in spread.legs)
        assert spread.current_pnl == expected

    def test_breakeven_bull_put(self):
        spread = SpreadPosition(
            spread_type="BULL_PUT_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "PUT", 24000, "exp", -75, 120, 115),
                OptionLeg("2", "NSE_FNO", "PUT", 23900, "exp", 75, 80, 75),
            ],
            net_premium=3000,
        )
        be = spread.breakeven_points
        per_lot = 3000 / 75
        assert len(be) == 1
        assert be[0] == 24000 - per_lot  # max_strike - per_lot

    def test_breakeven_bear_call(self):
        spread = SpreadPosition(
            spread_type="BEAR_CALL_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "CALL", 24000, "exp", -75, 120, 115),
                OptionLeg("2", "NSE_FNO", "CALL", 24100, "exp", 75, 80, 75),
            ],
            net_premium=3000,
        )
        be = spread.breakeven_points
        per_lot = 3000 / 75
        assert len(be) == 1
        assert be[0] == 24000 + per_lot  # min_strike + per_lot

    def test_breakeven_iron_condor_two_points(self):
        spread = SpreadPosition(
            spread_type="IRON_CONDOR",
            legs=[
                OptionLeg("4", "NSE_FNO", "PUT", 23700, "exp", 75, 20, 18),
                OptionLeg("3", "NSE_FNO", "PUT", 23800, "exp", -75, 50, 45),
                OptionLeg("1", "NSE_FNO", "CALL", 24200, "exp", -75, 50, 45),
                OptionLeg("2", "NSE_FNO", "CALL", 24300, "exp", 75, 20, 18),
            ],
            net_premium=4500,
        )
        be = spread.breakeven_points
        assert len(be) == 2
        per_lot = 4500 / 75
        assert be[0] == 23700 + per_lot  # lower breakeven
        assert be[1] == 24300 - per_lot  # upper breakeven

    def test_empty_spread_no_breakevens(self):
        spread = SpreadPosition(spread_type="BULL_PUT_SPREAD")
        assert spread.breakeven_points == []


# ═══════════════════════════════════════════════════════════════════
# 6. PROJECTION TESTS
# ═══════════════════════════════════════════════════════════════════

class TestProjections:

    def test_long_call_projection_itm(self, mgr):
        pos = make_position("100", net_qty=75, avg_price=100, ltp=110,
                            option_type="CALL", strike_price=24000)
        projections = mgr.calculate_projections(pos, [24200])
        assert projections[0]["option_intrinsic"] == 200
        assert projections[0]["projected_pnl"] == (200 - 100) * 75

    def test_long_call_projection_otm(self, mgr):
        pos = make_position("100", net_qty=75, avg_price=100, ltp=110,
                            option_type="CALL", strike_price=24000)
        projections = mgr.calculate_projections(pos, [23800])
        assert projections[0]["option_intrinsic"] == 0
        assert projections[0]["projected_pnl"] == (0 - 100) * 75

    def test_long_put_projection_itm(self, mgr):
        pos = make_position("100", net_qty=75, avg_price=100, ltp=110,
                            option_type="PUT", strike_price=24000)
        projections = mgr.calculate_projections(pos, [23800])
        assert projections[0]["option_intrinsic"] == 200
        assert projections[0]["projected_pnl"] == (200 - 100) * 75

    def test_short_call_projection(self, mgr):
        pos = make_position("100", net_qty=-75, avg_price=100, ltp=95,
                            option_type="CALL", strike_price=24000)
        projections = mgr.calculate_projections(pos, [24200])
        # Short: pnl = (entry - intrinsic) * |qty|
        assert projections[0]["projected_pnl"] == (100 - 200) * 75

    def test_spread_projection_at_various_prices(self, mgr):
        spread = SpreadPosition(
            spread_type="BULL_PUT_SPREAD",
            legs=[
                OptionLeg("1", "NSE_FNO", "PUT", 24000, "exp", -75, 120, 115),
                OptionLeg("2", "NSE_FNO", "PUT", 23900, "exp", 75, 80, 75),
            ],
            net_premium=3000,
        )
        targets = [23700, 23900, 24000, 24100, 24200]
        projections = mgr.calculate_spread_projections(spread, targets)
        assert len(projections) == 5
        # Above both strikes → both OTM → max profit
        above = projections[4]
        assert above["projected_pnl"] > 0

    def test_projection_at_strike(self, mgr):
        pos = make_position("100", net_qty=75, avg_price=100,
                            option_type="CALL", strike_price=24000)
        projections = mgr.calculate_projections(pos, [24000])
        assert projections[0]["option_intrinsic"] == 0

    def test_projection_deep_itm(self, mgr):
        pos = make_position("100", net_qty=75, avg_price=100,
                            option_type="CALL", strike_price=24000)
        projections = mgr.calculate_projections(pos, [25000])
        assert projections[0]["option_intrinsic"] == 1000

    def test_projection_list_multiple_prices(self, mgr):
        pos = make_position("100", net_qty=75, avg_price=100,
                            option_type="CALL", strike_price=24000)
        targets = list(range(23500, 24600, 100))
        projections = mgr.calculate_projections(pos, targets)
        assert len(projections) == len(targets)

    def test_projection_breakeven_pnl_zero(self, mgr):
        # For a long call with entry=100, breakeven at underlying = strike + entry = 24100
        pos = make_position("100", net_qty=75, avg_price=100,
                            option_type="CALL", strike_price=24000)
        projections = mgr.calculate_projections(pos, [24100])
        assert projections[0]["projected_pnl"] == 0

    def test_projection_is_profitable_flag(self, mgr):
        pos = make_position("100", net_qty=75, avg_price=100,
                            option_type="CALL", strike_price=24000)
        projections = mgr.calculate_projections(pos, [24200, 23800])
        assert projections[0]["is_profitable"] is True   # ITM with profit
        assert projections[1]["is_profitable"] is False  # OTM


# ═══════════════════════════════════════════════════════════════════
# 7. PENDING SPREAD TESTS
# ═══════════════════════════════════════════════════════════════════

class TestPendingSpreads:

    def test_add_pending_spread(self, mgr):
        spread_id = mgr.add_pending_spread({
            "sell_security_id": "100",
            "sell_price": 120.0,
            "sell_trigger_price": 115.0,
            "sell_sl": 130.0,
            "buy_security_id": "200",
            "quantity": 75,
        })
        assert spread_id.startswith("SP")
        assert spread_id in mgr._pending_spreads
        s = mgr._pending_spreads[spread_id]
        assert s.sell_security_id == "100"
        assert s.quantity == 75
        assert s.status == "PENDING"

    def test_cancel_pending_spread(self, mgr):
        sid = mgr.add_pending_spread({
            "sell_security_id": "100", "sell_price": 120.0,
            "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
        })
        assert mgr.cancel_pending_spread(sid) is True
        assert mgr._pending_spreads[sid].status == "CANCELLED"

    def test_cancel_already_triggered(self, mgr):
        sid = mgr.add_pending_spread({
            "sell_security_id": "100", "sell_price": 120.0,
            "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
        })
        mgr._pending_spreads[sid].status = "TRIGGERED"
        assert mgr.cancel_pending_spread(sid) is False

    def test_get_pending_summary(self, mgr):
        sid1 = mgr.add_pending_spread({
            "sell_security_id": "100", "sell_price": 120.0,
            "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
        })
        sid2 = mgr.add_pending_spread({
            "sell_security_id": "300", "sell_price": 80.0,
            "sell_trigger_price": 75.0, "buy_security_id": "400", "quantity": 75,
        })
        mgr._pending_spreads[sid2].status = "FILLED"
        summary = mgr.get_pending_spreads_summary()
        assert len(summary) == 1
        assert summary[0]["spread_id"] == sid1

    def test_update_spread_status(self, mgr):
        sid = mgr.add_pending_spread({
            "sell_security_id": "100", "sell_price": 120.0,
            "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
        })
        mgr.update_spread_status(sid, "FILLED", hedge_order_id="H1", sell_order_id="S1")
        s = mgr._pending_spreads[sid]
        assert s.status == "FILLED"
        assert s.hedge_order_id == "H1"
        assert s.sell_order_id == "S1"

    def test_multiple_pending_spreads(self, mgr):
        ids = []
        for i in range(5):
            sid = mgr.add_pending_spread({
                "sell_security_id": str(i * 100), "sell_price": 120.0,
                "sell_trigger_price": 115.0, "buy_security_id": str(i * 100 + 1),
                "quantity": 75,
            })
            ids.append(sid)
        assert len(set(ids)) == 5
        assert len(mgr._pending_spreads) == 5

    def test_spread_id_unique(self, mgr):
        ids = set()
        for _ in range(10):
            sid = mgr.add_pending_spread({
                "sell_security_id": "100", "sell_price": 120.0,
                "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
            })
            ids.add(sid)
        assert len(ids) == 10

    def test_check_pending_triggers_ltp_below(self, mgr):
        mgr.api.get_ltp.return_value = {
            "data": {"100": {"last_price": 114.0}}
        }
        mgr.add_pending_spread({
            "sell_security_id": "100", "sell_price": 120.0,
            "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
        })
        triggered = mgr.check_pending_spreads()
        assert len(triggered) == 1
        assert triggered[0]["ltp"] == 114.0

    def test_check_pending_triggers_ltp_above(self, mgr):
        mgr.api.get_ltp.return_value = {
            "data": {"100": {"last_price": 120.0}}
        }
        mgr.add_pending_spread({
            "sell_security_id": "100", "sell_price": 120.0,
            "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
        })
        triggered = mgr.check_pending_spreads()
        assert len(triggered) == 0

    def test_check_pending_with_api_failure(self, mgr):
        mgr.api.get_ltp.side_effect = Exception("API down")
        mgr.add_pending_spread({
            "sell_security_id": "100", "sell_price": 120.0,
            "sell_trigger_price": 115.0, "buy_security_id": "200", "quantity": 75,
        })
        # Should not raise, just return empty
        triggered = mgr.check_pending_spreads()
        assert len(triggered) == 0


# ═══════════════════════════════════════════════════════════════════
# 8. SPREAD SUMMARY TESTS
# ═══════════════════════════════════════════════════════════════════

class TestSpreadSummary:

    def test_get_spread_summary(self, mgr):
        positions = [
            {"securityId": "1", "netQty": -75, "avgPrice": 120, "lastTradedPrice": 115,
             "exchangeSegment": "NSE_FNO", "drvOptionType": "PUT", "drvStrikePrice": 24000,
             "expiryDate": "2026-03-05"},
            {"securityId": "2", "netQty": 75, "avgPrice": 80, "lastTradedPrice": 75,
             "exchangeSegment": "NSE_FNO", "drvOptionType": "PUT", "drvStrikePrice": 23900,
             "expiryDate": "2026-03-05"},
        ]
        mgr.detect_spreads(positions)
        summary = mgr.get_spread_summary()
        assert len(summary) >= 1
        s = summary[0]
        assert "type" in s
        assert "legs" in s
        assert "net_premium" in s
        assert "max_profit" in s
        assert "max_loss" in s
        assert "current_pnl" in s
        assert "breakevens" in s

    def test_get_spread_summary_empty(self, mgr):
        mgr.detect_spreads([])
        summary = mgr.get_spread_summary()
        assert summary == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
