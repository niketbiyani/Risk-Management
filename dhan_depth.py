"""
Dhan Market Depth Analyzer — 20-level order book via WebSocket.

Connects to Dhan's dedicated 20-level depth feed, parses binary data,
maintains order book state, and provides real-time analytics:
  - Order book imbalance at 5/10/20 levels
  - Wall detection (abnormally large qty at a price level)
  - Absorption tracking (wall being eaten through)
  - Pull detection (wall disappearing before price reaches it)
  - Cumulative delta (bid vs ask aggression over time)
"""

import asyncio
import json
import logging
import struct
import threading
import time
from collections import deque
from datetime import datetime

import websockets

from config import Config

logger = logging.getLogger(__name__)

# WebSocket endpoint for 20-level depth
DEPTH_WS_URL = "wss://depth-api-feed.dhan.co/twentydepth"

# Feed codes in binary packets
FEED_CODE_BID = 41
FEED_CODE_ASK = 51

# Analytics constants
WALL_THRESHOLD_MULTIPLIER = 3.0   # Qty > 3x average = wall
DELTA_HISTORY_SIZE = 300          # ~5 min at 1s intervals
ALERT_HISTORY_SIZE = 50           # Keep last 50 alerts


class DhanDepthAnalyzer:
    """20-level depth WebSocket client with order book analytics."""

    def __init__(self, socketio, instrument_cache=None):
        self._socketio = socketio
        self._instrument_cache = instrument_cache

        # Connection state
        self._running = False
        self._connected = False
        self._ws = None
        self._thread = None

        # Instrument
        self._security_id = None
        self._trading_symbol = ""
        self._lot_size = 75
        self._expiry = ""

        # Order book (20 levels each side)
        self._bids = [{"price": 0.0, "qty": 0, "orders": 0} for _ in range(20)]
        self._asks = [{"price": 0.0, "qty": 0, "orders": 0} for _ in range(20)]
        self._total_bid_qty = 0
        self._total_ask_qty = 0
        self._last_update = 0.0

        # Previous state (for delta / absorption tracking)
        self._prev_bids = None
        self._prev_asks = None

        # Time-series analytics
        self._delta_history = deque(maxlen=DELTA_HISTORY_SIZE)
        self._cumulative_delta = 0.0
        self._alerts = deque(maxlen=ALERT_HISTORY_SIZE)

        # Throttle SocketIO emissions to max ~4/sec
        self._last_emit = 0.0
        self._emit_interval = 0.25

    # ── Instrument Discovery ─────────────────────────────────────────

    def find_nifty_futures(self):
        """Auto-detect nearest-month Nifty futures security ID."""
        if not self._instrument_cache:
            logger.warning("[DEPTH] No instrument cache, cannot auto-detect")
            return False

        today = datetime.now()
        best = None
        best_days = 999

        for inst in self._instrument_cache._instruments:
            if (inst.instrument_type == "FUTIDX"
                    and inst.symbol_name == "NIFTY"
                    and inst.exchange == "NSE"
                    and inst.expiry_date):
                try:
                    exp = datetime.strptime(inst.expiry_date, "%Y-%m-%d")
                    days = (exp - today).days
                    if 0 <= days < best_days:
                        best = inst
                        best_days = days
                except ValueError:
                    continue

        if best:
            self._security_id = best.security_id
            self._trading_symbol = best.trading_symbol
            self._lot_size = best.lot_size
            self._expiry = best.expiry_date
            logger.info("[DEPTH] Found Nifty Futures: %s (ID: %s, Lot: %d, Expiry: %s)",
                        self._trading_symbol, self._security_id,
                        self._lot_size, self._expiry)
            return True

        logger.warning("[DEPTH] Could not find Nifty futures in instrument cache")
        return False

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self):
        """Start the depth analyzer in a background thread."""
        if self._running:
            return

        if not self._security_id:
            if not self.find_nifty_futures():
                logger.error("[DEPTH] Cannot start — no instrument found")
                return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="dhan-depth"
        )
        self._thread.start()
        logger.info("[DEPTH] Background thread started")

    def stop(self):
        """Stop the depth analyzer."""
        self._running = False
        self._connected = False
        logger.info("[DEPTH] Stopping...")

    def reconnect(self):
        """Force reconnect (e.g. after token refresh)."""
        self._connected = False
        logger.info("[DEPTH] Reconnect requested")

    # ── Public Getters ───────────────────────────────────────────────

    def get_config(self):
        """Return current depth configuration."""
        return {
            "security_id": self._security_id,
            "trading_symbol": self._trading_symbol,
            "lot_size": self._lot_size,
            "expiry": self._expiry,
            "connected": self._connected,
            "running": self._running,
        }

    def get_snapshot(self):
        """Full order book snapshot with computed analytics."""
        bids = [b.copy() for b in self._bids if b["price"] > 0]
        asks = [a.copy() for a in self._asks if a["price"] > 0]
        walls = self._detect_walls(bids, asks)

        ltp = 0.0
        spread = 0.0
        if bids and asks:
            ltp = round((bids[0]["price"] + asks[0]["price"]) / 2, 2)
            spread = round(asks[0]["price"] - bids[0]["price"], 2)

        return {
            "symbol": self._trading_symbol,
            "security_id": self._security_id,
            "lot_size": self._lot_size,
            "expiry": self._expiry,
            "connected": self._connected,
            "bids": bids,
            "asks": asks,
            "ltp": ltp,
            "spread": spread,
            "total_bid_qty": self._total_bid_qty,
            "total_ask_qty": self._total_ask_qty,
            "timestamp": self._last_update,
            "imbalance_5": self._calc_imbalance(bids, asks, 5),
            "imbalance_10": self._calc_imbalance(bids, asks, 10),
            "imbalance_20": self._calc_imbalance(bids, asks, 20),
            "walls": walls,
            "cumulative_delta": self._cumulative_delta,
            "delta_history": list(self._delta_history)[-60:],  # last 60 points
            "alerts": list(self._alerts)[-20:],  # last 20 alerts
        }

    # ── WebSocket Client ─────────────────────────────────────────────

    def _run_loop(self):
        """Entry point for the background thread."""
        asyncio.run(self._ws_client())

    async def _ws_client(self):
        """Main WebSocket loop with auto-reconnection."""
        logger.info("[DEPTH] WebSocket client started")

        while self._running:
            try:
                token = Config.DHAN_ACCESS_TOKEN
                client_id = Config.DHAN_CLIENT_ID
                if not token or not client_id:
                    logger.warning("[DEPTH] Missing credentials, waiting...")
                    await asyncio.sleep(10)
                    continue

                url = (f"{DEPTH_WS_URL}?token={token}"
                       f"&clientId={client_id}&authType=2")
                logger.info("[DEPTH] Connecting to 20-level depth feed...")

                async with websockets.connect(
                    url, ping_interval=30, ping_timeout=10
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("[DEPTH] Connected!")

                    await self._subscribe(ws)

                    while self._running and self._connected:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            if isinstance(msg, bytes):
                                self._process_binary(msg)
                            else:
                                logger.debug("[DEPTH] Text: %s", msg[:200])
                        except asyncio.TimeoutError:
                            continue
                        except websockets.ConnectionClosed:
                            logger.warning("[DEPTH] Connection closed by server")
                            break

            except Exception as e:
                logger.error("[DEPTH] Connection error: %s", e)

            self._connected = False
            self._ws = None

            if self._running:
                logger.info("[DEPTH] Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _subscribe(self, ws):
        """Subscribe to depth data for our instrument."""
        sub_msg = {
            "RequestCode": 23,
            "InstrumentCount": 1,
            "InstrumentList": [{
                "ExchangeSegment": "NSE_FNO",
                "SecurityId": str(self._security_id),
            }],
        }
        await ws.send(json.dumps(sub_msg))
        logger.info("[DEPTH] Subscribed to %s (ID: %s)",
                     self._trading_symbol, self._security_id)

    # ── Binary Parsing ───────────────────────────────────────────────

    def _process_binary(self, data):
        """Parse one or more depth packets from a WebSocket frame."""
        offset = 0
        while offset < len(data):
            if offset + 12 > len(data):
                break

            # Header: msg_len(2) + feed_code(1) + exchange(1) + sec_id(4) + seq(4)
            msg_len, feed_code, exchange_seg, sec_id, seq = struct.unpack_from(
                '<HBBII', data, offset
            )

            # Safety: if msg_len seems wrong, try single-packet mode
            if msg_len == 0 or msg_len > len(data):
                # Treat entire data as one packet
                self._parse_single_packet(data)
                break

            self._parse_single_packet(data[offset:offset + msg_len])
            offset += msg_len

            # If we've consumed all data or msg_len covered everything, stop
            if offset >= len(data):
                break

    def _parse_single_packet(self, data):
        """Parse a single depth packet (bid or ask, 20 levels)."""
        if len(data) < 12:
            return

        header = struct.unpack_from('<HBBII', data, 0)
        feed_code = header[1]
        # sec_id = header[3]

        payload = data[12:]
        levels = []
        for i in range(20):
            off = i * 16
            if off + 16 > len(payload):
                break
            price, qty, orders = struct.unpack_from('<dII', payload, off)
            levels.append({"price": round(price, 2), "qty": qty, "orders": orders})

        # Pad if fewer than 20 levels received
        while len(levels) < 20:
            levels.append({"price": 0.0, "qty": 0, "orders": 0})

        now = time.time()

        if feed_code == FEED_CODE_BID:
            self._prev_bids = [b.copy() for b in self._bids]
            self._bids = levels
        elif feed_code == FEED_CODE_ASK:
            self._prev_asks = [a.copy() for a in self._asks]
            self._asks = levels
        else:
            return

        self._last_update = now
        self._total_bid_qty = sum(b["qty"] for b in self._bids)
        self._total_ask_qty = sum(a["qty"] for a in self._asks)

        # Analytics
        self._update_delta()
        self._track_wall_changes()

        # Throttled SocketIO emit
        if now - self._last_emit >= self._emit_interval:
            self._emit_update()
            self._last_emit = now

    # ── Analytics ────────────────────────────────────────────────────

    def _calc_imbalance(self, bids, asks, depth):
        """Order book imbalance at given depth."""
        bid_qty = sum(b["qty"] for b in bids[:depth])
        ask_qty = sum(a["qty"] for a in asks[:depth])
        total = bid_qty + ask_qty

        if total == 0:
            return {"bid_qty": 0, "ask_qty": 0, "pct": 0.0, "text": "No Data"}

        pct = round(((bid_qty - ask_qty) / total) * 100, 1)

        if pct > 30:
            text = "Strong Buying"
        elif pct > 15:
            text = "Moderate Buying"
        elif pct > 5:
            text = "Slight Buying"
        elif pct < -30:
            text = "Strong Selling"
        elif pct < -15:
            text = "Moderate Selling"
        elif pct < -5:
            text = "Slight Selling"
        else:
            text = "Balanced"

        return {"bid_qty": bid_qty, "ask_qty": ask_qty, "pct": pct, "text": text}

    def _detect_walls(self, bids, asks):
        """Find price levels with qty > 3x the average (walls)."""
        walls = {"bids": [], "asks": []}

        bid_qtys = [b["qty"] for b in bids if b["qty"] > 0]
        ask_qtys = [a["qty"] for a in asks if a["qty"] > 0]

        avg_bid = sum(bid_qtys) / len(bid_qtys) if bid_qtys else 0
        avg_ask = sum(ask_qtys) / len(ask_qtys) if ask_qtys else 0

        for i, b in enumerate(bids):
            if avg_bid > 0 and b["qty"] > avg_bid * WALL_THRESHOLD_MULTIPLIER:
                walls["bids"].append({
                    "level": i, "price": b["price"], "qty": b["qty"],
                    "orders": b["orders"],
                    "strength": round(b["qty"] / avg_bid, 1),
                })

        for i, a in enumerate(asks):
            if avg_ask > 0 and a["qty"] > avg_ask * WALL_THRESHOLD_MULTIPLIER:
                walls["asks"].append({
                    "level": i, "price": a["price"], "qty": a["qty"],
                    "orders": a["orders"],
                    "strength": round(a["qty"] / avg_ask, 1),
                })

        return walls

    def _update_delta(self):
        """Track cumulative delta from qty changes at best bid/ask."""
        if not self._prev_bids or not self._prev_asks:
            return

        # Bid side: if qty at best bid decreased at same price → sellers hit the bid
        if (self._prev_bids[0]["price"] > 0
                and self._prev_bids[0]["price"] == self._bids[0]["price"]):
            change = self._prev_bids[0]["qty"] - self._bids[0]["qty"]
            if change > 0:
                self._cumulative_delta -= change  # selling aggression

        # Ask side: if qty at best ask decreased at same price → buyers lifted the ask
        if (self._prev_asks[0]["price"] > 0
                and self._prev_asks[0]["price"] == self._asks[0]["price"]):
            change = self._prev_asks[0]["qty"] - self._asks[0]["qty"]
            if change > 0:
                self._cumulative_delta += change  # buying aggression

        self._delta_history.append({
            "t": round(time.time(), 1),
            "d": round(self._cumulative_delta),
        })

    def _track_wall_changes(self):
        """Detect wall absorption and pulling."""
        if not self._prev_bids or not self._prev_asks:
            return

        now = time.time()

        # Check top 5 bid levels for wall changes
        self._check_wall_side(
            self._prev_bids[:5], self._bids[:5], "bid", now
        )
        # Check top 5 ask levels for wall changes
        self._check_wall_side(
            self._prev_asks[:5], self._asks[:5], "ask", now
        )

    def _check_wall_side(self, prev_levels, curr_levels, side, now):
        """Compare previous vs current levels for one side."""
        all_qty = [l["qty"] for l in prev_levels if l["qty"] > 0]
        avg_qty = sum(all_qty) / len(all_qty) if all_qty else 0

        for prev, curr in zip(prev_levels, curr_levels):
            if prev["price"] <= 0 or prev["qty"] <= 0:
                continue
            if avg_qty <= 0 or prev["qty"] <= avg_qty * WALL_THRESHOLD_MULTIPLIER:
                continue  # not a wall

            if curr["price"] == prev["price"] and curr["qty"] > 0:
                # Same price — check if wall is being absorbed
                eaten = prev["qty"] - curr["qty"]
                if eaten > prev["qty"] * 0.3:
                    self._alerts.appendleft({
                        "time": now,
                        "type": "absorption",
                        "side": side,
                        "price": prev["price"],
                        "prev_qty": prev["qty"],
                        "curr_qty": curr["qty"],
                        "pct": round((eaten / prev["qty"]) * 100),
                    })
            elif curr["price"] != prev["price"] or curr["qty"] == 0:
                # Wall disappeared — pulled
                self._alerts.appendleft({
                    "time": now,
                    "type": "pull",
                    "side": side,
                    "price": prev["price"],
                    "qty": prev["qty"],
                })

    # ── SocketIO Push ────────────────────────────────────────────────

    def _emit_update(self):
        """Push snapshot to connected clients."""
        if not self._socketio:
            return
        try:
            self._socketio.emit("depth_update", self.get_snapshot())
        except Exception as e:
            logger.debug("[DEPTH] Emit error: %s", e)
