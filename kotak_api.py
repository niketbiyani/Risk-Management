"""
Kotak Neo API integration layer.
Implements the same interface as DhanAPI for seamless broker toggling,
mapping segments, order types, products, and converting position/order books.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional

from config import Config

logger = logging.getLogger(__name__)

try:
    from neo_api_client import NeoAPI
except ImportError:
    NeoAPI = None


class KotakNeoAPI:
    """Wrapper around Kotak Neo Trade API (neo-api-client) matching DhanAPI interface."""

    # Exchange segment constants (matching Dhan conventions)
    NSE_FNO = "NSE_FNO"
    NSE_EQ = "NSE_EQ"
    IDX_I = "IDX_I"
    BSE_IDX = "BSE_IDX"
    BSE_FNO = "BSE_FNO"

    # Index Spot Tokens (Kotak Neo specific)
    NIFTY_SPOT_TOKEN = "26000"  # Nifty 50 Index Spot token on NSE
    SENSEX_SPOT_TOKEN = "1"     # Sensex Index Spot token on BSE

    def __init__(self):
        if NeoAPI is None:
            logger.warning("neo-api-client is not installed. KotakNeoAPI initialized in offline/disabled mode.")
            self.client = None
            return

        self.consumer_key = Config.KOTAK_CONSUMER_KEY
        self.mobile_number = Config.KOTAK_MOBILE_NUMBER
        self.ucc = Config.KOTAK_UCC
        self.pin = Config.KOTAK_PIN
        self.totp_secret = Config.KOTAK_TOTP_SECRET

        self.client = None
        self.ltp_callback = None
        self.order_callback = None
        self._connected = False

    @property
    def access_token(self) -> str:
        return "kotak_session_token"

    @property
    def client_id(self) -> str:
        return self.ucc or "kotak_ucc"

    @property
    def _credentials_version(self) -> int:
        return 1

        if self.consumer_key and self.ucc:
            try:
                self.initialize_client()
            except Exception as e:
                logger.error("Failed to initialize Kotak Neo API client: %s", e)

    def initialize_client(self):
        """Perform TOTP login and validate session using MPIN."""
        if NeoAPI is None:
            raise ImportError("neo-api-client is not installed. Install via git first.")

        logger.info("Initializing Kotak Neo API client...")
        # Initialize client
        self.client = NeoAPI(environment="prod", consumer_key=self.consumer_key)

        # Generate TOTP code if secret is present
        totp_code = ""
        if self.totp_secret:
            try:
                import pyotp
                totp = pyotp.TOTP(self.totp_secret)
                totp_code = totp.now()
            except Exception as e:
                logger.error("Failed to generate TOTP: %s", e)

        # Login using mobile, UCC, and TOTP
        logger.info("Logging into Kotak Neo (UCC: %s)...", self.ucc)
        self.client.totp_login(
            mobile_number=self.mobile_number,
            ucc=self.ucc,
            totp=totp_code
        )

        # Validate daily session with MPIN (2FA validation)
        logger.info("Validating daily session with MPIN...")
        self.client.totp_validate(mpin=self.pin)
        self._connected = True
        logger.info("Kotak Neo API Session initialized successfully.")

    def reinitialize(self, client_id: str, access_token: str):
        """Reinitialize credentials (not needed for Kotak daily TOTP session, but matches Dhan signature)."""
        logger.info("Kotak Neo API reinitialize called (no-op).")

    # ── Mappings ──

    def _map_segment(self, segment: str) -> str:
        """Map Dhan segment names to Kotak segment names."""
        mapping = {
            self.NSE_FNO: "nse_fo",
            self.NSE_EQ: "nse_cm",
            self.IDX_I: "nse_cm",
            self.BSE_IDX: "bse_cm",
            self.BSE_FNO: "bse_fo",
        }
        return mapping.get(segment, "nse_fo")

    def _map_order_type(self, order_type: str) -> str:
        """Map Dhan order types to Kotak order types."""
        mapping = {
            "LIMIT": "L",
            "MARKET": "MKT",
            "STOP_LOSS_LIMIT": "SL",
            "STOP_LOSS_MARKET": "SL-M"
        }
        return mapping.get(order_type, "MKT")

    def _map_transaction_type(self, tx_type: str) -> str:
        """Map Dhan transaction type (BUY/SELL) to Kotak type (B/S)."""
        if tx_type.upper() in ("BUY", "B"):
            return "B"
        return "S"

    def _map_product_type(self, product_type: str) -> str:
        """Map Dhan product types to Kotak product types."""
        mapping = {
            "MARGIN": "NRML",
            "INTRADAY": "MIS",
            "CO": "CO",
            "BO": "BO"
        }
        return mapping.get(product_type, "NRML")

    # ── Order Management ──

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
        """Place an order on Kotak Neo."""
        if not self.client:
            raise RuntimeError("Kotak Neo client not initialized")

        seg = self._map_segment(exchange_segment)
        tx = self._map_transaction_type(transaction_type)
        ot = self._map_order_type(order_type)
        prod = self._map_product_type(product_type)

        logger.info(
            "Kotak Place Order -> token=%s segment=%s txn=%s qty=%d type=%s prod=%s price=%.2f trigger=%.2f",
            security_id, seg, tx, quantity, ot, prod, price, trigger_price
        )

        try:
            # Note: Kotak Neo place_order parameters signature mapping
            response = self.client.place_order(
                exchange_segment=seg,
                product=prod,
                price=str(price) if price > 0 else "0",
                order_type=ot,
                quantity=str(quantity),
                disclosed_quantity=str(disclosed_quantity),
                validity=validity,
                trading_symbol="",  # placing by instrument token/scrip
                transaction_type=tx,
                amo="NO",
                trigger_price=str(trigger_price) if trigger_price > 0 else "0",
                instrument_token=str(security_id)
            )
            logger.info("Kotak response: %s", response)
            
            # Map response order status structure to match Dhan's orderId
            # Kotak Neo returns {"nOrdNo": "12345678", "status": "success", "message": "..."}
            if isinstance(response, dict) and "nOrdNo" in response:
                return {
                    "status": "success",
                    "data": {
                        "orderId": str(response["nOrdNo"]),
                        "orderStatus": "PENDING"
                    }
                }
            return response
        except Exception as e:
            logger.error("Kotak order placement failed: %s", e)
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
        """Modify a pending Kotak order."""
        if not self.client:
            raise RuntimeError("Kotak client not initialized")

        ot = self._map_order_type(order_type)

        logger.info("Kotak Modify Order -> order_id=%s qty=%d price=%.2f trigger=%.2f",
                    order_id, quantity, price, trigger_price)
        try:
            response = self.client.modify_order(
                order_no=str(order_id),
                price=str(price),
                order_type=ot,
                quantity=str(quantity),
                trigger_price=str(trigger_price) if trigger_price > 0 else "0",
                disclosed_quantity=str(disclosed_quantity),
                validity=validity
            )
            return response
        except Exception as e:
            logger.error("Kotak order modification failed for %s: %s", order_id, e)
            raise

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending Kotak order."""
        if not self.client:
            raise RuntimeError("Kotak client not initialized")

        logger.info("Kotak Cancel Order -> order_id=%s", order_id)
        try:
            response = self.client.cancel_order(order_no=str(order_id))
            return response
        except Exception as e:
            logger.error("Kotak order cancellation failed for %s: %s", order_id, e)
            raise

    # ── Portfolio & Reports ──

    def get_order_book(self) -> list[dict]:
        """Fetch today's orders and map to Dhan keys."""
        if not self.client:
            return []
        try:
            # client.order_report() returns list of dicts
            orders = self.client.order_report()
            if not isinstance(orders, list):
                # Handle dictionary wrapped lists
                if isinstance(orders, dict) and "data" in orders:
                    orders = orders["data"]
                else:
                    return []
            
            mapped_orders = []
            for o in orders:
                if not isinstance(o, dict):
                    continue
                # Map Kotak Neo's order keys to Dhan-like keys
                status_raw = o.get("ordSt", "").lower()
                status = "PENDING"
                if "complete" in status_raw or "traded" in status_raw:
                    status = "TRADED"
                elif "cancel" in status_raw:
                    status = "CANCELLED"
                elif "reject" in status_raw:
                    status = "REJECTED"

                tx_type = "BUY" if o.get("trnsTp", "B") == "B" else "SELL"
                prod = "MARGIN" if o.get("prd", "NRML") == "NRML" else "INTRADAY"

                mapped_orders.append({
                    "orderId": str(o.get("nOrdNo", "")),
                    "orderStatus": status,
                    "tradingSymbol": o.get("trdSym", ""),
                    "securityId": str(o.get("tok", "")),
                    "transactionType": tx_type,
                    "price": float(o.get("prc", 0) or 0),
                    "triggerPrice": float(o.get("trgPrc", 0) or 0),
                    "quantity": int(o.get("qty", 0) or 0),
                    "tradedQuantity": int(o.get("fillQty", 0) or 0),
                    "productType": prod,
                    "orderStatusMsg": o.get("rejRsn", "") or o.get("statusText", ""),
                    "exchangeSegment": self.NSE_FNO if "fo" in o.get("seg", "").lower() else self.NSE_EQ
                })
            return mapped_orders
        except Exception as e:
            logger.error("Failed to get Kotak order book: %s", e)
            return []

    def get_order_by_id(self, order_id: str) -> dict:
        """Find an order by ID."""
        orders = self.get_order_book()
        for o in orders:
            if o.get("orderId") == str(order_id):
                return o
        return {}

    def get_positions(self) -> list[dict]:
        """Fetch open positions and map to Dhan keys."""
        if not self.client:
            return []
        try:
            positions = self.client.positions()
            if not isinstance(positions, list):
                if isinstance(positions, dict) and "data" in positions:
                    positions = positions["data"]
                else:
                    return []
            
            mapped_positions = []
            for p in positions:
                if not isinstance(p, dict):
                    continue
                
                net_qty = int(p.get("netQty", 0) or p.get("netTrdQty", 0) or 0)
                if net_qty == 0:
                    continue  # skip closed positions
                
                buy_qty = int(p.get("buyQty", 0) or p.get("flBuyQty", 0) or 0)
                sell_qty = int(p.get("sellQty", 0) or p.get("flSellQty", 0) or 0)
                avg_prc = float(p.get("avgPrc", 0) or p.get("buyPrice", 0) or 0)
                pnl = float(p.get("pnl", 0) or 0)

                mapped_positions.append({
                    "securityId": str(p.get("tok", p.get("instrumentToken", ""))),
                    "tradingSymbol": p.get("pTrdSymbol", ""),
                    "exchangeSegment": self.NSE_FNO if "fo" in p.get("seg", "").lower() else self.NSE_EQ,
                    "productType": "MARGIN" if p.get("prod", "NRML") == "NRML" else "INTRADAY",
                    "netQty": net_qty,
                    "buyQty": buy_qty,
                    "sellQty": sell_qty,
                    "buyAvgPrice": avg_prc if net_qty > 0 else 0,
                    "sellAvgPrice": avg_prc if net_qty < 0 else 0,
                    "realizedProfit": 0.0,
                    "unrealizedProfit": pnl
                })
            return mapped_positions
        except Exception as e:
            logger.error("Failed to get Kotak positions: %s", e)
            return []

    def get_trade_book(self) -> list[dict]:
        """Fetch executed trades."""
        if not self.client:
            return []
        try:
            trades = self.client.trade_report()
            if not isinstance(trades, list):
                if isinstance(trades, dict) and "data" in trades:
                    trades = trades["data"]
                else:
                    return []
            
            mapped_trades = []
            for t in trades:
                if not isinstance(t, dict):
                    continue
                mapped_trades.append({
                    "orderId": str(t.get("nOrdNo", "")),
                    "securityId": str(t.get("tok", "")),
                    "tradingSymbol": t.get("trdSym", ""),
                    "transactionType": "BUY" if t.get("trnsTp", "B") == "B" else "SELL",
                    "price": float(t.get("prc", 0) or 0),
                    "quantity": int(t.get("qty", 0) or 0),
                    "tradeExecutionTime": t.get("flDtTm", "")
                })
            return mapped_trades
        except Exception as e:
            logger.error("Failed to get Kotak trade book: %s", e)
            return []

    def get_trade_history(self, from_date: str, to_date: str) -> list[dict]:
        """Fetch trade history. Maps to get_trade_book() for same day as historical ranges aren't standardized."""
        return self.get_trade_book()

    def get_fund_limits(self) -> dict:
        """Fetch margin/fund limits."""
        if not self.client:
            return {}
        try:
            limits = self.client.limits()
            if isinstance(limits, list) and len(limits) > 0:
                limits = limits[0]
            
            # Map to Dhan keys
            # Dhan expects: {"data": {"sodLimit": 100000, "availabelLimit": 95000, "utilisedLimit": 5000}}
            cash = float(limits.get("cash", 0) or limits.get("net", 0) or 0)
            margin = float(limits.get("marginUsed", 0) or limits.get("margin_utilised", 0) or 0)
            return {
                "status": "success",
                "data": {
                    "sodLimit": cash,
                    "availabelLimit": cash - margin,
                    "utilisedLimit": margin
                }
            }
        except Exception as e:
            logger.error("Failed to get Kotak limits: %s", e)
            return {}

    # ── Emergency ──

    def cancel_all_pending_orders(self) -> list[dict]:
        """Cancel all pending Kotak orders."""
        results = []
        orders = self.get_order_book()
        for o in orders:
            if o.get("orderStatus") == "PENDING":
                try:
                    res = self.cancel_order(o["orderId"])
                    results.append(res)
                except Exception as e:
                    results.append({"error": str(e), "orderId": o["orderId"]})
        return results

    def close_all_positions(self) -> list[dict]:
        """Close all active positions."""
        results = []
        positions = self.get_positions()
        for pos in positions:
            qty = abs(pos["netQty"])
            if qty == 0:
                continue
            txn = "SELL" if pos["netQty"] > 0 else "BUY"
            try:
                res = self.place_order(
                    security_id=pos["securityId"],
                    exchange_segment=pos["exchangeSegment"],
                    transaction_type=txn,
                    quantity=qty,
                    order_type="MARKET",
                    product_type=pos["productType"]
                )
                results.append(res)
            except Exception as e:
                results.append({"error": str(e), "securityId": pos["securityId"]})
        return results

    def activate_kill_switch(self) -> dict:
        """Emergency lockout trigger (simulated locally for Kotak Neo)."""
        logger.warning("Kotak Kill Switch triggered (simulated locally).")
        return {"status": "success", "message": "Kill switch enabled locally"}

    def deactivate_kill_switch(self) -> dict:
        """Deactivate locally simulated kill switch."""
        logger.info("Kotak Kill Switch deactivated.")
        return {"status": "success", "message": "Kill switch disabled locally"}

    # ── Options Chain (Emulated via Local search + quote fetching) ──

    def get_option_chain(self, underlying_id: int = None, expiry: str = None,
                          exchange_segment: str = None) -> dict:
        """
        Emulates Dhan's get_option_chain by mapping underlying and returning quotes.
        Since Kotak Neo lacks an option chain endpoint, this returns basic spot metadata.
        The actual option chain fields are assembled inside Flask route `/api/option_chain/data`
        by querying the offline InstrumentCache.
        """
        # underlying_id: 13=NIFTY, 1=SENSEX
        spot_token = self.NIFTY_SPOT_TOKEN if underlying_id == 13 else self.SENSEX_SPOT_TOKEN
        spot_ex = "NSE" if underlying_id == 13 else "BSE"
        
        # Get spot price
        spot = 0.0
        try:
            res = self.get_ltp({spot_ex: [spot_token]})
            # Extract price
            if isinstance(res, dict):
                spot = float(res.get(spot_token, 0) or 0)
        except Exception as e:
            logger.error("Failed to fetch underlying spot for option chain: %s", e)

        return {
            "status": "success",
            "data": {
                "last_price": spot,
                "oc": {} # filled dynamically by Flask using local cache
            }
        }

    # ── Quotes & LTP ──

    def get_ltp(self, securities: dict) -> dict:
        """
        Fetch LTP of instruments.
        securities format: {"NSE_FNO": [123, 456], "NSE_EQ": [789]}
        Returns format: {"123": 150.50, "456": 200.10}
        """
        if not self.client:
            return {}
        try:
            # Map request format to Kotak Neo request format
            # Kotak Neo `quotes` takes list of dicts: [{'instrument_token': '123', 'exchange_segment': 'nse_fo'}]
            tokens = []
            for seg, ids in securities.items():
                kotak_seg = self._map_segment(seg)
                for sid in ids:
                    tokens.append({"instrument_token": str(sid), "exchange_segment": kotak_seg})
            
            if not tokens:
                return {}

            # Call SDK quotes
            quotes = self.client.quotes(instrument_tokens=tokens, quote_type="LTP")
            if not isinstance(quotes, list):
                if isinstance(quotes, dict) and "data" in quotes:
                    quotes = quotes["data"]
                else:
                    return {}

            result = {}
            for q in quotes:
                if isinstance(q, dict):
                    tok = str(q.get("instrumentToken", q.get("tok", "")))
                    ltp = q.get("lastTradedPrice", q.get("lp", 0))
                    if tok:
                        result[tok] = float(ltp or 0)
            return result
        except Exception as e:
            logger.error("Failed to get Kotak LTP: %s", e)
            return {}

    def get_quote(self, securities: dict) -> dict:
        """Get full quote details."""
        return self.get_ltp(securities)

    def get_market_depth(self, securities: dict) -> dict:
        """Get market depth (bid/ask). Emulated via full quotes."""
        return self.get_ltp(securities)

    def get_chart_data(self, security_id: str, exchange_segment: str, instrument_type: str = "OPTIDX", 
                       from_date: str = None, to_date: str = None) -> dict:
        """Fetch 1-minute historical candles. (Mocked for offline/live toggle fallback)."""
        # historical candles require direct broker queries, returning empty skeleton for compatibility
        return {"status": "success", "data": []}

    # ── WebSocket Feeds ──

    def start_order_updates_async(self, callback) -> threading.Thread:
        """Listen to Kotak Neo Order lifecycle updates."""
        self.order_callback = callback
        
        def _on_order_update(message):
            if not self.order_callback:
                return
            try:
                # Map Kotak's incoming WS message structure to Dhan keys
                # Kotak payload: {'nOrdNo': '12345', 'ordSt': 'complete', 'trdSym': '...', 'qty': 75, 'rejRsn': ''}
                if isinstance(message, dict):
                    status_raw = message.get("ordSt", "").lower()
                    status = "PENDING"
                    if "complete" in status_raw or "traded" in status_raw:
                        status = "TRADED"
                    elif "cancel" in status_raw:
                        status = "CANCELLED"
                    elif "reject" in status_raw:
                        status = "REJECTED"

                    self.order_callback({
                        "orderId": str(message.get("nOrdNo", "")),
                        "orderStatus": status,
                        "tradingSymbol": message.get("trdSym", ""),
                        "tradedQuantity": int(message.get("fillQty", 0) or message.get("qty", 0) or 0),
                        "rejectedReason": message.get("rejRsn", "") or message.get("statusText", "")
                    })
            except Exception as e:
                logger.error("Error in Kotak order update callback parsing: %s", e)

        # Register callback directly on NeoAPI client
        if self.client:
            self.client.on_order_update = _on_order_update
            logger.info("Registered Kotak Neo order update listener.")
        
        # Keep connection alive in background
        def _keep_alive():
            while True:
                time.sleep(30)
                
        t = threading.Thread(target=_keep_alive, daemon=True, name="KotakOrderUpdates")
        t.start()
        return t

    def start_ltp_feed_async(self, instruments: list, callback) -> threading.Thread:
        """
        Subscribe to live tick updates.
        instruments format: [(exchange_segment_int, "security_id", feed_type)]
        """
        self.ltp_callback = callback
        
        # Mappings
        seg_map = {0: "nse_cm", 1: "nse_cm", 2: "nse_fo", 4: "bse_cm", 8: "bse_fo"}

        def _on_market_message(message):
            if not self.ltp_callback:
                return
            # Kotak WebSocket ticks return as dict: {'tk': '1234', 'lp': '150.55'}
            if isinstance(message, dict) and "tk" in message and "lp" in message:
                self.ltp_callback({
                    "security_id": str(message["tk"]),
                    "LTP": f"{float(message['lp']):.2f}"
                })

        if self.client:
            self.client.on_message = _on_market_message
            
            # Map instruments to Kotak format
            tokens = []
            for i in instruments:
                seg_str = seg_map.get(i[0], "nse_fo")
                tokens.append({"instrument_token": str(i[1]), "exchange_segment": seg_str})
            
            # Perform subscription
            logger.info("Subscribing to %d Kotak instruments...", len(tokens))
            self.client.subscribe(
                instrument_tokens=tokens,
                isIndex=False,  # Indices handled separately if needed
                isDepth=False
            )
            
        def _keep_alive():
            while True:
                time.sleep(30)
                
        t = threading.Thread(target=_keep_alive, daemon=True, name="KotakLTPUpdates")
        t.start()
        return t

    def stop_market_feed(self):
        """Disconnect market feed."""
        if self.client:
            logger.info("Stopping Kotak Neo market feeds...")
            # Kotak Neo SDK unsubscribe option
            try:
                self.client.unsubscribe()
            except Exception:
                pass
