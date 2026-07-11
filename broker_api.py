"""
Broker API Router.
Dynamically dispatches broker calls to either DhanAPI or KotakNeoAPI
depending on the Config.ACTIVE_BROKER setting, facilitating live toggle.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional

from config import Config

logger = logging.getLogger(__name__)


class BrokerAPI:
    """Wrapper that dynamically routes API calls to the configured active broker (Dhan or Kotak)."""

    def __init__(self):
        self._dhan_api = None
        self._kotak_api = None
        self.active_broker = None
        self._init_active_client()

    def _init_active_client(self):
        """Lazy initialize the active broker client based on config."""
        self.active_broker = Config.ACTIVE_BROKER.upper()
        if self.active_broker == "DHAN":
            if not self._dhan_api:
                logger.info("Initializing Dhan API client...")
                from dhan_api import DhanAPI
                self._dhan_api = DhanAPI()
        elif self.active_broker == "KOTAK":
            if not self._kotak_api:
                logger.info("Initializing Kotak Neo API client...")
                from kotak_api import KotakNeoAPI
                self._kotak_api = KotakNeoAPI()
        else:
            logger.error("Unknown active broker: %s. Defaulting to Dhan.", self.active_broker)
            self.active_broker = "DHAN"
            if not self._dhan_api:
                from dhan_api import DhanAPI
                self._dhan_api = DhanAPI()

    def get_active_client(self):
        """Retrieve the active broker client instance, ensuring config matches."""
        current_config = Config.ACTIVE_BROKER.upper()
        if self.active_broker != current_config:
            logger.info("Broker switch detected: %s -> %s", self.active_broker, current_config)
            self._init_active_client()

        if self.active_broker == "DHAN":
            return self._dhan_api
        return self._kotak_api

    def set_active_broker(self, broker_name: str) -> bool:
        """Switch the active broker at runtime."""
        bname = broker_name.upper()
        if bname not in ("DHAN", "KOTAK"):
            logger.error("Cannot switch to invalid broker: %s", broker_name)
            return False

        Config.ACTIVE_BROKER = bname
        self._init_active_client()
        logger.info("Switched active broker dynamically to: %s", bname)
        return True

    @property
    def _context(self):
        """Context property mapping for compatability with dashboard.py credential retrievals."""
        client = self.get_active_client()
        if hasattr(client, "_context") and client._context:
            return client._context

        # Mock context helper for Kotak Neo API
        class KotakContextMock:
            def __init__(self, api_client):
                self.api_client = api_client
            def get_access_token(self):
                # Return session tokens
                return getattr(self.api_client, "access_token", "") or "kotak_session_token"
            def get_client_id(self):
                return getattr(self.api_client, "ucc", "") or "kotak_ucc"

        return KotakContextMock(client)

    def __getattr__(self, name):
        """Forward all other attributes and method calls to the active client."""
        client = self.get_active_client()
        return getattr(client, name)


class DepthWebSocket:
    """Wrapper that dynamically creates and manages the active broker's market depth subscription."""

    def __init__(self, access_token: str = None, client_id: str = None):
        self.active_broker = Config.ACTIVE_BROKER.upper()
        self._dhan_ws = None
        self._security_id = None
        self._exchange_segment = "NSE_FNO"
        
        # Kotak Neo Depth states
        self._kotak_lock = threading.Lock()
        self._kotak_bids = []
        self._kotak_asks = []
        self._kotak_connected = False
        
        if self.active_broker == "DHAN":
            from dhan_api import DepthWebSocket as DhanDepthWS
            self._dhan_ws = DhanDepthWS(access_token, client_id)
        else:
            # Kotak Neo handles subscriptions via the active client instance
            self._kotak_connected = True

    def set_ltp_instruments(self, instruments: list, callback):
        """Set Option Chain strikes for LTP-only streaming ticks."""
        if self._dhan_ws:
            self._dhan_ws.set_ltp_instruments(instruments, callback)
        else:
            # Kotak Neo: Subscribe via KotakNeoAPI instance
            import dashboard
            if dashboard._monitor and hasattr(dashboard._monitor, "api"):
                kotak_client = dashboard._monitor.api.get_active_client()
                if kotak_client and hasattr(kotak_client, "client") and kotak_client.client:
                    # Mappings
                    seg_map = {"NSE_FNO": "nse_fo", "NSE_EQ": "nse_cm", "BSE_FNO": "bse_fo", "BSE_EQ": "bse_cm"}
                    tokens = []
                    for seg, sid in instruments:
                        kseg = seg_map.get(seg, "nse_fo")
                        tokens.append({"instrument_token": str(sid), "exchange_segment": kseg})
                    
                    kotak_client.client.on_message = lambda msg: _on_kotak_ltp_msg(msg, callback)
                    logger.info("DepthWS: Subscribing %d Kotak LTP instruments...", len(tokens))
                    kotak_client.client.subscribe(instrument_tokens=tokens, isIndex=False, isDepth=False)

    def subscribe(self, security_id: str, exchange_segment: str = "NSE_FNO"):
        """Subscribe to 20-level market depth for selected instrument."""
        self._security_id = str(security_id)
        self._exchange_segment = exchange_segment
        
        if self._dhan_ws:
            self._dhan_ws.subscribe(security_id, exchange_segment)
        else:
            # Kotak Neo depth subscription
            import dashboard
            if dashboard._monitor and hasattr(dashboard._monitor, "api"):
                kotak_client = dashboard._monitor.api.get_active_client()
                if kotak_client and hasattr(kotak_client, "client") and kotak_client.client:
                    # Map segment
                    seg_map = {"NSE_FNO": "nse_fo", "NSE_EQ": "nse_cm", "BSE_FNO": "bse_fo", "BSE_EQ": "bse_cm"}
                    kseg = seg_map.get(exchange_segment, "nse_fo")
                    
                    with self._kotak_lock:
                        self._kotak_bids = []
                        self._kotak_asks = []
                        self._kotak_connected = True
                    
                    # Set depth message listener
                    kotak_client.client.on_message = self._update_kotak_depth
                    
                    logger.info("DepthWS: Subscribing Kotak depth for %s (%s)...", security_id, kseg)
                    kotak_client.client.subscribe(
                        instrument_tokens=[{"instrument_token": str(security_id), "exchange_segment": kseg}],
                        isIndex=False,
                        isDepth=True
                    )

    def _update_kotak_depth(self, message):
        """Parse Kotak Neo market depth ticks into local lists."""
        if not isinstance(message, dict):
            return
        
        # Verify it matches our subscribed token
        token = str(message.get("tk", ""))
        if token != self._security_id:
            return

        # Parse bids and asks from payload
        # Standard Kotak depth formats may yield bids/asks directly or as list parameters:
        # e.g., {'tk': '12345', 'bids': [{'price': 105.2, 'quantity': 100, 'orders': 1}]}
        bids_raw = message.get("bids", []) or []
        asks_raw = message.get("asks", []) or []
        
        bids = []
        for b in bids_raw:
            if isinstance(b, dict):
                bids.append({
                    "price": float(b.get("price", b.get("prc", 0))),
                    "quantity": int(b.get("quantity", b.get("qty", 0))),
                    "orders": int(b.get("orders", b.get("ord", 0)))
                })
                
        asks = []
        for a in asks_raw:
            if isinstance(a, dict):
                asks.append({
                    "price": float(a.get("price", a.get("prc", 0))),
                    "quantity": int(a.get("quantity", a.get("qty", 0))),
                    "orders": int(a.get("orders", a.get("ord", 0)))
                })

        with self._kotak_lock:
            if bids:
                self._kotak_bids = bids
            if asks:
                self._kotak_asks = asks

    def get_depth(self) -> dict:
        """Fetch current depth cache thread-safely."""
        if self._dhan_ws:
            return self._dhan_ws.get_depth()
            
        with self._kotak_lock:
            return {
                "bids": list(self._kotak_bids),
                "asks": list(self._kotak_asks),
                "security_id": self._security_id,
                "connected": self._kotak_connected,
                "disconnect_reason": "",
                "connect_attempts": 1,
                "last_error": "",
                "ever_connected": True
            }

    def stop(self):
        """Disconnect WebSocket subscriptions."""
        if self._dhan_ws:
            self._dhan_ws.stop()
        else:
            import dashboard
            if dashboard._monitor and hasattr(dashboard._monitor, "api"):
                kotak_client = dashboard._monitor.api.get_active_client()
                if kotak_client:
                    kotak_client.stop_market_feed()
            self._kotak_connected = False


def _on_kotak_ltp_msg(message, callback):
    """Callback parsing Kotak LTP ticks and calling position monitor tick router."""
    if isinstance(message, dict) and "tk" in message and "lp" in message:
        callback({
            "security_id": str(message["tk"]),
            "LTP": f"{float(message['lp']):.2f}"
        })
