"""
Trade Management Features.
Provides SL/TP management, projected P&L calculations for options,
spread detection, and order management utilities.

Designed for Nifty options scalping and credit spread strategies.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class OptionLeg:
    """Represents one leg of an options position."""
    security_id: str
    exchange_segment: str
    option_type: str  # "CALL" or "PUT"
    strike_price: float
    expiry: str
    quantity: int  # positive = long, negative = short
    entry_price: float
    current_price: float = 0.0
    ltp: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def pnl(self) -> float:
        if self.is_long:
            return (self.current_price - self.entry_price) * abs(self.quantity)
        else:
            return (self.entry_price - self.current_price) * abs(self.quantity)


@dataclass
class SpreadPosition:
    """Represents a detected option spread (credit/debit)."""
    spread_type: str  # "CREDIT_SPREAD", "DEBIT_SPREAD", "IRON_CONDOR", etc.
    legs: list[OptionLeg] = field(default_factory=list)
    net_premium: float = 0.0  # Positive = credit received, Negative = debit paid

    @property
    def max_profit(self) -> float:
        """Maximum possible profit for the spread."""
        if self.spread_type in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
            return abs(self.net_premium)
        elif self.spread_type in ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"):
            if len(self.legs) >= 2:
                width = abs(self.legs[0].strike_price - self.legs[1].strike_price)
                return (width * abs(self.legs[0].quantity)) - abs(self.net_premium)
            return 0
        elif self.spread_type == "IRON_CONDOR":
            return abs(self.net_premium)
        return 0

    @property
    def max_loss(self) -> float:
        """Maximum possible loss for the spread."""
        if self.spread_type in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
            if len(self.legs) >= 2:
                width = abs(self.legs[0].strike_price - self.legs[1].strike_price)
                return (width * abs(self.legs[0].quantity)) - abs(self.net_premium)
            return 0
        elif self.spread_type in ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"):
            return abs(self.net_premium)
        elif self.spread_type == "IRON_CONDOR":
            if len(self.legs) >= 4:
                # Max loss = wider wing width - net premium
                call_width = abs(self.legs[2].strike_price - self.legs[3].strike_price)
                put_width = abs(self.legs[0].strike_price - self.legs[1].strike_price)
                return max(call_width, put_width) * abs(self.legs[0].quantity) - abs(self.net_premium)
            return 0
        return 0

    @property
    def current_pnl(self) -> float:
        return sum(leg.pnl for leg in self.legs)

    @property
    def breakeven_points(self) -> list[float]:
        """Calculate breakeven strike prices."""
        if not self.legs:
            return []
        strikes = sorted(set(leg.strike_price for leg in self.legs))
        per_lot = abs(self.net_premium) / abs(self.legs[0].quantity) if self.legs[0].quantity else 0

        if self.spread_type == "BULL_PUT_SPREAD":
            return [max(strikes) - per_lot]
        elif self.spread_type == "BEAR_CALL_SPREAD":
            return [min(strikes) + per_lot]
        elif self.spread_type == "BULL_CALL_SPREAD":
            return [min(strikes) + per_lot]
        elif self.spread_type == "BEAR_PUT_SPREAD":
            return [max(strikes) - per_lot]
        elif self.spread_type == "IRON_CONDOR":
            return [
                min(strikes) + per_lot,  # Lower breakeven
                max(strikes) - per_lot,  # Upper breakeven
            ]
        return []


@dataclass
class StopLossTarget:
    """SL/TP configuration for a position."""
    position_security_id: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    trailing_sl: bool = False
    trailing_sl_points: float = 0.0
    trailing_sl_trigger: float = 0.0  # Move SL after this profit
    current_sl_price: Optional[float] = None  # Actual current SL (may differ if trailing)
    exchange_sl_order_id: Optional[str] = None  # Dhan order ID for exchange STOP_LOSS_MARKET
    is_active: bool = True


@dataclass
class PendingSpreadOrder:
    """A pending spread order waiting for trigger price."""
    spread_id: str
    sell_security_id: str
    sell_exchange_segment: str
    sell_price: float
    sell_trigger_price: float
    sell_sl: float
    buy_security_id: str
    buy_exchange_segment: str
    quantity: int
    sell_order_type: str = "LIMIT"
    status: str = "PENDING"  # PENDING, TRIGGERED, FILLED, FAILED
    created_at: float = 0.0
    triggered_at: float = 0.0
    hedge_order_id: str = ""
    sell_order_id: str = ""
    error_message: str = ""


class TradeManager:
    """
    Manages active trades with SL/TP, spread detection, and projections.
    Works alongside the risk engine to provide trade-level management.
    """

    def __init__(self, dhan_api):
        self.api = dhan_api
        self._sl_tp_orders: dict[str, StopLossTarget] = {}
        self._detected_spreads: list[SpreadPosition] = []
        self._pending_spreads: dict[str, PendingSpreadOrder] = {}
        self._spread_counter = 0
        self._position_cache: dict = {}
        logger.info("Trade manager initialized")

    # ── Spread Detection ───────────────────────────────────────────────

    def detect_spreads(self, positions: list[dict]) -> list[SpreadPosition]:
        """
        Analyze open positions and detect option spread combinations.
        Groups positions by expiry and identifies spread types.
        """
        self._detected_spreads = []

        # Filter to FNO positions with non-zero quantity
        fno_positions = [
            p for p in positions
            if p.get("exchangeSegment") in ("NSE_FNO", "BSE_FNO")
            and p.get("netQty", 0) != 0
        ]

        if not fno_positions:
            return []

        # Group by expiry
        by_expiry: dict[str, list] = {}
        for pos in fno_positions:
            expiry = pos.get("expiryDate", "unknown")
            by_expiry.setdefault(expiry, []).append(pos)

        for expiry, group in by_expiry.items():
            # Separate calls and puts
            calls = [p for p in group if p.get("drvOptionType") == "CALL"]
            puts = [p for p in group if p.get("drvOptionType") == "PUT"]

            # Detect vertical call spreads
            if len(calls) >= 2:
                long_calls = [c for c in calls if c.get("netQty", 0) > 0]
                short_calls = [c for c in calls if c.get("netQty", 0) < 0]
                self._detect_vertical_spread(long_calls, short_calls, "CALL", expiry)

            # Detect vertical put spreads
            if len(puts) >= 2:
                long_puts = [p for p in puts if p.get("netQty", 0) > 0]
                short_puts = [p for p in puts if p.get("netQty", 0) < 0]
                self._detect_vertical_spread(long_puts, short_puts, "PUT", expiry)

            # Detect iron condor (short call spread + short put spread)
            if len(calls) >= 2 and len(puts) >= 2:
                self._detect_iron_condor(calls, puts, expiry)

        return self._detected_spreads

    def _detect_vertical_spread(self, long_legs: list, short_legs: list,
                                option_type: str, expiry: str):
        """Detect and classify vertical spreads."""
        for long_pos in long_legs:
            for short_pos in short_legs:
                long_strike = long_pos.get("drvStrikePrice", 0)
                short_strike = short_pos.get("drvStrikePrice", 0)
                long_qty = abs(long_pos.get("netQty", 0))
                short_qty = abs(short_pos.get("netQty", 0))

                # Must have matching quantities for a proper spread
                matched_qty = min(long_qty, short_qty)
                if matched_qty == 0:
                    continue

                long_entry = long_pos.get("avgPrice", 0)
                short_entry = short_pos.get("avgPrice", 0)

                if option_type == "CALL":
                    if short_strike < long_strike:
                        # Bear call spread (credit)
                        spread_type = "BEAR_CALL_SPREAD"
                        net_premium = (short_entry - long_entry) * matched_qty
                    else:
                        # Bull call spread (debit)
                        spread_type = "BULL_CALL_SPREAD"
                        net_premium = (short_entry - long_entry) * matched_qty
                else:  # PUT
                    if short_strike > long_strike:
                        # Bull put spread (credit)
                        spread_type = "BULL_PUT_SPREAD"
                        net_premium = (short_entry - long_entry) * matched_qty
                    else:
                        # Bear put spread (debit)
                        spread_type = "BEAR_PUT_SPREAD"
                        net_premium = (short_entry - long_entry) * matched_qty

                legs = [
                    OptionLeg(
                        security_id=str(short_pos.get("securityId", "")),
                        exchange_segment=short_pos.get("exchangeSegment", ""),
                        option_type=option_type,
                        strike_price=short_strike,
                        expiry=expiry,
                        quantity=-matched_qty,
                        entry_price=short_entry,
                        current_price=short_pos.get("lastTradedPrice", short_entry),
                    ),
                    OptionLeg(
                        security_id=str(long_pos.get("securityId", "")),
                        exchange_segment=long_pos.get("exchangeSegment", ""),
                        option_type=option_type,
                        strike_price=long_strike,
                        expiry=expiry,
                        quantity=matched_qty,
                        entry_price=long_entry,
                        current_price=long_pos.get("lastTradedPrice", long_entry),
                    ),
                ]

                spread = SpreadPosition(
                    spread_type=spread_type,
                    legs=legs,
                    net_premium=net_premium,
                )
                self._detected_spreads.append(spread)

    def _detect_iron_condor(self, calls: list, puts: list, expiry: str):
        """Detect iron condor (simultaneous bull put + bear call spread)."""
        short_calls = [c for c in calls if c.get("netQty", 0) < 0]
        long_calls = [c for c in calls if c.get("netQty", 0) > 0]
        short_puts = [p for p in puts if p.get("netQty", 0) < 0]
        long_puts = [p for p in puts if p.get("netQty", 0) > 0]

        if not (short_calls and long_calls and short_puts and long_puts):
            return

        # Simple detection: 1 short call + 1 long call (higher) + 1 short put + 1 long put (lower)
        sc = min(short_calls, key=lambda x: x.get("drvStrikePrice", 0))
        lc = max(long_calls, key=lambda x: x.get("drvStrikePrice", 0))
        sp = max(short_puts, key=lambda x: x.get("drvStrikePrice", 0))
        lp = min(long_puts, key=lambda x: x.get("drvStrikePrice", 0))

        sc_strike = sc.get("drvStrikePrice", 0)
        lc_strike = lc.get("drvStrikePrice", 0)
        sp_strike = sp.get("drvStrikePrice", 0)
        lp_strike = lp.get("drvStrikePrice", 0)

        if sc_strike < lc_strike and sp_strike > lp_strike:
            matched_qty = min(
                abs(sc.get("netQty", 0)), abs(lc.get("netQty", 0)),
                abs(sp.get("netQty", 0)), abs(lp.get("netQty", 0))
            )

            net_premium = (
                (sc.get("avgPrice", 0) + sp.get("avgPrice", 0)) -
                (lc.get("avgPrice", 0) + lp.get("avgPrice", 0))
            ) * matched_qty

            legs = []
            for pos, qty_sign in [(lp, 1), (sp, -1), (sc, -1), (lc, 1)]:
                legs.append(OptionLeg(
                    security_id=str(pos.get("securityId", "")),
                    exchange_segment=pos.get("exchangeSegment", ""),
                    option_type=pos.get("drvOptionType", ""),
                    strike_price=pos.get("drvStrikePrice", 0),
                    expiry=expiry,
                    quantity=qty_sign * matched_qty,
                    entry_price=pos.get("avgPrice", 0),
                    current_price=pos.get("lastTradedPrice", pos.get("avgPrice", 0)),
                ))

            spread = SpreadPosition(
                spread_type="IRON_CONDOR",
                legs=legs,
                net_premium=net_premium,
            )
            self._detected_spreads.append(spread)

    # ── SL/TP Management ───────────────────────────────────────────────

    def set_stop_loss(self, security_id: str, sl_price: float,
                      trailing: bool = False, trail_points: float = 0,
                      trail_trigger: float = 0, exchange_sl_order_id: str = ""):
        """Set or update stop loss for a position."""
        key = security_id
        if key in self._sl_tp_orders:
            self._sl_tp_orders[key].stop_loss_price = sl_price
            self._sl_tp_orders[key].current_sl_price = sl_price
            self._sl_tp_orders[key].trailing_sl = trailing
            self._sl_tp_orders[key].trailing_sl_points = trail_points
            self._sl_tp_orders[key].trailing_sl_trigger = trail_trigger
            if exchange_sl_order_id:
                self._sl_tp_orders[key].exchange_sl_order_id = exchange_sl_order_id
        else:
            self._sl_tp_orders[key] = StopLossTarget(
                position_security_id=security_id,
                stop_loss_price=sl_price,
                current_sl_price=sl_price,
                trailing_sl=trailing,
                trailing_sl_points=trail_points,
                trailing_sl_trigger=trail_trigger,
                exchange_sl_order_id=exchange_sl_order_id or None,
            )
        logger.info("SL set for %s: ₹%.2f (trailing=%s, exchange_order=%s)",
                    security_id, sl_price, trailing, exchange_sl_order_id or "none")

    def set_take_profit(self, security_id: str, tp_price: float):
        """Set or update take profit for a position."""
        key = security_id
        if key in self._sl_tp_orders:
            self._sl_tp_orders[key].take_profit_price = tp_price
        else:
            self._sl_tp_orders[key] = StopLossTarget(
                position_security_id=security_id,
                take_profit_price=tp_price,
            )
        logger.info("TP set for %s: ₹%.2f", security_id, tp_price)

    def remove_sl_tp(self, security_id: str):
        """Remove SL/TP for a position."""
        self._sl_tp_orders.pop(security_id, None)

    def check_sl_tp_triggers(self, positions: list[dict]) -> list[dict]:
        """
        Check all positions against their SL/TP levels.
        Returns list of triggered actions to execute.
        """
        triggered = []

        for pos in positions:
            security_id = str(pos.get("securityId", ""))
            net_qty = pos.get("netQty", 0)
            ltp = pos.get("lastTradedPrice", 0)
            avg_price = pos.get("avgPrice", 0)

            if net_qty == 0 or security_id not in self._sl_tp_orders:
                continue

            config = self._sl_tp_orders[security_id]
            if not config.is_active:
                continue

            is_long = net_qty > 0

            # Update trailing SL
            if config.trailing_sl and config.trailing_sl_points > 0:
                pnl_per_unit = (ltp - avg_price) if is_long else (avg_price - ltp)
                if pnl_per_unit >= config.trailing_sl_trigger:
                    new_sl = (ltp - config.trailing_sl_points) if is_long else (ltp + config.trailing_sl_points)
                    if config.current_sl_price is None:
                        config.current_sl_price = new_sl
                    elif is_long and new_sl > config.current_sl_price:
                        config.current_sl_price = new_sl
                    elif not is_long and new_sl < config.current_sl_price:
                        config.current_sl_price = new_sl

            sl_price = config.current_sl_price or config.stop_loss_price
            tp_price = config.take_profit_price

            # Check SL hit
            if sl_price:
                if (is_long and ltp <= sl_price) or (not is_long and ltp >= sl_price):
                    triggered.append({
                        "action": "STOP_LOSS",
                        "security_id": security_id,
                        "exchange_segment": pos.get("exchangeSegment", ""),
                        "product_type": pos.get("productType", ""),
                        "quantity": abs(net_qty),
                        "transaction_type": "SELL" if is_long else "BUY",
                        "trigger_price": sl_price,
                        "ltp": ltp,
                    })
                    config.is_active = False

            # Check TP hit
            if tp_price:
                if (is_long and ltp >= tp_price) or (not is_long and ltp <= tp_price):
                    triggered.append({
                        "action": "TAKE_PROFIT",
                        "security_id": security_id,
                        "exchange_segment": pos.get("exchangeSegment", ""),
                        "product_type": pos.get("productType", ""),
                        "quantity": abs(net_qty),
                        "transaction_type": "SELL" if is_long else "BUY",
                        "trigger_price": tp_price,
                        "ltp": ltp,
                    })
                    config.is_active = False

        return triggered

    def update_position_cache(self, positions: list[dict]):
        """Called from monitor poll loop to keep position metadata fresh for WS callbacks."""
        self._position_cache = {
            str(p.get("securityId", "")): p
            for p in positions if p.get("netQty", 0) != 0
        }

    def check_sl_tp_for_security(self, security_id: str, ltp: float) -> list[dict]:
        """Called from WebSocket tick callback. Fast path — single-instrument SL/TP check."""
        if security_id not in self._sl_tp_orders:
            return []
        config = self._sl_tp_orders[security_id]
        if not config.is_active:
            return []

        pos = self._position_cache.get(security_id)
        if not pos:
            return []

        net_qty = pos.get("netQty", 0)
        if net_qty == 0:
            return []

        is_long = net_qty > 0
        avg_price = pos.get("avgPrice", 0)

        # Update trailing SL
        if config.trailing_sl and config.trailing_sl_points > 0:
            pnl_per_unit = (ltp - avg_price) if is_long else (avg_price - ltp)
            if pnl_per_unit >= config.trailing_sl_trigger:
                new_sl = (ltp - config.trailing_sl_points) if is_long else (ltp + config.trailing_sl_points)
                if config.current_sl_price is None:
                    config.current_sl_price = new_sl
                elif is_long and new_sl > config.current_sl_price:
                    config.current_sl_price = new_sl
                elif not is_long and new_sl < config.current_sl_price:
                    config.current_sl_price = new_sl

        sl_price = config.current_sl_price or config.stop_loss_price
        tp_price = config.take_profit_price
        triggered = []

        if sl_price:
            if (is_long and ltp <= sl_price) or (not is_long and ltp >= sl_price):
                triggered.append({
                    "action": "STOP_LOSS",
                    "security_id": security_id,
                    "exchange_segment": pos.get("exchangeSegment", ""),
                    "product_type": pos.get("productType", ""),
                    "quantity": abs(net_qty),
                    "transaction_type": "SELL" if is_long else "BUY",
                    "trigger_price": sl_price,
                    "ltp": ltp,
                })
                config.is_active = False

        if tp_price and not triggered:
            if (is_long and ltp >= tp_price) or (not is_long and ltp <= tp_price):
                triggered.append({
                    "action": "TAKE_PROFIT",
                    "security_id": security_id,
                    "exchange_segment": pos.get("exchangeSegment", ""),
                    "product_type": pos.get("productType", ""),
                    "quantity": abs(net_qty),
                    "transaction_type": "SELL" if is_long else "BUY",
                    "trigger_price": tp_price,
                    "ltp": ltp,
                })
                config.is_active = False

        return triggered

    def get_active_sl_tp(self) -> dict:
        """Get all active SL/TP configurations."""
        return {
            sid: {
                "stop_loss": cfg.stop_loss_price,
                "current_sl": cfg.current_sl_price,
                "take_profit": cfg.take_profit_price,
                "trailing": cfg.trailing_sl,
                "trailing_points": cfg.trailing_sl_points,
                "active": cfg.is_active,
            }
            for sid, cfg in self._sl_tp_orders.items()
            if cfg.is_active
        }

    # ── Projected P&L Calculator ───────────────────────────────────────

    def calculate_projections(self, position: dict, target_prices: list[float]) -> list[dict]:
        """
        Calculate projected P&L at various underlying prices.
        Useful for options to see payoff at different Nifty levels.
        """
        projections = []
        net_qty = position.get("netQty", 0)
        avg_price = position.get("avgPrice", 0)
        ltp = position.get("lastTradedPrice", 0)
        option_type = position.get("drvOptionType", "")
        strike = position.get("drvStrikePrice", 0)

        for target in target_prices:
            if option_type == "CALL":
                intrinsic = max(0, target - strike)
            elif option_type == "PUT":
                intrinsic = max(0, strike - target)
            else:
                intrinsic = 0

            # At expiry, option value = intrinsic
            if net_qty > 0:  # Long
                pnl = (intrinsic - avg_price) * abs(net_qty)
            else:  # Short
                pnl = (avg_price - intrinsic) * abs(net_qty)

            projections.append({
                "underlying_price": target,
                "option_intrinsic": intrinsic,
                "projected_pnl": pnl,
                "is_profitable": pnl > 0,
            })

        return projections

    def calculate_spread_projections(self, spread: SpreadPosition,
                                     target_prices: list[float]) -> list[dict]:
        """Calculate projected P&L for a spread at various underlying prices."""
        projections = []

        for target in target_prices:
            total_pnl = 0
            for leg in spread.legs:
                if leg.option_type == "CALL":
                    intrinsic = max(0, target - leg.strike_price)
                else:
                    intrinsic = max(0, leg.strike_price - target)

                if leg.quantity > 0:
                    pnl = (intrinsic - leg.entry_price) * abs(leg.quantity)
                else:
                    pnl = (leg.entry_price - intrinsic) * abs(leg.quantity)
                total_pnl += pnl

            projections.append({
                "underlying_price": target,
                "projected_pnl": total_pnl,
                "is_profitable": total_pnl > 0,
            })

        return projections

    # ── Pending Spread Orders ──────────────────────────────────────

    def add_pending_spread(self, spread_data: dict) -> str:
        """Add a new pending spread order. Returns spread_id."""
        self._spread_counter += 1
        spread_id = f"SP{int(time.time())}-{self._spread_counter}"

        spread = PendingSpreadOrder(
            spread_id=spread_id,
            sell_security_id=str(spread_data["sell_security_id"]),
            sell_exchange_segment=spread_data.get("sell_exchange_segment", "NSE_FNO"),
            sell_price=float(spread_data["sell_price"]),
            sell_trigger_price=float(spread_data["sell_trigger_price"]),
            sell_sl=float(spread_data.get("sell_sl", 0)),
            sell_order_type=spread_data.get("sell_order_type", "LIMIT"),
            buy_security_id=str(spread_data["buy_security_id"]),
            buy_exchange_segment=spread_data.get("buy_exchange_segment", "NSE_FNO"),
            quantity=int(spread_data["quantity"]),
            created_at=time.time(),
        )

        self._pending_spreads[spread_id] = spread
        logger.info(
            "Pending spread %s: SELL %s qty=%d trigger=%.2f, hedge BUY %s @ MKT",
            spread_id, spread.sell_security_id, spread.quantity,
            spread.sell_trigger_price, spread.buy_security_id,
        )
        return spread_id

    def check_pending_spreads(self) -> list[dict]:
        """Check if any pending spread triggers are hit. Returns triggered spreads."""
        triggered = []
        pending = [s for s in self._pending_spreads.values() if s.status == "PENDING"]
        if not pending:
            return triggered

        # Batch fetch LTPs for all pending sell instruments
        by_segment: dict[str, list] = {}
        for spread in pending:
            seg = spread.sell_exchange_segment
            by_segment.setdefault(seg, []).append(spread.sell_security_id)

        ltps: dict[str, float] = {}
        for seg, sec_ids in by_segment.items():
            try:
                ltp_data = self.api.get_ltp({seg: sec_ids})
                if isinstance(ltp_data, dict) and "data" in ltp_data:
                    for key, val in ltp_data["data"].items():
                        if isinstance(val, dict):
                            ltps[key] = val.get("last_price", 0)
                        elif isinstance(val, (int, float)):
                            ltps[key] = val
            except Exception as e:
                logger.error("Failed to fetch LTP for pending spreads: %s", e)

        for spread in pending:
            ltp = ltps.get(spread.sell_security_id, 0)
            if ltp <= 0:
                continue

            # For sell SL trigger: LTP <= trigger_price (standard exchange behavior)
            if ltp <= spread.sell_trigger_price:
                spread.status = "TRIGGERED"
                spread.triggered_at = time.time()
                triggered.append({
                    "spread_id": spread.spread_id,
                    "sell_security_id": spread.sell_security_id,
                    "sell_exchange_segment": spread.sell_exchange_segment,
                    "sell_price": spread.sell_price,
                    "sell_order_type": spread.sell_order_type,
                    "sell_sl": spread.sell_sl,
                    "buy_security_id": spread.buy_security_id,
                    "buy_exchange_segment": spread.buy_exchange_segment,
                    "quantity": spread.quantity,
                    "ltp": ltp,
                })
                logger.warning(
                    "Spread %s TRIGGERED: sell %s LTP=%.2f <= trigger=%.2f",
                    spread.spread_id, spread.sell_security_id,
                    ltp, spread.sell_trigger_price,
                )

        return triggered

    def check_pending_spreads_for_security(self, security_id: str, ltp: float) -> list[dict]:
        """Check pending spreads for a specific security on a WebSocket tick. No API call."""
        triggered = []
        for spread in list(self._pending_spreads.values()):
            if spread.status != "PENDING":
                continue
            if str(spread.sell_security_id) != str(security_id):
                continue
            if ltp <= spread.sell_trigger_price:
                spread.status = "TRIGGERED"
                spread.triggered_at = time.time()
                triggered.append({
                    "spread_id": spread.spread_id,
                    "sell_security_id": spread.sell_security_id,
                    "sell_exchange_segment": spread.sell_exchange_segment,
                    "sell_price": spread.sell_price,
                    "sell_order_type": spread.sell_order_type,
                    "sell_sl": spread.sell_sl,
                    "buy_security_id": spread.buy_security_id,
                    "buy_exchange_segment": spread.buy_exchange_segment,
                    "quantity": spread.quantity,
                    "ltp": ltp,
                })
                logger.warning(
                    "Spread %s TRIGGERED via tick: sell %s LTP=%.2f <= trigger=%.2f",
                    spread.spread_id, spread.sell_security_id,
                    ltp, spread.sell_trigger_price,
                )
        return triggered

    def update_spread_status(self, spread_id: str, status: str,
                             hedge_order_id: str = "", sell_order_id: str = "",
                             error_message: str = ""):
        """Update a pending spread order status after execution."""
        spread = self._pending_spreads.get(spread_id)
        if spread:
            spread.status = status
            if hedge_order_id:
                spread.hedge_order_id = hedge_order_id
            if sell_order_id:
                spread.sell_order_id = sell_order_id
            if error_message:
                spread.error_message = error_message

    def cancel_pending_spread(self, spread_id: str) -> bool:
        """Cancel a pending spread order."""
        spread = self._pending_spreads.get(spread_id)
        if spread and spread.status == "PENDING":
            spread.status = "CANCELLED"
            logger.info("Spread %s cancelled", spread_id)
            return True
        return False

    def get_pending_spreads_summary(self) -> list[dict]:
        """Get summary of all pending spread orders."""
        return [
            {
                "spread_id": s.spread_id,
                "sell_security_id": s.sell_security_id,
                "sell_price": s.sell_price,
                "sell_trigger_price": s.sell_trigger_price,
                "sell_sl": s.sell_sl,
                "buy_security_id": s.buy_security_id,
                "quantity": s.quantity,
                "status": s.status,
                "created_at": s.created_at,
                "triggered_at": s.triggered_at,
                "error_message": s.error_message,
            }
            for s in self._pending_spreads.values()
            if s.status in ("PENDING", "TRIGGERED")
        ]

    def get_spread_summary(self) -> list[dict]:
        """Get summary of all detected spreads."""
        summaries = []
        for spread in self._detected_spreads:
            summaries.append({
                "type": spread.spread_type,
                "legs": [
                    {
                        "option_type": leg.option_type,
                        "strike": leg.strike_price,
                        "qty": leg.quantity,
                        "entry": leg.entry_price,
                        "ltp": leg.current_price,
                        "pnl": leg.pnl,
                    }
                    for leg in spread.legs
                ],
                "net_premium": spread.net_premium,
                "max_profit": spread.max_profit,
                "max_loss": spread.max_loss,
                "current_pnl": spread.current_pnl,
                "breakevens": spread.breakeven_points,
            })
        return summaries
