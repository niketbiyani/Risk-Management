"""
Comprehensive tests for analyze_depth() function from dashboard.py.
Tests the pure depth-of-market analysis function covering spread calculations,
imbalance ratios, sentiment, wall detection, and edge cases.
"""

import os
import sys

os.environ["STATE_ENCRYPTION_KEY"] = ""
os.environ["DHAN_CLIENT_ID"] = "test"
os.environ["DHAN_ACCESS_TOKEN"] = "test"

sys.path.insert(0, os.path.dirname(__file__))

import pytest

from dashboard import analyze_depth


# ── Helpers ────────────────────────────────────────────────────────

def make_level(price, quantity, orders=5):
    return {"price": price, "quantity": quantity, "orders": orders}


def make_book(bid_start=100.0, ask_start=100.05, levels=5,
              bid_qty=100, ask_qty=100, bid_orders=5, ask_orders=5):
    """Create a simple symmetric order book."""
    bids = [make_level(bid_start - i * 0.05, bid_qty, bid_orders) for i in range(levels)]
    asks = [make_level(ask_start + i * 0.05, ask_qty, ask_orders) for i in range(levels)]
    return bids, asks


# ═══════════════════════════════════════════════════════════════════
# 1. CORE DEPTH ANALYSIS TESTS
# ═══════════════════════════════════════════════════════════════════

class TestDepthAnalysis:

    def test_spread_calculation(self):
        bids, asks = make_book(100.0, 100.10)
        result = analyze_depth(bids, asks)
        assert result["bid_ask_spread"] == 0.10

    def test_spread_pct(self):
        bids, asks = make_book(100.0, 100.10)
        result = analyze_depth(bids, asks)
        mid = (100.0 + 100.10) / 2
        expected_pct = round(0.10 / mid * 100, 3)
        assert result["spread_pct"] == expected_pct

    def test_mid_price(self):
        bids, asks = make_book(100.0, 100.20)
        result = analyze_depth(bids, asks)
        assert result["mid_price"] == round((100.0 + 100.20) / 2, 2)

    def test_imbalance_ratio_bid_heavy(self):
        bids = [make_level(100.0 - i * 0.05, 200) for i in range(5)]
        asks = [make_level(100.05 + i * 0.05, 100) for i in range(5)]
        result = analyze_depth(bids, asks)
        assert result["imbalance_ratio"] == 2.0

    def test_imbalance_ratio_ask_heavy(self):
        bids = [make_level(100.0 - i * 0.05, 100) for i in range(5)]
        asks = [make_level(100.05 + i * 0.05, 200) for i in range(5)]
        result = analyze_depth(bids, asks)
        assert result["imbalance_ratio"] == 0.5

    def test_imbalance_ratio_balanced(self):
        bids, asks = make_book(bid_qty=100, ask_qty=100)
        result = analyze_depth(bids, asks)
        assert result["imbalance_ratio"] == 1.0

    def test_sentiment_bullish(self):
        # Need imbalance > 1.2
        bids = [make_level(100.0 - i * 0.05, 200) for i in range(5)]
        asks = [make_level(100.05 + i * 0.05, 100) for i in range(5)]
        result = analyze_depth(bids, asks)
        assert result["sentiment"] == "BULLISH"

    def test_sentiment_bearish(self):
        # Need imbalance < 0.8
        bids = [make_level(100.0 - i * 0.05, 70) for i in range(5)]
        asks = [make_level(100.05 + i * 0.05, 100) for i in range(5)]
        result = analyze_depth(bids, asks)
        assert result["sentiment"] == "BEARISH"

    def test_sentiment_neutral(self):
        bids, asks = make_book(bid_qty=100, ask_qty=100)
        result = analyze_depth(bids, asks)
        assert result["sentiment"] == "NEUTRAL"

    def test_max_bid_wall(self):
        bids = [
            make_level(100.0, 100),
            make_level(99.95, 500),  # Wall
            make_level(99.90, 100),
        ]
        asks = [make_level(100.05 + i * 0.05, 100) for i in range(3)]
        result = analyze_depth(bids, asks)
        assert result["max_bid_wall"]["price"] == 99.95
        assert result["max_bid_wall"]["quantity"] == 500

    def test_max_ask_wall(self):
        bids = [make_level(100.0 - i * 0.05, 100) for i in range(3)]
        asks = [
            make_level(100.05, 100),
            make_level(100.10, 800),  # Wall
            make_level(100.15, 100),
        ]
        result = analyze_depth(bids, asks)
        assert result["max_ask_wall"]["price"] == 100.10
        assert result["max_ask_wall"]["quantity"] == 800

    def test_support_levels_above_threshold(self):
        # avg bid qty = (100 + 100 + 400) / 3 = 200
        # threshold = 200 * 1.5 = 300 → only the 400 level qualifies
        bids = [
            make_level(100.0, 100),
            make_level(99.95, 100),
            make_level(99.90, 400),
        ]
        asks = [make_level(100.05 + i * 0.05, 100) for i in range(3)]
        result = analyze_depth(bids, asks)
        assert len(result["support_levels"]) == 1
        assert result["support_levels"][0]["quantity"] == 400

    def test_support_levels_below_threshold(self):
        # All equal → avg = 100, threshold = 150 → none above
        bids, asks = make_book(bid_qty=100, ask_qty=100, levels=3)
        result = analyze_depth(bids, asks)
        assert len(result["support_levels"]) == 0

    def test_resistance_levels(self):
        bids = [make_level(100.0 - i * 0.05, 100) for i in range(3)]
        asks = [
            make_level(100.05, 100),
            make_level(100.10, 100),
            make_level(100.15, 500),  # Resistance wall
        ]
        result = analyze_depth(bids, asks)
        # avg ask qty = (100+100+500)/3 ≈ 233, threshold = 350 → 500 qualifies
        assert len(result["resistance_levels"]) == 1
        assert result["resistance_levels"][0]["quantity"] == 500

    def test_institutional_bids(self):
        # avg bid orders = (5 + 5 + 25) / 3 ≈ 11.67
        # threshold = 11.67 * 2 = 23.33 → only 25 orders level counts (> 23.33)
        bids = [
            make_level(100.0, 100, orders=5),
            make_level(99.95, 100, orders=5),
            make_level(99.90, 100, orders=25),
        ]
        asks = [make_level(100.05 + i * 0.05, 100, orders=5) for i in range(3)]
        result = analyze_depth(bids, asks)
        assert result["institutional_bids"] == 1

    def test_institutional_asks(self):
        bids = [make_level(100.0 - i * 0.05, 100, orders=5) for i in range(3)]
        asks = [
            make_level(100.05, 100, orders=5),
            make_level(100.10, 100, orders=25),
            make_level(100.15, 100, orders=30),
        ]
        result = analyze_depth(bids, asks)
        # avg ask orders = (5+25+30)/3 = 20, threshold = 40 → none strictly > 40
        # Actually 30 > 40? No. Let's recalculate:
        # avg = (5+25+30)/3 = 20. 2x = 40. 25 not > 40, 30 not > 40
        assert result["institutional_asks"] == 0

    def test_institutional_asks_with_high_orders(self):
        bids = [make_level(100.0 - i * 0.05, 100, orders=5) for i in range(3)]
        asks = [
            make_level(100.05, 100, orders=5),
            make_level(100.10, 100, orders=5),
            make_level(100.15, 100, orders=50),
        ]
        result = analyze_depth(bids, asks)
        # avg = (5+5+50)/3 = 20, threshold = 40, 50 > 40 → 1
        assert result["institutional_asks"] == 1

    def test_total_quantities(self):
        bids, asks = make_book(bid_qty=150, ask_qty=200, levels=5)
        result = analyze_depth(bids, asks)
        assert result["total_bid_qty"] == 150 * 5
        assert result["total_ask_qty"] == 200 * 5

    def test_all_return_keys_present(self):
        bids, asks = make_book()
        result = analyze_depth(bids, asks)
        expected_keys = [
            "bid_ask_spread", "spread_pct", "mid_price",
            "total_bid_qty", "total_ask_qty", "imbalance_ratio",
            "max_bid_wall", "max_ask_wall",
            "support_levels", "resistance_levels",
            "institutional_bids", "institutional_asks",
            "sentiment",
        ]
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_single_level_each_side(self):
        bids = [make_level(100.0, 100)]
        asks = [make_level(100.05, 100)]
        result = analyze_depth(bids, asks)
        assert result["bid_ask_spread"] == 0.05
        assert result["total_bid_qty"] == 100

    def test_20_levels_each_side(self):
        bids = [make_level(100.0 - i * 0.05, 100 + i * 10) for i in range(20)]
        asks = [make_level(100.05 + i * 0.05, 100 + i * 10) for i in range(20)]
        result = analyze_depth(bids, asks)
        assert result["total_bid_qty"] == sum(100 + i * 10 for i in range(20))
        assert result["total_ask_qty"] == sum(100 + i * 10 for i in range(20))


# ═══════════════════════════════════════════════════════════════════
# 2. EDGE CASE TESTS
# ═══════════════════════════════════════════════════════════════════

class TestDepthEdgeCases:

    def test_empty_bids_returns_empty(self):
        asks = [make_level(100.05, 100)]
        result = analyze_depth([], asks)
        assert result == {}

    def test_empty_asks_returns_empty(self):
        bids = [make_level(100.0, 100)]
        result = analyze_depth(bids, [])
        assert result == {}

    def test_both_empty_returns_empty(self):
        result = analyze_depth([], [])
        assert result == {}

    def test_zero_ask_quantity_no_division_error(self):
        bids = [make_level(100.0, 100)]
        asks = [make_level(100.05, 0)]
        result = analyze_depth(bids, asks)
        assert result["imbalance_ratio"] == 999

    def test_all_same_quantity_no_support_resistance(self):
        bids, asks = make_book(bid_qty=100, ask_qty=100, levels=5)
        result = analyze_depth(bids, asks)
        # All equal → avg = 100, threshold = 150 → none exceed
        assert len(result["support_levels"]) == 0
        assert len(result["resistance_levels"]) == 0

    def test_one_huge_wall_rest_tiny(self):
        bids = [
            make_level(100.0, 10),
            make_level(99.95, 10),
            make_level(99.90, 10000),
        ]
        asks = [make_level(100.05 + i * 0.05, 10) for i in range(3)]
        result = analyze_depth(bids, asks)
        assert result["max_bid_wall"]["quantity"] == 10000
        assert len(result["support_levels"]) == 1
        assert result["support_levels"][0]["quantity"] == 10000

    def test_large_numbers_no_overflow(self):
        bids = [make_level(50000.0 - i * 0.05, 10_000_000) for i in range(5)]
        asks = [make_level(50000.05 + i * 0.05, 10_000_000) for i in range(5)]
        result = analyze_depth(bids, asks)
        assert result["total_bid_qty"] == 50_000_000
        assert result["total_ask_qty"] == 50_000_000

    def test_zero_prices(self):
        bids = [make_level(0.0, 100)]
        asks = [make_level(0.0, 100)]
        result = analyze_depth(bids, asks)
        assert result["bid_ask_spread"] == 0.0
        assert result["spread_pct"] == 0  # mid_price = 0 → 0%

    def test_negative_spread_crossed_book(self):
        # Crossed book: ask price < bid price
        bids = [make_level(100.10, 100)]
        asks = [make_level(100.00, 100)]
        result = analyze_depth(bids, asks)
        assert result["bid_ask_spread"] == -0.10

    def test_single_order_per_level_no_institutional(self):
        bids = [make_level(100.0 - i * 0.05, 100, orders=1) for i in range(5)]
        asks = [make_level(100.05 + i * 0.05, 100, orders=1) for i in range(5)]
        result = analyze_depth(bids, asks)
        # avg orders = 1, threshold = 2 → none exceed
        assert result["institutional_bids"] == 0
        assert result["institutional_asks"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
