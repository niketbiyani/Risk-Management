"""
Dhan API integration layer.
Wraps the dhanhq SDK with error handling and provides a clean interface
for the risk management system.
"""

import logging
from datetime import datetime
from typing import Any

from dhanhq import DhanContext, dhanhq, MarketFeed, OrderUpdate

from config import Config

logger = logging.getLogger(__name__)


class DhanAPI:
    """Wrapper around Dhan's API with error handling and convenience methods."""

    # Exchange segment constants
    NSE_FNO = "NSE_FNO"
    NSE_EQ = "NSE_EQ"
    IDX_I = "IDX_I"

    # Nifty underlying security ID
    NIFTY_SECURITY_ID = 13
    BANKNIFTY_SECURITY_ID = 25

    def __init__(self):
        self.client_id = Config.DHAN_CLIENT_ID
        self.access_token = Config.DHAN_ACCESS_TOKEN
        self._context = DhanContext(self.client_id, self.access_token)
        self.dhan = dhanhq(self._context)
        self._order_update_client = None
        self._market_feed = None
        logger.info("Dhan API initialized for client: %s", self.client_id)

    def reinitialize(self, client_id: str, access_token: str):
        """Reinitialize the API client with new credentials (for token refresh)."""
        self.client_id = client_id
        self.access_token = access_token
        self._context = DhanContext(client_id, access_token)
        self.dhan = dhanhq(self._context)
        logger.info("Dhan API reinitialized for client: %s", client_id)

    # ── Order Management ───────────────────────────────────────────────

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
        disclosed_quantity: int = 0,
        validity: str = "DAY",
        correlation_id: str = "",
    ) -> dict:
        """Place an order on Dhan."""
        logger.info(
            "Placing order -> security_id=%s exchange=%s txn=%s qty=%d "
            "type=%s product=%s price=%.2f trigger=%.2f",
            security_id, exchange_segment, transaction_type, quantity,
            order_type, product_type, price, trigger_price,
        )
        try:
            response = self.dhan.place_order(
                security_id=security_id,
                exchange_segment=exchange_segment,
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=order_type,
                product_type=product_type,
                price=price,
                trigger_price=trigger_price,
                disclosed_quantity=disclosed_quantity,
                validity=validity,
            )
            logger.info("Dhan response: %s", response)
            return response
        except Exception as e:
            logger.error("Order placement exception: %s", e)
            raise

    def place_slice_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        product_type: str,
        price: float = 0,
        trigger_price: float = 0,
        validity: str = "DAY",
    ) -> dict:
        """Place a slice order for large F&O quantities exceeding freeze limits."""
        try:
            response = self.dhan.place_slice_order(
                security_id=security_id,
                exchange_segment=exchange_segment,
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=order_type,
                product_type=product_type,
                price=price,
                trigger_price=trigger_price,
                validity=validity,
            )
            logger.info("Slice order placed: %s %s qty=%d", transaction_type,
                        security_id, quantity)
            return response
        except Exception as e:
            logger.error("Slice order failed: %s", e)
            raise

    def modify_order(
        self,
        order_id: str,
        order_type: str,
        quantity: int,
        price: float,
        trigger_price: float = 0,
        disclosed_quantity: int = 0,
        validity: str = "DAY",
        leg_name: str = "",
    ) -> dict:
        """Modify a pending order."""
        try:
            response = self.dhan.modify_order(
                order_id=order_id,
                order_type=order_type,
                leg_name=leg_name,
                quantity=quantity,
                price=price,
                trigger_price=trigger_price,
                disclosed_quantity=disclosed_quantity,
                validity=validity,
            )
            logger.info("Order modified: %s", order_id)
            return response
        except Exception as e:
            logger.error("Order modify failed for %s: %s", order_id, e)
            raise

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending order."""
        try:
            response = self.dhan.cancel_order(order_id=order_id)
            logger.info("Order cancelled: %s", order_id)
            return response
        except Exception as e:
            logger.error("Order cancel failed for %s: %s", order_id, e)
            raise

    def get_order_book(self) -> list[dict]:
        """Get all orders for the day."""
        try:
            response = self.dhan.get_order_list()
            if isinstance(response, dict) and "data" in response:
                data = response["data"]
                if isinstance(data, list):
                    return [o for o in data if isinstance(o, dict)]
                return []
            return []
        except Exception as e:
            logger.error("Failed to get order book: %s", e)
            return []

    def get_order_by_id(self, order_id: str) -> dict:
        """Get specific order details."""
        try:
            return self.dhan.get_order_by_id(order_id=order_id)
        except Exception as e:
            logger.error("Failed to get order %s: %s", order_id, e)
            return {}

    def get_trade_book(self) -> list[dict]:
        """Get all executed trades for the day."""
        try:
            response = self.dhan.get_trade_book()
            if isinstance(response, dict) and "data" in response:
                data = response["data"]
                if isinstance(data, list):
                    return [t for t in data if isinstance(t, dict)]
                return []
            return []
        except Exception as e:
            logger.error("Failed to get trade book: %s", e)
            return []

    # ── Position & Portfolio ───────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """Get all open positions for the day."""
        try:
            response = self.dhan.get_positions()
            if isinstance(response, dict) and "data" in response:
                data = response["data"]
                if isinstance(data, list):
                    return [p for p in data if isinstance(p, dict)]
                return []
            return []
        except Exception as e:
            logger.error("Failed to get positions: %s", e)
            return []

    def get_holdings(self) -> list[dict]:
        """Get portfolio holdings."""
        try:
            response = self.dhan.get_holdings()
            if isinstance(response, dict) and "data" in response:
                data = response["data"]
                if isinstance(data, list):
                    return [h for h in data if isinstance(h, dict)]
                return []
            return []
        except Exception as e:
            logger.error("Failed to get holdings: %s", e)
            return []

    def get_fund_limits(self) -> dict:
        """Get account fund details (balance, margin utilised, etc.)."""
        try:
            return self.dhan.get_fund_limits()
        except Exception as e:
            logger.error("Failed to get fund limits: %s", e)
            return {}

    # ── Kill Switch ────────────────────────────────────────────────────

    def activate_kill_switch(self) -> dict:
        """
        Activate Dhan's kill switch - disables ALL trading for the day.
        NOTE: Requires all positions closed and no pending orders.
        """
        try:
            response = self.dhan.kill_switch("ACTIVATE")
            logger.warning("KILL SWITCH ACTIVATED")
            return response
        except Exception as e:
            logger.error("Kill switch activation failed: %s", e)
            raise

    def deactivate_kill_switch(self) -> dict:
        """Deactivate kill switch (only possible programmatically)."""
        try:
            response = self.dhan.kill_switch("DEACTIVATE")
            logger.info("Kill switch deactivated")
            return response
        except Exception as e:
            logger.error("Kill switch deactivation failed: %s", e)
            raise

    def get_kill_switch_status(self) -> str:
        """Check current kill switch status."""
        try:
            response = self.dhan.status_kill_switch()
            if isinstance(response, dict):
                return response.get("killSwitchStatus", "UNKNOWN")
            return "UNKNOWN"
        except Exception as e:
            logger.debug("Failed to get kill switch status: %s", e)
            return "UNKNOWN"

    # ── Options Chain & Market Data ────────────────────────────────────

    def get_option_chain(self, underlying_id: int = None, expiry: str = None) -> dict:
        """
        Get option chain for an underlying.
        underlying_id: 13 for NIFTY, 25 for BANKNIFTY
        expiry: date string like '2024-10-31'
        """
        uid = underlying_id or self.NIFTY_SECURITY_ID
        try:
            return self.dhan.option_chain(
                under_security_id=uid,
                under_exchange_segment=self.IDX_I,
                expiry=expiry or "",
            )
        except Exception as e:
            logger.error("Failed to get option chain: %s", e)
            return {}

    def get_expiry_list(self, underlying_id: int = None) -> list:
        """Get list of available expiry dates."""
        uid = underlying_id or self.NIFTY_SECURITY_ID
        try:
            response = self.dhan.expiry_list(
                under_security_id=uid,
                under_exchange_segment=self.IDX_I,
            )
            if isinstance(response, dict) and "data" in response:
                data = response["data"]
                if isinstance(data, list):
                    return data
                return []
            return []
        except Exception as e:
            logger.error("Failed to get expiry list: %s", e)
            return []

    def get_ltp(self, securities: dict) -> dict:
        """
        Get last traded price.
        securities: {"NSE_FNO": [security_id1, security_id2]}
        """
        try:
            return self.dhan.ticker_data(securities=securities)
        except Exception as e:
            logger.error("Failed to get LTP: %s", e)
            return {}

    def get_quote(self, securities: dict) -> dict:
        """
        Get full quote data (LTP + OI + volume).
        securities: {"NSE_FNO": [security_id1]}
        """
        try:
            return self.dhan.quote_data(securities=securities)
        except Exception as e:
            logger.error("Failed to get quote: %s", e)
            return {}

    def get_market_depth(self, securities: dict) -> dict:
        """Get OHLC + market depth data."""
        try:
            return self.dhan.ohlc_data(securities=securities)
        except Exception as e:
            logger.error("Failed to get market depth: %s", e)
            return {}

    # ── Fund & Margin ──────────────────────────────────────────────────

    def get_margin_calculator(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        product_type: str,
        price: float,
    ) -> dict:
        """Calculate margin required for a potential order."""
        try:
            return self.dhan.margin_calculator(
                security_id=security_id,
                exchange_segment=exchange_segment,
                transaction_type=transaction_type,
                quantity=quantity,
                product_type=product_type,
                price=price,
            )
        except Exception as e:
            logger.error("Margin calculation failed: %s", e)
            return {}

    # ── Emergency: Close All ───────────────────────────────────────────

    def cancel_all_pending_orders(self) -> list[dict]:
        """Cancel every pending order."""
        results = []
        orders = self.get_order_book()
        for order in orders:
            status = order.get("orderStatus", "")
            if status in ("PENDING", "TRANSIT", "PART_TRADED"):
                oid = order.get("orderId", "")
                if oid:
                    result = self.cancel_order(oid)
                    results.append(result)
        return results

    def close_all_positions(self) -> list[dict]:
        """Close every open position with market orders."""
        results = []
        positions = self.get_positions()
        for pos in positions:
            net_qty = pos.get("netQty", 0)
            if net_qty == 0:
                continue

            security_id = str(pos.get("securityId", ""))
            exchange_segment = pos.get("exchangeSegment", "")
            product_type = pos.get("productType", "")

            # Determine exit direction
            if net_qty > 0:
                txn_type = "SELL"
                qty = abs(net_qty)
            else:
                txn_type = "BUY"
                qty = abs(net_qty)

            try:
                result = self.place_order(
                    security_id=security_id,
                    exchange_segment=exchange_segment,
                    transaction_type=txn_type,
                    quantity=qty,
                    order_type="MARKET",
                    product_type=product_type,
                    price=0,
                )
                results.append(result)
            except Exception as e:
                logger.error("Failed to close position %s: %s", security_id, e)
                results.append({"error": str(e), "securityId": security_id})

        return results

    # ── Real-time Feeds ────────────────────────────────────────────────

    def start_order_updates(self, callback) -> OrderUpdate:
        """Start receiving real-time order updates via websocket."""
        self._order_update_client = OrderUpdate(self._context)
        self._order_update_client.on_update = callback
        self._order_update_client.connect_to_dhan_websocket_sync()
        return self._order_update_client

    def start_market_feed(self, instruments: list, callback) -> MarketFeed:
        """
        Start real-time market feed.
        instruments: [(exchange_segment_int, "security_id", feed_type)]
        """
        self._market_feed = MarketFeed(self._context, instruments, version="v2",
                                       on_message=callback)
        self._market_feed.run_forever()
        return self._market_feed

    def stop_market_feed(self):
        """Disconnect market feed."""
        if self._market_feed:
            self._market_feed.close_connection()
            self._market_feed = None
