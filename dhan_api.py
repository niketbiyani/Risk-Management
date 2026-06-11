"""
Dhan API integration layer.
Wraps the dhanhq SDK with error handling and provides a clean interface
for the risk management system.
"""

import json
import logging
import ssl
import struct
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Any

import websocket
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
    SENSEX_SECURITY_ID = 1
    BSE_IDX = "BSE_IDX"

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
        self._credentials_version = getattr(self, '_credentials_version', 0) + 1
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
            logger.info("ORDER BOOK RAW: type=%s keys=%s data_type=%s data_len=%s",
                        type(response).__name__,
                        list(response.keys()) if isinstance(response, dict) else "N/A",
                        type(response.get("data")).__name__ if isinstance(response, dict) else "N/A",
                        len(response.get("data") or []) if isinstance(response, dict) else "N/A")
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

    def get_trade_history(self, from_date: str, to_date: str) -> list[dict]:
        """Get historical trades for a date range (works after market close)."""
        try:
            response = self.dhan.get_trade_history(from_date, to_date, 0)
            logger.info("TRADE HISTORY RAW: type=%s keys=%s data_type=%s data_len=%s",
                        type(response).__name__,
                        list(response.keys()) if isinstance(response, dict) else "N/A",
                        type(response.get("data")).__name__ if isinstance(response, dict) else "N/A",
                        len(response.get("data") or []) if isinstance(response, dict) else "N/A")
            if isinstance(response, dict) and "data" in response:
                data = response["data"]
                if isinstance(data, list):
                    return [t for t in data if isinstance(t, dict)]
                return []
            return []
        except Exception as e:
            logger.error("Failed to get trade history: %s", e)
            return []

    def get_trade_book(self) -> list[dict]:
        """Get all executed trades for the day."""
        try:
            response = self.dhan.get_trade_book()
            logger.info("TRADE BOOK RAW: type=%s keys=%s data_type=%s data_len=%s",
                        type(response).__name__,
                        list(response.keys()) if isinstance(response, dict) else "N/A",
                        type(response.get("data")).__name__ if isinstance(response, dict) else "N/A",
                        len(response.get("data") or []) if isinstance(response, dict) else "N/A")
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

    def get_option_chain(self, underlying_id: int = None, expiry: str = None,
                         exchange_segment: str = None) -> dict:
        """
        Get option chain for an underlying.
        underlying_id: 13=NIFTY, 25=BANKNIFTY, 1=SENSEX
        exchange_segment: IDX_I for NSE indices, BSE_IDX for BSE indices
        expiry: date string like '2024-10-31'
        """
        uid = underlying_id or self.NIFTY_SECURITY_ID
        seg = exchange_segment or self.IDX_I
        try:
            return self.dhan.option_chain(
                under_security_id=uid,
                under_exchange_segment=seg,
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

    def get_chart_data(self, security_id: str, exchange_segment: str, instrument_type: str = "OPTIDX", from_date: str = None, to_date: str = None) -> dict:
        """Fetch intraday 1-minute OHLCV candles for the given date range (defaults to today)."""
        try:
            from datetime import date
            today = date.today().strftime("%Y-%m-%d")
            return self.dhan.intraday_minute_data(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=from_date or today,
                to_date=to_date or today,
            )
        except Exception as e:
            logger.error("Failed to get chart data for %s: %s", security_id, e)
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

    def start_ltp_feed_async(self, instruments: list, callback) -> threading.Thread:
        """Start LtpFeedWebSocket (depth endpoint, multi-instrument). Non-blocking."""
        if not hasattr(self, "_ltp_feed"):
            self._ltp_feed = None
        if self._ltp_feed:
            self._ltp_feed.stop()
        token = self._context.get_access_token()
        client_id = self._context.get_client_id()
        self._ltp_feed = LtpFeedWebSocket(token, client_id)
        return self._ltp_feed.start(instruments, callback)

    def start_order_updates_async(self, callback) -> threading.Thread:
        """Start OrderUpdate in a daemon thread with auto-reconnect. Non-blocking."""
        def _run():
            while True:
                try:
                    logger.info("OrderUpdate: connecting")
                    self._order_update_client = OrderUpdate(self._context)
                    self._order_update_client.on_update = callback
                    self._order_update_client.connect_to_dhan_websocket_sync()
                except Exception as e:
                    logger.error("OrderUpdate disconnected: %s — reconnecting in 5s", e)
                time.sleep(5)

        t = threading.Thread(target=_run, daemon=True, name="OrderUpdate")
        t.start()
        return t


# ── Multi-instrument LTP feed via depth WebSocket ─────────────────
# Uses the same proven endpoint as the DOM depth panel, bypassing the
# unreliable MarketFeed SDK. Best-bid price used as LTP (options have
# thin spreads so bid ≈ last traded price).

class LtpFeedWebSocket:
    WS_URL = "wss://depth-api-feed.dhan.co/twentydepth"
    FEED_BID = 41
    FEED_DISCONNECT = 50
    HEADER_SIZE = 12   # msg_len(2) + feed_code(1) + exchange_seg(1) + security_id(4) + sequence(4)
    LEVEL_SIZE = 16    # price(f64=8) + quantity(u32=4) + orders(u32=4)

    def __init__(self, access_token: str, client_id: str):
        self._token = access_token
        self._client_id = client_id
        self._ws = None
        self._stop = threading.Event()
        self._instruments = []
        self._callback = None

    def start(self, instruments: list, callback) -> threading.Thread:
        """Start feed. instruments: [(seg_int, security_id_str, feed_type), ...]"""
        self.stop()
        self._stop.clear()
        self._callback = callback
        seg_map = {0:"IDX_I", 1:"NSE_EQ", 2:"NSE_FNO", 3:"NSE_CURRENCY",
                   4:"BSE_EQ", 5:"MCX_COMM", 7:"BSE_CURRENCY", 8:"BSE_FNO"}
        self._instruments = [(str(i[1]), seg_map.get(i[0], "NSE_FNO")) for i in instruments]
        t = threading.Thread(target=self._run, daemon=True, name="LtpFeed")
        t.start()
        return t

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self):
        token_encoded = urllib.parse.quote(self._token, safe="")
        client_encoded = urllib.parse.quote(self._client_id, safe="")
        url = f"{self.WS_URL}?token={token_encoded}&clientId={client_encoded}&authType=2"
        ssl_ctx = ssl.create_default_context()
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, e: logger.error("LtpFeed error: %s", e),
                    on_close=lambda ws, c, m: None,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10,
                                     sslopt={"context": ssl_ctx})
            except Exception as e:
                logger.error("LtpFeed run_forever: %s", e)
            if not self._stop.is_set():
                logger.info("LtpFeed: reconnecting in 2s")
                self._stop.wait(2)

    def _on_open(self, ws):
        payload = json.dumps({
            "RequestCode": 23,
            "InstrumentCount": len(self._instruments),
            "InstrumentList": [
                {"ExchangeSegment": seg, "SecurityId": sid}
                for sid, seg in self._instruments
            ],
        })
        ws.send(payload)
        logger.info("LtpFeed: connected, subscribed %d instruments", len(self._instruments))

    def _on_message(self, ws, message):
        if not isinstance(message, (bytes, bytearray)):
            return
        data = message
        offset = 0
        total = len(data)
        while offset < total:
            if total - offset < self.HEADER_SIZE:
                break
            msg_len    = struct.unpack_from("<h", data, offset)[0]
            feed_code  = struct.unpack_from("<B", data, offset + 2)[0]
            security_id = struct.unpack_from("<i", data, offset + 4)[0]
            pkt_size = msg_len if msg_len > 0 else (self.HEADER_SIZE + self.LEVEL_SIZE)
            if offset + pkt_size > total:
                break
            if feed_code == self.FEED_BID:
                # Best bid price is first level, float64 right after header
                if total - offset >= self.HEADER_SIZE + 8:
                    price = struct.unpack_from("<d", data, offset + self.HEADER_SIZE)[0]
                    if price > 0 and self._callback:
                        self._callback({"security_id": str(security_id), "LTP": f"{price:.2f}"})
            elif feed_code == self.FEED_DISCONNECT:
                err_code = struct.unpack_from("<I", data, offset + 8)[0] if total - offset >= 12 else 0
                logger.warning("LtpFeed: server disconnect code=%d", err_code)
            offset += max(pkt_size, self.HEADER_SIZE)


# ── 20-Level Depth of Market WebSocket ────────────────────────────

class DepthWebSocket:
    """Custom 20-level depth WebSocket client.

    The dhanhq FullDepth SDK class is unusable programmatically (get_data()
    only prints to console and returns None), so we connect directly to the
    Dhan 20-depth WebSocket and parse the binary packets ourselves.

    Binary protocol (Little Endian):
        Header  (12 bytes): msg_len(int16) + feed_code(uint8) + exchange_seg(uint8)
                             + security_id(int32) + sequence(uint32)
        Per-level (16 bytes × 20): price(float64) + quantity(uint32) + orders(uint32)

    Feed codes: 41 = Bid (buy side), 51 = Ask (sell side), 50 = Disconnect
    """

    WS_URL = "wss://depth-api-feed.dhan.co/twentydepth"
    FEED_BID = 41
    FEED_ASK = 51
    FEED_DISCONNECT = 50
    HEADER_SIZE = 12
    LEVEL_SIZE = 16
    NUM_LEVELS = 20

    def __init__(self, access_token: str, client_id: str):
        self._token = access_token
        self._client_id = client_id
        self._ws = None
        self._thread = None
        self._running = False
        self._connected = False
        self._disconnect_reason = ""
        self._security_id = None
        self._depth_exchange_segment = "NSE_FNO"
        self._lock = threading.Lock()
        self._bids = []  # [{price, quantity, orders}, ...] up to 20
        self._asks = []
        # Connection diagnostics
        self._connect_attempts = 0
        self._last_error = ""
        # Multi-instrument LTP tracking (OC strikes share this connection)
        self._ltp_instruments: list = []  # [(exchange_seg_str, sid_str), ...]
        self._ltp_sids: set = set()
        self._ltp_callback = None
        self._ever_connected = False

    @property
    def security_id(self):
        return self._security_id

    def set_ltp_instruments(self, instruments: list, callback):
        """Set OC instruments for LTP-only ticks. Shares this WS connection.

        instruments: list of (exchange_segment_str, security_id_str)
        callback: called with {"security_id": sid, "LTP": "price"} on each bid tick
        """
        self._ltp_instruments = [(str(seg), str(sid)) for seg, sid in instruments]
        self._ltp_sids = {str(sid) for _, sid in instruments}
        self._ltp_callback = callback
        if self._connected and self._ws:
            self._send_subscribe(self._ws)
            logger.info("Depth WS: LTP instruments updated (%d strikes)", len(instruments))
        elif not self._running:
            # No depth instrument yet — start a headless LTP-only connection
            self._running = True
            self._connected = False
            self._thread = threading.Thread(
                target=self._run, args=(None, None), daemon=True, name="DepthWS-LTP"
            )
            self._thread.start()

    def _send_subscribe(self, ws):
        """Send (re)subscribe payload with all instruments — depth + LTP."""
        instrs = []
        if self._security_id:
            instrs.append({"ExchangeSegment": self._depth_exchange_segment,
                           "SecurityId": self._security_id})
        for seg, sid in self._ltp_instruments:
            if sid != self._security_id:
                instrs.append({"ExchangeSegment": seg, "SecurityId": sid})
        if not instrs:
            return
        payload = json.dumps({
            "RequestCode": 23,
            "InstrumentCount": len(instrs),
            "InstrumentList": instrs,
        })
        try:
            ws.send(payload)
        except Exception as e:
            logger.error("Depth WS: _send_subscribe failed: %s", e)

    def subscribe(self, security_id: str, exchange_segment: str = "NSE_FNO"):
        """Connect and subscribe to 20-level depth for one instrument."""
        self._security_id = str(security_id)
        self._depth_exchange_segment = exchange_segment
        if self._connected and self._ws:
            # Already connected — just add the new depth instrument
            self._send_subscribe(self._ws)
            with self._lock:
                self._bids = []
                self._asks = []
            logger.info("Depth WS: switched depth instrument to %s (%s)", security_id, exchange_segment)
            return
        self.stop()
        self._running = True
        self._connected = False
        self._disconnect_reason = ""
        self._connect_attempts = 0
        self._last_error = ""
        self._ever_connected = False
        with self._lock:
            self._bids = []
            self._asks = []
        self._thread = threading.Thread(
            target=self._run,
            args=(str(security_id), exchange_segment),
            daemon=True,
        )
        self._thread.start()
        logger.info("Depth WS: subscribing to %s (%s)", security_id, exchange_segment)

    def _run(self, security_id, exchange_segment):
        """WebSocket connection loop with auto-reconnect."""
        token_encoded = urllib.parse.quote(self._token, safe="")
        client_encoded = urllib.parse.quote(self._client_id, safe="")
        url = (
            f"{self.WS_URL}"
            f"?token={token_encoded}"
            f"&clientId={client_encoded}"
            f"&authType=2"
        )
        ssl_context = ssl.create_default_context()
        logger.info("Depth WS: connecting (depth=%s, ltp_count=%d)",
                    security_id, len(self._ltp_instruments))
        while self._running:
            self._connect_attempts += 1
            logger.info("Depth WS: attempt #%d for %s (%s)",
                        self._connect_attempts, security_id, exchange_segment)
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=lambda ws: self._on_open(ws, security_id, exchange_segment),
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(
                    ping_interval=30,
                    ping_timeout=10,
                    sslopt={"context": ssl_context},
                )
            except Exception as e:
                self._last_error = f"run_forever: {e}"
                logger.error("Depth WS run_forever error: %s", e, exc_info=True)
            if self._running:
                import time
                logger.info("Depth WS: reconnecting in 2s (attempt #%d)...",
                            self._connect_attempts)
                time.sleep(2)

    def _on_open(self, ws, security_id, exchange_segment):
        """Send subscription JSON on connect."""
        self._connected = True
        self._ever_connected = True
        self._send_subscribe(ws)
        logger.info("Depth WS: connected, subscribed depth=%s ltp_instruments=%d",
                    security_id, len(self._ltp_instruments))

    def _on_message(self, ws, message):
        """Parse binary depth packets."""
        if not isinstance(message, (bytes, bytearray)):
            logger.info("Depth WS: received text message: %s", str(message)[:500])
            return

        offset = 0
        data = message
        total_len = len(data)

        while offset < total_len:
            remaining = total_len - offset
            if remaining < self.HEADER_SIZE:
                break

            # Parse header (12 bytes, little-endian): <hBBiI
            #   msg_len(int16) + feed_code(uint8) + exchange_seg(uint8)
            #   + security_id(int32) + sequence(uint32)
            header = data[offset:offset + self.HEADER_SIZE]
            msg_len = struct.unpack_from("<h", header, 0)[0]
            feed_code = struct.unpack_from("<B", header, 2)[0]
            pkt_security_id = struct.unpack_from("<i", header, 4)[0]

            if feed_code == self.FEED_DISCONNECT:
                # Parse disconnect error code from header
                err_code = struct.unpack_from("<I", header, 8)[0] if remaining >= 12 else 0
                err_msgs = {
                    805: "max active WS connections exceeded",
                    806: "subscribe to Data APIs required",
                    807: "access token expired",
                    808: "invalid client ID",
                    809: "authentication failed",
                }
                err_msg = err_msgs.get(err_code, f"code={err_code}")
                self._disconnect_reason = err_msg
                self._connected = False
                logger.warning("Depth WS: server disconnect — %s", err_msg)
                # Use msg_len if valid, otherwise skip header only
                offset += max(msg_len, self.HEADER_SIZE) if msg_len > 0 else self.HEADER_SIZE
                continue

            # Use msg_len from header (like Dhan's SDK) to determine packet size
            if msg_len > 0 and offset + msg_len <= total_len:
                packet_size = msg_len
            else:
                # Fallback to fixed size
                packet_size = self.HEADER_SIZE + (self.NUM_LEVELS * self.LEVEL_SIZE)
                if remaining < packet_size:
                    break

            if feed_code not in (self.FEED_BID, self.FEED_ASK):
                offset += packet_size
                continue

            sid_str = str(pkt_security_id)

            # LTP-only instrument (OC strike) — extract best bid and call back
            if sid_str in self._ltp_sids:
                if feed_code == self.FEED_BID:
                    body = data[offset + self.HEADER_SIZE:offset + packet_size]
                    if len(body) >= 8:
                        price = struct.unpack_from("<d", body, 0)[0]
                        if price > 0 and self._ltp_callback:
                            self._ltp_callback({"security_id": sid_str, "LTP": f"{price:.2f}"})
                offset += packet_size
                continue

            # Skip packets for other instruments not in depth or ltp sets
            if self._security_id and sid_str != self._security_id:
                offset += packet_size
                continue

            # Parse depth levels from body (after 12-byte header)
            body = data[offset + self.HEADER_SIZE:offset + packet_size]
            levels = []
            for i in range(self.NUM_LEVELS):
                base = i * self.LEVEL_SIZE
                if base + self.LEVEL_SIZE > len(body):
                    break
                price = struct.unpack_from("<d", body, base)[0]
                quantity = struct.unpack_from("<I", body, base + 8)[0]
                orders = struct.unpack_from("<I", body, base + 12)[0]
                if price > 0 or quantity > 0:
                    levels.append({
                        "price": round(price, 2),
                        "quantity": quantity,
                        "orders": orders,
                    })

            with self._lock:
                if feed_code == self.FEED_BID:
                    self._bids = levels
                elif feed_code == self.FEED_ASK:
                    self._asks = levels

            offset += packet_size

    def _on_error(self, ws, error):
        self._last_error = f"{type(error).__name__}: {error}"
        logger.error("Depth WS error: %s (%s)", error, type(error).__name__)

    def _on_close(self, ws, close_status_code, close_msg):
        was_connected = self._connected
        self._connected = False
        logger.warning("Depth WS closed: code=%s msg=%s was_connected=%s",
                       close_status_code, close_msg, was_connected)

    def get_depth(self) -> dict:
        """Thread-safe read of current depth data."""
        with self._lock:
            return {
                "bids": list(self._bids),
                "asks": list(self._asks),
                "security_id": self._security_id,
                "connected": self._connected,
                "disconnect_reason": self._disconnect_reason,
                "connect_attempts": self._connect_attempts,
                "last_error": self._last_error,
                "ever_connected": self._ever_connected,
            }

    def stop(self):
        """Disconnect and clean up."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._security_id = None
        logger.info("Depth WS: stopped")
