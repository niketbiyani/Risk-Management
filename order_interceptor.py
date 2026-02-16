"""
Order Interceptor - Pre-trade Risk Gate.
This module wraps the Dhan API to intercept all order requests and enforce
risk rules BEFORE any order reaches the exchange.

Since you trade directly on charts (Dhan web/app), this interceptor works
at the API level via the monitor. For orders placed via the platform API,
it provides a hard pre-check.

For chart-based trading, the monitor detects new positions and enforces
rules post-execution (with emergency close if limits are breached).
"""

import logging
from typing import Optional

from config import Config
from dhan_api import DhanAPI
from risk_engine import RiskEngine, RiskDecision
from state_manager import StateManager

logger = logging.getLogger(__name__)


class OrderInterceptor:
    """
    Wraps DhanAPI to enforce risk checks before every order.
    Use this instead of DhanAPI directly for risk-checked order placement.
    """

    def __init__(self, api: DhanAPI, risk: RiskEngine, state: StateManager):
        self.api = api
        self.risk = risk
        self.state = state

    def place_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        product_type: str,
        price: float = 0,
        trigger_price: float = 0,
        **kwargs,
    ) -> dict:
        """
        Place an order with full risk pre-checks.
        Blocks the order if any risk rule is violated.
        """
        # Count current open positions
        positions = self.api.get_positions()
        open_count = sum(1 for p in positions if p.get("netQty", 0) != 0)

        # Estimate risk for this order
        estimated_risk = self._estimate_order_risk(
            security_id, exchange_segment, transaction_type,
            quantity, price, positions
        )

        # Run risk checks
        decision = self.risk.check_new_order(
            order_quantity=quantity,
            num_open_positions=open_count,
            estimated_risk=estimated_risk,
        )

        if not decision:
            logger.warning("ORDER BLOCKED: %s | %s %s qty=%d | Reason: %s",
                           security_id, transaction_type, order_type,
                           quantity, decision.reason)
            return {
                "status": "BLOCKED",
                "reason": decision.reason,
                "action": decision.action,
            }

        # Order passes risk check - place it
        logger.info("Order approved: %s %s %s qty=%d risk=₹%.0f",
                     transaction_type, exchange_segment, security_id,
                     quantity, estimated_risk)

        return self.api.place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
            price=price,
            trigger_price=trigger_price,
            **kwargs,
        )

    def _estimate_order_risk(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        price: float,
        current_positions: list,
    ) -> float:
        """
        Estimate the risk of a new order.
        For options, risk depends on whether it's a naked position or part of a spread.
        """
        # Check if this is closing an existing position (reduces risk)
        for pos in current_positions:
            if str(pos.get("securityId", "")) == security_id:
                net_qty = pos.get("netQty", 0)
                # Closing trade: long position + sell, or short position + buy
                if (net_qty > 0 and transaction_type == "SELL") or \
                   (net_qty < 0 and transaction_type == "BUY"):
                    return 0  # Closing trade has no additional risk

        # For options, estimate risk based on premium
        if exchange_segment in ("NSE_FNO", "BSE_FNO"):
            if price > 0:
                if transaction_type == "BUY":
                    # Buying options: max risk = premium paid
                    return price * quantity
                else:
                    # Selling options: theoretically unlimited, use margin as proxy
                    # Use a conservative estimate (2x premium or config limit)
                    return min(price * quantity * 2, Config.MAX_SINGLE_TRADE_RISK)
            else:
                # Market order: use max single trade risk as estimate
                return Config.MAX_SINGLE_TRADE_RISK * 0.5

        # For equity
        if price > 0:
            return price * quantity * 0.05  # Assume 5% adverse move
        return Config.MAX_SINGLE_TRADE_RISK * 0.5

    def check_order_feasibility(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        price: float = 0,
    ) -> RiskDecision:
        """
        Pre-check if an order would be allowed without placing it.
        Useful for UI to show whether an order will be accepted.
        """
        positions = self.api.get_positions()
        open_count = sum(1 for p in positions if p.get("netQty", 0) != 0)
        estimated_risk = self._estimate_order_risk(
            security_id, exchange_segment, transaction_type,
            quantity, price, positions
        )
        return self.risk.check_new_order(
            order_quantity=quantity,
            num_open_positions=open_count,
            estimated_risk=estimated_risk,
        )
